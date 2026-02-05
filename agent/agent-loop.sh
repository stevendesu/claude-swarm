#!/bin/bash
set -euo pipefail

AGENT_ID="${AGENT_ID:?AGENT_ID must be set}"
WORKSPACE="/workspace"
REPO_BARE="/repo.git"
TICKET_DB="/tickets/tickets.db"
NTFY_TOPIC="${NTFY_TOPIC:-}"
MAX_TURNS="${MAX_TURNS:-50}"
ALLOWED_TOOLS="${ALLOWED_TOOLS:-Bash,Read,Write,Edit,Glob,Grep}"
VERIFY_RETRIES="${VERIFY_RETRIES:-2}"
LOG_DIR="/workspace/.agent-logs"

# ── Verification gate ────────────────────────────────────────────────────────
# Runs checks after Claude finishes work but before git commit.
# Returns error output on stdout (empty string = pass).
run_verification() {
  local errors=""

  # Universal check: scan for merge conflict markers in tracked files
  local conflict_files
  conflict_files=$(git diff --name-only HEAD 2>/dev/null || true)
  conflict_files="$conflict_files"$'\n'"$(git diff --cached --name-only 2>/dev/null || true)"
  conflict_files="$conflict_files"$'\n'"$(git ls-files --others --exclude-standard 2>/dev/null || true)"

  for f in $(echo "$conflict_files" | sort -u | grep -v '^$'); do
    if [ -f "$f" ] && grep -qE '^(<{7}|>{7})' "$f" 2>/dev/null; then
      errors="${errors}Conflict markers found in $f"$'\n'
    fi
  done

  # Project-specific: run verify.sh if it exists and is executable
  if [ -x "./verify.sh" ]; then
    local verify_output
    if ! verify_output=$(./verify.sh 2>&1); then
      errors="${errors}verify.sh failed:"$'\n'"${verify_output}"$'\n'
    fi
  fi

  echo -n "$errors"
}

# ── Proposal flow ────────────────────────────────────────────────────────────
# Runs Claude in read-only mode to generate a proposal, then reassigns the
# ticket to a human for review.  Called both when the queue is empty (fresh
# proposal) and when an agent claims a ticket whose type is "proposal"
# (crash-recovery of a previous proposal attempt).
run_proposal_flow() {
  local PROP_TICKET_ID="$1"

  PROPOSAL_LOG="$LOG_DIR/proposal-$(date +%Y%m%d-%H%M%S).log"
  claude -p "Review the codebase. Identify the single most impactful \
    improvement. Output ONLY a title line and a description paragraph, \
    nothing else." \
    --output-format stream-json \
    --verbose \
    --allowedTools "Read,Glob,Grep" \
    --max-turns 20 \
    2>&1 | tee "$PROPOSAL_LOG" > /tmp/proposal.txt

  PROP_RESULT=$(tail -n 1 /tmp/proposal.txt | jq -r '.result // empty')
  PROP_TITLE=$(echo "$PROP_RESULT" | head -1 | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//')
  PROP_DESC=$(echo "$PROP_RESULT" | tail -n +2 | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//')

  if [ -z "$PROP_TITLE" ]; then
    ticket --db "$TICKET_DB" comment "$PROP_TICKET_ID" \
      "Proposal generation produced no output — see $PROPOSAL_LOG" --author "$AGENT_ID"
    ticket --db "$TICKET_DB" update "$PROP_TICKET_ID" \
      --title "[Empty proposal]" \
      --description "Agent failed to generate a proposal. Check logs." \
      --assign human --status open
  else
    ticket --db "$TICKET_DB" update "$PROP_TICKET_ID" \
      --title "$PROP_TITLE" \
      --description "$PROP_DESC" \
      --assign human --status open
  fi

  CURRENT_TICKET_ID=""  # Proposal handed off to human, no longer ours

  if [ -n "$NTFY_TOPIC" ]; then
    curl -s -d "Agent proposal: ${PROP_TITLE:-[empty]}" "ntfy.sh/$NTFY_TOPIC"
  fi
}

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

# ── Recover from previous crash: unclaim any orphaned tickets ──────────────
# If this agent previously crashed, tickets may still be assigned to it.
# Docker restart and check-alive.sh restarts bypass `swarm start`, so the
# global unclaim-all doesn't run. Clean up per-agent here instead.
ORPHANS=$(ticket --db "$TICKET_DB" list --assigned-to "$AGENT_ID" --status open,in_progress --format json 2>/dev/null || echo "[]")
for ORPHAN_ID in $(echo "$ORPHANS" | jq -r '.[].id'); do
    ticket --db "$TICKET_DB" comment "$ORPHAN_ID" \
        "Agent restarted — releasing orphaned ticket" --author "$AGENT_ID" || true
    ticket --db "$TICKET_DB" unclaim "$ORPHAN_ID" || true
    echo "[$AGENT_ID] Released orphaned ticket #$ORPHAN_ID"
done

while true; do
  # Try to claim a ticket
  TICKET_JSON=$(ticket --db "$TICKET_DB" claim-next --agent "$AGENT_ID" --format json 2>/dev/null || echo "")

  if [ -z "$TICKET_JSON" ]; then
    # No ticket available — check if the queue is completely empty
    TOTAL=$(ticket --db "$TICKET_DB" count --status open,in_progress 2>/dev/null || echo "0")

    if [ "$TOTAL" -eq 0 ]; then
      # Queue is completely empty — propose improvements
      PROPOSAL_ID=$(ticket --db "$TICKET_DB" create "Reviewing codebase for improvements" \
        --assign "$AGENT_ID" --created-by "$AGENT_ID" --type proposal)
      CURRENT_TICKET_ID="$PROPOSAL_ID"  # Track for graceful shutdown
      run_proposal_flow "$PROPOSAL_ID"
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

  # If this is a proposal ticket (e.g. re-claimed after a crash), run the
  # proposal flow instead of the normal work flow.
  TICKET_TYPE=$(echo "$TICKET_JSON" | jq -r '.type // "task"')
  if [ "$TICKET_TYPE" = "proposal" ]; then
    run_proposal_flow "$TICKET_ID"
    sleep 300
    continue
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
    --output-format stream-json \
    --verbose \
    --allowedTools "$ALLOWED_TOOLS" \
    --max-turns "$MAX_TURNS" \
    2>&1 | tee "$WORK_LOG"

  ticket --db "$TICKET_DB" comment "$TICKET_ID" "Claude session log: $WORK_LOG ($(wc -l < "$WORK_LOG") lines)" --author "$AGENT_ID"

  # Post-agent git workflow

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

  # Ensure any uncommitted changes are on a branch (not detached HEAD)
  CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "")
  if [ -z "$CURRENT_BRANCH" ]; then
    git checkout -B "$BRANCH" 2>/dev/null || true
  fi

  # ── Verification gate ──────────────────────────────────────────────
  # Run checks before committing. If they fail, give Claude a chance to fix.
  if [ -n "$(git status --porcelain)" ]; then
    VERIFY_ERRORS=$(run_verification)
    VERIFY_ATTEMPT=0
    while [ -n "$VERIFY_ERRORS" ] && [ "$VERIFY_ATTEMPT" -lt "$VERIFY_RETRIES" ]; do
      VERIFY_ATTEMPT=$((VERIFY_ATTEMPT + 1))
      ticket --db "$TICKET_DB" comment "$TICKET_ID" \
        "Verification failed (attempt $VERIFY_ATTEMPT/$VERIFY_RETRIES): $VERIFY_ERRORS" --author "$AGENT_ID"

      VERIFY_LOG="$LOG_DIR/ticket-${TICKET_ID}-verify-$(date +%Y%m%d-%H%M%S).log"
      claude -p "You are autonomous agent $AGENT_ID fixing verification errors for ticket #$TICKET_ID.

TICKET: $TITLE
DESCRIPTION: $DESC

The following verification checks failed. Fix all errors:

$VERIFY_ERRORS" \
        --output-format stream-json \
        --verbose \
        --allowedTools "$ALLOWED_TOOLS" \
        --max-turns 10 \
        2>&1 | tee "$VERIFY_LOG"

      VERIFY_ERRORS=$(run_verification)
    done

    if [ -n "$VERIFY_ERRORS" ]; then
      ticket --db "$TICKET_DB" comment "$TICKET_ID" \
        "Verification still failing after $VERIFY_RETRIES retries, releasing ticket: $VERIFY_ERRORS" --author "$AGENT_ID"
      ticket --db "$TICKET_DB" unclaim "$TICKET_ID"
      git checkout main 2>/dev/null || true
      for branch in $(git for-each-ref --format='%(refname:short)' refs/heads/ | grep -v '^main$'); do
        git branch -D "$branch" >/dev/null 2>&1 || true
      done
      git reset --hard origin/main 2>/dev/null || true
      git clean -fd 2>/dev/null || true
      continue
    fi

    git add -A
    git commit -m "ticket-${TICKET_ID}: ${TITLE}"
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
        --output-format stream-json \
        --verbose \
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
          --output-format stream-json \
          --verbose \
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
