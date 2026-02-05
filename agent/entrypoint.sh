#!/bin/bash
set -euo pipefail

# ── Verify Claude Code credentials ───────────────────────────────────────
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "FATAL: CLAUDE_CODE_OAUTH_TOKEN is not set" >&2
  echo "  Extract it with: security find-generic-password -s 'Claude Code-credentials' -w | jq -r '.claudeAiOauth.accessToken'" >&2
  exit 1
fi

# ── Validate required env ─────────────────────────────────────────────────
: "${AGENT_ID:?AGENT_ID must be set}"

# ── Seed ~/.claude from host config (skip interactive setup wizard) ──────
CLAUDE_HOME="${HOME}/.claude"
mkdir -p "$CLAUDE_HOME"
if [ -f /host-claude-config/settings.json ]; then
  cp /host-claude-config/settings.json "$CLAUDE_HOME/settings.json"
  echo "[$AGENT_ID] Copied settings.json from host"
fi
if [ -d /host-claude-config/statsig ]; then
  cp -r /host-claude-config/statsig "$CLAUDE_HOME/statsig"
  echo "[$AGENT_ID] Copied statsig/ from host"
fi

# ── Clone from the shared bare repo ──────────────────────────────────────
if [ ! -d /workspace/.git ]; then
  echo "[$AGENT_ID] Cloning from bare repo..."
  git clone /repo.git /workspace
else
  echo "[$AGENT_ID] Workspace already exists, fetching latest..."
  cd /workspace
  git fetch origin
  git checkout main 2>/dev/null || git checkout -b main origin/main 2>/dev/null || true
  git pull origin main --ff-only 2>/dev/null || true
fi

# ── Configure git identity ────────────────────────────────────────────────
cd /workspace
git config user.name "$AGENT_ID"
git config user.email "agent@swarm.local"

# ── Export TICKET_DB so ticket CLI finds it ───────────────────────────────
export TICKET_DB="/tickets/tickets.db"

# ── Start cron for stall detection ────────────────────────────────────────
# Cron runs check-alive.sh every 5 minutes to detect stalled agents.
# If no log activity for 20+ minutes, it sends SIGTERM to restart the agent.
cron
echo "[$AGENT_ID] Started cron daemon for stall detection"

# ── Hand off to the agent loop ────────────────────────────────────────────
echo "[$AGENT_ID] Starting agent loop..."
exec /usr/local/bin/agent-loop.sh
