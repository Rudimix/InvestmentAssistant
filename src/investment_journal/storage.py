"""Хранилище журнала в SQLite."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Decision, DecisionDraft, User

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL CHECK (kind IN ('buy', 'sell', 'hold')),
    ticker TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    entry_price REAL NOT NULL,
    created_at TEXT NOT NULL,
    exit_price REAL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS credentials (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    token BLOB NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    connected_at TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JournalRepository:
    """Пользователи и решения журнала."""

    def __init__(self, path: Path | str = ":memory:"):
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def get_or_create_user(self, chat_id: int) -> User:
        row = self._db.execute("SELECT id, chat_id FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            cur = self._db.execute("INSERT INTO users (chat_id) VALUES (?)", (chat_id,))
            self._db.commit()
            return User(id=cur.lastrowid, chat_id=chat_id)
        return User(id=row["id"], chat_id=row["chat_id"])

    def save_credentials(self, user_id: int, token: bytes, account_id: str) -> None:
        """Сохранить (заменить) токен пользователя. ``token`` — уже зашифрованный."""
        self._db.execute(
            "INSERT OR REPLACE INTO credentials (user_id, token, account_id, connected_at) VALUES (?, ?, ?, ?)",
            (user_id, token, account_id, _now().isoformat()),
        )
        self._db.commit()

    def get_credentials(self, user_id: int) -> tuple[bytes, str] | None:
        """Зашифрованный токен и выбранный счёт или ``None``, если пользователь не подключён."""
        row = self._db.execute("SELECT token, account_id FROM credentials WHERE user_id = ?", (user_id,)).fetchone()
        return (row["token"], row["account_id"]) if row else None

    def set_account(self, user_id: int, account_id: str) -> None:
        self._db.execute("UPDATE credentials SET account_id = ? WHERE user_id = ?", (account_id, user_id))
        self._db.commit()

    def delete_credentials(self, user_id: int) -> bool:
        cur = self._db.execute("DELETE FROM credentials WHERE user_id = ?", (user_id,))
        self._db.commit()
        return cur.rowcount > 0

    def add_decision(self, draft: DecisionDraft, entry_price: float, created_at: datetime | None = None) -> Decision:
        created_at = created_at or _now()
        cur = self._db.execute(
            "INSERT INTO decisions (user_id, kind, ticker, hypothesis, entry_price, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (draft.user_id, draft.kind, draft.ticker.upper(), draft.hypothesis, entry_price, created_at.isoformat()),
        )
        self._db.commit()
        return Decision(
            id=cur.lastrowid,
            user_id=draft.user_id,
            kind=draft.kind,
            ticker=draft.ticker.upper(),
            hypothesis=draft.hypothesis,
            entry_price=entry_price,
            created_at=created_at,
        )

    def get_decision(self, user_id: int, decision_id: int) -> Decision | None:
        row = self._db.execute(
            "SELECT * FROM decisions WHERE id = ? AND user_id = ?", (decision_id, user_id)
        ).fetchone()
        return self._to_decision(row) if row else None

    def list_decisions(self, user_id: int, open_only: bool = False) -> list[Decision]:
        sql = "SELECT * FROM decisions WHERE user_id = ?"
        if open_only:
            sql += " AND closed_at IS NULL"
        rows = self._db.execute(sql + " ORDER BY id", (user_id,)).fetchall()
        return [self._to_decision(r) for r in rows]

    def close_decision(self, decision: Decision, exit_price: float, closed_at: datetime | None = None) -> Decision:
        closed_at = closed_at or _now()
        self._db.execute(
            "UPDATE decisions SET exit_price = ?, closed_at = ? WHERE id = ?",
            (exit_price, closed_at.isoformat(), decision.id),
        )
        self._db.commit()
        decision.exit_price = exit_price
        decision.closed_at = closed_at
        return decision

    @staticmethod
    def _to_decision(row: sqlite3.Row) -> Decision:
        return Decision(
            id=row["id"],
            user_id=row["user_id"],
            kind=row["kind"],
            ticker=row["ticker"],
            hypothesis=row["hypothesis"],
            entry_price=row["entry_price"],
            created_at=datetime.fromisoformat(row["created_at"]),
            exit_price=row["exit_price"],
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
        )
