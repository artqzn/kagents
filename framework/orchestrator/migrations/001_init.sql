-- Migration 001: Initial schema
CREATE TABLE IF NOT EXISTS tickets (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    iteration INTEGER DEFAULT 0,
    payload JSON NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS test_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT,
    started_at TEXT,
    ok INTEGER,
    FOREIGN KEY (task_id) REFERENCES tickets(task_id)
);
