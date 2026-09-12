from fastapi import APIRouter, HTTPException, Depends, Request, Response, Header, WebSocket
from fastapi.exceptions import HTTPException as FastAPIHTTPException
import asyncio
from core.security import get_current_user, create_connect_ticket, get_user_from_connect_ticket
from sqlalchemy.ext.asyncio import AsyncSession
from db.database import get_db_session
from crud.repository import add_machine, is_machine_exists, get_machine_by_secret, delete_machine_record, reconcile_usage, update_usage_on_suspend, DAILY_LIMIT_SECONDS, TOTAL_LIMIT_SECONDS
import logging
import httpx
import secrets
from datetime import datetime, timezone
import os
import time

router = APIRouter(prefix="/machines")
logger = logging.getLogger(__name__)

@router.post("/launch", status_code=202)
async def launch_machine(request: Request, db: AsyncSession = Depends(get_db_session), user = Depends(get_current_user)):
    try:
        machine = await is_machine_exists(db, user.id)
        
        if machine:
            await reconcile_usage(db, machine)
            if machine.total_usage_seconds >= TOTAL_LIMIT_SECONDS:
                raise HTTPException(status_code=403, detail="You have consumed your 90-hour free tier limit.")
            if machine.daily_usage_seconds >= DAILY_LIMIT_SECONDS:
                raise HTTPException(status_code=403, detail="You have consumed your 3-hour daily limit. Come back tomorrow!")

        # No DB record at all: create fresh
        if not machine:
            machine_secret = secrets.token_urlsafe(32)
            response = await request.app.state.machine_service.create_machine(machine_secret, str(user.id))
            machine_id = response["id"]
            machine_name = response["name"]
            machine_state = response["state"]
            
            # Map Fly's "created" state to our DB Enum "starting"
            if machine_state == "created":
                machine_state = "starting"
                
            machine_db = await add_machine(db, machine_id, user.id, machine_state, machine_secret)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            machine_db.last_started_at = now
            # Baseline last_active_at at launch too, so a machine that's
            # launched but never actually connected to (tab never opened,
            # or opened and immediately closed before any WS connects)
            # still gets caught by the idle sweep instead of running until
            # the full daily/lifetime quota is exhausted.
            machine_db.last_active_at = now
            await db.commit()
            
            logger.info(f"machine {machine_name} created with state {machine_state}")
            return {"status": machine_state, "machine_name": machine_name}

        # Tombstoned record (user previously deleted their machine):
        # reuse the existing DB row so quota counters are preserved.
        if machine.fly_machine_id is None:
            machine_secret = secrets.token_urlsafe(32)
            response = await request.app.state.machine_service.create_machine(machine_secret, str(user.id))
            machine_id = response["id"]
            machine_name = response["name"]
            machine_state = response["state"]

            if machine_state == "created":
                machine_state = "starting"

            machine.fly_machine_id = machine_id
            machine.machine_secret = machine_secret
            machine.status = machine_state
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            machine.last_started_at = now
            machine.last_active_at = now
            await db.commit()

            logger.info(f"machine {machine_name} re-provisioned for existing user (quota preserved), state={machine_state}")
            return {"status": machine_state, "machine_name": machine_name}
        
        # NOTE: The Fly /start endpoint returns {previous_state, migrated, new_host}
        # It does NOT return the machine name or new state.
        # We use the data we already have from the DB record.
        try:
            # Automatically upgrade image if outdated before starting
            await request.app.state.machine_service.update_machine_image_if_needed(machine.fly_machine_id)
            
            await request.app.state.machine_service.start_machine(machine.fly_machine_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 412):
                logger.warning(f"Machine {machine.fly_machine_id} was destroyed or missing. Auto-healing...")
                # Flush any accumulated usage before we lose last_started_at
                await update_usage_on_suspend(db, machine)

                # Provision a new Fly machine, reuse the same DB row to preserve quota
                machine_secret = secrets.token_urlsafe(32)
                response = await request.app.state.machine_service.create_machine(machine_secret, str(user.id))
                machine_id = response["id"]
                machine_name = response["name"]
                machine_state = response["state"]
                
                if machine_state == "created":
                    machine_state = "starting"

                machine.fly_machine_id = machine_id
                machine.machine_secret = machine_secret
                machine.status = machine_state
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                machine.last_started_at = now
                machine.last_active_at = now
                await db.commit()
                
                logger.info(f"machine {machine_name} created with state {machine_state} after auto-heal")
                return {"status": machine_state, "machine_name": machine_name}
            else:
                raise e

        # Existing machine being restarted: flush any un-flushed time first so
        # it is not lost when we overwrite last_started_at below.
        await update_usage_on_suspend(db, machine)

        machine.status = "starting"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        machine.last_started_at = now
        machine.last_active_at = now
        await db.commit()
        await db.refresh(machine)

        logger.info(f"machine {machine.fly_machine_id} is starting")
        return {"status": "starting", "machine_id": machine.fly_machine_id}
    
    except FastAPIHTTPException:
        # Re-raise HTTPExceptions (403 quota, 404 not found, etc.) so they
        # are NOT caught by the generic handler below and mangled into a 500.
        raise
    except httpx.HTTPError as e:
        logger.exception("Communication with Fly.io failed")
        raise HTTPException(
            status_code=502,
            detail="Infrastructure provider is currently unreachable"
        )
    except Exception as e: 
        logger.exception("Internal error during machine launch")
        raise HTTPException(
            status_code=500, 
            detail="Something went wrong on our end."
        )

@router.get("/status")
async def check_machine_status(request: Request, db: AsyncSession = Depends(get_db_session), user = Depends(get_current_user)):
    try:
        machine = await is_machine_exists(db, user.id)
        if not machine:
            raise HTTPException(
                status_code=404,
                detail="Machine not found"
            )
            
        await reconcile_usage(db, machine)
        
        if machine.last_started_at:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            live_usage = int((now - machine.last_started_at).total_seconds())
            daily_exceeded = (machine.daily_usage_seconds + live_usage) >= DAILY_LIMIT_SECONDS
            total_exceeded = (machine.total_usage_seconds + live_usage) >= TOTAL_LIMIT_SECONDS
            if daily_exceeded or total_exceeded:
                await request.app.state.machine_service.suspend_machine(machine.fly_machine_id)
                await update_usage_on_suspend(db, machine)
                machine.status = "stopped"
                await db.commit()
                return {"status": "quota_exceeded"}

        machine_id = machine.fly_machine_id

        machine_res = await request.app.state.machine_service.check_status(machine_id)
        state = machine_res.get("state")
        checks = machine_res.get("checks",[])
        if state == "started" and checks and checks[0].get("status") == "passing":
            return {"status": "ready"}
        return {"status": state}

    except httpx.HTTPError as e:
        logger.exception("Communication with Fly.io failed")
        raise HTTPException(
            status_code=502,
            detail="Infrastructure provider is currently unreachable"
        )
    except HTTPException:
        raise
    except Exception as e: 
        logger.exception("Internal error during machine status check")
        raise HTTPException(
            status_code=500,
            detail="Something went wrong on our end."
        )
    
@router.websocket("/ws/status")
async def check_machine_status_ws(websocket: WebSocket, db: AsyncSession = Depends(get_db_session)):
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008, reason="Missing token")
        return
    
    from core.security import get_current_user_ws
    try:
        user = await get_current_user_ws(token, db)
    except ValueError:
        await websocket.close(code=1008, reason="Invalid token")
        return
        
    if not user:
        await websocket.close(code=1008, reason="User not found")
        return

    await websocket.accept()

    try:
        while True:
            machine = await is_machine_exists(db, user.id)
            if not machine:
                await websocket.send_json({"status": "idle"})
            else:
                await reconcile_usage(db, machine)
                
                if machine.last_started_at:
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    live_usage = int((now - machine.last_started_at).total_seconds())
                    daily_exceeded = (machine.daily_usage_seconds + live_usage) >= DAILY_LIMIT_SECONDS
                    total_exceeded = (machine.total_usage_seconds + live_usage) >= TOTAL_LIMIT_SECONDS
                    if daily_exceeded or total_exceeded:
                        try:
                            await websocket.app.state.machine_service.suspend_machine(machine.fly_machine_id)
                        except httpx.HTTPError:
                            pass
                        await update_usage_on_suspend(db, machine)
                        machine.status = "stopped"
                        await db.commit()
                        await websocket.send_json({
                            "status": "quota_exceeded", 
                            "message": "Daily quota for 3 hours is exhausted please come back at UTC time of server + 5 hours"
                        })
                        await asyncio.sleep(4)
                        continue

                machine_id = machine.fly_machine_id
                if not machine_id:
                    await websocket.send_json({"status": "idle"})
                    await asyncio.sleep(4)
                    continue
                
                try:
                    machine_res = await websocket.app.state.machine_service.check_status(machine_id)
                    state = machine_res.get("state")
                    checks = machine_res.get("checks",[])
                    if state == "started" and checks and checks[0].get("status") == "passing":
                        await websocket.send_json({"status": "ready", "machine_name": machine.fly_machine_id})
                    else:
                        await websocket.send_json({"status": state, "machine_name": machine.fly_machine_id})
                except httpx.HTTPError:
                    await websocket.send_json({"status": "error", "message": "Infrastructure provider is currently unreachable"})

            await asyncio.sleep(4)
    except Exception as e:
        logger.error(f"Status WebSocket closed/error: {e}")
        try:
            await websocket.close()
        except:
            pass

@router.api_route("/connect-ticket", methods=["GET", "POST"])
async def generate_connect_ticket(user = Depends(get_current_user)):
    """Generates a 30-second ticket so the user's main token doesn't go in the URL."""
    ticket = create_connect_ticket(user.email)
    return {"ticket": ticket}

@router.get("/connect")
async def connect_to_machine(db: AsyncSession = Depends(get_db_session), user = Depends(get_user_from_connect_ticket)):
    machine = await is_machine_exists(db, user.id)
    if not machine:
        raise HTTPException(
            status_code=404,
            detail="Machine not found"
        )
    machine_id = machine.fly_machine_id
    import os
    app_name = os.environ.get("WORKSPACE_APP_NAME", "codepilot-workspaces")
    
    # We set a cookie with the machine ID and redirect the browser to the root path `/`
    # The catch-all route in main.py will see this cookie and replay the request to the workspace
    from fastapi.responses import RedirectResponse
    response = RedirectResponse(url="/")
    response.set_cookie(
        key="workspace_machine_id",
        value=machine_id,
        httponly=True,
        samesite="none",
        secure=True,
        max_age=3600 * 24 # 24 hours
    )
    return response

@router.api_route("/delete", methods=["POST", "DELETE"])
async def delete_machine(request: Request, db: AsyncSession = Depends(get_db_session), user = Depends(get_current_user)):
    try:
        machine = await is_machine_exists(db, user.id)
        if not machine:
            raise HTTPException(
                status_code=404,
                detail="Machine not found"
            )

        # Flush any un-flushed usage time before we lose last_started_at
        await update_usage_on_suspend(db, machine)

        # Hard-delete the Fly machine only if it exists (may already be gone)
        if machine.fly_machine_id:
            await request.app.state.machine_service.delete_machine(machine.fly_machine_id)

        # Soft-delete the DB row: preserve quota counters, clear credentials
        await delete_machine_record(db, machine)
        return {"deleted": True}

    except httpx.HTTPError as e:
        logger.exception("Communication with Fly.io failed during delete")
        raise HTTPException(
            status_code=502,
            detail="Infrastructure provider is currently unreachable"
        )

@router.post("/suspend")
async def suspend_machine(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
    x_machine_secret: str = Header(...)
    ):

    machine = await get_machine_by_secret(db, x_machine_secret)
    if not machine:
        raise HTTPException(
            status_code=401,
             detail="Invalid machine secret"
        )
    try:
        await request.app.state.machine_service.suspend_machine(machine.fly_machine_id)
        machine.status = "stopped"
        await update_usage_on_suspend(db, machine)
        return {"suspended": True}
    except httpx.HTTPError as e:
        logger.exception("Communication with Fly.io failed during suspend")
        raise HTTPException(
            status_code=502,
            detail="Infrastructure provider is currently unreachable"
        )
    except Exception as e:
        logger.exception("Internal error during machine suspension")
        raise HTTPException(
            status_code=500,
            detail="Something went wrong on our end."
        )

# --- B2 Storage Credentials ---

KEY_DURATION_SECONDS = 3600

async def _authorize_master() -> dict:
    master_key_id = os.environ.get("B2_MASTER_KEY_ID")
    master_app_key = os.environ.get("B2_MASTER_APPLICATION_KEY")
    if not master_key_id or not master_app_key:
        raise HTTPException(status_code=500, detail="B2 master credentials not configured")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.backblazeb2.com/b2api/v3/b2_authorize_account",
            auth=(master_key_id, master_app_key),
        )
        resp.raise_for_status()
        return resp.json()

@router.post("/internal/b2-credentials")
async def issue_b2_credentials(db: AsyncSession = Depends(get_db_session), x_machine_secret: str = Header(...)):
    machine = await get_machine_by_secret(db, x_machine_secret)
    if not machine:
        raise HTTPException(status_code=403, detail="Invalid machine secret")
        
    workspace_id = str(machine.user_id)  # Assuming 1 workspace per user for now
    name_prefix = f"workspaces/{workspace_id}/"

    auth = await _authorize_master()
    api_url = auth["apiInfo"]["storageApi"]["apiUrl"]
    auth_token = auth["authorizationToken"]

    b2_bucket_id = os.environ.get("B2_BUCKET_ID")
    b2_bucket_name = os.environ.get("B2_BUCKET_NAME")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{api_url}/b2api/v3/b2_create_key",
            headers={"Authorization": auth_token},
            json={
                "accountId": auth["accountId"],
                "capabilities": ["readFiles", "writeFiles", "listFiles", "deleteFiles"],
                "keyName": f"ws-{workspace_id}-{int(time.time())}",
                "bucketId": b2_bucket_id,
                "namePrefix": name_prefix,
                "validDurationInSeconds": KEY_DURATION_SECONDS,
            },
        )
        resp.raise_for_status()
        key = resp.json()

    return {
        "application_key_id": key["applicationKeyId"],
        "application_key": key["applicationKey"],
        "bucket_id": b2_bucket_id,
        "bucket_name": b2_bucket_name,
        "name_prefix": name_prefix,
        "expires_at": int(time.time()) + KEY_DURATION_SECONDS,
    }

# END OF FILE