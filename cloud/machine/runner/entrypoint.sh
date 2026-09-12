#!/bin/bash
# entrypoint.sh

# This function will be called when the container receives a shutdown signal (SIGTERM)
cleanup() {
    echo "[INFO] Received termination signal. Shutting down gracefully..."
    
    # Notify daemon to sync and wait for it to finish!
    if [ -n "$DAEMON_PID" ]; then
        echo "[INFO] Notifying B2 snapshot daemon to sync..."
        kill -TERM "$DAEMON_PID" 2>/dev/null
        wait "$DAEMON_PID"
        echo "[INFO] B2 sync complete."
    fi

    # Send SIGTERM to other background processes
    kill -TERM "$AGENT_PID" 2>/dev/null
    kill -TERM "$CODE_PID" 2>/dev/null
}

# Trap the SIGTERM signal (which Fly.io sends on stop) and route it to cleanup
trap cleanup SIGTERM SIGINT

# Self-heal: a bind-mounted agent.yaml (local self-hosted usage) can be
# missing or empty on first run, before anything has ever written to it.
# Restore the image's baked-in default in that case. Always a no-op on
# Fly, where the file is already present and non-empty from the build.
if [ ! -s /opt/codepilot/agent.yaml ]; then
    echo "[INFO] agent.yaml missing or empty, restoring default."
    cp /opt/codepilot/agent.yaml.default /opt/codepilot/agent.yaml
fi

# Boot logic for B2 Persistence
if [ ! -f /.rootfs_initialized ]; then
    echo "[INFO] New rootfs detected. Downloading workspace from B2..."
    snapshot-daemon --restore
    touch /.rootfs_initialized
else
    echo "[INFO] Preserved rootfs detected. Skipping B2 restore."
fi

echo "[INFO] Starting B2 snapshot daemon in watch mode..."
snapshot-daemon &
DAEMON_PID=$!

echo "[INFO] Starting Codepilot Agent Server..."
# agent_server.py reads all secrets from environment, then pops them itself
# (see the _MACHINE_SECRET / _CONTROL_PLANE_URL / API key scrubbing at startup).
# We launch it in the background before unsetting from THIS shell so it can read them.
python3 /opt/codepilot/agent_server.py &
AGENT_PID=$!

# Defence-in-depth: unset secrets from the parent shell immediately after forking.
# The child (agent_server.py) already has its own copy; these unsets only affect
# this shell and any processes forked AFTER this point (i.e., code-server).
unset MACHINE_SECRET
unset CONTROL_PLANE_URL
unset DASHSCOPE_API_KEY
unset ALIBABA_API_KEY
unset VOYAGE_API_KEY
unset TAVILY_API_KEY

# Fly.io sometimes writes secrets to /.env, remove it!
rm -f /.env 2>/dev/null
rm -f /etc/environment 2>/dev/null

echo "[INFO] Starting CodePilot Workspace Server..."
# Run the Rust server in the background. It binds to 8080.
codepilot-server &
CODE_PID=$!

# Wait tells bash to sit here and block until BOTH specific processes exit.
wait $AGENT_PID $CODE_PID