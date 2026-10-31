"""SQLite persistence for orchestrator state."""
from __future__ import annotations
import sqlite3
import os
from pathlib import Path
from framework.orchestrator.schemas import Ticket

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _get_state_db() -> Path:
    """Get the state DB path (dynamic, reads KAG_WORKSPACE at call time)."""
    workspace = os.environ.get("KAG_WORKSPACE", "/workspace")
    return Path(workspace) / "state" / "orchestrator.db"


def init_db():
    """Initialize the state DB and run pending migrations."""
    state_db = _get_state_db()
    state_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY)"
    )
    applied = conn.execute(
        "SELECT version FROM schema_version"
    ).fetchall()
    applied_versions = {row[0] for row in applied}

    # Run pending migrations in order
    if MIGRATIONS_DIR.exists():
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(migration.stem.split("_")[0])
            if version not in applied_versions:
                conn.executescript(migration.read_text())
                conn.execute(
                    "INSERT INTO schema_version VALUES (?)", (version,)
                )
                conn.commit()

    conn.close()


def save_ticket(ticket: Ticket):
    """Insert or update a ticket in the state DB."""
    state_db = _get_state_db()
    conn = sqlite3.connect(str(state_db))
    conn.execute(
        """
        INSERT OR REPLACE INTO tickets
        (task_id, title, subsystem, status, iteration, payload, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            ticket.task_id,
            ticket.title,
            ticket.subsystem,
            ticket.status,
            ticket.iteration,
            ticket.model_dump_json(),
        ),
    )
    conn.commit()
    conn.close()


def load_ticket(task_id: str) -> Ticket | None:
    """Load a ticket by ID, or None if not found."""
    state_db = _get_state_db()
    conn = sqlite3.connect(str(state_db))
    row = conn.execute(
        "SELECT payload FROM tickets WHERE task_id = ?", (task_id,)
    ).fetchone()
    conn.close()
    if row:
        return Ticket.model_validate_json(row[0])
    return None


def list_tickets(limit: int = 20) -> list[Ticket]:
    """List recent tickets."""
    state_db = _get_state_db()
    conn = sqlite3.connect(str(state_db))
    rows = conn.execute(
        "SELECT payload FROM tickets ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [Ticket.model_validate_json(r[0]) for r in rows]
