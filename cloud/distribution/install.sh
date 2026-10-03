#!/usr/bin/env bash
# Installs the `codepilot-workspace` command to /usr/local/bin.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Jahanzeb-git/codepilot/main/cloud/distribution/install.sh | bash
#
# What this does:
#   1. Detects your CPU architecture (amd64 or arm64).
#   2. Resolves the latest release tag from GitHub.
#   3. Downloads the pre-compiled Go binary for your arch.
#   4. Installs it to /usr/local/bin/codepilot-workspace.
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

# ---- 2. Resolve latest release tag -----------------------------------------

echo "[codepilot] Resolving latest release..."

LATEST="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
  | grep '"tag_name"' \
  | sed 's/.*"tag_name": *"\(.*\)".*/\1/')"

if [ -z "$LATEST" ]; then
  echo "ERROR: could not determine latest release. Check your internet connection." >&2
  exit 1
fi

echo "[codepilot] Latest release: ${LATEST}"

# ---- 3. Download the binary ------------------------------------------------

DOWNLOAD_URL="https://github.com/${REPO}/releases/download/${LATEST}/${BINARY}-linux-${ARCH}"

echo "[codepilot] Downloading ${BINARY}-linux-${ARCH}..."

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
