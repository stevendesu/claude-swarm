-- Migration 003: Add 'verify' ticket type
-- Supports human verification tickets with Pass/Fail actions
-- SQLite doesn't support ALTER CHECK constraints, so we recreate the table

PRAGMA foreign_keys=OFF;

CREATE TABLE tickets_new (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL,
  description TEXT,
  status      TEXT NOT NULL DEFAULT 'open',
  type        TEXT DEFAULT 'task' CHECK(type IN ('task', 'proposal', 'question', 'verify')),
  assigned_to TEXT,
  parent_id   INTEGER REFERENCES tickets_new(id),
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO tickets_new (id, title, description, status, type, assigned_to, parent_id, created_by, created_at, updated_at)
    SELECT id, title, description, status, type, assigned_to, parent_id, created_by, created_at, updated_at
    FROM tickets;

DROP TABLE tickets;
ALTER TABLE tickets_new RENAME TO tickets;

CREATE INDEX idx_tickets_status ON tickets(status);
CREATE INDEX idx_tickets_assigned ON tickets(assigned_to);
CREATE INDEX idx_tickets_parent ON tickets(parent_id);

PRAGMA foreign_keys=ON;
