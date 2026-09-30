from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GROUP_ID_PATTERN = re.compile(r"^60\d{3}$")
ROLE_OWNER = 1
ROLE_ADMIN = 2
ROLE_EDITOR = 3
ROLE_MEDIA_MANAGER = ROLE_EDITOR
ROLE_VIEWER = 4
ROLE_NAMES = {ROLE_OWNER: "owner", ROLE_ADMIN: "admin", ROLE_EDITOR: "editor", ROLE_VIEWER: "viewer"}


@dataclass(frozen=True)
class MediaItem:
    media_type: str
    file_id: str
    created_at: str


@dataclass(frozen=True)
class Group:
    group_id: str
    comment: str | None
    size: str | None
    lighting: str | None
    created_at: str
    media: tuple[MediaItem, ...]


def validate_group_id(value: str) -> str:
    normalized = value.strip()
    if not GROUP_ID_PATTERN.fullmatch(normalized):
        raise ValueError("ID осмосу має містити рівно 5 цифр і починатися з 60")
    return normalized


class MediaRepository:
    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                group_id TEXT PRIMARY KEY CHECK (group_id GLOB '60[0-9][0-9][0-9]'),
                comment TEXT,
                size TEXT,
                lighting TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
                media_type TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
                file_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_media_group_id ON media(group_id);
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                role INTEGER NOT NULL CHECK (role BETWEEN 1 AND 4),
                display_name TEXT,
                username TEXT,
                added_by INTEGER,
                updated_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT
            );
            """
        )
        group_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(groups)")}
        for column in ("size", "lighting"):
            if column not in group_columns:
                self.connection.execute(f"ALTER TABLE groups ADD COLUMN {column} TEXT")
        user_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(users)")}
        for column, column_type in (
            ("added_by", "INTEGER"),
            ("updated_by", "INTEGER"),
            ("updated_at", "TEXT"),
        ):
            if column not in user_columns:
                self.connection.execute(f"ALTER TABLE users ADD COLUMN {column} {column_type}")
        self.connection.execute("UPDATE users SET updated_at = created_at WHERE updated_at IS NULL")
        self.connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_group(
        self,
        group_id: str,
        comment: str | None = None,
        size: str | None = None,
        lighting: str | None = None,
    ) -> str:
        group_id = validate_group_id(group_id)
        try:
            self.connection.execute(
                "INSERT INTO groups(group_id, comment, size, lighting, created_at) VALUES (?, ?, ?, ?, ?)",
                (group_id, comment, size, lighting, self._now()),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("Осмос із таким ID вже існує") from exc
        return group_id

    def delete_group(self, group_id: str) -> None:
        group_id = validate_group_id(group_id)
        cursor = self.connection.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
        if cursor.rowcount == 0:
            raise KeyError(group_id)
        self.connection.commit()

    def rename_group(self, group_id: str, new_group_id: str) -> str:
        group_id = validate_group_id(group_id)
        new_group_id = validate_group_id(new_group_id)
        if not self.group_exists(group_id):
            raise KeyError(group_id)
        if self.group_exists(new_group_id):
            raise ValueError("Осмос із таким ID вже існує")
        try:
            self.connection.execute("BEGIN")
            self.connection.execute(
                """INSERT INTO groups(group_id, comment, size, lighting, created_at)
                   SELECT ?, comment, size, lighting, created_at FROM groups WHERE group_id = ?""",
                (new_group_id, group_id),
            )
            self.connection.execute(
                "UPDATE media SET group_id = ? WHERE group_id = ?",
                (new_group_id, group_id),
            )
            user_group_table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_groups'"
            ).fetchone()
            if user_group_table:
                self.connection.execute(
                    """INSERT OR IGNORE INTO user_groups(user_id, group_id, assigned_by, assigned_at)
                       SELECT user_id, ?, assigned_by, assigned_at
                       FROM user_groups WHERE group_id = ?""",
                    (new_group_id, group_id),
                )
                self.connection.execute("DELETE FROM user_groups WHERE group_id = ?", (group_id,))
            self.connection.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise ValueError("Осмос із таким ID вже існує") from exc
        return new_group_id

    def get_user_role(self, user_id: int) -> int | None:
        row = self.connection.execute("SELECT role FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["role"]) if row else None

    def add_user(
        self,
        user_id: int,
        role: int,
        display_name: str = "",
        username: str = "",
        added_by: int | None = None,
        updated_by: int | None = None,
    ) -> None:
        if role not in ROLE_NAMES:
            raise ValueError("Невідома роль")
        existing = self.get_user_role(user_id)
        if existing == ROLE_OWNER and role != ROLE_OWNER:
            raise PermissionError("Власника не можна змінити")
        now = self._now()
        existing = self.get_user_record(user_id)
        if existing is None:
            self.connection.execute(
                """INSERT INTO users(
                    user_id, role, display_name, username, added_by, updated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, role, display_name, username, added_by, updated_by or added_by, now, now),
            )
        else:
            self.connection.execute(
                """UPDATE users SET role = ?, display_name = ?, username = ?,
                   updated_by = ?, updated_at = ? WHERE user_id = ?""",
                (
                    role,
                    display_name or existing["display_name"] or "",
                    username or existing["username"] or "",
                    updated_by or added_by,
                    now,
                    user_id,
                ),
            )
        self.connection.commit()

    def remove_user(self, user_id: int) -> None:
        self.connection.execute("DELETE FROM users WHERE user_id = ? AND role != ?", (user_id, ROLE_OWNER))
        self.connection.commit()

    def list_users(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """SELECT user_id, role, display_name, username, added_by, updated_by, created_at, updated_at
               FROM users ORDER BY role, user_id"""
        ).fetchall()

    def get_user_record(self, user_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT user_id, role, display_name, username, added_by, updated_by, created_at, updated_at
               FROM users WHERE user_id = ?""",
            (user_id,),
        ).fetchone()

    def list_groups(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT group_id, comment, size, lighting, created_at FROM groups ORDER BY group_id"
        ).fetchall()

    def add_media(self, group_id: str, media_type: str, file_id: str) -> None:
        group_id = validate_group_id(group_id)
        if media_type not in {"photo", "video"}:
            raise ValueError("Непідтримуваний тип медіа")
        if not file_id.strip():
            raise ValueError("file_id не може бути порожнім")
        if not self.group_exists(group_id):
            raise KeyError(group_id)
        self.connection.execute(
            "INSERT INTO media(group_id, media_type, file_id, created_at) VALUES (?, ?, ?, ?)",
            (group_id, media_type, file_id, self._now()),
        )
        self.connection.commit()

    def set_comment(self, group_id: str, comment: str) -> None:
        group_id = validate_group_id(group_id)
        if not self.group_exists(group_id):
            raise KeyError(group_id)
        self.connection.execute(
            "UPDATE groups SET comment = ? WHERE group_id = ?",
            (comment.strip() or None, group_id),
        )
        self.connection.commit()

    def set_type(self, group_id: str, size: str, lighting: str) -> None:
        group_id = validate_group_id(group_id)
        if not self.group_exists(group_id):
            raise KeyError(group_id)
        self.connection.execute(
            "UPDATE groups SET size = ?, lighting = ? WHERE group_id = ?",
            (size, lighting, group_id),
        )
        self.connection.commit()

    def clear_media(self, group_id: str) -> None:
        group_id = validate_group_id(group_id)
        if not self.group_exists(group_id):
            raise KeyError(group_id)
        self.connection.execute("DELETE FROM media WHERE group_id = ?", (group_id,))
        self.connection.commit()

    def group_exists(self, group_id: str) -> bool:
        group_id = validate_group_id(group_id)
        return self.connection.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (group_id,)
        ).fetchone() is not None

    def get_group(self, group_id: str) -> Group | None:
        group_id = validate_group_id(group_id)
        row = self.connection.execute(
            "SELECT group_id, comment, size, lighting, created_at FROM groups WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            return None
        media = self.connection.execute(
            "SELECT media_type, file_id, created_at FROM media "
            "WHERE group_id = ? ORDER BY id",
            (group_id,),
        ).fetchall()
        return Group(
            group_id=row["group_id"],
            comment=row["comment"],
            size=row["size"],
            lighting=row["lighting"],
            created_at=row["created_at"],
            media=tuple(MediaItem(item["media_type"], item["file_id"], item["created_at"]) for item in media),
        )

    def close(self) -> None:
        self.connection.close()
