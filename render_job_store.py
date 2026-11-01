import asyncio
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


FINAL_STATUSES = ("completed", "failed", "cancelled")


class RenderJobStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.database_path = self.root / "render_jobs.db"
        self.connection = sqlite3.connect(self.database_path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.lock = asyncio.Lock()
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS render_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                worker_id TEXT,
                lease_token TEXT,
                lease_expires_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                source_replay TEXT NOT NULL,
                artifact_path TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_render_jobs_status_created
                ON render_jobs(status, created_at, id);
            CREATE INDEX IF NOT EXISTS idx_render_jobs_lease_expiry
                ON render_jobs(status, lease_expires_at);
            """
        )
        self.connection.commit()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    async def claim_job(self, worker_id: str, lease_seconds: float) -> dict[str, Any] | None:
        async with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    "SELECT id FROM render_jobs WHERE status = 'queued' "
                    "ORDER BY created_at, id LIMIT 1"
                ).fetchone()
                if row is None:
                    self.connection.commit()
                    return None

                now = time.time()
                lease_token = str(uuid.uuid4())
                self.connection.execute(
                    "UPDATE render_jobs SET status = 'leased', worker_id = ?, lease_token = ?, "
                    "lease_expires_at = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                    (worker_id, lease_token, now + lease_seconds, now, row["id"]),
                )
                claimed = self.connection.execute(
                    "SELECT * FROM render_jobs WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                self.connection.commit()
                return self._dict(claimed)
            except Exception:
                self.connection.rollback()
                raise

    async def heartbeat(self, job_id: str, lease_token: str, lease_seconds: float) -> bool:
        now = time.time()
        final_placeholders = ",".join("?" for _ in FINAL_STATUSES)
        async with self.lock:
            try:
                cursor = self.connection.execute(
                    f"UPDATE render_jobs SET lease_expires_at = ?, updated_at = ? "
                    f"WHERE id = ? AND lease_token = ? AND status NOT IN ({final_placeholders})",
                    (now + lease_seconds, now, job_id, lease_token, *FINAL_STATUSES),
                )
                self.connection.commit()
                return cursor.rowcount == 1
            except Exception:
                self.connection.rollback()
                raise

    async def reconcile_expired_jobs(self) -> int:
        now = time.time()
        async with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    "UPDATE render_jobs SET status = 'queued', worker_id = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, attempts = attempts + 1, updated_at = ? "
                    "WHERE status IN ('leased', 'rendering') AND lease_expires_at < ?",
                    (now, now),
                )
                self.connection.commit()
                return cursor.rowcount
            except Exception:
                self.connection.rollback()
                raise

    async def close(self) -> None:
        async with self.lock:
            self.connection.close()
