# Autonomous Agent Swarm

A system where autonomous AI agents collaborate through a shared ticketing system to build and maintain software. Agents run in isolated Docker containers, each with their own repository clone, and coordinate through a SQLite-backed ticket queue. Humans interact through a CLI and a real-time web dashboard.

Point it at a project, tell it what you want built, and walk away. Come back to review proposals, answer questions, and merge results.

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│                    Your Project                         │
│                                                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐               │
│  │ Agent 1  │  │ Agent 2  │  │ Agent 3  │  ...          │
│  │ (Docker) │  │ (Docker) │  │ (Docker) │               │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘               │
│       │             │             │                     │
│       └──────────┬──┴─────────────┘                     │
│                  │                                      │
│         ┌────────┴────────┐                             │
│         │  Shared State   │                             │
│         │  ┌───────────┐  │                             │
│         │  │ tickets.db│  │  SQLite ticket queue        │
│         │  │ repo.git  │  │  Bare git repo              │
│         │  └───────────┘  │                             │
│         └────────┬────────┘                             │
│                  │                                      │
│         ┌────────┴────────┐                             │
│         │    Dashboard    │  :3000                      │
│         └─────────────────┘                             │
└─────────────────────────────────────────────────────────┘
```

Each agent runs an infinite loop:

1. **Claim** the next available ticket from the queue
2. **Branch**, then invoke Claude Code to do the work
3. **Verify** — check for merge conflicts, run `verify.sh` (your project's linter/tests)
4. **Commit, merge, and push** to the shared repo
5. **Mark done** and loop back to step 1

When the queue is empty, agents switch to read-only mode, analyze the codebase, and propose improvements for human review.

## Features

- **Autonomous agent coordination** — Multiple Claude Code instances working in parallel on separate tickets, with automatic conflict resolution via rebase-and-retry
- **SQLite ticket system** — Lightweight task management with blocking/dependencies, atomic claim-next, and parent-child relationships
- **Ticket types** — Tasks, proposals (agent suggestions for human review), and questions (agent needs human input)
- **Web dashboard** — Real-time view of ticket queue, agent status, activity feed, container logs, and Claude Code session logs
- **Verification gate** — Every change is verified (conflict markers + project-specific `verify.sh`) before commit, with automatic retry on failure
- **Crash recovery** — Three layers of protection: unclaim-on-start, SIGTERM traps, and cron-based stall detection
- **Push notifications** — Optional [ntfy.sh](https://ntfy.sh) integration alerts you when agents need human input
- **Zero dependencies** — All Python components use stdlib only. No pip, no node modules (beyond Claude Code itself)

## Prerequisites

- **macOS** (see [Limitations](#limitations))
- **Docker** with Docker Compose
- **Python 3.6+**
- **Claude Code CLI** — `npm install -g @anthropic-ai/claude-code`
- **An active Claude subscription** (Max, Teams, or Enterprise) signed in via `claude`

## Installation

Clone this repo, then symlink the two CLIs to somewhere on your `$PATH`:

```bash
git clone https://github.com/stevendesu/claude-swarm.git
cd claude-swarm

# Symlink the swarm CLI
ln -s "$(pwd)/swarm/swarm.py" /usr/local/bin/swarm

# Symlink the ticket CLI
ln -s "$(pwd)/ticket/ticket.py" /usr/local/bin/ticket
```

Verify:

```bash
swarm --help
ticket --help
```

## Quick Start

### 1. Initialize a project

```bash
cd /path/to/your-project
swarm init .
```

This runs in two phases:

- **Phase 1** (automatic): Scaffolds `.swarm/` directory, copies agent infrastructure, initializes the ticket database, creates a bare git repo from your current code
- **Phase 2** (interactive): Claude Code interviews you about your project — what it does, who uses it, constraints, tech stack preferences. This produces `PROJECT.md` (business context for agents) and `verify.sh` (your project-specific test/lint script)

### 2. Start the swarm

```bash
swarm start
```

This extracts your OAuth token from the macOS Keychain, builds the Docker images, and spins up your agents. By default you get 3 agents and a web dashboard on port 3000.

### 3. Monitor progress

```bash
# Web dashboard
open http://localhost:3000

# CLI
swarm status
ticket list
ticket list --status in_progress

# Container logs
swarm logs agent-1

# Pull agent changes into your working tree
swarm pull

# Or auto-pull on every new commit
swarm watch
```

### 4. Interact with agents

Agents may create tickets assigned to `human` when they need input:

```bash
# See what needs your attention
ticket list --assigned-to human

# Answer a question
ticket comment 5 "Use PostgreSQL, not SQLite, for production"
ticket mark-done 5

# Approve a proposal
ticket mark-done 3

# Reject a proposal
ticket update 3 --status done
ticket comment 3 "Not needed — we already have this"
```

### 5. (Optional) Create some tickets

The interview process ("Initialize a project" > "Phase 2") creates the first seed ticket so agents can get to work. However, if you decide you want changes to your application or discover a bug, you can file tickets manually:

```bash
ticket create "Add user authentication" \
  --description "Implement JWT-based auth with login/signup endpoints"

ticket create "Write API tests" \
  --description "Add pytest tests for all REST endpoints" \
  --blocked-by 1
```

Or use the web dashboard at `http://localhost:3000` once the swarm is running.

### 6. Stop the swarm

```bash
swarm stop
```

## CLI Reference

### `swarm`

| Command | Description |
|---------|-------------|
| `swarm init <dir>` | Initialize a project for swarm development |
| `swarm start` | Start all agents and the dashboard |
| `swarm stop` | Stop all containers |
| `swarm status` | Show container status and ticket counts |
| `swarm logs <service>` | Tail logs for a container |
| `swarm scale <n>` | Change the number of agents and restart |
| `swarm regenerate` | Re-copy source files and regenerate docker-compose.yml |
| `swarm pull` | Pull latest agent commits into your working tree |
| `swarm watch` | Auto-pull new commits (polls every 5s) |

### `ticket`

| Command | Description |
|---------|-------------|
| `ticket create "Title"` | Create a ticket (`--description`, `--assign`, `--blocked-by`, `--type`) |
| `ticket list` | List tickets (`--status`, `--assigned-to`, `--format json`) |
| `ticket show <id>` | Show ticket details |
| `ticket update <id>` | Update ticket fields |
| `ticket comment <id> "text"` | Add a comment |
| `ticket comments <id>` | View comments on a ticket |
| `ticket complete <id>` | Mark a ticket as ready |
| `ticket mark-done <id>` | Finalize a ticket as done |
| `ticket block <id> --by <id>` | Add a blocking dependency |
| `ticket unblock <id> --by <id>` | Remove a blocking dependency |
| `ticket count` | Count tickets by status |
| `ticket log` | View activity log |
| `ticket migrate` | Run pending database migrations |

## Configuration

After `swarm init`, settings live in `.swarm/config.json`:

```json
{
  "agents": 3,
  "ntfy_topic": "",
  "allowed_tools": "Bash,Read,Write,Edit,Glob,Grep",
  "max_turns": 50,
  "monitor_port": 3000,
  "verify_retries": 2
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `agents` | `3` | Number of agent containers |
| `ntfy_topic` | `""` | [ntfy.sh](https://ntfy.sh) topic for push notifications |
| `allowed_tools` | `Bash,Read,Write,Edit,Glob,Grep` | Tools agents are allowed to use |
| `max_turns` | `50` | Maximum Claude Code turns per ticket |
| `monitor_port` | `3000` | Web dashboard port |
| `verify_retries` | `2` | Verification retry attempts before unclaiming |

## Project Structure

```
claude-swarm/
  agent/           # Agent container: Dockerfile, loop, entrypoint, stall detection
  ticket/          # SQLite-backed ticket CLI and migrations
  monitor/         # Web dashboard: Python HTTP server + vanilla SPA
  swarm/           # Swarm CLI: init, start, stop, scale, regenerate
  templates/       # Templates copied into target projects on init
```

When you run `swarm init`, these are copied into your project's `.swarm/` directory along with a generated `docker-compose.yml`, `CLAUDE.md` (agent instructions), and `PROJECT.md` (your project context).

## Limitations

- **macOS only** — OAuth token extraction uses the macOS Keychain (`security find-generic-password`). Linux/Windows support would require an alternative credential store.
- **Short-lived tokens** — Claude OAuth access tokens expire after a few hours. Restart the swarm (`swarm stop && swarm start`) to refresh. Automatic token refresh inside containers is not yet implemented.
- **`ANTHROPIC_API_KEY` is not supported** — This tool uses Claude Code's OAuth flow (for Max/Teams/Enterprise subscriptions), which is a completely separate auth system from the Anthropic API.
- **No remote/distributed operation** — All agents run on a single Docker host sharing a local SQLite database and bare git repo.
- **SQLite concurrency** — WAL mode handles concurrent reads well, but write contention increases with many agents. The atomic `claim-next` transaction mitigates this for ticket assignment.

## How to Contribute

Contributions are welcome! Here's how to get started:

1. **Fork and clone** the repository
2. **Explore the codebase** — each directory has its own `README.md` explaining its contents
3. **Make changes** — all Python must use stdlib only (no pip dependencies)
4. **Run tests** — `python -m pytest ticket/` for the ticket system
5. **Open a PR** with a clear description of what changed and why

Some areas that could use help:

- **Linux support** — Alternative credential extraction for non-macOS systems
- **Windows support** - Similar
- **Token refresh** — Automatic OAuth token renewal inside containers
- **Better conflict resolution** — Smarter merge strategies when agents collide
- **Distributed operation** — Support for multi-host setups with a shared database
- **More notification backends** — Slack, Discord, email, etc.

Please keep PRs focused — one feature or fix per PR. If you're planning something large, open an issue first to discuss the approach.

## License

[MIT](LICENSE)
