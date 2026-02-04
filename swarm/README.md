# swarm/ — Swarm CLI

Bootstrap and lifecycle management for autonomous agent swarms. This CLI sets up the infrastructure for any project so that AI agents can collaborate through a shared ticketing system, each running in isolated Docker containers.

## File

- **swarm.py** — The CLI script (pure Python, stdlib only)

## Commands

```bash
# Initialize a new project for agent swarms
swarm init /path/to/project

# Lifecycle management (run from the project directory)
swarm start              # Build and start all agent containers + monitor
swarm stop               # Shut down everything
swarm status             # Show container status and ticket queue summary
swarm logs agent-1       # Tail logs for a specific agent
swarm scale 5            # Change the number of agents and restart
```

## What `swarm init` Does

### Phase 1 — Scaffold infrastructure (automatic)

Creates a `.swarm/` directory inside the target project:

```
project/
├── .swarm/
│   ├── config.json          # settings: agent count, ntfy topic, etc.
│   ├── tickets/
│   │   └── tickets.db       # SQLite database (initialized with schema)
│   ├── repo.git/            # bare git repo cloned from project
│   ├── agent/               # copied from our agent/ directory
│   │   ├── agent-loop.sh
│   │   ├── entrypoint.sh
│   │   └── Dockerfile
│   ├── ticket/              # copied from our ticket/ directory
│   │   └── ticket.py
│   ├── monitor/             # copied from our monitor/ directory (if it exists)
│   └── secrets/             # directory for API keys
│       └── .gitkeep
├── docker-compose.yml       # generated based on config
├── CLAUDE.md                # generated agent operating manual
├── PROJECT.md               # placeholder for business context
└── .gitignore               # updated to ignore .swarm/
```

### Phase 2 — Project clarification (manual)

After scaffolding, the user edits `PROJECT.md` to fill in business context. A seed ticket is automatically created that tells agents to read PROJECT.md and decompose the project into work tickets.

## Configuration

Stored in `.swarm/config.json` (JSON, not YAML, to avoid external dependencies):

```json
{
  "agents": 3,
  "ntfy_topic": "",
  "allowed_tools": "Bash,Read,Write,Edit,Glob,Grep",
  "max_turns": 50,
  "monitor_port": 3000
}
```

## docker-compose.yml Generation

The generated `docker-compose.yml` is placed at the project root with all paths relative to the project root:

- **Build context**: `.swarm/` (agent/, ticket/ are inside .swarm/)
- **Dockerfile**: `.swarm/agent/Dockerfile`
- **Volumes**:
  - `.swarm/tickets:/tickets` — shared SQLite database directory
  - `.swarm/repo.git:/repo.git` — shared bare git repository
  - `.swarm/secrets:/secrets:ro` — API keys (read-only)

Each agent gets a unique `AGENT_ID` environment variable. The monitor service gets read-only access to tickets and the Docker socket.

## Dependencies

None beyond Python 3 standard library. Uses `json` instead of YAML to avoid requiring PyYAML.

## Usage from Any Directory

The `start`, `stop`, `status`, `logs`, and `scale` commands automatically find the project root by walking up from the current working directory looking for `.swarm/`. You can run them from any subdirectory of the project.
