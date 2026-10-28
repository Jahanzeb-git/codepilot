#!/usr/bin/env bash
# Installs the `codepilot-workspace` command to /usr/local/bin.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Jahanzeb-git/codepilot/main/cloud/distribution/install.sh | bash
#
# What this does:
#   1. Detects your CPU architecture (amd64 or arm64).
#   2. Downloads the latest pre-compiled Go binary for your arch.
#   3. Installs it to /usr/local/bin/codepilot-workspace.
#
# Requirements: Linux only. curl. sudo if /usr/local/bin is not user-writable.
set -euo pipefail

REPO="Jahanzeb-git/codepilot"
BINARY="codepilot-workspace"
DEST="/usr/local/bin/${BINARY}"

# ---- 1. Detect architecture ------------------------------------------------

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)           ARCH="amd64" ;;
  aarch64 | arm64)  ARCH="arm64" ;;
  *)
    echo "ERROR: unsupported architecture: $ARCH" >&2
    exit 1
    ;;
esac

# ---- 2. Download the latest binary -----------------------------------------
#
# Use GitHub's direct "latest asset" URL instead of the REST API. The API is
# rate-limited to 60 requests/hour per IP for unauthenticated clients, which
# breaks installs on shared/NAT networks. This endpoint redirects straight to
# the newest release asset and is not subject to that limit.

DOWNLOAD_URL="https://github.com/${REPO}/releases/latest/download/${BINARY}-linux-${ARCH}"

echo "[codepilot] Downloading ${BINARY}-linux-${ARCH} from the latest release..."

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

curl -fsSL "$DOWNLOAD_URL" -o "$TMP"

# ---- 4. Install ------------------------------------------------------------

echo "[codepilot] Installing to ${DEST}..."

if [ -w "$(dirname "$DEST")" ]; then
  mv "$TMP" "$DEST"
  chmod +x "$DEST"
else
  sudo mv "$TMP" "$DEST"
  sudo chmod +x "$DEST"
fi

echo "[codepilot] Done. Run:  codepilot-workspace from project directory to start the workspace."
