# Monitor — Swarm Dashboard

Browser-based dashboard for the autonomous agent swarm system. Provides a real-time view of tickets, agent status, and activity.

## What It Does

- Reads and writes the shared SQLite database (tickets, comments, activity log)
- Queries the Docker Engine API for agent container status, stats, and logs
- Reads Claude Code session logs from `/agent-logs/` (bind-mounted from `.swarm/agent-logs/`)
- Serves a single-page app with three tabs: Tickets, Agents, Activity
- Auto-refreshes every 5 seconds

## Running

### With Docker (production)

The monitor runs as a service in docker-compose:

```yaml
monitor:
  build: ./monitor
  ports:
    - "3000:3000"
  volumes:
    - tickets-db:/tickets:ro
    - /var/run/docker.sock:/var/run/docker.sock:ro
```

Then open `http://localhost:3000` in a browser.

### Standalone (development)

```bash
TICKET_DB=./tickets.db python3 monitor/server.py
```

The server listens on `0.0.0.0:3000` by default. Configure with environment variables:

| Variable    | Default                | Description                  |
|-------------|------------------------|------------------------------|
| `TICKET_DB`      | `/tickets/tickets.db`  | Path to SQLite database                |
| `PORT`           | `3000`                 | HTTP listen port                       |
| `AGENT_LOGS_DIR` | `/agent-logs`          | Directory containing per-agent session logs |

## REST API

All API endpoints return JSON. The server adds CORS headers for cross-origin access.

### Tickets

| Method | Endpoint                       | Description                                        |
|--------|--------------------------------|----------------------------------------------------|
| GET    | `/api/tickets`                 | List tickets. Query params: `status`, `assigned_to` |
| GET    | `/api/tickets/:id`             | Ticket detail with comments, blockers, children    |
| POST   | `/api/tickets`                 | Create ticket. Body: `{title, description, parent_id, assigned_to}` |
| POST   | `/api/tickets/:id/comment`     | Add comment. Body: `{body, author}`                |
| POST   | `/api/tickets/:id/complete`    | Mark ticket done                                   |
| POST   | `/api/tickets/:id/update`      | Update fields. Body: `{title, description, status, assigned_to}` |

### Activity

| Method | Endpoint        | Description                           |
|--------|-----------------|---------------------------------------|
| GET    | `/api/activity`  | Activity feed. Query param: `limit`  |

### Agents (Docker)

| Method | Endpoint                  | Description                          |
|--------|---------------------------|--------------------------------------|
| GET    | `/api/agents`                       | List containers with stats               |
| GET    | `/api/agents/:name/logs`            | Fetch Docker container logs              |
| GET    | `/api/agents/:name/sessions`        | List Claude Code session log files       |
| GET    | `/api/agents/:name/sessions/:file`  | Parsed session log content (stream-json) |

### Stats

| Method | Endpoint      | Description                                            |
|--------|---------------|--------------------------------------------------------|
| GET    | `/api/stats`  | Summary counts: open, in_progress, done, blocked, needs_human |

## Architecture

- **server.py** — Python HTTP server using only stdlib modules (`http.server`, `sqlite3`, `json`, `subprocess`). No external dependencies.
- **static/index.html** — Self-contained SPA. Vanilla HTML/CSS/JS with no frameworks or CDN links. Dark theme, responsive layout.
- **Docker integration** — Uses `curl --unix-socket` to talk to the Docker Engine API at `/var/run/docker.sock`.

## Files

```
monitor/
├── Dockerfile          # Container image definition
├── README.md           # This file
├── server.py           # Python HTTP server + REST API
└── static/
    └── index.html      # Single-page dashboard app
```
