#!/bin/bash
#
# Scheduled runner for the Asana <-> Notion sync.
#
# Safe to call from cron or launchd: it resolves its own directory, so the
# working directory it's invoked from doesn't matter, and it calls the venv's
# python by absolute path, so it doesn't depend on PATH either.
#
# Usage:
#   ./run_sync.sh                          # full sync
#   ./run_sync.sh --discover               # list visible Notion DBs
#   ./run_sync.sh --merge-dupes --dry-run  # any flag the script accepts
#
# Set PYTHON=/full/path/to/python before calling to use a different interpreter.
#
# Logs to logs/sync.log (appended, trimmed to the last 5000 lines).

set -euo pipefail

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/sync.log"
MAX_LOG_LINES=5000

mkdir -p "$LOG_DIR"

# Send everything (stdout + stderr) to the log and to the console. Under cron
# the console half is what lands in the job's mail; interactively you just see it.
exec > >(tee -a "$LOG_FILE") 2>&1
TEE_PID=$!

echo "════════════════════════════════════════════════════════════════"
echo "Sync started: $(date '+%Y-%m-%d %H:%M:%S %Z')"

# ── Preflight ─────────────────────────────────────────────────────────────────

if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    echo "ERROR: no .env found at $SCRIPT_DIR/.env (needs ASANA_TOKEN and NOTION_TOKEN)."
    exec 1>&- 2>&-; wait "$TEE_PID" 2>/dev/null || true
    exit 1
fi

# Build the venv on first run, or if it's been deleted, so a fresh clone works.
if [[ -z "${PYTHON:-}" && ! -x "$VENV_DIR/bin/python" ]]; then
    echo "No venv found — creating one at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
    echo "Venv created and dependencies installed."
fi

# No `activate` needed: python reads pyvenv.cfg next to this binary and sets
# sys.prefix from it, so calling it by path gives the venv's site-packages.
PYTHON="${PYTHON:-$VENV_DIR/bin/python}"

# ── Run ───────────────────────────────────────────────────────────────────────

cd "$SCRIPT_DIR"

echo "Python: $("$PYTHON" --version 2>&1) at $PYTHON"

# `|| EXIT_CODE=$?` keeps a failing run from tripping `set -e`, so we still
# reach the log flush and trim below and can report the real exit code.
EXIT_CODE=0
"$PYTHON" asana_notion_sync.py "$@" || EXIT_CODE=$?

if [[ $EXIT_CODE -eq 0 ]]; then
    echo "Sync finished OK: $(date '+%Y-%m-%d %H:%M:%S %Z')"
else
    echo "Sync FAILED (exit $EXIT_CODE): $(date '+%Y-%m-%d %H:%M:%S %Z')"
fi

# ── Finish ────────────────────────────────────────────────────────────────────

# Close our end of the pipe and wait for tee to flush, so the log always has the
# full run even when we exit non-zero. Nothing may be printed after this point.
exec 1>&- 2>&-
wait "$TEE_PID" 2>/dev/null || true

# Trim the log so it can't grow without bound.
if [[ $(wc -l < "$LOG_FILE") -gt $MAX_LOG_LINES ]]; then
    tail -n $MAX_LOG_LINES "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

exit $EXIT_CODE
