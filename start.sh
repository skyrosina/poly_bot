#!/bin/bash
# PolyBot startup helper for macOS/Linux.

set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION_NAME="${SESSION_NAME:-polybot}"

echo "PolyBot startup"
echo "Project: $PROJ_DIR"

if pgrep -x tor > /dev/null 2>&1; then
    echo "Stopping stale Tor processes..."
    pkill tor 2>/dev/null || true
    sleep 2
fi

if command -v lsof >/dev/null 2>&1 && lsof -i :9050 > /dev/null 2>&1; then
    echo "Freeing port 9050..."
    lsof -ti :9050 | xargs kill -9 2>/dev/null || true
    sleep 2
fi

if command -v tmux >/dev/null 2>&1; then
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        tmux kill-session -t "$SESSION_NAME"
    fi

    PYTHON_BIN="python"
    if [ -x "$PROJ_DIR/.venv/bin/python" ]; then
        PYTHON_BIN="$PROJ_DIR/.venv/bin/python"
    fi

    tmux new-session -d -s "$SESSION_NAME" -c "$PROJ_DIR" \
        "$PYTHON_BIN bot.py; echo 'Bot exited. Press enter to close.'; read"

    echo "Running in tmux session: $SESSION_NAME"
    echo "Attach: tmux attach -t $SESSION_NAME"
else
    echo "tmux not found; running in the foreground."
    if [ -x "$PROJ_DIR/.venv/bin/python" ]; then
        "$PROJ_DIR/.venv/bin/python" "$PROJ_DIR/bot.py"
    else
        python "$PROJ_DIR/bot.py"
    fi
fi
