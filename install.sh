#!/usr/bin/env bash
# Symlink bin/agenttalk into ~/.local/bin so it is available on PATH globally.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${AGENTTALK_INSTALL_DIR:-$HOME/.local/bin}"

mkdir -p "$TARGET_DIR"
chmod +x "$SCRIPT_DIR/bin/agenttalk"
ln -sf "$SCRIPT_DIR/bin/agenttalk" "$TARGET_DIR/agenttalk"

echo "Linked $TARGET_DIR/agenttalk -> $SCRIPT_DIR/bin/agenttalk"

case ":$PATH:" in
    *":$TARGET_DIR:"*) echo "$TARGET_DIR is on PATH." ;;
    *) echo "WARNING: $TARGET_DIR is not on PATH. Add it to your shell profile." ;;
esac
