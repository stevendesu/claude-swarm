#!/usr/bin/env python3
"""ticket — SQLite-backed task management CLI for autonomous agent swarms.

Usage:
    ticket create "Title" [--description TEXT] [--parent ID] [--assign WHO]
                          [--blocked-by ID] [--block-dependents-of ID]
                          [--created-by WHO] [--type TYPE] [--db PATH]
    ticket update ID [--title TEXT] [--description TEXT] [--assign WHO]
                     [--status STATUS] [--type TYPE] [--db PATH]
    ticket list [--status STATUS] [--assigned-to WHO] [--format FMT] [--db PATH]
    ticket show ID [--format FMT] [--db PATH]
    ticket count [--status STATUS] [--db PATH]
    ticket claim-next --agent AGENT [--format FMT] [--db PATH]
    ticket comment ID "BODY" [--author WHO] [--db PATH]
    ticket comments ID [--format FMT] [--db PATH]
    ticket complete ID [--db PATH]
    ticket unclaim ID [--db PATH]
    ticket block ID --by ID [--db PATH]
    ticket unblock ID --by ID [--db PATH]
    ticket log [--limit N] [--db PATH]
    ticket migrate [--db PATH]
"""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import textwrap
from datetime import datetime, timezone

def _find_swarm_db() -> str:
    """Walk up from cwd looking for .swarm/tickets/tickets.db."""
    d = os.path.abspath(os.getcwd())
    while True:
        candidate = os.path.join(d, ".swarm", "tickets", "tickets.db")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return "./tickets.db"


DEFAULT_DB = os.environ.get("TICKET_DB") or _find_swarm_db()

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def get_expected_version() -> int:
    """Derive expected schema version from highest-numbered migration file."""
    if not MIGRATIONS_DIR.exists():
        return 0
    versions = []
    for f in MIGRATIONS_DIR.glob("*.sql"):
        try:
            versions.append(int(f.name.split("_")[0]))
        except ValueError:
            pass
    return max(versions) if versions else 0


def get_current_version(conn: sqlite3.Connection) -> int | None:
    """Read schema version from database. Returns None if no schema_version table."""
    try:
        row = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        # Table doesn't exist
        return None


def check_version(conn: sqlite3.Connection) -> None:
    """Verify database schema version matches expected. Exits with error if mismatch."""
    current = get_current_version(conn)
    expected = get_expected_version()

    if current is None:
        print("Error: Database not initialized. Run 'ticket migrate' first.", file=sys.stderr)
        sys.exit(1)
    if current < expected:
        print(f"Error: Database schema outdated (v{current}, need v{expected}). "
              "Run 'ticket migrate'.", file=sys.stderr)
        sys.exit(1)
    if current > expected:
        print(f"Error: Database schema newer than code (v{current} > v{expected}). "
              "Update your swarm toolkit.", file=sys.stderr)
        sys.exit(1)


def run_migrations(db_path: str) -> None:
    """Run all pending migrations on the database."""
    # Create parent directories if needed
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)

    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    current = get_current_version(conn)
    if current is None:
        current = 0  # For migration purposes, treat missing as 0

    if not MIGRATIONS_DIR.exists():
        print(f"Error: Migrations directory not found: {MIGRATIONS_DIR}", file=sys.stderr)
        sys.exit(1)

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        print("No migrations found.", file=sys.stderr)
        sys.exit(1)

    applied_any = False
    for migration_file in migration_files:
        try:
            version = int(migration_file.name.split("_")[0])
        except ValueError:
            continue

        if version > current:
            print(f"Applying migration {migration_file.name}...")
            sql = migration_file.read_text()
            conn.executescript(sql)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version, applied_at) "
                "VALUES (?, datetime('now'))",
                (version,),
            )
            conn.commit()
            applied_any = True

    expected = get_expected_version()
    if applied_any:
        print(f"Database migrated to version {expected}.")
    else:
        print(f"Database already at version {expected}.")

    conn.close()


def connect(db_path: str) -> sqlite3.Connection:
    """Open the database and verify schema version."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    check_version(conn)
    return conn


def log_activity(conn, ticket_id, agent_id, action, detail=None):
    """Insert a row into activity_log."""
    conn.execute(
        "INSERT INTO activity_log (ticket_id, agent_id, action, detail) "
        "VALUES (?, ?, ?, ?)",
        (ticket_id, agent_id, action, detail),
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def format_ticket_row(row):
    """Return a dict from a sqlite3.Row for a ticket."""
    return {k: row[k] for k in row.keys()}


def print_ticket_table(rows):
    """Print a list of ticket rows as a formatted text table."""
    if not rows:
        print("No tickets found.")
        return
    header = f"{'ID':>5}  {'Status':<14}  {'Type':<10}  {'Assigned':<14}  Title"
    print(header)
    print("-" * len(header))
    for r in rows:
        assigned = r["assigned_to"] or ""
        ttype = r["type"] or "task"
        print(f"{r['id']:>5}  {r['status']:<14}  {ttype:<10}  {assigned:<14}  {r['title']}")


def print_ticket_detail(conn, row):
    """Print full detail for a single ticket in text format."""
    print(f"Ticket #{row['id']}")
    print(f"  Title:       {row['title']}")
    print(f"  Type:        {row['type'] or 'task'}")
    print(f"  Status:      {row['status']}")
    print(f"  Assigned:    {row['assigned_to'] or '(none)'}")
    print(f"  Created by:  {row['created_by']}")
    print(f"  Parent:      {row['parent_id'] or '(none)'}")
    print(f"  Created:     {row['created_at']}")
    print(f"  Updated:     {row['updated_at']}")
    if row["description"]:
        print(f"  Description:")
        for line in row["description"].splitlines():
            print(f"    {line}")

    # Blockers
    blockers = conn.execute(
        "SELECT blocked_by FROM blockers WHERE ticket_id = ?", (row["id"],)
    ).fetchall()
    if blockers:
        ids = ", ".join(str(b["blocked_by"]) for b in blockers)
        print(f"  Blocked by:  {ids}")

    # Blocks
    blocks = conn.execute(
        "SELECT ticket_id FROM blockers WHERE blocked_by = ?", (row["id"],)
    ).fetchall()
    if blocks:
        ids = ", ".join(str(b["ticket_id"]) for b in blocks)
        print(f"  Blocks:      {ids}")

    # Comments
    comments = conn.execute(
        "SELECT * FROM comments WHERE ticket_id = ? ORDER BY created_at",
        (row["id"],),
    ).fetchall()
    if comments:
        print(f"\n  Comments ({len(comments)}):")
        for c in comments:
            print(f"    [{c['created_at']}] {c['author']}: {c['body']}")


def ticket_detail_json(conn, row):
    """Return a dict with full ticket detail including comments and blockers."""
    d = format_ticket_row(row)
    blockers = conn.execute(
        "SELECT blocked_by FROM blockers WHERE ticket_id = ?", (row["id"],)
    ).fetchall()
    d["blocked_by"] = [b["blocked_by"] for b in blockers]

    blocks = conn.execute(
        "SELECT ticket_id FROM blockers WHERE blocked_by = ?", (row["id"],)
    ).fetchall()
    d["blocks"] = [b["ticket_id"] for b in blocks]

    comments = conn.execute(
        "SELECT * FROM comments WHERE ticket_id = ? ORDER BY created_at",
        (row["id"],),
    ).fetchall()
    d["comments"] = [dict(c) for c in comments]
    return d


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_create(args):
    if not args.title or not args.title.strip():
        print("Error: ticket title cannot be empty.", file=sys.stderr)
        sys.exit(1)
    conn = connect(args.db)

    # Determine ticket type (default based on context)
    ticket_type = args.type
    if ticket_type is None:
        # Smart defaults: if assigned to human with a blocker, it's likely a question
        # If assigned to human standalone, it's likely a proposal
        # Otherwise it's a task
        if args.assign == "human":
            ticket_type = "question" if args.blocked_by else "proposal"
        else:
            ticket_type = "task"

    cur = conn.execute(
        "INSERT INTO tickets (title, description, parent_id, assigned_to, created_by, type) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (args.title, args.description, args.parent, args.assign, args.created_by, ticket_type),
    )
    new_id = cur.lastrowid

    if args.blocked_by is not None:
        conn.execute(
            "INSERT INTO blockers (ticket_id, blocked_by) VALUES (?, ?)",
            (new_id, args.blocked_by),
        )
        log_activity(conn, new_id, args.created_by, "blocker_added",
                      f"Blocked by ticket #{args.blocked_by}")

    if args.block_dependents_of is not None:
        source_id = args.block_dependents_of
        # Verify source ticket exists
        if not conn.execute("SELECT id FROM tickets WHERE id = ?", (source_id,)).fetchone():
            print(f"Error: source ticket {source_id} not found for --block-dependents-of.",
                  file=sys.stderr)
            conn.rollback()
            sys.exit(1)
        # Find all tickets that are blocked by the source ticket
        dependents = conn.execute(
            "SELECT ticket_id FROM blockers WHERE blocked_by = ?", (source_id,)
        ).fetchall()
        for dep in dependents:
            dep_id = dep["ticket_id"]
            try:
                conn.execute(
                    "INSERT INTO blockers (ticket_id, blocked_by) VALUES (?, ?)",
                    (dep_id, new_id),
                )
                log_activity(conn, dep_id, args.created_by, "blocker_added",
                             f"Blocked by #{new_id} (via --block-dependents-of #{source_id})")
            except sqlite3.IntegrityError:
                pass  # Already blocked by this ticket (shouldn't happen for new tickets)

    log_activity(conn, new_id, args.created_by, "created", args.title)
    conn.commit()
    print(new_id)


def cmd_update(args):
    conn = connect(args.db)
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"Ticket {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    if args.status == "done":
        print("Error: Cannot set status to 'done' directly. Use 'mark-done' instead.",
              file=sys.stderr)
        sys.exit(1)

    fields = []
    params = []
    changes = []
    if args.title is not None:
        fields.append("title = ?")
        params.append(args.title)
        changes.append(f"title -> {args.title}")
    if args.description is not None:
        fields.append("description = ?")
        params.append(args.description)
        changes.append("description updated")
    if args.assign is not None:
        fields.append("assigned_to = ?")
        params.append(args.assign)
        changes.append(f"assigned_to -> {args.assign}")
    if args.status is not None:
        fields.append("status = ?")
        params.append(args.status)
        changes.append(f"status -> {args.status}")
    if args.type is not None:
        fields.append("type = ?")
        params.append(args.type)
        changes.append(f"type -> {args.type}")

    if not fields:
        print("Nothing to update.", file=sys.stderr)
        sys.exit(2)

    fields.append("updated_at = datetime('now')")
    params.append(args.id)
    conn.execute(f"UPDATE tickets SET {', '.join(fields)} WHERE id = ?", params)
    log_activity(conn, args.id, None, "updated", "; ".join(changes))
    conn.commit()
    print(f"Ticket {args.id} updated.")


def cmd_list(args):
    conn = connect(args.db)
    query = "SELECT * FROM tickets"
    conditions = []
    params = []

    if args.status:
        statuses = [s.strip() for s in args.status.split(",")]
        placeholders = ",".join("?" for _ in statuses)
        conditions.append(f"status IN ({placeholders})")
        params.extend(statuses)
    else:
        conditions.append("status != 'done'")

    if args.assigned_to:
        conditions.append("assigned_to = ?")
        params.append(args.assigned_to)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id"

    rows = conn.execute(query, params).fetchall()

    if args.format == "json":
        print(json.dumps([format_ticket_row(r) for r in rows], indent=2))
    else:
        print_ticket_table(rows)


def cmd_show(args):
    conn = connect(args.db)
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"Ticket {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        print(json.dumps(ticket_detail_json(conn, row), indent=2))
    else:
        print_ticket_detail(conn, row)


def cmd_count(args):
    conn = connect(args.db)
    if args.status:
        statuses = [s.strip() for s in args.status.split(",")]
        placeholders = ",".join("?" for _ in statuses)
        row = conn.execute(
            f"SELECT COUNT(*) as cnt FROM tickets WHERE status IN ({placeholders})",
            statuses,
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tickets WHERE status != 'done'"
        ).fetchone()
    print(row["cnt"])


def cmd_claim_next(args):
    conn = connect(args.db)
    # Atomic: find and claim the next available ticket in one transaction.
    # A ticket is available when:
    #   1. status = 'open'
    #   2. assigned_to IS NULL
    #   3. no open blockers (no row in blockers where blocked_by references
    #      a ticket whose status != 'done')
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        """
        SELECT * FROM tickets
        WHERE status = 'open'
          AND assigned_to IS NULL
          AND id NOT IN (
              SELECT b.ticket_id
              FROM blockers b
              JOIN tickets t ON t.id = b.blocked_by
              WHERE t.status != 'done'
          )
        ORDER BY id ASC
        LIMIT 1
        """,
    ).fetchone()

    if not row:
        conn.rollback()
        sys.exit(1)

    conn.execute(
        "UPDATE tickets SET assigned_to = ?, status = 'in_progress', "
        "updated_at = datetime('now') WHERE id = ?",
        (args.agent, row["id"]),
    )
    log_activity(conn, row["id"], args.agent, "claimed", f"Claimed by {args.agent}")
    conn.commit()

    # Re-fetch updated row
    updated = conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (row["id"],)
    ).fetchone()

    if args.format == "json":
        print(json.dumps(ticket_detail_json(conn, updated), indent=2))
    else:
        print_ticket_detail(conn, updated)


def cmd_comment(args):
    conn = connect(args.db)
    row = conn.execute("SELECT id FROM tickets WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"Ticket {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    conn.execute(
        "INSERT INTO comments (ticket_id, author, body) VALUES (?, ?, ?)",
        (args.id, args.author, args.body),
    )
    log_activity(conn, args.id, args.author, "commented", args.body[:200])
    conn.commit()
    print(f"Comment added to ticket {args.id}.")


def cmd_comments(args):
    conn = connect(args.db)
    row = conn.execute("SELECT id FROM tickets WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"Ticket {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    comments = conn.execute(
        "SELECT * FROM comments WHERE ticket_id = ? ORDER BY created_at",
        (args.id,),
    ).fetchall()

    if args.format == "json":
        print(json.dumps([dict(c) for c in comments], indent=2))
    else:
        if not comments:
            print("No comments.")
            return
        for c in comments:
            print(f"[{c['created_at']}] {c['author']}: {c['body']}")


def cmd_complete(args):
    conn = connect(args.db)
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"Ticket {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    conn.execute(
        "UPDATE tickets SET status = 'ready', updated_at = datetime('now') WHERE id = ?",
        (args.id,),
    )
    log_activity(conn, args.id, row["assigned_to"], "completed",
                 f"Ticket #{args.id} marked work complete")
    conn.commit()
    print(f"Ticket {args.id} work complete.")


def cmd_mark_done(args):
    conn = connect(args.db)
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"Ticket {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    conn.execute(
        "UPDATE tickets SET status = 'done', updated_at = datetime('now') WHERE id = ?",
        (args.id,),
    )
    log_activity(conn, args.id, row["assigned_to"], "done",
                 f"Ticket #{args.id} marked done")
    conn.commit()
    print(f"Ticket {args.id} done.")


def cmd_unclaim(args):
    conn = connect(args.db)
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"Ticket {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    prev_agent = row["assigned_to"]
    conn.execute(
        "UPDATE tickets SET assigned_to = NULL, status = 'open', "
        "updated_at = datetime('now') WHERE id = ?",
        (args.id,),
    )
    log_activity(conn, args.id, prev_agent, "unclaimed",
                 f"Released by {prev_agent}")
    conn.commit()
    print(f"Ticket {args.id} unclaimed.")


def cmd_block(args):
    conn = connect(args.db)
    # Verify both tickets exist
    for tid in (args.id, args.by):
        if not conn.execute("SELECT id FROM tickets WHERE id = ?", (tid,)).fetchone():
            print(f"Ticket {tid} not found.", file=sys.stderr)
            sys.exit(1)

    try:
        conn.execute(
            "INSERT INTO blockers (ticket_id, blocked_by) VALUES (?, ?)",
            (args.id, args.by),
        )
    except sqlite3.IntegrityError:
        print(f"Ticket {args.id} is already blocked by {args.by}.", file=sys.stderr)
        sys.exit(1)

    log_activity(conn, args.id, None, "blocker_added",
                 f"Blocked by #{args.by}")

    # Auto-unclaim: if the blocked ticket is currently assigned, release it
    row = conn.execute("SELECT assigned_to, status FROM tickets WHERE id = ?",
                       (args.id,)).fetchone()
    if row["assigned_to"] is not None:
        prev_agent = row["assigned_to"]
        conn.execute(
            "UPDATE tickets SET assigned_to = NULL, status = 'open', "
            "updated_at = datetime('now') WHERE id = ?",
            (args.id,),
        )
        log_activity(conn, args.id, prev_agent, "unclaimed",
                     f"Auto-released (blocked by #{args.by})")

    conn.commit()
    print(f"Ticket {args.id} is now blocked by ticket {args.by}.")


def cmd_unblock(args):
    conn = connect(args.db)
    cur = conn.execute(
        "DELETE FROM blockers WHERE ticket_id = ? AND blocked_by = ?",
        (args.id, args.by),
    )
    if cur.rowcount == 0:
        print(f"No such blocker relationship found.", file=sys.stderr)
        sys.exit(1)

    log_activity(conn, args.id, None, "blocker_removed",
                 f"Unblocked from #{args.by}")
    conn.commit()
    print(f"Ticket {args.id} is no longer blocked by ticket {args.by}.")


def cmd_log(args):
    conn = connect(args.db)
    rows = conn.execute(
        "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()

    if not rows:
        print("No activity.")
        return

    for r in rows:
        ticket_str = f"#{r['ticket_id']}" if r["ticket_id"] else "   "
        agent_str = r["agent_id"] or ""
        print(f"[{r['created_at']}] {ticket_str:<6} {r['action']:<18} "
              f"{agent_str:<14} {r['detail'] or ''}")


def cmd_migrate(args):
    """Run database migrations."""
    run_migrations(args.db)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="ticket",
        description="SQLite-backed task management for autonomous agent swarms.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to SQLite database")

    sub = parser.add_subparsers(dest="command")

    # create
    p = sub.add_parser("create", help="Create a new ticket")
    p.add_argument("title", help="Ticket title")
    p.add_argument("--description", default=None, help="Ticket description")
    p.add_argument("--parent", type=int, default=None, help="Parent ticket ID")
    p.add_argument("--assign", default=None, help="Assign to agent/human")
    p.add_argument("--blocked-by", type=int, default=None, dest="blocked_by",
                   help="ID of ticket that must complete before this one")
    p.add_argument("--created-by", default="human", dest="created_by",
                   help="Creator identifier (default: human)")
    p.add_argument("--type", default=None, choices=["task", "proposal", "question", "verify"],
                   help="Ticket type (default: auto-detected)")
    p.add_argument("--block-dependents-of", type=int, default=None, dest="block_dependents_of",
                   help="Copy blocking relationships: all tickets blocked by this ID also become blocked by the new ticket")

    # update
    p = sub.add_parser("update", help="Update a ticket")
    p.add_argument("id", type=int, help="Ticket ID")
    p.add_argument("--title", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--assign", default=None)
    p.add_argument("--status", default=None)
    p.add_argument("--type", default=None, choices=["task", "proposal", "question", "verify"])

    # list
    p = sub.add_parser("list", help="List tickets")
    p.add_argument("--status", default=None,
                   help="Filter by status (comma-separated)")
    p.add_argument("--assigned-to", default=None, dest="assigned_to")
    p.add_argument("--format", default="text", choices=["text", "json"])

    # show
    p = sub.add_parser("show", help="Show ticket detail")
    p.add_argument("id", type=int, help="Ticket ID")
    p.add_argument("--format", default="text", choices=["text", "json"])

    # count
    p = sub.add_parser("count", help="Count tickets")
    p.add_argument("--status", default=None,
                   help="Filter by status (comma-separated)")

    # claim-next
    p = sub.add_parser("claim-next", help="Claim the next available ticket")
    p.add_argument("--agent", required=True, help="Agent identifier")
    p.add_argument("--format", default="text", choices=["text", "json"])

    # comment
    p = sub.add_parser("comment", help="Add a comment to a ticket")
    p.add_argument("id", type=int, help="Ticket ID")
    p.add_argument("body", help="Comment body")
    p.add_argument("--author", default="human", help="Comment author")

    # comments
    p = sub.add_parser("comments", help="List comments on a ticket")
    p.add_argument("id", type=int, help="Ticket ID")
    p.add_argument("--format", default="text", choices=["text", "json"])

    # complete
    p = sub.add_parser("complete", help="Signal work is finished (sets ready)")
    p.add_argument("id", type=int, help="Ticket ID")

    # mark-done (hidden — only used by agent-loop after git push)
    p = sub.add_parser("mark-done", help=argparse.SUPPRESS)
    p.add_argument("id", type=int, help="Ticket ID")

    # unclaim
    p = sub.add_parser("unclaim", help="Release a claimed ticket")
    p.add_argument("id", type=int, help="Ticket ID")

    # block
    p = sub.add_parser("block", help="Add a blocker relationship")
    p.add_argument("id", type=int, help="Ticket that is blocked")
    p.add_argument("--by", type=int, required=True, help="Ticket that blocks it")

    # unblock
    p = sub.add_parser("unblock", help="Remove a blocker relationship")
    p.add_argument("id", type=int, help="Ticket that was blocked")
    p.add_argument("--by", type=int, required=True, help="Ticket that was blocking")

    # log
    p = sub.add_parser("log", help="Show activity log")
    p.add_argument("--limit", type=int, default=20, help="Max entries to show")

    # migrate
    sub.add_parser("migrate", help="Run database migrations")

    return parser


DISPATCH = {
    "create": cmd_create,
    "update": cmd_update,
    "list": cmd_list,
    "show": cmd_show,
    "count": cmd_count,
    "claim-next": cmd_claim_next,
    "comment": cmd_comment,
    "comments": cmd_comments,
    "complete": cmd_complete,
    "mark-done": cmd_mark_done,
    "unclaim": cmd_unclaim,
    "block": cmd_block,
    "unblock": cmd_unblock,
    "log": cmd_log,
    "migrate": cmd_migrate,
}


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(2)

    handler = DISPATCH.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(2)

    handler(args)


if __name__ == "__main__":
    main()
