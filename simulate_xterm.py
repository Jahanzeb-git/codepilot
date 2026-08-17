import socket
import json
import time
import subprocess
import os

# 1. Start the multiplexer daemon in the background
socket_path = "/tmp/codepilot_test.sock"
if os.path.exists(socket_path):
    os.remove(socket_path)

print("Starting MuxServer daemon...")
server_proc = subprocess.Popen(
    ["python3", "codepilot/core/multiplexer.py", socket_path, "--cols", "80", "--rows", "24"]
)

# Wait for the socket to be created
time.sleep(1)

print("\n--- Starting Client Simulation ---")
try:
    # 2. Connect to the Unix Domain Socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    print(f"Connecting to {socket_path}...")
    sock.connect(socket_path)
    print("Connected!")

    # 3. Read the JSON handshake (First line up to \n)
    raw = b""
    while b"\n" not in raw:
        chunk = sock.recv(1024)
        if not chunk:
            break
        raw += chunk
    
    handshake_line, remainder = raw.split(b"\n", 1)
    handshake = json.loads(handshake_line.decode())
    print(f"Received Handshake: {handshake}")

    # 4. Read initial PTY bytes (bash prompt)
    sock.setblocking(False)
    time.sleep(0.5)
    try:
        initial_output = sock.recv(4096)
        print(f"Initial PTY Output: {initial_output!r}")
    except BlockingIOError:
        pass

    # 5. Simulate xterm.js sending a command
    command = "echo 'Hello from Simulated xterm.js Client!'\n"
    print(f"\nSending command: {command.strip()}")
    sock.sendall(command.encode('utf-8'))

    # 6. Read the response (echo + command output)
    time.sleep(0.5)
    try:
        response_output = sock.recv(4096)
        print(f"Response PTY Output: {response_output!r}")
    except BlockingIOError:
        pass

finally:
    print("\nCleaning up...")
    sock.close()
    server_proc.terminate()
    server_proc.wait()
