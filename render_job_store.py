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
                error TEXT,
                score_id INTEGER,
                submission_id INTEGER,
                mods INTEGER DEFAULT 0,
                mods_label TEXT DEFAULT 'NM',
                description TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_render_jobs_status_created
                ON render_jobs(status, created_at, id);
            CREATE INDEX IF NOT EXISTS idx_render_jobs_lease_expiry
                ON render_jobs(status, lease_expires_at);
            """
        )
        # Handle existing databases without the metadata columns
        cursor = self.connection.execute("PRAGMA table_info(render_jobs)")
        existing_cols = {row["name"] for row in cursor.fetchall()}
        new_cols = [
            ("score_id", "INTEGER"),
            ("submission_id", "INTEGER"),
            ("mods", "INTEGER DEFAULT 0"),
            ("mods_label", "TEXT DEFAULT 'NM'"),
            ("description", "TEXT"),
        ]
        for col_name, col_type in new_cols:
            if col_name not in existing_cols:
                self.connection.execute(f"ALTER TABLE render_jobs ADD COLUMN {col_name} {col_type}")
        self.connection.commit()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    async def create_job(
        self,
        source_replay: str,
        score_id: int | None = None,
        submission_id: int | None = None,
        mods: int = 0,
        mods_label: str = "NM",
        description: str = "",
    ) -> str:
        job_id = str(uuid.uuid4())
        now = time.time()
        async with self.lock:
            self.connection.execute(
                """
                INSERT INTO render_jobs (
                    id, status, source_replay, score_id, submission_id,
                    mods, mods_label, description, created_at, updated_at
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    source_replay,
                    score_id,
                    submission_id,
                    mods,
                    mods_label,
                    description,
                    now,
                    now,
                ),
            )
            self.connection.commit()
        return job_id

    async def count_active_jobs(self) -> int:
        async with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM render_jobs WHERE status IN ('queued', 'leased', 'rendering')"
            ).fetchone()
            return int(row[0]) if row else 0

    async def get_completed_jobs(self) -> list[dict[str, Any]]:
        async with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM render_jobs WHERE status = 'completed' ORDER BY updated_at ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    async def archive_job(self, job_id: str) -> bool:
        now = time.time()
        async with self.lock:
            cursor = self.connection.execute(
                "UPDATE render_jobs SET status = 'archived', updated_at = ? WHERE id = ? AND status = 'completed'",
                (now, job_id),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    async def claim_job(self, worker_id: str, lease_seconds: float = 60.0) -> dict[str, Any] | None:
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

    async def heartbeat(self, job_id: str, lease_token: str, lease_seconds: float = 60.0) -> bool:
        final_placeholders = ",".join("?" for _ in FINAL_STATUSES)
        async with self.lock:
            try:
                now = time.time()
                cursor = self.connection.execute(
                    f"UPDATE render_jobs SET lease_expires_at = ?, updated_at = ? "
                    f"WHERE id = ? AND lease_token = ? AND lease_expires_at >= ? "
                    f"AND status NOT IN ({final_placeholders})",
                    (now + lease_seconds, now, job_id, lease_token, now, *FINAL_STATUSES),
                )
                self.connection.commit()
                return cursor.rowcount == 1
            except Exception:
                self.connection.rollback()
                raise

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        async with self.lock:
            row = self.connection.execute(
                "SELECT * FROM render_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            return self._dict(row)

    async def has_active_lease(self, job_id: str, lease_token: str) -> bool:
        final_placeholders = ",".join("?" for _ in FINAL_STATUSES)
        async with self.lock:
            now = time.time()
            row = self.connection.execute(
                f"SELECT 1 FROM render_jobs "
                f"WHERE id = ? AND lease_token = ? AND lease_expires_at >= ? "
                f"AND status NOT IN ({final_placeholders})",
                (job_id, lease_token, now, *FINAL_STATUSES),
            ).fetchone()
            return row is not None

    async def complete_job(
        self,
        job_id: str,
        lease_token: str,
    ) -> bool:
        final_placeholders = ",".join("?" for _ in FINAL_STATUSES)
        async with self.lock:
            try:
                now = time.time()
                cursor = self.connection.execute(
                    f"UPDATE render_jobs SET status = 'completed', "
                    f"lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                    f"WHERE id = ? AND lease_token = ? AND lease_expires_at >= ? "
                    f"AND artifact_path IS NOT NULL "
                    f"AND status NOT IN ({final_placeholders})",
                    (now, job_id, lease_token, now, *FINAL_STATUSES),
                )
                self.connection.commit()
                return cursor.rowcount == 1
            except Exception:
                self.connection.rollback()
                raise

    async def fail_job(
        self,
        job_id: str,
        lease_token: str,
        error: str | None = None,
    ) -> bool:
        final_placeholders = ",".join("?" for _ in FINAL_STATUSES)
        async with self.lock:
            try:
                now = time.time()
                cursor = self.connection.execute(
                    f"UPDATE render_jobs SET status = 'failed', error = ?, "
                    f"lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                    f"WHERE id = ? AND lease_token = ? AND lease_expires_at >= ? "
                    f"AND status NOT IN ({final_placeholders})",
                    (error or "Render job failed", now, job_id, lease_token, now, *FINAL_STATUSES),
                )
                self.connection.commit()
                return cursor.rowcount == 1
            except Exception:
                self.connection.rollback()
                raise

    async def update_artifact_path(
        self,
        job_id: str,
        lease_token: str,
        artifact_path: str,
    ) -> bool:
        final_placeholders = ",".join("?" for _ in FINAL_STATUSES)
        async with self.lock:
            try:
                now = time.time()
                cursor = self.connection.execute(
                    f"UPDATE render_jobs SET artifact_path = ?, updated_at = ? "
                    f"WHERE id = ? AND lease_token = ? AND lease_expires_at >= ? "
                    f"AND status NOT IN ({final_placeholders})",
                    (artifact_path, now, job_id, lease_token, now, *FINAL_STATUSES),
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
