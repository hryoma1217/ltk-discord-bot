from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class Member:
    user_id: int
    display_name: str
    role_label: str | None
    note: str | None
    created_at: str


@dataclass
class Practice:
    id: int
    title: str
    description: str | None
    channel_id: int
    created_by: int
    created_at: str
    collect_deadline: str | None
    is_closed: int
    closed_reason: str | None


@dataclass
class PracticeOption:
    id: int
    practice_id: int
    option_no: int
    starts_at: str
    note: str | None
    is_confirmed: int


@dataclass
class Availability:
    option_id: int
    user_id: int
    status: str
    comment: str | None
    updated_at: str


@dataclass
class PracticeTarget:
    practice_id: int
    user_id: int
    display_name: str
    role_kind: str
    sort_order: int


class Storage:
    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS members (
                    user_id INTEGER PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    role_label TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS practices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    channel_id INTEGER NOT NULL,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    collect_deadline TEXT,
                    closed_reason TEXT,
                    is_closed INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS practice_options (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    practice_id INTEGER NOT NULL,
                    option_no INTEGER NOT NULL,
                    starts_at TEXT NOT NULL,
                    note TEXT,
                    is_confirmed INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (practice_id, option_no),
                    FOREIGN KEY (practice_id) REFERENCES practices(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS practice_targets (
                    practice_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    role_kind TEXT NOT NULL DEFAULT 'member',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (practice_id, user_id),
                    FOREIGN KEY (practice_id) REFERENCES practices(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS availability (
                    option_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    comment TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (option_id, user_id),
                    FOREIGN KEY (option_id) REFERENCES practice_options(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS reminder_logs (
                    option_id INTEGER NOT NULL,
                    stage_minutes INTEGER NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (option_id, stage_minutes),
                    FOREIGN KEY (option_id) REFERENCES practice_options(id) ON DELETE CASCADE
                );
                """
            )
            self._ensure_column(conn, "practices", "collect_deadline", "TEXT")
            self._ensure_column(conn, "practices", "closed_reason", "TEXT")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def add_member(self, user_id: int, display_name: str, role_label: str | None, note: str | None, created_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members (user_id, display_name, role_label, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  display_name = excluded.display_name,
                  role_label = excluded.role_label,
                  note = excluded.note
                """,
                (user_id, display_name, role_label, note, created_at),
            )

    def remove_member(self, user_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM members WHERE user_id = ?", (user_id,))
            return cur.rowcount > 0

    def get_member(self, user_id: int) -> Member | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM members WHERE user_id = ?", (user_id,)).fetchone()
        return Member(**dict(row)) if row else None

    def list_members(self) -> list[Member]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM members ORDER BY display_name COLLATE NOCASE").fetchall()
        return [Member(**dict(row)) for row in rows]

    def create_practice(
        self,
        title: str,
        description: str | None,
        channel_id: int,
        created_by: int,
        created_at: str,
        collect_deadline: str | None,
        options: Iterable[tuple[int, str, str | None]],
        targets: Iterable[tuple[int, str, str, int]],
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO practices (title, description, channel_id, created_by, created_at, collect_deadline)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (title, description, channel_id, created_by, created_at, collect_deadline),
            )
            practice_id = int(cur.lastrowid)
            conn.executemany(
                """
                INSERT INTO practice_options (practice_id, option_no, starts_at, note)
                VALUES (?, ?, ?, ?)
                """,
                [(practice_id, option_no, starts_at, note) for option_no, starts_at, note in options],
            )
            conn.executemany(
                """
                INSERT INTO practice_targets (practice_id, user_id, display_name, role_kind, sort_order)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(practice_id, user_id, display_name, role_kind, sort_order) for user_id, display_name, role_kind, sort_order in targets],
            )
        return practice_id

    def list_practices(self, include_closed: bool = False) -> list[Practice]:
        sql = "SELECT * FROM practices"
        if not include_closed:
            sql += " WHERE is_closed = 0"
        sql += " ORDER BY id DESC"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [Practice(**dict(row)) for row in rows]

    def get_practice(self, practice_id: int) -> Practice | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM practices WHERE id = ?", (practice_id,)).fetchone()
        return Practice(**dict(row)) if row else None

    def get_practice_options(self, practice_id: int) -> list[PracticeOption]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM practice_options WHERE practice_id = ? ORDER BY option_no ASC",
                (practice_id,),
            ).fetchall()
        return [PracticeOption(**dict(row)) for row in rows]

    def list_practice_targets(self, practice_id: int) -> list[PracticeTarget]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT practice_id, user_id, display_name, role_kind, sort_order
                FROM practice_targets
                WHERE practice_id = ?
                ORDER BY sort_order ASC, display_name COLLATE NOCASE, user_id ASC
                """,
                (practice_id,),
            ).fetchall()
        return [PracticeTarget(**dict(row)) for row in rows]

    def is_practice_target(self, practice_id: int, user_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM practice_targets WHERE practice_id = ? AND user_id = ?",
                (practice_id, user_id),
            ).fetchone()
        return row is not None

    def close_practice(self, practice_id: int, reason: str | None = None) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE practices SET is_closed = 1, closed_reason = COALESCE(?, closed_reason) WHERE id = ?",
                (reason, practice_id),
            )
            return cur.rowcount > 0

    def set_confirmed_option(self, practice_id: int, option_no: int) -> bool:
        with self._connect() as conn:
            option = conn.execute(
                "SELECT id FROM practice_options WHERE practice_id = ? AND option_no = ?",
                (practice_id, option_no),
            ).fetchone()
            if not option:
                return False
            conn.execute("UPDATE practice_options SET is_confirmed = 0 WHERE practice_id = ?", (practice_id,))
            conn.execute(
                "UPDATE practice_options SET is_confirmed = 1 WHERE practice_id = ? AND option_no = ?",
                (practice_id, option_no),
            )
            return True

    def get_confirmed_options(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.id AS practice_id, p.title, p.channel_id, o.id AS option_id, o.option_no, o.starts_at, o.note
                FROM practice_options o
                JOIN practices p ON p.id = o.practice_id
                WHERE o.is_confirmed = 1
                  AND p.is_closed = 0
                ORDER BY o.starts_at ASC
                """
            ).fetchall()
        return rows

    def get_expired_open_practices(self, now_iso: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM practices
                WHERE is_closed = 0
                  AND collect_deadline IS NOT NULL
                  AND collect_deadline <= ?
                ORDER BY collect_deadline ASC
                """,
                (now_iso,),
            ).fetchall()
        return rows

    def set_availability(self, option_id: int, user_id: int, status: str, comment: str | None, updated_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO availability (option_id, user_id, status, comment, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(option_id, user_id) DO UPDATE SET
                  status = excluded.status,
                  comment = excluded.comment,
                  updated_at = excluded.updated_at
                """,
                (option_id, user_id, status, comment, updated_at),
            )

    def get_availability_for_practice(self, practice_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  o.practice_id,
                  o.id AS option_id,
                  o.option_no,
                  a.user_id,
                  a.status,
                  a.comment,
                  a.updated_at
                FROM practice_options o
                LEFT JOIN availability a ON a.option_id = o.id
                WHERE o.practice_id = ?
                ORDER BY o.option_no ASC, a.updated_at DESC
                """,
                (practice_id,),
            ).fetchall()
        return rows

    def get_responses_for_option(self, option_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.user_id, a.status, a.comment, a.updated_at, m.display_name
                FROM availability a
                LEFT JOIN members m ON m.user_id = a.user_id
                WHERE a.option_id = ?
                ORDER BY m.display_name COLLATE NOCASE, a.user_id
                """,
                (option_id,),
            ).fetchall()
        return rows

    def was_reminder_sent(self, option_id: int, stage_minutes: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM reminder_logs WHERE option_id = ? AND stage_minutes = ?",
                (option_id, stage_minutes),
            ).fetchone()
        return row is not None

    def mark_reminder_sent(self, option_id: int, stage_minutes: int, sent_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO reminder_logs (option_id, stage_minutes, sent_at)
                VALUES (?, ?, ?)
                """,
                (option_id, stage_minutes, sent_at),
            )
