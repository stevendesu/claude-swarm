#!/bin/bash
set -euo pipefail

AGENT_ID="${AGENT_ID:?AGENT_ID must be set}"
WORKSPACE="/workspace"
REPO_BARE="/repo.git"
TICKET_DB="/tickets/tickets.db"
NTFY_TOPIC="${NTFY_TOPIC:-}"
MAX_TURNS="${MAX_TURNS:-50}"
ALLOWED_TOOLS="${ALLOWED_TOOLS:-Bash,Read,Write,Edit,Glob,Grep}"
LOG_DIR="/workspace/.agent-logs"

# Track current ticket for graceful shutdown
CURRENT_TICKET_ID=""

# ── Graceful shutdown handler ────────────────────────────────────────────────
# When the container receives SIGTERM (docker stop) or SIGINT (Ctrl+C),
# unclaim any in-progress ticket so it returns to the pool.
cleanup() {
    echo "[$AGENT_ID] Received shutdown signal, cleaning up..."
    if [ -n "$CURRENT_TICKET_ID" ]; then
        ticket --db "$TICKET_DB" comment "$CURRENT_TICKET_ID" \
            "Agent $AGENT_ID shutting down, releasing ticket" --author "$AGENT_ID" || true
        ticket --db "$TICKET_DB" unclaim "$CURRENT_TICKET_ID" || true
        echo "[$AGENT_ID] Released ticket #$CURRENT_TICKET_ID"
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

mkdir -p "$LOG_DIR"
cd "$WORKSPACE"

while true; do
  # Try to claim a ticket
  TICKET_JSON=$(ticket --db "$TICKET_DB" claim-next --agent "$AGENT_ID" --format json 2>/dev/null || echo "")

  if [ -z "$TICKET_JSON" ]; then
    # No ticket available — check if the queue is completely empty
    TOTAL=$(ticket --db "$TICKET_DB" count --status open,in_progress 2>/dev/null || echo "0")

    if [ "$TOTAL" -eq 0 ]; then
      # Queue is completely empty — propose improvements
      PROPOSAL_ID=$(ticket --db "$TICKET_DB" create "Reviewing codebase for improvements" \
        --assign "$AGENT_ID" --created-by "$AGENT_ID")

      PROPOSAL_LOG="$LOG_DIR/proposal-$(date +%Y%m%d-%H%M%S).log"
      claude -p "Review the codebase. Identify the single most impactful \
        improvement. Output ONLY a title line and a description paragraph, \
        nothing else." \
        --output-format text \
        --allowedTools "Read,Glob,Grep" \
        --max-turns 20 \
        2>&1 | tee "$PROPOSAL_LOG" > /tmp/proposal.txt

      PROP_TITLE=$(head -1 /tmp/proposal.txt | xargs)
      PROP_DESC=$(tail -n +2 /tmp/proposal.txt | xargs)

      if [ -z "$PROP_TITLE" ]; then
        ticket --db "$TICKET_DB" comment "$PROPOSAL_ID" "Proposal generation produced no output — see $PROPOSAL_LOG" --author "$AGENT_ID"
        ticket --db "$TICKET_DB" update "$PROPOSAL_ID" \
          --title "[Empty proposal]" \
          --description "Agent failed to generate a proposal. Check logs." \
          --assign human \
          --status open
      else
        ticket --db "$TICKET_DB" update "$PROPOSAL_ID" \
          --title "$PROP_TITLE" \
          --description "$PROP_DESC" \
          --assign human \
          --status open
      fi

      # Notify human
      if [ -n "$NTFY_TOPIC" ]; then
        curl -s -d "Agent proposal: $PROP_TITLE" "ntfy.sh/$NTFY_TOPIC"
      fi

      sleep 300  # Wait 5 minutes before checking again
    else
      sleep 10   # Tickets exist but none available for us
    fi
    continue
  fi

  # Extract ticket details
  TICKET_ID=$(echo "$TICKET_JSON" | jq -r '.id')
  CURRENT_TICKET_ID="$TICKET_ID"  # Track for graceful shutdown
  TITLE=$(echo "$TICKET_JSON" | jq -r '.title')
  DESC=$(echo "$TICKET_JSON" | jq -r '.description // ""')
  COMMENTS=$(ticket --db "$TICKET_DB" comments "$TICKET_ID" --format text 2>/dev/null || echo "")
  PARENT_CONTEXT=""
  PARENT_ID=$(echo "$TICKET_JSON" | jq -r '.parent_id // ""')
  if [ -n "$PARENT_ID" ] && [ "$PARENT_ID" != "null" ]; then
    PARENT_CONTEXT=$(ticket --db "$TICKET_DB" show "$PARENT_ID" --format text 2>/dev/null || echo "")
  fi

  # Ensure clean workspace before starting
  git checkout main >/dev/null 2>&1 || git checkout -b main >/dev/null 2>&1
  for branch in $(git for-each-ref --format='%(refname:short)' refs/heads/ | grep -v '^main$'); do
    git branch -D "$branch" >/dev/null 2>&1 || true
  done
  git fetch origin main >/dev/null 2>&1 || true
  git reset --hard origin/main >/dev/null 2>&1 || true
  git clean -fd >/dev/null 2>&1 || true

  # Create a branch for this ticket
  BRANCH="ticket-${TICKET_ID}"
  git checkout -b "$BRANCH" main

  # Run Claude Code
  ticket --db "$TICKET_DB" comment "$TICKET_ID" "Starting work on branch $BRANCH" --author "$AGENT_ID"

  WORK_LOG="$LOG_DIR/ticket-${TICKET_ID}-$(date +%Y%m%d-%H%M%S).log"

  claude -p "You are autonomous agent $AGENT_ID working on ticket #$TICKET_ID.

TICKET: $TITLE
DESCRIPTION: $DESC
COMMENTS: $COMMENTS
PARENT TICKET CONTEXT: $PARENT_CONTEXT

TICKET COMMANDS (use --db $TICKET_DB for all commands):
- Log progress: ticket comment $TICKET_ID \"message\" --author $AGENT_ID
- Create sub-tickets: ticket create \"Title\" --parent $TICKET_ID --description \"...\" --created-by $AGENT_ID
- Depends on other work: ticket create \"Dependent task\" --blocked-by <PREREQUISITE_TICKET_ID> --created-by $AGENT_ID
- Mark blocked: ticket block $TICKET_ID --by <BLOCKER_ID> (releases ticket; all code changes discarded)
- Ask a human: ticket create \"Question\" --assign human --created-by $AGENT_ID (then: ticket block $TICKET_ID --by <QUESTION_TICKET_ID>)
- Release if stuck: ticket unclaim $TICKET_ID (all code changes discarded)

Read CLAUDE.md for full operating guidelines." \
    --output-format text \
    --allowedTools "$ALLOWED_TOOLS" \
    --max-turns "$MAX_TURNS" \
    2>&1 | tee "$WORK_LOG"

  ticket --db "$TICKET_DB" comment "$TICKET_ID" "Claude session log: $WORK_LOG ($(wc -l < "$WORK_LOG") lines)" --author "$AGENT_ID"

  # Post-agent git workflow
  # Ensure any uncommitted changes are on a branch (not detached HEAD)
  CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "")
  if [ -z "$CURRENT_BRANCH" ]; then
    git checkout -B "$BRANCH" 2>/dev/null || true
  fi
  if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git commit -m "ticket-${TICKET_ID}: ${TITLE}"
  fi

  # Check if the agent released the ticket (blocked or unclaimed)
  TICKET_STATE_JSON=$(ticket --db "$TICKET_DB" show "$TICKET_ID" --format json 2>/dev/null || echo "{}")
  TICKET_ASSIGNED=$(echo "$TICKET_STATE_JSON" | jq -r '.assigned_to // ""')
  TICKET_STATUS=$(echo "$TICKET_STATE_JSON" | jq -r '.status // ""')

  if [ "$TICKET_ASSIGNED" != "$AGENT_ID" ] && [ "$TICKET_STATUS" != "ready" ]; then
    # Agent released the ticket — discard half-baked changes
    ticket --db "$TICKET_DB" comment "$TICKET_ID" \
      "Discarding code changes — ticket was released during work" --author "$AGENT_ID"
    git checkout main 2>/dev/null || true
    for branch in $(git for-each-ref --format='%(refname:short)' refs/heads/ | grep -v '^main$'); do
      git branch -D "$branch" >/dev/null 2>&1 || true
    done
    git reset --hard origin/main 2>/dev/null || true
    git clean -fd 2>/dev/null || true
    continue
  fi

  # Merge all local branches with new commits into main
  git checkout main 2>/dev/null || true
  MERGE_FAILED=false
  for branch in $(git for-each-ref --format='%(refname:short)' refs/heads/ | grep -v '^main$'); do
    AHEAD=$(git rev-list main.."$branch" --count 2>/dev/null || echo "0")
    if [ "$AHEAD" -eq 0 ]; then
      git branch -D "$branch" 2>/dev/null || true
      continue
    fi

    if ! git merge "$branch" --no-edit 2>/dev/null; then
      ticket --db "$TICKET_DB" comment "$TICKET_ID" \
        "Merge conflict merging $branch into main, invoking conflict resolution" --author "$AGENT_ID"
      CONFLICT_LOG="$LOG_DIR/ticket-${TICKET_ID}-conflict-$(date +%Y%m%d-%H%M%S).log"
      claude -p "There is a merge conflict merging branch $branch into main in /workspace. \
        Resolve all conflicts, keeping both sets of changes where possible. \
        After resolving, stage all resolved files with git add." \
        --output-format text \
        --allowedTools "Bash,Read,Write,Edit,Glob,Grep" \
        --max-turns 10 \
        2>&1 | tee "$CONFLICT_LOG"

      if [ -z "$(git diff --name-only --diff-filter=U 2>/dev/null)" ]; then
        git commit --no-edit 2>/dev/null || true
      else
        git merge --abort 2>/dev/null || true
        MERGE_FAILED=true
        break
      fi
    fi

    git branch -d "$branch" 2>/dev/null || true
  done

  if [ "$MERGE_FAILED" = true ]; then
    ticket --db "$TICKET_DB" comment "$TICKET_ID" \
      "Failed to merge branches into main — needs manual resolution" --author "$AGENT_ID"
    ticket --db "$TICKET_DB" unclaim "$TICKET_ID"
    git checkout main 2>/dev/null || true
    continue
  fi

  # Check if main has new commits to push
  git fetch origin main 2>/dev/null || true
  AHEAD=$(git rev-list origin/main..main --count 2>/dev/null || echo "0")

  if [ "$AHEAD" -gt 0 ]; then
    # Push main to origin with merge-and-retry on rejection
    PUSH_SUCCESS=false
    MAX_RETRIES=3
    for i in $(seq 1 $MAX_RETRIES); do
      if git push origin main 2>/dev/null; then
        PUSH_SUCCESS=true
        break
      fi

      # Push rejected — merge origin/main and retry
      git fetch origin main 2>/dev/null || true
      if ! git merge origin/main --no-edit 2>/dev/null; then
        ticket --db "$TICKET_DB" comment "$TICKET_ID" \
          "Merge conflict with origin on push attempt $i, invoking conflict resolution" --author "$AGENT_ID"
        CONFLICT_LOG="$LOG_DIR/ticket-${TICKET_ID}-origin-conflict-$(date +%Y%m%d-%H%M%S).log"
        claude -p "There is a merge conflict between local main and origin/main in /workspace. \
          Resolve all conflicts, keeping both sets of changes where possible. \
          After resolving, stage all resolved files with git add." \
          --output-format text \
          --allowedTools "Bash,Read,Write,Edit,Glob,Grep" \
          --max-turns 10 \
          2>&1 | tee "$CONFLICT_LOG"

        if [ -z "$(git diff --name-only --diff-filter=U 2>/dev/null)" ]; then
          git commit --no-edit 2>/dev/null || true
        else
          git merge --abort 2>/dev/null || true
          break
        fi
      fi
    done

    if [ "$PUSH_SUCCESS" = true ]; then
      ticket --db "$TICKET_DB" mark-done "$TICKET_ID"
      ticket --db "$TICKET_DB" comment "$TICKET_ID" "Merged to main and completed" --author "$AGENT_ID"
    else
      ticket --db "$TICKET_DB" comment "$TICKET_ID" \
        "Failed to push to origin after $MAX_RETRIES attempts — needs manual resolution" --author "$AGENT_ID"
      ticket --db "$TICKET_DB" unclaim "$TICKET_ID"
    fi
  else
    ticket --db "$TICKET_DB" comment "$TICKET_ID" "No code changes produced" --author "$AGENT_ID"
    ticket --db "$TICKET_DB" mark-done "$TICKET_ID"
  fi

  # Clean up for next iteration
  CURRENT_TICKET_ID=""  # Clear ticket tracking (no longer our responsibility)
  git checkout main 2>/dev/null || true
done
