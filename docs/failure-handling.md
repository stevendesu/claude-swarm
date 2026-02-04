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

## 4. Agent crash detection

If a container crashes (OOM, Docker restart, etc.), the ticket stays `in_progress` with an assignee, but nobody is working on it.

Ideas:
- Heartbeat: agents periodically touch a file or update a timestamp. A monitor process detects stale heartbeats and unclaims tickets
- Use Docker health checks — if a container is unhealthy, unclaim its tickets
- The monitor web app could detect containers that are down and auto-unclaim their in-progress tickets
- On agent startup, check if this agent already has in_progress tickets from a previous run. If the branch has no new commits, unclaim and start fresh
- See if Claude Code has built-in crash recovery or session resumption

## 5. Claimed but not worked

A ticket could be claimed but the agent is idle (waiting on rate limits, sleeping in a retry loop, or simply stuck without crashing).

Ideas:
- Track the last activity timestamp per ticket (last comment, last commit). If stale for M minutes, unclaim
- The agent loop already has `MAX_TURNS` but doesn't have a wall-clock timeout. Add one
- Combine with heartbeat: if the agent process is alive but hasn't made progress, force-kill the Claude session and unclaim

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
