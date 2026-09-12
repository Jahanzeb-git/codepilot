#!/usr/bin/env bash
# Installs the `codepilot-workspace` command to /usr/local/bin.
# Usage:  curl -fsSL https://raw.githubusercontent.com/Jahanzeb-git/codepilot-cloud/main/distribution/install.sh | bash
set -euo pipefail

RAW_URL="https://raw.githubusercontent.com/Jahanzeb-git/codepilot-cloud/main/distribution/codepilot-workspace"
DEST="/usr/local/bin/codepilot-workspace"

echo "Installing codepilot-workspace to $DEST ..."
if [ -w "$(dirname "$DEST")" ]; then
  curl -fsSL "$RAW_URL" -o "$DEST"
else
  curl -fsSL "$RAW_URL" | sudo tee "$DEST" >/dev/null
  sudo chmod +x "$DEST"
fi
chmod +x "$DEST" 2>/dev/null || true

echo "Installed. Run: codepilot-workspace"
