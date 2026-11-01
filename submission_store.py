import asyncio
import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = ("queued", "preparing", "rendering")


class SubmissionRejected(RuntimeError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class SubmissionStore:
    def __init__(self, root: Path, queue_limit: int, user_cooldown_seconds: int):
        self.root = root.resolve()
        self.queue_limit = queue_limit
        self.user_cooldown_seconds = user_cooldown_seconds
        self.base_dir = self.root / "submissions"
        self.originals_dir = self.base_dir / "originals"
        self.temp_dir = self.base_dir / "tmp"
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "submissions.db"
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.lock = asyncio.Lock()
        self._create_schema()
        self._recover_interrupted_work()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                external_user_name TEXT NOT NULL,
                guild_id TEXT,
                channel_id TEXT,
                status_message_id TEXT,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                size INTEGER NOT NULL,
                mode INTEGER NOT NULL,
                mods INTEGER NOT NULL,
                mods_label TEXT NOT NULL,
                skin TEXT NOT NULL,
                status TEXT NOT NULL,
                render_id INTEGER,
                video_name TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                completed_at INTEGER,
                notified_status TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_submissions_status_id
                ON submissions(status, id);
            CREATE INDEX IF NOT EXISTS idx_submissions_user_created
                ON submissions(platform, external_user_id, created_at DESC);
            """
        )
        self.connection.commit()

    def _recover_interrupted_work(self) -> None:
        now = int(time.time())
        self.connection.execute(
            "UPDATE submissions SET status = 'queued', updated_at = ?, error = NULL "
            "WHERE status = 'preparing' OR (status = 'rendering' AND render_id IS NULL)",
            (now,),
        )
        self.connection.commit()

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    async def create(
        self,
        *,
        source_path: Path,
        platform: str,
        external_user_id: str,
        external_user_name: str,
        guild_id: str | None,
        channel_id: str | None,
        original_name: str,
        mode: int,
        mods: int,
        mods_label: str,
        skin: str,
    ) -> tuple[dict[str, Any], int]:
        size = source_path.stat().st_size
        checksum = self.hash_file(source_path)
        now = int(time.time())
        async with self.lock:
            duplicate = self.connection.execute(
                "SELECT id, status FROM submissions WHERE sha256 = ?",
                (checksum,),
            ).fetchone()
            if duplicate:
                raise SubmissionRejected(
                    f"This replay was already submitted as #{duplicate['id']} ({duplicate['status']}).",
                    "duplicate",
                )
            placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
            active = self.connection.execute(
                f"SELECT id FROM submissions WHERE platform = ? AND external_user_id = ? "
                f"AND status IN ({placeholders}) LIMIT 1",
                (platform, external_user_id, *ACTIVE_STATUSES),
            ).fetchone()
            if active:
                raise SubmissionRejected(
                    f"You already have submission #{active['id']} waiting or rendering.",
                    "active-submission",
                )
            latest = self.connection.execute(
                "SELECT created_at FROM submissions WHERE platform = ? AND external_user_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (platform, external_user_id),
            ).fetchone()
            if latest:
                remaining = self.user_cooldown_seconds - (now - int(latest["created_at"]))
                if remaining > 0:
                    raise SubmissionRejected(
                        f"Please wait {remaining} seconds before submitting another replay.",
                        "user-cooldown",
                    )
            active_count = self.connection.execute(
                f"SELECT COUNT(*) FROM submissions WHERE status IN ({placeholders})",
                ACTIVE_STATUSES,
            ).fetchone()[0]
            if active_count >= self.queue_limit:
                raise SubmissionRejected("The submission queue is currently full.", "queue-full")
            safe_user = "".join(character for character in external_user_id if character.isdigit()) or "user"
            stored_name = f"{now}-{platform}-{safe_user}-{checksum[:12]}.osr"
            stored_path = (self.originals_dir / stored_name).resolve()
            temporary = stored_path.with_suffix(".osr.part")
            try:
                source_path.replace(temporary)
                temporary.replace(stored_path)
                cursor = self.connection.execute(
                    """
                    INSERT INTO submissions (
                        platform, external_user_id, external_user_name, guild_id, channel_id,
                        original_name, stored_path, sha256, size, mode, mods, mods_label,
                        skin, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        platform,
                        external_user_id,
                        external_user_name,
                        guild_id,
                        channel_id,
                        original_name,
                        str(stored_path),
                        checksum,
                        size,
                        mode,
                        mods,
                        mods_label,
                        skin,
                        now,
                        now,
                    ),
                )
                submission_id = int(cursor.lastrowid)
                self.connection.commit()
            except Exception:
                temporary.unlink(missing_ok=True)
                stored_path.unlink(missing_ok=True)
                self.connection.rollback()
                raise
            position = self.connection.execute(
                f"SELECT COUNT(*) FROM submissions WHERE id <= ? AND status IN ({placeholders})",
                (submission_id, *ACTIVE_STATUSES),
            ).fetchone()[0]
            row = self.connection.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            return dict(row), int(position)

    async def attach_status_message(
        self,
        submission_id: int,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        async with self.lock:
            self.connection.execute(
                "UPDATE submissions SET guild_id = ?, channel_id = ?, status_message_id = ?, "
                "notified_status = status, updated_at = ? WHERE id = ?",
                (str(guild_id), str(channel_id), str(message_id), int(time.time()), submission_id),
            )
            self.connection.commit()

    async def claim_next(self) -> dict[str, Any] | None:
        async with self.lock:
            row = self.connection.execute(
                "SELECT * FROM submissions WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = int(time.time())
            self.connection.execute(
                "UPDATE submissions SET status = 'preparing', error = NULL, updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            self.connection.commit()
            return dict(self.connection.execute("SELECT * FROM submissions WHERE id = ?", (row["id"],)).fetchone())

    async def mark_rendering(self, submission_id: int, render_id: int | None) -> None:
        await self._set_status(submission_id, "rendering", render_id=render_id, error=None)

    async def mark_ready(self, submission_id: int, video_name: str, skin: str | None = None) -> None:
        values: dict[str, Any] = {
            "video_name": video_name,
            "completed_at": int(time.time()),
            "error": None,
        }
        if skin:
            values["skin"] = skin
        await self._set_status(submission_id, "ready", **values)

    async def mark_failed(self, submission_id: int, error: str) -> None:
        await self._set_status(
            submission_id,
            "failed",
            error=error[:1000],
            completed_at=int(time.time()),
        )

    async def requeue(self, submission_id: int, error: str | None = None) -> None:
        await self._set_status(submission_id, "queued", error=error[:1000] if error else None)

    async def _set_status(self, submission_id: int, status: str, **values: Any) -> None:
        allowed = {"render_id", "video_name", "error", "completed_at", "skin"}
        assignments = ["status = ?", "updated_at = ?"]
        parameters: list[Any] = [status, int(time.time())]
        for key, value in values.items():
            if key not in allowed:
                continue
            assignments.append(f"{key} = ?")
            parameters.append(value)
        parameters.append(submission_id)
        async with self.lock:
            self.connection.execute(
                f"UPDATE submissions SET {', '.join(assignments)} WHERE id = ?",
                parameters,
            )
            self.connection.commit()

    async def get(self, submission_id: int) -> dict[str, Any] | None:
        async with self.lock:
            return self._dict(
                self.connection.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            )

    async def list_active(self, limit: int = 20) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        async with self.lock:
            rows = self.connection.execute(
                f"SELECT * FROM submissions WHERE status IN ({placeholders}) ORDER BY id LIMIT ?",
                (*ACTIVE_STATUSES, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    async def cancel(self, submission_id: int, external_user_id: str, is_admin: bool) -> dict[str, Any]:
        async with self.lock:
            row = self.connection.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if not row:
                raise SubmissionRejected("Submission not found.", "not-found")
            if not is_admin and row["external_user_id"] != external_user_id:
                raise SubmissionRejected("You can only cancel your own submission.", "not-owner")
            if row["status"] != "queued":
                raise SubmissionRejected(
                    f"Submission #{submission_id} can no longer be cancelled ({row['status']}).",
                    "not-cancellable",
                )
            self.connection.execute(
                "UPDATE submissions SET status = 'cancelled', updated_at = ?, completed_at = ? WHERE id = ?",
                (int(time.time()), int(time.time()), submission_id),
            )
            self.connection.commit()
            return dict(self.connection.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone())

    async def pending_notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM submissions WHERE status_message_id IS NOT NULL "
                "AND COALESCE(notified_status, '') != status ORDER BY updated_at LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    async def mark_notified(self, submission_id: int, status: str) -> None:
        async with self.lock:
            self.connection.execute(
                "UPDATE submissions SET notified_status = ? WHERE id = ? AND status = ?",
                (status, submission_id, status),
            )
            self.connection.commit()

    async def queue_position(self, submission_id: int) -> int | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        async with self.lock:
            row = self.connection.execute("SELECT status FROM submissions WHERE id = ?", (submission_id,)).fetchone()
            if not row or row["status"] not in ACTIVE_STATUSES:
                return None
            return int(
                self.connection.execute(
                    f"SELECT COUNT(*) FROM submissions WHERE id <= ? AND status IN ({placeholders})",
                    (submission_id, *ACTIVE_STATUSES),
                ).fetchone()[0]
            )

    async def close(self) -> None:
        async with self.lock:
            self.connection.close()
