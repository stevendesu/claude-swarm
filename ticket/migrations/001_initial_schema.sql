-- Migration 001: Initial schema
-- Creates the core tables for the ticket system

-- Schema version tracking (must be first)
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now'))
);

-- Core tables
CREATE TABLE tickets (
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

CREATE TABLE blockers (
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    blocked_by  INTEGER NOT NULL REFERENCES tickets(id),
    PRIMARY KEY (ticket_id, blocked_by)
);

CREATE TABLE comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    author      TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER,
    agent_id    TEXT,
    action      TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Indexes
CREATE INDEX idx_tickets_status ON tickets(status);
CREATE INDEX idx_tickets_assigned ON tickets(assigned_to);
CREATE INDEX idx_tickets_parent ON tickets(parent_id);
CREATE INDEX idx_comments_ticket ON comments(ticket_id);
CREATE INDEX idx_activity_log_ticket ON activity_log(ticket_id);
