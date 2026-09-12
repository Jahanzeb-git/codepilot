"""
main.py
Author: Jahanzeb Ahmed <jahanzebahmed.mail@gmail.com>
Description: This file handles the Entrypoint for application.
Licensed: MIT
"""
import os
import logging
import asyncio

import httpx
import websockets
from websockets.exceptions import ConnectionClosed
import collections

from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import Base, _async_engine, get_db_session, SessionFactory
from core.machines import MachineService
from api import auth, machines
from crud.repository import (
    get_running_machines,
    reconcile_usage,
    update_usage_on_suspend,
    touch_machine_activity,
    get_idle_running_machines,
    DAILY_LIMIT_SECONDS,
    TOTAL_LIMIT_SECONDS,
)
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# How often the authoritative, DB-backed enforcer sweeps for quota-exceeded
# or idle machines. This is the ONLY thing that can be trusted to actually
# stop a machine no matter what else goes wrong (a crashed WS proxy, a
# restarted backend instance, a browser tab that vanished without a clean
# disconnect) — it doesn't depend on any single process's in-memory state,
# and it re-reads everything fresh from the DB on every pass. Kept short
# (30s) because the stakes of a machine running unmetered are real money,
# not just a stale UI.
ENFORCER_INTERVAL_SECONDS = 30

async def quota_enforcer_task(app: FastAPI):
    """
    Background task, run continuously for the lifetime of the process:
    every ENFORCER_INTERVAL_SECONDS, suspend any machine that has either
    (a) exceeded its daily or lifetime usage quota, or
    (b) gone quiet (no proxied WebSocket activity) for longer than
        IDLE_TIMEOUT_SECONDS — our stand-in for "the workspace tab closed".

    This is the authoritative safety net. The WS proxy in this file also
    tries to suspend promptly on a clean disconnect as a fast-path for
    good UX, but that in-memory mechanism can silently vanish (process
    restart, multiple backend replicas) — this loop is what actually
    guarantees a machine can never run unbounded.
    """
    logger.info("Usage enforcer background task started.")
    try:
        while True:
            await asyncio.sleep(ENFORCER_INTERVAL_SECONDS)
            try:
                async with SessionFactory() as db:
                    running = await get_running_machines(db)

                    for machine in running:
                        # reconcile_usage() must run before ANY quota
                        # comparison — see its docstring for the exploit
                        # this prevents.
                        await reconcile_usage(db, machine)

                        now = datetime.now(timezone.utc).replace(tzinfo=None)
                        live_usage = int((now - machine.last_started_at).total_seconds()) if machine.last_started_at else 0

                        daily_limit_reached = (machine.daily_usage_seconds + live_usage) >= DAILY_LIMIT_SECONDS
                        total_limit_reached = (machine.total_usage_seconds + live_usage) >= TOTAL_LIMIT_SECONDS

                        if daily_limit_reached or total_limit_reached:
                            logger.info(f"Quota exceeded for machine {machine.fly_machine_id}. Suspending...")
                            try:
                                await app.state.machine_service.suspend_machine(machine.fly_machine_id)
                            except httpx.HTTPError as e:
                                logger.error(f"Failed to suspend machine {machine.fly_machine_id}: {e}")
                                continue  # Skip DB update if API call fails

                            await update_usage_on_suspend(db, machine)
                            machine.status = "stopped"
                            await db.commit()
                            continue

                    # Idle sweep: separate query since reconcile_usage() above
                    # may have just flipped some machines to suspended.
                    idle = await get_idle_running_machines(db)
                    for machine in idle:
                        logger.info(
                            f"Machine {machine.fly_machine_id} idle for >={ENFORCER_INTERVAL_SECONDS}s "
                            f"(no WS activity) — suspending as if the workspace tab was closed."
                        )
                        try:
                            await app.state.machine_service.suspend_machine(machine.fly_machine_id)
                        except httpx.HTTPError as e:
                            logger.error(f"Failed to idle-suspend machine {machine.fly_machine_id}: {e}")
                            continue
                        await update_usage_on_suspend(db, machine)
                        machine.status = "stopped"
                        await db.commit()
            except Exception as e:
                logger.error(f"Error in usage enforcer loop: {e}")
    except asyncio.CancelledError:
        logger.info("Usage enforcer background task cancelled.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with _async_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Base.metadata.create_all only creates missing TABLES, not missing
        # COLUMNS on tables that already exist — this app has no Alembic
        # migrations, so new columns need this same ad-hoc, idempotent
        # pattern to reach an already-deployed database.
        from sqlalchemy import text
        await connection.execute(text(
            "ALTER TABLE machines ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP"
        ))
    app.state.machine_service = MachineService()
    app.state.quota_task = asyncio.create_task(quota_enforcer_task(app))
    logging.info("App started...")
    yield
    app.state.quota_task.cancel()
    try:
        await app.state.quota_task
    except asyncio.CancelledError:
        pass
    await app.state.machine_service.client.aclose()
    logging.info("App Shutdown...")


app = FastAPI(title="Codepilot Web API (v1)", lifespan=lifespan)

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://shiny-otter-e8d72c.netlify.app",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:4173",
    ],
    allow_origin_regex=r"https://.*\.netlify\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(machines.router)


@app.get("/health")
async def health_check(db: AsyncSession = Depends(get_db_session)):
    return {"status": "healthy"}


# ---------------------------------------------------------------------------
# Workspace reverse proxy
#
# Any path NOT matching an excluded API prefix is proxied to the user's
# workspace machine over Fly's private 6PN network, using the machine_id
# stored in the `workspace_machine_id` cookie (set by /machines/connect).
#
# We do NOT use fly-replay for this. Both an HTTP proxy route and a
# WebSocket proxy route below manually forward traffic to
# http(s)://<machine_id>.vm.<WORKSPACE_APP_NAME>.internal:8080
# ---------------------------------------------------------------------------

EXCLUDED_PREFIXES = ("auth", "machines", "docs", "openapi.json", "health")
WORKSPACE_PORT = 8080

# State for server-side auto-suspend
active_connections: dict[str, int] = collections.defaultdict(int)
pending_suspends: dict[str, asyncio.Task] = {}

async def delayed_suspend(machine_id: str, app: FastAPI, delay_seconds: int = 60):
    try:
        await asyncio.sleep(delay_seconds)
        logger.info(f"Machine {machine_id} idle for {delay_seconds} seconds, suspending...")
        
        # Suspend the machine
        await app.state.machine_service.suspend_machine(machine_id)
        
        # Update database status
        async with SessionFactory() as db:
            from sqlalchemy import select
            from models.models import MachineDB
            query = select(MachineDB).where(MachineDB.fly_machine_id == machine_id)
            exec = await db.execute(query)
            machine = exec.scalar_one_or_none()
            if machine:
                machine.status = "stopped"
                await update_usage_on_suspend(db, machine)
                await db.commit()
    except asyncio.CancelledError:
        logger.info(f"Auto-suspend cancelled for machine {machine_id} due to new connection")
    except Exception as e:
        logger.error(f"Error during delayed auto-suspend for machine {machine_id}: {e}")
    finally:
        # Cleanup pending_suspends if this task is still tracked
        if machine_id in pending_suspends and pending_suspends[machine_id] == asyncio.current_task():
            del pending_suspends[machine_id]



def get_target_host(machine_id: str) -> str:
    app_name = os.environ.get("WORKSPACE_APP_NAME", "codepilot-workspaces")
    return f"{machine_id}.vm.{app_name}.internal"


def is_excluded(path: str) -> bool:
    first_segment = path.split("/", 1)[0]
    return first_segment in EXCLUDED_PREFIXES


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def http_proxy(path: str, request: Request):
    if is_excluded(path):
        return Response(status_code=404)

    # Disable code-server's Service Worker to prevent it from intercepting FastAPI routes
    # like /machines/connect. When the browser checks for SW updates, it gets this script,
    # which unregisters the SW and forces a clean reload.
    if "serviceWorker.js" in path:
        sw_script = """
        self.addEventListener('install', function(e) { self.skipWaiting(); });
        self.addEventListener('activate', function(e) {
            self.registration.unregister()
            .then(function() { return self.clients.matchAll(); })
            .then(function(clients) {
                clients.forEach(client => client.navigate(client.url));
            });
        });
        """
        return Response(content=sw_script, media_type="application/javascript")

    machine_id = request.cookies.get("workspace_machine_id")
    if not machine_id:
        return Response(status_code=401, content="Missing workspace_machine_id cookie")

    host = get_target_host(machine_id)
    target_url = f"http://{host}:{WORKSPACE_PORT}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "cookie", "content-length", "accept-encoding")
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream_resp = await client.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                content=await request.body(),
            )
    except httpx.HTTPError as e:
        logger.error(f"[http_proxy] Upstream error for machine {machine_id}: {e}")
        return Response(status_code=502, content="Workspace unreachable")

    excluded_resp_headers = {"content-encoding", "transfer-encoding", "connection"}
    response_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in excluded_resp_headers
    }

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=response_headers,
    )


@app.websocket("/{path:path}")
async def websocket_proxy(websocket: WebSocket, path: str):
    if is_excluded(path):
        await websocket.close(code=1008, reason="Not found")
        return

    machine_id = websocket.cookies.get("workspace_machine_id")
    if not machine_id:
        await websocket.close(code=1008, reason="Missing workspace_machine_id cookie")
        return

    host = get_target_host(machine_id)
    query_string = str(websocket.url.query)
    target_url = f"ws://{host}:{WORKSPACE_PORT}/{path}"
    if query_string:
        target_url += f"?{query_string}"

    await websocket.accept()
    logger.info(f"[ws_proxy] Accepted browser WS for machine {machine_id}, path={path}")

    # Track connection and cancel any pending suspend
    active_connections[machine_id] += 1
    if machine_id in pending_suspends:
        pending_suspends[machine_id].cancel()
        del pending_suspends[machine_id]
        logger.info(f"[ws_proxy] Cancelled pending suspend for machine {machine_id}")

    try:
        async with SessionFactory() as db:
            await touch_machine_activity(db, machine_id)
    except Exception:
        pass

    async def _heartbeat():
        """
        Keep last_active_at fresh in the DB while this connection is open,
        so the authoritative idle sweep (which works off last_active_at,
        not this process's in-memory active_connections dict) never
        mistakes an actively-used-but-quiet connection for a closed tab.
        Interval is well under IDLE_TIMEOUT_SECONDS so a single missed
        beat can't cause a false suspend.
        """
        try:
            while True:
                await asyncio.sleep(30)
                async with SessionFactory() as db:
                    await touch_machine_activity(db, machine_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[ws_proxy] Heartbeat error for {machine_id}: {e}")

    heartbeat_task = asyncio.create_task(_heartbeat())

    try:
        async with websockets.connect(
            target_url,
            open_timeout=20,
            close_timeout=5,
            ping_interval=None,  # code-server doesn't answer generic WS pings on its own schedule;
                                 # let VS Code's own protocol-level heartbeat handle liveness instead.
        ) as target_ws:
            logger.info(f"[ws_proxy] Connected upstream to {target_url}")

            async def forward_to_target():
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        if message.get("text") is not None:
                            await target_ws.send(message["text"])
                        elif message.get("bytes") is not None:
                            await target_ws.send(message["bytes"])
                except (WebSocketDisconnect, ConnectionClosed):
                    pass

            async def forward_to_client():
                try:
                    async for data in target_ws:
                        if isinstance(data, str):
                            await websocket.send_text(data)
                        else:
                            await websocket.send_bytes(data)
                except ConnectionClosed:
                    pass

            tasks = [
                asyncio.create_task(forward_to_target()),
                asyncio.create_task(forward_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    except Exception as e:
        logger.error(f"[ws_proxy] Error proxying to machine {machine_id}: {e}")
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
        
        # Track disconnection and schedule suspend if no connections remain.
        # This is a fast-path for good UX (close the tab, see it suspend
        # quickly) — it is NOT the safety net. touch_machine_activity()
        # below already marks this exact moment as "last seen"; the
        # DB-backed idle sweep in quota_enforcer_task will catch this
        # machine within IDLE_TIMEOUT_SECONDS regardless of whether this
        # in-memory path runs at all (e.g. if this backend process gets
        # killed/restarted between now and the 60s mark).
        active_connections[machine_id] -= 1
        if active_connections[machine_id] <= 0:
            active_connections[machine_id] = 0
            pending_suspends[machine_id] = asyncio.create_task(delayed_suspend(machine_id, websocket.app, 60))
            logger.info(f"[ws_proxy] No active connections for {machine_id}, scheduled fast-path suspend in 60s")

        try:
            async with SessionFactory() as db:
                await touch_machine_activity(db, machine_id)
        except Exception:
            pass  # best-effort — the idle sweep re-derives this from last_started_at if it's ever missed

        logger.info(f"[ws_proxy] Closed proxy session for machine {machine_id}")

# END OF FILE