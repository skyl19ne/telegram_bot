from __future__ import annotations

import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GROUP_ID_PATTERN = re.compile(r"^60\d{3}$")


@dataclass(frozen=True)
class MediaItem:
    media_type: str
    file_id: str
    created_at: str


@dataclass(frozen=True)
class Group:
    group_id: str
    comment: str | None
    created_at: str
    media: tuple[MediaItem, ...]


def validate_group_id(value: str) -> str:
    normalized = value.strip()
    if not GROUP_ID_PATTERN.fullmatch(normalized):
        raise ValueError("ID групи має містити рівно 5 цифр і починатися з 60")
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
            """
        )
        self.connection.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_group(self, comment: str | None = None) -> str:
        for _ in range(1000):
            group_id = f"60{random.randint(0, 999):03d}"
            try:
                self.connection.execute(
                    "INSERT INTO groups(group_id, comment, created_at) VALUES (?, ?, ?)",
                    (group_id, comment, self._now()),
                )
                self.connection.commit()
                return group_id
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Не вдалося згенерувати унікальний ID групи")

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

    def group_exists(self, group_id: str) -> bool:
        group_id = validate_group_id(group_id)
        return self.connection.execute(
            "SELECT 1 FROM groups WHERE group_id = ?", (group_id,)
        ).fetchone() is not None

    def get_group(self, group_id: str) -> Group | None:
        group_id = validate_group_id(group_id)
        row = self.connection.execute(
            "SELECT group_id, comment, created_at FROM groups WHERE group_id = ?",
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
            created_at=row["created_at"],
            media=tuple(MediaItem(item["media_type"], item["file_id"], item["created_at"]) for item in media),
        )

    def close(self) -> None:
        self.connection.close()
