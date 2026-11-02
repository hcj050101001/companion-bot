"""Explicit care choices and bounded delivery receipts; no conversation content."""

import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.config import ROOT

MAX_CONSENT = timedelta(days=30)
RECEIPT_RETENTION = timedelta(days=2)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return value.astimezone(timezone.utc)


class CarePreferences:
    """Separate, external SQLite store for choices and short-lived send receipts."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser().resolve()
        if self.path == ROOT.resolve() or self.path.is_relative_to(ROOT.resolve()):
            raise ValueError("care choices must be stored outside the repository")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS choices (
                    user_id TEXT PRIMARY KEY, expires REAL NOT NULL, paused_until REAL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    user_id TEXT NOT NULL, work_hash TEXT NOT NULL, local_day TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY (user_id, work_hash),
                    UNIQUE (user_id, local_day)
                );
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', '1');
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA secure_delete=ON")
        return db

    @staticmethod
    def _prune(db, now: datetime) -> None:
        stamp = _utc(now).timestamp()
        db.execute("DELETE FROM choices WHERE expires <= ?", (stamp,))
        db.execute(
            "DELETE FROM receipts WHERE created <= ?", (stamp - RECEIPT_RETENTION.total_seconds(),)
        )

    def opt_in(self, user_id: str, *, now: datetime, expires: datetime) -> None:
        now, expires = _utc(now), _utc(expires)
        if not now < expires <= now + MAX_CONSENT:
            raise ValueError("consent must expire within 30 days")
        with self._connect() as db:
            self._prune(db, now)
            db.execute(
                "INSERT OR REPLACE INTO choices(user_id, expires, paused_until) "
                "VALUES (?, ?, NULL)",
                (str(user_id), expires.timestamp()),
            )

    def pause(self, user_id: str, *, until: datetime, now: datetime) -> None:
        now, until = _utc(now), _utc(until)
        if until <= now:
            raise ValueError("pause must end in the future")
        with self._connect() as db:
            self._prune(db, now)
            db.execute(
                "UPDATE choices SET paused_until=? WHERE user_id=?",
                (until.timestamp(), str(user_id)),
            )

    def consent_state(self, user_id: str, *, now: datetime) -> str:
        """Current consent state: "none" | "active" | "paused" | "expired".

        Read-only; expiry is judged against ``now`` without pruning, so a
        stored-but-lapsed choice reports "expired" until a write prunes it.
        """
        stamp = _utc(now).timestamp()
        with self._connect() as db:
            row = db.execute(
                "SELECT expires, paused_until FROM choices WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
        if row is None:
            return "none"
        if row[0] <= stamp:
            return "expired"
        if row[1] is not None and row[1] > stamp:
            return "paused"
        return "active"

    def stop(self, user_id: str) -> None:
        """Withdraw consent immediately; re-enabling requires a new explicit opt-in."""
        with self._connect() as db:
            db.execute("DELETE FROM choices WHERE user_id=?", (str(user_id),))

    def forget(self, user_id: str) -> None:
        """Erase choices. Minimal receipts expire separately to prevent duplicate sends."""
        self.stop(user_id)

    def prune(self, now: datetime) -> None:
        """Delete expired records; callers can also run this during maintenance."""
        with self._connect() as db:
            self._prune(db, now)

    def reserve(self, user_id: str, work_id: str, local_day: str, *, now: datetime) -> bool:
        """Atomically recheck consent and reserve the day's single attempt and work ID."""
        stamp = _utc(now).timestamp()
        work_hash = hashlib.sha256(work_id.encode("utf-8")).hexdigest()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prune(db, now)
            row = db.execute(
                "SELECT expires, paused_until FROM choices WHERE user_id=?", (str(user_id),)
            ).fetchone()
            if not row or row[0] <= stamp or (row[1] is not None and row[1] > stamp):
                return False
            try:
                db.execute(
                    "INSERT INTO receipts VALUES (?, ?, ?, ?)",
                    (str(user_id), work_hash, local_day, stamp),
                )
            except sqlite3.IntegrityError:
                return False
            return True
