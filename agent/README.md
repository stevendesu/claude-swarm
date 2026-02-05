# Agent

This directory contains everything needed to build and run an autonomous agent container: the Dockerfile, the entrypoint script, and the agent loop script.

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Builds the agent container image. Base is `node:lts-slim` with git, jq, curl, python3, and Claude Code CLI installed. |
| `entrypoint.sh` | Container startup script. Verifies Claude credentials, clones the repo, configures git, then hands off to the agent loop. |
| `agent-loop.sh` | Main process. Continuously claims tickets, invokes Claude Code, and manages the git workflow. |
| `check-alive.sh` | Cron script that detects stalled agents and triggers a restart. |
| `project-setup.sh` | Project-specific Docker build hook. No-op by default; populated by the interview for stacks that need extra packages (e.g. Android SDK). Preserved across `swarm regenerate`. |

## Building

The Dockerfile expects the **project root** as its build context (not the `agent/` directory) because it copies files from both `ticket/` and `agent/`. The `docker-compose.yml` at the project root handles this:

```yaml
agent-1:
  build:
    context: .
    dockerfile: agent/Dockerfile
```

To build manually:

```bash
docker build -f agent/Dockerfile -t swarm-agent .
```

## Volume Mounts

Each agent container expects these mounts:

| Mount | Purpose |
|---|---|
| `/tickets` | Directory containing `tickets.db` (shared SQLite database). All agents read/write the same database. |
| `/repo.git` | Shared bare git repository. Agents clone from and push to this. |
| `/workspace` | Agent's private clone. Container-local by default, but can be volume-mounted for persistence across restarts. |
| `/home/node/.claude` | Claude Code config/credentials directory (mounted read-only from host `~/.claude`). |

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENT_ID` | Yes | -- | Unique identifier for this agent (e.g. `agent-1`). Used for git commits, ticket claiming, and comments. |
| `CLAUDE_CONFIG_DIR` | No | `/home/node/.claude` | Path to the Claude Code config directory containing OAuth credentials. |
| `NTFY_TOPIC` | No | `""` | [ntfy.sh](https://ntfy.sh) topic for push notifications. If set, the agent sends notifications when proposing improvements. |
| `MAX_TURNS` | No | `50` | Maximum number of turns Claude Code can take per ticket. |
| `ALLOWED_TOOLS` | No | `Bash,Read,Write,Edit,Glob,Grep` | Comma-separated list of tools Claude Code is allowed to use. |
| `VERIFY_RETRIES` | No | `2` | Number of times to retry verification (lint/test) failures before unclaiming the ticket. |

## Entrypoint Startup Sequence

The `entrypoint.sh` script runs these steps before handing off to the agent loop:

1. **Verify Claude credentials** by checking that `$CLAUDE_CONFIG_DIR` (default `/home/node/.claude`) exists and is mounted. Exits with an error if missing.
2. **Clone the repo** from `/repo.git` into `/workspace`. If the workspace already exists (container restart with a persistent volume), it fetches and fast-forwards instead.
3. **Configure git** identity using `$AGENT_ID` as the committer name and `agent@swarm.local` as the email.
4. **Export `TICKET_DB`** so the ticket CLI finds the shared database at `/tickets/tickets.db`.
5. **Exec the agent loop** (`agent-loop.sh`), replacing the entrypoint process.

## Agent Loop

The agent loop script (`agent-loop.sh`) is the main process that runs inside each agent container. It drives autonomous behavior by continuously claiming tickets, invoking Claude Code to do the work, and managing the git workflow around each task.

### How It Works

The script runs an infinite loop with this cycle:

1. **Pull latest main** -- ensures the workspace starts from the most recent shared state.
2. **Claim a ticket** -- calls `ticket claim-next` to atomically grab the next available open ticket.
3. **Branch** -- creates a `ticket-<ID>` branch off main.
4. **Invoke Claude Code** -- passes the ticket title, description, comments, and parent context as a prompt. Claude does the actual coding work.
5. **Verification gate** -- scans for merge conflict markers and runs `./verify.sh` if present. On failure, invokes Claude to fix errors (up to `VERIFY_RETRIES` times). If still failing, unclaims the ticket.
6. **Commit and push** -- after verification passes, the script stages all changes, commits with a `ticket-<ID>: <title>` message, and pushes the branch.
7. **Merge to main** -- fast-forward merges the branch into main and pushes. On success the ticket is marked complete; on failure it is unclaimed so another agent (or a human) can retry.
8. **Repeat**.

### When No Tickets Are Available

- If open/in-progress tickets exist but none are claimable (e.g. all assigned), the agent sleeps 10 seconds and retries.
- If the queue is completely empty, the agent asks Claude to review the codebase and propose an improvement. The proposal is created as a ticket assigned to `human` for review. The agent then sleeps 5 minutes before checking again.

### Merge Conflict Resolution

If `git push` is rejected (another agent merged first), the script:

1. Fetches the latest main and attempts a rebase.
2. If the rebase fails due to conflicts, it aborts the rebase and invokes Claude Code specifically for conflict resolution.
3. Retries up to 3 times before giving up.

### Failure Handling

- Claude is instructed to unclaim the ticket if it gets stuck after 2 attempts.
- If the final merge to main fails, the script unclaims the ticket and adds a comment explaining the failure.
- If Claude produces no code changes, the ticket is marked complete with a comment noting that.

## Dependencies (installed in the image)

- `bash`
- `git`
- `jq` -- used to parse JSON output from the ticket CLI
- `curl` -- used for ntfy.sh notifications
- `python3` -- runtime for the ticket CLI
- `ticket` -- the ticket CLI (`ticket/ticket.py` symlinked onto PATH)
- `claude` -- Claude Code CLI (installed globally via npm)

## Design Principles

- **Git is handled by bash, not Claude.** The script owns branching, committing, pushing, rebasing, and merging. Claude only writes code.
- **Verification before commit.** After Claude finishes, `run_verification()` checks for conflict markers and runs `./verify.sh` (project-specific linting/tests). Failures trigger Claude retries before the ticket is unclaimed.
- **Claude is invoked for merge conflicts** because resolving conflicts requires semantic understanding of both sides of the change.
- **Rebase-and-retry** handles the case where multiple agents push concurrently.
- **Unclaim on failure** returns tickets to the pool so they are not permanently stuck.
- **Parent context injection** gives Claude the broader context when working on sub-tickets.
- **OAuth credentials from host** — the host's `~/.claude` directory is bind-mounted read-only, so agents share the user's existing Claude Code authentication without needing a separate API key.
