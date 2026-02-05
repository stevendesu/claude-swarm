#!/bin/bash
# check-alive.sh — Detect stalled agent-loop and restart if needed
#
# This script is run by cron every 5 minutes. It checks the modification time
# of the most recent log file in the agent's log directory. If no log activity
# has occurred for STALE_THRESHOLD_MINUTES (default 20), it sends SIGTERM to
# the agent-loop process, which triggers graceful shutdown (unclaiming any
# in-progress ticket). Docker's restart policy then brings the container back.

set -euo pipefail

LOG_DIR="/workspace/.agent-logs"
STALE_THRESHOLD_MINUTES="${STALE_THRESHOLD_MINUTES:-20}"

# Find the most recently modified log file
latest_log=$(ls -t "$LOG_DIR"/*.log 2>/dev/null | head -1 || true)

if [ -z "$latest_log" ]; then
    # No logs yet — agent might still be starting up, or no tickets processed
    echo "$(date -Iseconds) No log files found in $LOG_DIR — skipping check"
    exit 0
fi

# Get file modification time in seconds since epoch
# Note: stat syntax differs between GNU (Linux) and BSD (macOS)
if stat --version 2>/dev/null | grep -q GNU; then
    file_mtime=$(stat -c %Y "$latest_log")
else
    file_mtime=$(stat -f %m "$latest_log")
fi

now=$(date +%s)
age_seconds=$((now - file_mtime))
age_minutes=$((age_seconds / 60))

echo "$(date -Iseconds) Latest log: $latest_log (${age_minutes}m old, threshold ${STALE_THRESHOLD_MINUTES}m)"

if [ "$age_minutes" -gt "$STALE_THRESHOLD_MINUTES" ]; then
    echo "$(date -Iseconds) Agent stalled: last log activity was ${age_minutes}m ago (threshold: ${STALE_THRESHOLD_MINUTES}m)"

    # Find and kill the agent-loop process
    # The entrypoint runs agent-loop.sh, so we look for that
    agent_pid=$(pgrep -f "agent-loop.sh" || true)

    if [ -n "$agent_pid" ]; then
        echo "$(date -Iseconds) Sending SIGTERM to agent-loop (pid $agent_pid)"
        kill -TERM "$agent_pid" 2>/dev/null || true
    else
        echo "$(date -Iseconds) No agent-loop process found — container may already be restarting"
    fi
fi
