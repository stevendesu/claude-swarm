-- Migration 002: Add ticket type column
-- Supports 'task' (default), 'proposal', and 'question' types
-- for differentiated human interaction flows

ALTER TABLE tickets ADD COLUMN type TEXT DEFAULT 'task' CHECK(type IN ('task', 'proposal', 'question'));
