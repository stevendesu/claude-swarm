# ticket CLI

SQLite-backed task management for autonomous agent swarms. This is the foundational component that all agents and humans use to coordinate work.

## Quick start

```bash
# Create a ticket
python ticket.py create "Implement login page" --description "Build the login form" --created-by human

# List open tickets
python ticket.py list

# An agent claims the next available ticket
python ticket.py claim-next --agent agent-1

# Add a comment
python ticket.py comment 1 "Started working on this"

# Mark done
python ticket.py complete 1
```

## Database

The CLI uses a local SQLite database (`./tickets.db` by default). Override with `--db PATH` on any command, or set the `TICKET_DB` environment variable.

The database is created automatically on first use. WAL mode is enabled for safe concurrent access by multiple agents.

### Schema

- **tickets** — core task records (id, title, description, status, assigned_to, parent_id, created_by, timestamps)
- **blockers** — dependency graph (ticket_id is blocked by blocked_by)
- **comments** — threaded discussion on tickets
- **activity_log** — audit trail of all write operations

## Commands

### Create and update

```bash
ticket create "Title" [--description TEXT] [--parent ID] [--assign WHO] [--blocks ID] [--created-by WHO]
ticket update ID [--title TEXT] [--description TEXT] [--assign WHO] [--status STATUS]
```

- `create` prints the new ticket ID to stdout.
- `--blocks ID` means the new ticket blocks the given ticket (adds a blocker row).
- `--created-by` defaults to `"human"`.

### Query

```bash
ticket list                             # all non-done tickets
ticket list --status open               # filter by status
ticket list --status open,in_progress   # comma-separated statuses
ticket list --assigned-to agent-1       # filter by assignee
ticket list --format json               # JSON output

ticket show 7                           # full detail with comments and blockers
ticket show 7 --format json

ticket count                            # count of non-done tickets
ticket count --status open              # count of open tickets
ticket count --status open,in_progress  # count across multiple statuses
```

### Work on tickets

```bash
ticket claim-next --agent agent-1       # atomically claim next available ticket
ticket claim-next --agent agent-1 --format json

ticket comment 7 "Found the issue" --author agent-1
ticket comments 7                       # list comments on a ticket
ticket comments 7 --format json

ticket complete 7                       # mark ticket done
ticket unclaim 7                        # release without completing
```

#### claim-next logic

A ticket is claimable when:
1. Status is `open`
2. `assigned_to` is NULL
3. No open blockers (all blocking tickets are `done`)
4. Oldest first (ORDER BY id ASC)

The claim is atomic (single transaction with `BEGIN IMMEDIATE`) to prevent race conditions between agents. Returns exit code 1 with no output if nothing is available.

### Blocking

```bash
ticket block 7 --by 12                 # ticket 7 is blocked by ticket 12
ticket unblock 7 --by 12               # remove the dependency
```

### Activity log

```bash
ticket log                              # last 20 events
ticket log --limit 50                   # last 50 events
```

All write operations are automatically logged to the activity feed.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 1    | Not found / nothing to claim |
| 2    | Usage error |

## Dependencies

None. Pure Python 3 standard library only (`sqlite3`, `argparse`, `json`, `datetime`, `sys`, `os`).
