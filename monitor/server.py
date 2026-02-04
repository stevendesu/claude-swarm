#!/usr/bin/env python3
"""monitor — Web dashboard and REST API for the autonomous agent swarm.

Serves a single-page app on port 3000 and provides JSON REST endpoints for
ticket management, activity feeds, and Docker agent status.

Environment variables:
    TICKET_DB  — path to SQLite database (default: /tickets/tickets.db)
    PORT       — HTTP listen port (default: 3000)
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("TICKET_DB", "/tickets/tickets.db")
PORT = int(os.environ.get("PORT", "3000"))
STATIC_DIR = Path(__file__).parent / "static"
DOCKER_SOCKET = "/var/run/docker.sock"

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

SCHEMA = """\
CREATE TABLE IF NOT EXISTS tickets (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  description TEXT,
  status      TEXT NOT NULL DEFAULT 'open',
  assigned_to TEXT,
  parent_id   INTEGER REFERENCES tickets(id),
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blockers (
  ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
  blocked_by  INTEGER NOT NULL REFERENCES tickets(id),
  PRIMARY KEY (ticket_id, blocked_by)
);

CREATE TABLE IF NOT EXISTS comments (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
  author      TEXT NOT NULL,
  body        TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS activity_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id   INTEGER,
  agent_id    TEXT,
  action      TEXT NOT NULL,
  detail      TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_assigned ON tickets(assigned_to);
CREATE INDEX IF NOT EXISTS idx_tickets_parent ON tickets(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_ticket ON comments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_ticket ON activity_log(ticket_id);
"""


def get_db() -> sqlite3.Connection:
    """Open and initialise the database."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return {k: row[k] for k in row.keys()}


def log_activity(conn, ticket_id, agent_id, action, detail=None):
    """Insert a row into activity_log."""
    conn.execute(
        "INSERT INTO activity_log (ticket_id, agent_id, action, detail) "
        "VALUES (?, ?, ?, ?)",
        (ticket_id, agent_id, action, detail),
    )


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def docker_api(path: str) -> dict | list | None:
    """Call the Docker Engine API via curl over the Unix socket.

    Returns parsed JSON on success, None on failure.
    """
    try:
        result = subprocess.run(
            [
                "curl", "-s", "--max-time", "5",
                "--unix-socket", DOCKER_SOCKET,
                f"http://localhost{path}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception:
        return None


def docker_logs(container_id: str, tail: int = 100) -> str:
    """Fetch container logs via the Docker API."""
    try:
        result = subprocess.run(
            [
                "curl", "-s", "--max-time", "5",
                "--unix-socket", DOCKER_SOCKET,
                f"http://localhost/containers/{container_id}/logs"
                f"?stdout=true&stderr=true&tail={tail}",
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return ""
        # Docker log stream has 8-byte header frames; strip them for display
        raw = result.stdout
        lines = []
        i = 0
        while i < len(raw):
            if i + 8 <= len(raw):
                # frame: [stream_type(1)][0(3)][size(4 big-endian)][payload]
                size = int.from_bytes(raw[i + 4 : i + 8], "big")
                payload = raw[i + 8 : i + 8 + size]
                lines.append(payload.decode("utf-8", errors="replace").rstrip("\n"))
                i += 8 + size
            else:
                # Fallback: treat remainder as plain text
                lines.append(raw[i:].decode("utf-8", errors="replace"))
                break
        return "\n".join(lines)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# API handlers
# ---------------------------------------------------------------------------

def api_list_tickets(query: dict) -> tuple[int, dict]:
    """GET /api/tickets — list tickets with optional filters."""
    conn = get_db()
    try:
        sql = "SELECT * FROM tickets"
        conditions = []
        params = []

        status_filter = query.get("status", [None])[0]
        assigned_to = query.get("assigned_to", [None])[0]

        if status_filter:
            statuses = [s.strip() for s in status_filter.split(",")]
            placeholders = ",".join("?" for _ in statuses)
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)

        if assigned_to:
            conditions.append("assigned_to = ?")
            params.append(assigned_to)

        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id"

        rows = conn.execute(sql, params).fetchall()
        tickets = []
        for r in rows:
            t = row_to_dict(r)
            # Attach comment count
            cnt = conn.execute(
                "SELECT COUNT(*) as cnt FROM comments WHERE ticket_id = ?",
                (r["id"],),
            ).fetchone()
            t["comment_count"] = cnt["cnt"]
            # Attach blocker info
            blockers = conn.execute(
                "SELECT b.blocked_by, t.status as blocker_status "
                "FROM blockers b JOIN tickets t ON t.id = b.blocked_by "
                "WHERE b.ticket_id = ?",
                (r["id"],),
            ).fetchall()
            t["blocked_by"] = [row_to_dict(b) for b in blockers]
            t["is_blocked"] = any(b["blocker_status"] != "done" for b in blockers)
            tickets.append(t)
        return 200, {"tickets": tickets}
    finally:
        conn.close()


def api_get_ticket(ticket_id: int) -> tuple[int, dict]:
    """GET /api/tickets/:id — full ticket detail."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return 404, {"error": f"Ticket {ticket_id} not found"}

        t = row_to_dict(row)

        # Comments
        comments = conn.execute(
            "SELECT * FROM comments WHERE ticket_id = ? ORDER BY created_at",
            (ticket_id,),
        ).fetchall()
        t["comments"] = [row_to_dict(c) for c in comments]

        # Blocked by
        blockers = conn.execute(
            "SELECT b.blocked_by, t.title, t.status "
            "FROM blockers b JOIN tickets t ON t.id = b.blocked_by "
            "WHERE b.ticket_id = ?",
            (ticket_id,),
        ).fetchall()
        t["blocked_by"] = [row_to_dict(b) for b in blockers]
        t["is_blocked"] = any(b["status"] != "done" for b in blockers)

        # Blocks (tickets this one blocks)
        blocks = conn.execute(
            "SELECT b.ticket_id, t.title, t.status "
            "FROM blockers b JOIN tickets t ON t.id = b.ticket_id "
            "WHERE b.blocked_by = ?",
            (ticket_id,),
        ).fetchall()
        t["blocks"] = [row_to_dict(b) for b in blocks]

        # Children
        children = conn.execute(
            "SELECT id, title, status FROM tickets WHERE parent_id = ? ORDER BY id",
            (ticket_id,),
        ).fetchall()
        t["children"] = [row_to_dict(c) for c in children]

        return 200, t
    finally:
        conn.close()


def api_create_ticket(body: dict) -> tuple[int, dict]:
    """POST /api/tickets — create a new ticket."""
    title = body.get("title")
    if not title:
        return 400, {"error": "title is required"}

    description = body.get("description")
    parent_id = body.get("parent_id")
    assigned_to = body.get("assigned_to")
    created_by = body.get("created_by", "human")

    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO tickets (title, description, parent_id, assigned_to, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, description, parent_id, assigned_to, created_by),
        )
        new_id = cur.lastrowid
        log_activity(conn, new_id, created_by, "created", title)
        conn.commit()
        return 201, {"id": new_id}
    finally:
        conn.close()


def api_add_comment(ticket_id: int, body: dict) -> tuple[int, dict]:
    """POST /api/tickets/:id/comment — add a comment."""
    comment_body = body.get("body")
    if not comment_body:
        return 400, {"error": "body is required"}
    author = body.get("author", "human")

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return 404, {"error": f"Ticket {ticket_id} not found"}

        conn.execute(
            "INSERT INTO comments (ticket_id, author, body) VALUES (?, ?, ?)",
            (ticket_id, author, comment_body),
        )
        log_activity(conn, ticket_id, author, "commented", comment_body[:200])
        conn.commit()
        return 201, {"ok": True}
    finally:
        conn.close()


def api_complete_ticket(ticket_id: int) -> tuple[int, dict]:
    """POST /api/tickets/:id/complete — mark ticket done."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return 404, {"error": f"Ticket {ticket_id} not found"}

        conn.execute(
            "UPDATE tickets SET status = 'done', updated_at = datetime('now') "
            "WHERE id = ?",
            (ticket_id,),
        )
        log_activity(
            conn, ticket_id, row["assigned_to"], "completed",
            f"Ticket #{ticket_id} marked done",
        )
        conn.commit()
        return 200, {"ok": True}
    finally:
        conn.close()


def api_update_ticket(ticket_id: int, body: dict) -> tuple[int, dict]:
    """POST /api/tickets/:id/update — update ticket fields."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if not row:
            return 404, {"error": f"Ticket {ticket_id} not found"}

        fields = []
        params = []
        changes = []

        for field in ("title", "description", "status", "assigned_to"):
            if field in body and body[field] is not None:
                fields.append(f"{field} = ?")
                params.append(body[field])
                changes.append(f"{field} -> {body[field]}")

        if not fields:
            return 400, {"error": "No fields to update"}

        fields.append("updated_at = datetime('now')")
        params.append(ticket_id)
        conn.execute(
            f"UPDATE tickets SET {', '.join(fields)} WHERE id = ?", params
        )
        log_activity(conn, ticket_id, "human", "updated", "; ".join(changes))
        conn.commit()
        return 200, {"ok": True}
    finally:
        conn.close()


def api_activity(query: dict) -> tuple[int, dict]:
    """GET /api/activity — activity feed."""
    limit = int(query.get("limit", [50])[0])
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT a.*, t.title as ticket_title "
            "FROM activity_log a "
            "LEFT JOIN tickets t ON t.id = a.ticket_id "
            "ORDER BY a.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return 200, {"activity": [row_to_dict(r) for r in rows]}
    finally:
        conn.close()


def api_agents() -> tuple[int, dict]:
    """GET /api/agents — agent container status from Docker."""
    containers = docker_api("/containers/json?all=true")
    if containers is None:
        return 200, {"agents": [], "error": "Docker not available"}

    agents = []
    # Also query the DB for current ticket assignments
    conn = get_db()
    try:
        assignments = {}
        rows = conn.execute(
            "SELECT assigned_to, id, title FROM tickets WHERE status = 'in_progress' "
            "AND assigned_to IS NOT NULL"
        ).fetchall()
        for r in rows:
            assignments[r["assigned_to"]] = {
                "ticket_id": r["id"],
                "ticket_title": r["title"],
            }
    finally:
        conn.close()

    for c in containers:
        name = c.get("Names", [""])[0].lstrip("/")
        # Filter to only agent-like containers (heuristic: name contains 'agent')
        # Also include all containers in the same compose project
        labels = c.get("Labels", {})
        state = c.get("State", "unknown")

        agent_info = {
            "id": c.get("Id", "")[:12],
            "name": name,
            "state": state,
            "status": c.get("Status", ""),
            "image": c.get("Image", ""),
            "created": c.get("Created", 0),
            "labels": labels,
            "current_ticket": assignments.get(name),
        }

        # Get stats for running containers
        if state == "running":
            stats = docker_api(f"/containers/{c['Id']}/stats?stream=false")
            if stats:
                # Calculate CPU percentage
                cpu_delta = (
                    stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
                    - stats.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
                )
                system_delta = (
                    stats.get("cpu_stats", {}).get("system_cpu_usage", 0)
                    - stats.get("precpu_stats", {}).get("system_cpu_usage", 0)
                )
                num_cpus = stats.get("cpu_stats", {}).get("online_cpus", 1) or 1
                cpu_pct = 0.0
                if system_delta > 0:
                    cpu_pct = (cpu_delta / system_delta) * num_cpus * 100.0

                # Memory
                mem_usage = stats.get("memory_stats", {}).get("usage", 0)
                mem_limit = stats.get("memory_stats", {}).get("limit", 1)
                mem_pct = (mem_usage / mem_limit) * 100.0 if mem_limit else 0

                agent_info["cpu_percent"] = round(cpu_pct, 2)
                agent_info["memory_usage"] = mem_usage
                agent_info["memory_limit"] = mem_limit
                agent_info["memory_percent"] = round(mem_pct, 2)

        agents.append(agent_info)

    return 200, {"agents": agents}


def api_agent_logs(container_name: str) -> tuple[int, dict]:
    """GET /api/agents/:name/logs — container logs."""
    # Find container by name
    containers = docker_api("/containers/json?all=true")
    if containers is None:
        return 503, {"error": "Docker not available"}

    container_id = None
    for c in containers:
        name = c.get("Names", [""])[0].lstrip("/")
        if name == container_name or c.get("Id", "").startswith(container_name):
            container_id = c["Id"]
            break

    if not container_id:
        return 404, {"error": f"Container '{container_name}' not found"}

    logs = docker_logs(container_id)
    return 200, {"logs": logs, "container": container_name}


def api_stats() -> tuple[int, dict]:
    """GET /api/stats — summary statistics."""
    conn = get_db()
    try:
        stats = {}

        # Count by status
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tickets GROUP BY status"
        ).fetchall()
        for r in rows:
            stats[r["status"]] = r["cnt"]

        # Needs human
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tickets "
            "WHERE assigned_to = 'human' AND status != 'done'"
        ).fetchone()
        stats["needs_human"] = row["cnt"]

        # Blocked count (tickets with at least one non-done blocker)
        row = conn.execute(
            "SELECT COUNT(DISTINCT b.ticket_id) as cnt "
            "FROM blockers b "
            "JOIN tickets bt ON bt.id = b.blocked_by "
            "JOIN tickets t ON t.id = b.ticket_id "
            "WHERE bt.status != 'done' AND t.status != 'done'"
        ).fetchone()
        stats["blocked"] = row["cnt"]

        # Total
        row = conn.execute("SELECT COUNT(*) as cnt FROM tickets").fetchone()
        stats["total"] = row["cnt"]

        return 200, stats
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class MonitorHandler(BaseHTTPRequestHandler):
    """HTTP handler for the monitor web app."""

    # Suppress default logging for each request
    def log_message(self, fmt, *args):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{ts}] {fmt % args}\n")

    # ---- helpers ----

    def _send_json(self, status: int, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _serve_static(self, file_path: str):
        """Serve a file from the static directory."""
        # Prevent path traversal
        safe = Path(STATIC_DIR / file_path).resolve()
        if not str(safe).startswith(str(STATIC_DIR.resolve())):
            self.send_error(403, "Forbidden")
            return

        if not safe.is_file():
            self.send_error(404, "Not Found")
            return

        ext = safe.suffix.lower()
        content_type = CONTENT_TYPES.get(ext, "application/octet-stream")

        data = safe.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---- routing ----

    def _route(self, method: str):
        """Route a request to the appropriate handler."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

        # API routes
        if path.startswith("/api/"):
            return self._route_api(method, path, query)

        # Static files
        if method == "GET":
            file_path = path.lstrip("/")
            if file_path == "" or file_path == "index.html":
                file_path = "index.html"
            self._serve_static(file_path)
            return

        self.send_error(404, "Not Found")

    def _route_api(self, method: str, path: str, query: dict):
        """Route API requests."""
        try:
            # GET /api/tickets
            if method == "GET" and path == "/api/tickets":
                status, data = api_list_tickets(query)
                self._send_json(status, data)
                return

            # GET /api/tickets/:id
            m = re.match(r"^/api/tickets/(\d+)$", path)
            if m and method == "GET":
                status, data = api_get_ticket(int(m.group(1)))
                self._send_json(status, data)
                return

            # POST /api/tickets
            if method == "POST" and path == "/api/tickets":
                body = self._read_body()
                status, data = api_create_ticket(body)
                self._send_json(status, data)
                return

            # POST /api/tickets/:id/comment
            m = re.match(r"^/api/tickets/(\d+)/comment$", path)
            if m and method == "POST":
                body = self._read_body()
                status, data = api_add_comment(int(m.group(1)), body)
                self._send_json(status, data)
                return

            # POST /api/tickets/:id/complete
            m = re.match(r"^/api/tickets/(\d+)/complete$", path)
            if m and method == "POST":
                status, data = api_complete_ticket(int(m.group(1)))
                self._send_json(status, data)
                return

            # POST /api/tickets/:id/update
            m = re.match(r"^/api/tickets/(\d+)/update$", path)
            if m and method == "POST":
                body = self._read_body()
                status, data = api_update_ticket(int(m.group(1)), body)
                self._send_json(status, data)
                return

            # GET /api/activity
            if method == "GET" and path == "/api/activity":
                status, data = api_activity(query)
                self._send_json(status, data)
                return

            # GET /api/agents
            if method == "GET" and path == "/api/agents":
                status, data = api_agents()
                self._send_json(status, data)
                return

            # GET /api/agents/:name/logs
            m = re.match(r"^/api/agents/([^/]+)/logs$", path)
            if m and method == "GET":
                status, data = api_agent_logs(m.group(1))
                self._send_json(status, data)
                return

            # GET /api/stats
            if method == "GET" and path == "/api/stats":
                status, data = api_stats()
                self._send_json(status, data)
                return

            self._send_json(404, {"error": "Not found"})

        except sqlite3.Error as e:
            self._send_json(500, {"error": f"Database error: {e}"})
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Invalid JSON"})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    # ---- HTTP methods ----

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Monitor starting on http://0.0.0.0:{PORT}")
    print(f"Database: {DB_PATH}")
    print(f"Static dir: {STATIC_DIR}")

    # Ensure DB exists and schema is applied
    try:
        conn = get_db()
        conn.close()
        print("Database connected and schema verified.")
    except Exception as e:
        print(f"WARNING: Could not connect to database: {e}")
        print("The server will start anyway; DB errors will appear at request time.")

    server = ThreadingHTTPServer(("0.0.0.0", PORT), MonitorHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
