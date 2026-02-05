# Failure Handling — Planning Notes

Open problems and ideas for making the swarm resilient to agent failures.

## 1. Agent thrashing detection

An agent can get stuck in a loop: claim ticket, fail, unclaim, re-claim the same ticket.
Need a way to detect this and escalate.

Ideas:
- Track claim count per ticket in the activity log. If a ticket is claimed N times (3?), flag it
- Add a `retry_count` column to tickets, incremented on unclaim. After threshold, auto-assign to human
- The agent loop could check if the ticket it just claimed was previously unclaimed by the same agent and skip it

## 2. Context window overflow

Long-running Claude sessions accumulate context and degrade in quality (context rot).
The current `MAX_TURNS` setting is a blunt limit but doesn't account for context size.

Ideas:
- Monitor the Claude session output size. If it exceeds a threshold, kill the session and unclaim
- Use shorter max_turns for retry attempts (e.g. first attempt 50 turns, second attempt 30)
- Log context window usage from Claude's output if available
- The agent loop could split work: run Claude once to plan, then a second session to execute (fresh context)

## 3. "Impossible" tickets

Some tickets may be genuinely too large, ambiguous, or impossible for an agent to complete.
After repeated failures these should be surfaced for human review and decomposition.

Ideas:
- After N failed attempts (claim + unclaim cycles), auto-assign to human with a comment summarizing attempts
- Include agent log excerpts in the escalation comment so the human has context
- Add a `needs_decomposition` flag or label system for tickets

## 4. Agent crash detection ✅ IMPLEMENTED

If a container crashes (OOM, Docker restart, etc.), the ticket stays `in_progress` with an assignee, but nobody is working on it.

**Solution implemented (three layers):**

1. **Unclaim all on swarm start** — `swarm start` calls `unclaim_all_in_progress()` which resets any in_progress tickets to open status. This handles container crashes, OOM kills, scale-downs, and OAuth expiry. Activity is logged as "Auto-released on swarm start".

2. **SIGTERM trap in agent-loop.sh** — When Docker sends SIGTERM (via `docker stop`), the agent gracefully unclaims its current ticket before exiting. The ticket returns to the pool immediately rather than waiting for the next swarm start.

3. **Cron-based stall detection** — Each container runs a cron job (every 5 minutes) that checks log file activity. If no logs have been written for 20+ minutes, it sends SIGTERM to agent-loop, triggering graceful shutdown. Docker's `restart: unless-stopped` policy brings the container back fresh.

**Configuration:** `STALE_THRESHOLD_MINUTES` env var (default 20) controls how long before a stalled agent is restarted.

## 5. Claimed but not worked ✅ IMPLEMENTED

A ticket could be claimed but the agent is idle (waiting on rate limits, sleeping in a retry loop, or simply stuck without crashing).

**Solution:** The cron-based stall detection (Layer 3 above) handles this case. If the agent-loop process is alive but not making progress (no log file updates), check-alive.sh detects the stall and restarts the agent after 20 minutes of inactivity.

## 6. Merge conflict loops

An agent resolves its conflict, pushes, but by then another agent has pushed again. Repeated rebase cycles.

Ideas:
- The current 3-retry limit handles this, but on final failure the ticket is unclaimed and will be retried from scratch (possibly hitting the same conflict)
- Consider serializing merges: a lock file or merge queue so only one agent merges at a time
- Track merge failure count in activity log. If a ticket keeps failing at merge, flag for review

## Priority

Roughly ordered by impact:
1. Agent crash detection (silent failures, tickets stuck forever)
2. Thrashing detection (wasted compute, tickets never completed)
3. Claimed but not worked (similar to crash — silent stall)
4. Context window overflow (quality degradation)
5. Impossible tickets (human escalation)
6. Merge conflict loops (less common with small teams)
