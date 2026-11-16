"""Хранилище журнала в SQLite."""

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .models import Decision, DecisionDraft, PortfolioOperation, PortfolioSnapshot, User, data_json, timestamp

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

PORTFOLIO_SCHEMA = """
CREATE TABLE portfolio_snapshots (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, account_id TEXT NOT NULL,
    observed_at TEXT NOT NULL, currency TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX snapshot_history ON portfolio_snapshots(user_id, account_id, observed_at);
CREATE TABLE portfolio_operations (
    user_id INTEGER NOT NULL, account_id TEXT NOT NULL, source_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(user_id, account_id, source_id)
);
CREATE INDEX operation_history ON portfolio_operations(user_id, account_id, occurred_at);
CREATE TABLE report_schedules (
    user_id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, rule TEXT NOT NULL,
    enabled INTEGER NOT NULL, revision INTEGER NOT NULL,
    next_run_at TEXT NOT NULL, pause_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE report_runs (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, account_id TEXT NOT NULL,
    origin TEXT NOT NULL, request_key TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
    scheduled_at TEXT, state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
    retry_at TEXT, lease_owner TEXT, lease_until TEXT, snapshot_id TEXT, baseline_id TEXT,
    payload TEXT, last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(user_id, origin, request_key)
);
CREATE INDEX run_queue ON report_runs(state, retry_at, lease_until);
CREATE INDEX report_history ON report_runs(user_id, account_id, origin, state, updated_at);
CREATE TABLE report_parts (
    run_id TEXT NOT NULL, number INTEGER NOT NULL, text TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued', message_id INTEGER,
    PRIMARY KEY(run_id, number)
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return timestamp(value).isoformat(timespec="microseconds")


class LeaseLost(RuntimeError):
    """Выполнение отменено или его аренда перешла к другому обработчику."""


class JournalRepository:
    """Пользователи и решения журнала."""

    def __init__(self, path: Path | str = ":memory:"):
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        try:
            self._db.executescript(SCHEMA)
            self._migrate_portfolio()
        except Exception:
            self._db.close()
            raise

    def _migrate_portfolio(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise ValueError("Версия базы новее поддерживаемой")
        if version == 0:
            try:
                self._db.executescript("BEGIN IMMEDIATE;\n" + PORTFOLIO_SCHEMA + "\nPRAGMA user_version = 1;\nCOMMIT;")
            except Exception:
                self._db.rollback()
                raise

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _transaction(self):
        """Атомарный переход состояния с блокировкой до проверок и записи."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def get_or_create_user(self, chat_id: int) -> User:
        row = self._db.execute("SELECT id, chat_id FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            cur = self._db.execute("INSERT INTO users (chat_id) VALUES (?)", (chat_id,))
            self._db.commit()
            return User(id=cur.lastrowid, chat_id=chat_id)
        return User(id=row["id"], chat_id=row["chat_id"])

    def get_user(self, user_id: int) -> User | None:
        row = self._db.execute("SELECT id, chat_id FROM users WHERE id=?", (user_id,)).fetchone()
        return User(row["id"], row["chat_id"]) if row else None

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO portfolio_snapshots VALUES(?,?,?,?,?,?)",
                (snapshot.id, snapshot.user_id, snapshot.account_id, _utc_text(snapshot.observed_at),
                 snapshot.currency, data_json(snapshot)),
            )

    def get_snapshot(self, user_id: int, account_id: str, snapshot_id: str) -> PortfolioSnapshot | None:
        row = self._db.execute(
            "SELECT payload FROM portfolio_snapshots WHERE id=? AND user_id=? AND account_id=?",
            (snapshot_id, user_id, account_id),
        ).fetchone()
        return PortfolioSnapshot.from_dict(json.loads(row["payload"])) if row else None

    def baseline_snapshot(self, user_id: int, account_id: str, origin: str,
                          before: datetime) -> PortfolioSnapshot | None:
        row = self._db.execute(
            "SELECT s.payload FROM report_runs r JOIN portfolio_snapshots s ON r.snapshot_id=s.id "
            "WHERE r.user_id=? AND r.account_id=? AND r.origin=? AND r.state='delivered' "
            "AND s.user_id=r.user_id AND s.account_id=r.account_id AND s.observed_at<? "
            "ORDER BY r.updated_at DESC,s.observed_at DESC LIMIT 1",
            (user_id, account_id, origin, _utc_text(before)),
        ).fetchone()
        if row is None:
            row = self._db.execute(
                "SELECT payload FROM portfolio_snapshots WHERE user_id=? AND account_id=? AND observed_at<? "
                "ORDER BY observed_at,id LIMIT 1", (user_id, account_id, _utc_text(before)),
            ).fetchone()
        return PortfolioSnapshot.from_dict(json.loads(row["payload"])) if row else None

    def save_operations(self, user_id: int, account_id: str, operations: list[PortfolioOperation]) -> None:
        with self._db:
            self._db.executemany(
                "INSERT INTO portfolio_operations VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(user_id,account_id,source_id) DO UPDATE SET "
                "occurred_at=excluded.occurred_at,state=excluded.state,payload=excluded.payload",
                [(user_id, account_id, item.id, _utc_text(item.occurred_at), item.state, data_json(item))
                 for item in operations],
            )

    def list_operations(self, user_id: int, account_id: str, start: datetime, end: datetime) -> list[PortfolioOperation]:
        rows = self._db.execute(
            "SELECT payload FROM portfolio_operations WHERE user_id=? AND account_id=? "
            "AND occurred_at>? AND occurred_at<=? ORDER BY occurred_at,source_id",
            (user_id, account_id, _utc_text(start), _utc_text(end)),
        ).fetchall()
        return [PortfolioOperation.from_dict(json.loads(row["payload"])) for row in rows]

    def get_schedule(self, user_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM report_schedules WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["rule"] = json.loads(result["rule"])
        result["next_run_at"] = timestamp(result["next_run_at"])
        return result

    def save_schedule(self, user_id: int, account_id: str, rule: dict, next_run_at: datetime) -> dict:
        with self._transaction():
            credentials = self.get_credentials(user_id)
            if credentials is None or credentials[1] != account_id:
                raise ValueError("Выбранный счёт изменился, откройте настройку заново")
            self._db.execute(
                "INSERT INTO report_schedules(user_id,account_id,rule,enabled,revision,next_run_at) "
                "VALUES(?,?,?,1,1,?) ON CONFLICT(user_id) DO UPDATE SET "
                "account_id=excluded.account_id,rule=excluded.rule,enabled=1,revision=revision+1,"
                "next_run_at=excluded.next_run_at,pause_reason=''",
                (user_id, account_id, data_json(rule), _utc_text(next_run_at)),
            )
            self._cancel_runs(user_id, scheduled_only=True)
        return self.get_schedule(user_id)

    def pause_schedule(self, user_id: int, reason: str) -> None:
        with self._transaction():
            self._db.execute("UPDATE report_schedules SET enabled=0,revision=revision+1,pause_reason=? WHERE user_id=?",
                             (reason, user_id))
            self._cancel_runs(user_id, scheduled_only=True)

    def _cancel_runs(self, user_id: int, scheduled_only: bool = False) -> None:
        sql = ("UPDATE report_runs SET state='cancelled',lease_owner=NULL,lease_until=NULL "
               "WHERE user_id=? AND state IN ('queued','retry','running')")
        if scheduled_only:
            sql += " AND origin='scheduled'"
        self._db.execute(sql, (user_id,))

    def _insert_run(self, user_id: int, account_id: str, origin: str, request_key: str,
                    revision: int, scheduled_at: datetime | None, now: datetime) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO report_runs(id,user_id,account_id,origin,request_key,revision,scheduled_at,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid4().hex, user_id, account_id, origin, request_key, revision,
             _utc_text(scheduled_at) if scheduled_at else None, _utc_text(now), _utc_text(now)),
        )

    def create_run(self, user_id: int, account_id: str, origin: str, request_key: str,
                   now: datetime, revision: int = 0, scheduled_at: datetime | None = None) -> dict:
        if origin not in {"manual", "scheduled"}:
            raise ValueError("Неизвестный источник отчёта")
        with self._transaction():
            self._insert_run(user_id, account_id, origin, request_key, revision, scheduled_at, now)
        row = self._db.execute("SELECT * FROM report_runs WHERE user_id=? AND origin=? AND request_key=?",
                               (user_id, origin, request_key)).fetchone()
        return dict(row)

    def get_run(self, user_id: int, run_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM report_runs WHERE id=? AND user_id=?", (run_id, user_id)).fetchone()
        return dict(row) if row else None

    def cancel_run(self, user_id: int, run_id: str) -> None:
        with self._transaction():
            self._db.execute("UPDATE report_runs SET state='cancelled',lease_owner=NULL,lease_until=NULL "
                             "WHERE id=? AND user_id=? AND state IN ('queued','retry','running')", (run_id, user_id))

    def last_run(self, user_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM report_runs WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
                               (user_id,)).fetchone()
        return dict(row) if row else None

    def claim_run(self, user_id: int, run_id: str, owner: str, now: datetime, seconds: int = 180) -> bool:
        with self._transaction():
            row = self.get_run(user_id, run_id)
            if row is None or row["state"] not in {"queued", "retry", "running"}:
                return False
            if row["retry_at"] and timestamp(row["retry_at"]) > now:
                return False
            if row["lease_until"] and timestamp(row["lease_until"]) > now:
                return False
            uncertain = self._db.execute("SELECT 1 FROM report_parts WHERE run_id=? AND state='sending'", (run_id,)).fetchone()
            if uncertain:
                self._db.execute("UPDATE report_parts SET state='unknown' WHERE run_id=? AND state='sending'", (run_id,))
                self._db.execute("UPDATE report_runs SET state='delivery_unknown',lease_owner=NULL,lease_until=NULL,"
                                 "last_error='Исход отправки неизвестен',updated_at=? WHERE id=?", (_utc_text(now), run_id))
                return False
            active = self._db.execute(
                "SELECT 1 FROM report_runs WHERE user_id=? AND id<>? AND state='running' AND lease_until>?",
                (user_id, run_id, _utc_text(now)),
            ).fetchone()
            if active:
                return False
            earlier = self._db.execute(
                "SELECT 1 FROM report_runs WHERE user_id=? AND origin=? AND state IN ('queued','retry','running') "
                "AND rowid<(SELECT rowid FROM report_runs WHERE id=?) LIMIT 1",
                (user_id, row["origin"], run_id),
            ).fetchone()
            if earlier:
                return False
            self._db.execute("UPDATE report_runs SET state='running',attempts=attempts+1,lease_owner=?,lease_until=?,"
                             "retry_at=NULL,updated_at=? WHERE id=?",
                             (owner, _utc_text(now+timedelta(seconds=seconds)), _utc_text(now), run_id))
            return True

    def _require_lease(self, user_id: int, run_id: str, owner: str, now: datetime) -> None:
        row = self._db.execute(
            "SELECT 1 FROM report_runs WHERE id=? AND user_id=? AND state='running' AND lease_owner=? AND lease_until>?",
            (run_id, user_id, owner, _utc_text(now)),
        ).fetchone()
        if row is None:
            raise LeaseLost("Аренда выполнения потеряна")

    def renew_run(self, user_id: int, run_id: str, owner: str, now: datetime, seconds: int = 180) -> bool:
        with self._transaction():
            updated = self._db.execute(
                "UPDATE report_runs SET lease_until=? WHERE id=? AND user_id=? AND state='running' "
                "AND lease_owner=?",
                (_utc_text(now+timedelta(seconds=seconds)), run_id, user_id, owner),
            )
        return updated.rowcount > 0

    def prepare_run(self, user_id: int, run_id: str, owner: str, snapshot_id: str,
                    baseline_id: str | None, payload: dict, parts: list[str], now: datetime) -> None:
        with self._transaction():
            self._require_lease(user_id, run_id, owner, now)
            row = self.get_run(user_id, run_id)
            if self.get_snapshot(user_id, row["account_id"], snapshot_id) is None:
                raise ValueError("Снимок отчёта недоступен")
            if baseline_id and self.get_snapshot(user_id, row["account_id"], baseline_id) is None:
                raise ValueError("Точка сравнения недоступна")
            if row["payload"] is not None or not parts:
                raise ValueError("Отчёт уже подготовлен или пуст")
            self._db.execute("UPDATE report_runs SET snapshot_id=?,baseline_id=?,payload=?,updated_at=? WHERE id=?",
                             (snapshot_id, baseline_id, data_json(payload), _utc_text(now), run_id))
            self._db.executemany("INSERT INTO report_parts(run_id,number,text) VALUES(?,?,?)",
                                 [(run_id, index, text) for index, text in enumerate(parts, 1)])

    def get_parts(self, user_id: int, run_id: str) -> list[dict]:
        rows = self._db.execute("SELECT p.* FROM report_parts p JOIN report_runs r ON p.run_id=r.id "
                                "WHERE r.user_id=? AND r.id=? ORDER BY p.number", (user_id, run_id)).fetchall()
        return [dict(row) for row in rows]

    def set_part_state(self, user_id: int, run_id: str, owner: str, number: int, state: str,
                       now: datetime, message_id: int | None = None) -> None:
        transitions = {"sending": "queued", "sent": "sending", "queued": "sending"}
        if state not in transitions:
            raise ValueError("Недопустимое состояние части")
        with self._transaction():
            self._require_lease(user_id, run_id, owner, now)
            updated = self._db.execute(
                "UPDATE report_parts SET state=?,message_id=? WHERE run_id=? AND number=? AND state=?",
                (state, message_id, run_id, number, transitions[state]),
            )
            if not updated.rowcount:
                raise ValueError("Состояние части изменилось")
            remaining = self._db.execute("SELECT 1 FROM report_parts WHERE run_id=? AND state<>'sent'", (run_id,)).fetchone()
            if state == "sent" and remaining is None:
                self._db.execute("UPDATE report_runs SET state='delivered',lease_owner=NULL,lease_until=NULL,"
                                 "last_error='',updated_at=? WHERE id=?", (_utc_text(now), run_id))

    def finish_run(self, user_id: int, run_id: str, owner: str, now: datetime) -> None:
        with self._transaction():
            current = self.get_run(user_id, run_id)
            if current is not None and current["state"] == "delivered":
                return
            self._require_lease(user_id, run_id, owner, now)
            parts = self.get_parts(user_id, run_id)
            if not parts or any(part["state"] != "sent" for part in parts):
                raise ValueError("Отчёт доставлен не полностью")
            self._db.execute("UPDATE report_runs SET state='delivered',lease_owner=NULL,lease_until=NULL,"
                             "last_error='',updated_at=? WHERE id=?", (_utc_text(now), run_id))

    def fail_run(self, user_id: int, run_id: str, owner: str, now: datetime, error: str,
                 retry_at: datetime | None = None, unknown: bool = False) -> None:
        state = "delivery_unknown" if unknown else "failed"
        if retry_at is not None:
            state = "retry"
        with self._transaction():
            self._require_lease(user_id, run_id, owner, now)
            self._db.execute("UPDATE report_runs SET state=?,retry_at=?,lease_owner=NULL,lease_until=NULL,"
                             "last_error=?,updated_at=? WHERE id=?",
                             (state, _utc_text(retry_at) if retry_at else None, error, _utc_text(now), run_id))
            if unknown:
                self._db.execute("UPDATE report_parts SET state='unknown' WHERE run_id=? AND state='sending'", (run_id,))

    def ready_runs(self, now: datetime) -> list[dict]:
        rows = self._db.execute(
            "SELECT r.* FROM report_runs r WHERE r.state IN ('queued','retry','running') "
            "AND (r.retry_at IS NULL OR r.retry_at<=?) AND (r.lease_until IS NULL OR r.lease_until<=?) "
            "AND NOT EXISTS(SELECT 1 FROM report_runs p WHERE p.user_id=r.user_id AND p.origin=r.origin "
            "AND p.rowid<r.rowid AND p.state IN ('queued','retry','running')) "
            "AND NOT EXISTS(SELECT 1 FROM report_runs a WHERE a.user_id=r.user_id AND a.id<>r.id "
            "AND a.state='running' AND a.lease_until>?) ORDER BY r.created_at,r.rowid",
            (_utc_text(now), _utc_text(now), _utc_text(now)),
        ).fetchall()
        return [dict(row) for row in rows]

    def due_schedules(self, now: datetime) -> list[dict]:
        rows = self._db.execute("SELECT user_id FROM report_schedules WHERE enabled=1 AND next_run_at<=?",
                                (_utc_text(now),)).fetchall()
        return [self.get_schedule(row["user_id"]) for row in rows]

    def enqueue_schedule(self, schedule: dict, next_run_at: datetime, now: datetime) -> None:
        with self._transaction():
            current = self.get_schedule(schedule["user_id"])
            if current is None or not current["enabled"] or current["revision"] != schedule["revision"]:
                return
            if current["next_run_at"] > now:
                return
            pending = self._db.execute("SELECT 1 FROM report_runs WHERE user_id=? AND origin='scheduled' "
                                       "AND state='queued' AND revision=?", (current["user_id"], current["revision"])).fetchone()
            if not pending:
                nominal = current["next_run_at"]
                self._insert_run(current["user_id"], current["account_id"], "scheduled",
                                 f"{current['revision']}:{_utc_text(nominal)}", current["revision"], nominal, now)
            self._db.execute("UPDATE report_schedules SET next_run_at=? WHERE user_id=?",
                             (_utc_text(next_run_at), current["user_id"]))

    def save_credentials(self, user_id: int, token: bytes, account_id: str) -> None:
        """Сохранить (заменить) токен пользователя. ``token`` — уже зашифрованный."""
        with self._transaction():
            self._db.execute(
                "INSERT OR REPLACE INTO credentials (user_id, token, account_id, connected_at) VALUES (?, ?, ?, ?)",
                (user_id, token, account_id, _now().isoformat()),
            )
            self._db.execute("UPDATE report_schedules SET account_id=?,enabled=0,revision=revision+1,"
                             "pause_reason='подключение обновлено, подтвердите возобновление' WHERE user_id=?",
                             (account_id, user_id))
            self._cancel_runs(user_id)

    def get_credentials(self, user_id: int) -> tuple[bytes, str] | None:
        """Зашифрованный токен и выбранный счёт или ``None``, если пользователь не подключён."""
        row = self._db.execute("SELECT token, account_id FROM credentials WHERE user_id = ?", (user_id,)).fetchone()
        return (row["token"], row["account_id"]) if row else None

    def set_account(self, user_id: int, account_id: str) -> None:
        with self._transaction():
            previous = self.get_credentials(user_id)
            self._db.execute("UPDATE credentials SET account_id = ? WHERE user_id = ?", (account_id, user_id))
            if previous is not None and previous[1] != account_id:
                self._db.execute("UPDATE report_schedules SET account_id=?,revision=revision+1 WHERE user_id=?",
                                 (account_id, user_id))
                self._cancel_runs(user_id)

    def delete_credentials(self, user_id: int) -> bool:
        with self._transaction():
            cur = self._db.execute("DELETE FROM credentials WHERE user_id = ?", (user_id,))
            self._db.execute("UPDATE report_schedules SET enabled=0,revision=revision+1,"
                             "pause_reason='подключение отключено' WHERE user_id=?", (user_id,))
            self._cancel_runs(user_id)
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
