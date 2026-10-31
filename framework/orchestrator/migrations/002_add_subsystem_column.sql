-- Migration 002: Add subsystem column and index
ALTER TABLE tickets ADD COLUMN subsystem TEXT DEFAULT 'mm';
CREATE INDEX IF NOT EXISTS idx_tickets_subsystem ON tickets(subsystem);
