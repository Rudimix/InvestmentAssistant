"""Хранилище журнала в SQLite."""

import sqlite3
import json
from hashlib import sha256
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .models import (
    AdviceProposal, BrokerMetadataRecord, CorporateEvent, DailyObservation, Decision, DecisionDraft, EvidenceChunk, EvidenceDocument, FinancialFact,
    GrowthForecast, HistoricalState, InvestorProfile, PortfolioOperation, PortfolioSnapshot,
    StockUniverse, User, data_json, timestamp,
)

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

LATEST_SCHEMA_VERSION = 2

ANALYSIS_SCHEMA = """
ALTER TABLE report_runs ADD COLUMN report_kind TEXT NOT NULL DEFAULT 'analysis';
ALTER TABLE report_runs ADD COLUMN period_request TEXT;
ALTER TABLE report_runs ADD COLUMN resolved_period TEXT;
ALTER TABLE report_runs ADD COLUMN profile_revision INTEGER;
ALTER TABLE report_runs ADD COLUMN model_version TEXT;
ALTER TABLE report_runs ADD COLUMN manifest TEXT NOT NULL DEFAULT '{}';
ALTER TABLE report_schedules ADD COLUMN analysis_policy TEXT NOT NULL DEFAULT '{"kind":"legacy_delivered"}';
ALTER TABLE report_schedules ADD COLUMN sections TEXT NOT NULL DEFAULT '["analysis"]';
CREATE TABLE investor_profiles (
    user_id INTEGER NOT NULL, revision INTEGER NOT NULL, confirmed_at TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(user_id, revision)
);
CREATE TABLE historical_states (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, account_id TEXT NOT NULL,
    effective_at TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX historical_state_scope ON historical_states(user_id, account_id, effective_at);
CREATE TABLE historical_inputs (
    state_id TEXT NOT NULL, source_id TEXT NOT NULL, source_version TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(state_id, source_id, source_version)
);
CREATE TABLE historical_prices (
    instrument_id TEXT NOT NULL, effective_at TEXT NOT NULL, source TEXT NOT NULL,
    version TEXT NOT NULL, retrieved_at TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(instrument_id, effective_at, source, version)
);
CREATE TABLE corporate_events (
    id TEXT NOT NULL, version TEXT NOT NULL, instrument_id TEXT NOT NULL,
    effective_at TEXT NOT NULL, available_at TEXT, payload TEXT NOT NULL, PRIMARY KEY(id, version)
);
CREATE TABLE evidence_documents (
    id TEXT NOT NULL, version TEXT NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL, published_at TEXT, available_at TEXT, payload TEXT NOT NULL,
    PRIMARY KEY(id, version)
);
CREATE INDEX evidence_availability ON evidence_documents(available_at, source);
CREATE TABLE evidence_attributions (
    document_id TEXT NOT NULL, document_version TEXT NOT NULL, source TEXT NOT NULL,
    original_url TEXT NOT NULL, canonical_url TEXT NOT NULL, observed_at TEXT NOT NULL,
    published_at TEXT, PRIMARY KEY(document_id, document_version, source, original_url, observed_at)
);
CREATE INDEX evidence_canonical_url ON evidence_attributions(canonical_url);
CREATE TABLE evidence_fingerprints (
    fingerprint TEXT NOT NULL, document_id TEXT NOT NULL, document_version TEXT NOT NULL,
    PRIMARY KEY(fingerprint, document_id, document_version)
);
CREATE TABLE evidence_instruments (
    document_id TEXT NOT NULL, document_version TEXT NOT NULL, instrument_id TEXT NOT NULL,
    confirmed_at TEXT NOT NULL, PRIMARY KEY(document_id, document_version, instrument_id)
);
CREATE TABLE evidence_chunks (
    id TEXT PRIMARY KEY, document_id TEXT NOT NULL, document_version TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE VIRTUAL TABLE evidence_search USING fts5(
    chunk_id UNINDEXED, document_id UNINDEXED, document_version UNINDEXED, normalized_text,
    tokenize='unicode61'
);
CREATE TABLE financial_facts (
    id TEXT NOT NULL, document_version TEXT NOT NULL, instrument_id TEXT NOT NULL,
    metric TEXT NOT NULL, available_at TEXT, payload TEXT NOT NULL, PRIMARY KEY(id, document_version)
);
CREATE INDEX financial_fact_lookup ON financial_facts(instrument_id, metric, available_at);
CREATE TABLE source_refreshes (
    source TEXT PRIMARY KEY, last_success_at TEXT, retry_at TEXT, failures INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE stock_universes (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, account_id TEXT NOT NULL,
    as_of TEXT NOT NULL, methodology TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE broker_metadata (
    tool TEXT NOT NULL, identifier TEXT NOT NULL, version TEXT NOT NULL,
    observed_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(tool,identifier,version)
);
CREATE TABLE stock_screenings (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, account_id TEXT NOT NULL,
    as_of TEXT NOT NULL, universe_id TEXT NOT NULL, mode TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX screening_scope ON stock_screenings(user_id, account_id, as_of);
CREATE TABLE forecast_models (
    version TEXT PRIMARY KEY, created_at TEXT NOT NULL, trained_until TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE growth_forecasts (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, account_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
    as_of TEXT NOT NULL, target_at TEXT NOT NULL, model_version TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX forecast_scope ON growth_forecasts(user_id, account_id, as_of);
CREATE TABLE advice_proposals (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, account_id TEXT NOT NULL,
    profile_revision INTEGER NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
    report_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX advice_scope ON advice_proposals(user_id, account_id, created_at);
CREATE TABLE hypothesis_evidence (
    decision_id INTEGER NOT NULL, user_id INTEGER NOT NULL, account_id TEXT NOT NULL,
    source_kind TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL, request_key TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(decision_id, source_kind, source_id)
);
CREATE UNIQUE INDEX hypothesis_request ON hypothesis_evidence(user_id,account_id,request_key) WHERE request_key<>'';
CREATE TABLE evaluation_scenarios (
    dataset_version TEXT NOT NULL, id TEXT NOT NULL, as_of TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(dataset_version, id)
);
CREATE TABLE evaluation_runs (
    id TEXT PRIMARY KEY, dataset_version TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE expert_reviews (
    run_id TEXT NOT NULL, proposal_id TEXT NOT NULL, reviewer TEXT NOT NULL,
    revision INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(run_id, proposal_id, reviewer, revision)
);
CREATE TABLE forecast_observations (
    forecast_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(forecast_id, observed_at)
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
        if version > LATEST_SCHEMA_VERSION:
            raise ValueError("Версия базы новее поддерживаемой")
        if version == 0:
            try:
                self._db.executescript("BEGIN IMMEDIATE;\n" + PORTFOLIO_SCHEMA + "\nPRAGMA user_version = 1;\nCOMMIT;")
            except Exception:
                self._db.rollback()
                raise
            version = 1
        if version == 1:
            try:
                self._db.executescript("BEGIN IMMEDIATE;\n" + ANALYSIS_SCHEMA + "\nPRAGMA user_version = 2;\nCOMMIT;")
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

    def _insert_immutable(self, table: str, values: dict, identity: tuple[str, ...]) -> bool:
        """Внутренняя запись в транзакции вызывающего метода, имена задаются кодом."""
        where = " AND ".join(f"{key}=?" for key in identity)
        existing = self._db.execute(f"SELECT * FROM {table} WHERE {where}", tuple(values[key] for key in identity)).fetchone()
        if existing is not None:
            for key, value in values.items():
                previous = existing[key]
                equal = json.loads(previous) == json.loads(value) if key == "payload" else previous == value
                if not equal:
                    raise ValueError("Сохранённая версия уже существует с другими данными")
            return False
        names = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        self._db.execute(f"INSERT INTO {table}({names}) VALUES({placeholders})", tuple(values.values()))
        return True

    def save_profile(self, user_id: int, profile: InvestorProfile) -> None:
        if profile.user_id != user_id or self.get_user(user_id) is None:
            raise ValueError("Профиль принадлежит другому владельцу или владелец отсутствует")
        with self._transaction():
            latest = self.get_profile(user_id)
            previous = self.get_profile(user_id, profile.revision)
            if previous is None and profile.revision != (latest.revision+1 if latest else 1):
                raise ValueError("Ревизия профиля изменилась, повторите подтверждение")
            self._insert_immutable("investor_profiles", {
                "user_id": user_id, "revision": profile.revision, "confirmed_at": _utc_text(profile.confirmed_at),
                "payload": data_json(profile),
            }, ("user_id", "revision"))

    def get_profile(self, user_id: int, revision: int | None = None) -> InvestorProfile | None:
        sql, arguments = "SELECT payload FROM investor_profiles WHERE user_id=?", [user_id]
        if revision is not None:
            sql += " AND revision=?"
            arguments.append(revision)
        row = self._db.execute(sql+" ORDER BY revision DESC LIMIT 1", arguments).fetchone()
        return InvestorProfile.from_dict(json.loads(row["payload"])) if row else None

    def save_historical_state(self, user_id: int, account_id: str, state: HistoricalState, inputs: dict) -> None:
        if (state.user_id, state.account_id) != (user_id, account_id) or self.get_user(user_id) is None:
            raise ValueError("Историческое состояние относится к другому владельцу или счёту")
        versions = state.coverage.source_versions
        if set(inputs) != set(versions) or any(inputs[key].get("version") != version for key, version in versions.items()):
            raise ValueError("Исходные данные не совпадают с манифестом состояния")
        with self._transaction():
            self._insert_immutable("historical_states", {
                "id": state.id, "user_id": user_id, "account_id": account_id,
                "effective_at": _utc_text(state.effective_at), "observed_at": _utc_text(state.observed_at),
                "payload": data_json(state),
            }, ("id",))
            for source_id, item in inputs.items():
                self._insert_immutable("historical_inputs", {
                    "state_id": state.id, "source_id": source_id, "source_version": item["version"],
                    "payload": data_json(item["payload"]),
                }, ("state_id", "source_id", "source_version"))

    def get_historical_state(self, user_id: int, account_id: str, state_id: str) -> HistoricalState | None:
        row = self._db.execute("SELECT payload FROM historical_states WHERE id=? AND user_id=? AND account_id=?",
                               (state_id, user_id, account_id)).fetchone()
        return HistoricalState.from_dict(json.loads(row["payload"])) if row else None

    def get_historical_inputs(self, user_id: int, account_id: str, state_id: str) -> dict:
        rows = self._db.execute(
            "SELECT i.source_id,i.source_version,i.payload FROM historical_inputs i "
            "JOIN historical_states s ON s.id=i.state_id WHERE s.id=? AND s.user_id=? AND s.account_id=?",
            (state_id, user_id, account_id),
        ).fetchall()
        return {row["source_id"]: {"version": row["source_version"], "payload": json.loads(row["payload"])} for row in rows}

    def save_historical_price(self, instrument_id: str, effective_at: datetime, source: str,
                              version: str, retrieved_at: datetime, payload: dict) -> None:
        with self._transaction():
            self._insert_immutable("historical_prices", {
                "instrument_id": instrument_id, "effective_at": _utc_text(effective_at), "source": source,
                "version": version, "retrieved_at": _utc_text(retrieved_at), "payload": data_json(payload),
            }, ("instrument_id", "effective_at", "source", "version"))

    def get_historical_prices(self, instrument_id: str, start: datetime, end: datetime) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM historical_prices WHERE instrument_id=? AND effective_at>=? AND effective_at<? "
            "ORDER BY effective_at,retrieved_at", (instrument_id, _utc_text(start), _utc_text(end)),
        ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def save_corporate_event(self, event: CorporateEvent) -> None:
        # Общий кэш содержит публичные события. Частные эффекты счёта входят
        # в historical_inputs, где чтение ограничено владельцем и счётом.
        if event.account_id is not None:
            raise ValueError("Частное событие сохраняется в исходных данных состояния счёта")
        with self._transaction():
            self._insert_immutable("corporate_events", {
                "id": event.id, "version": event.source_version, "instrument_id": event.instrument_id,
                "effective_at": _utc_text(event.effective_at),
                "available_at": _utc_text(event.available_at) if event.available_at else None, "payload": data_json(event),
            }, ("id", "version"))

    def get_corporate_events(self, instrument_id: str, start: datetime, end: datetime,
                             account_id: str | None = None, as_of: datetime | None = None) -> list[CorporateEvent]:
        sql = "SELECT payload FROM corporate_events WHERE instrument_id=? AND effective_at>=? AND effective_at<?"
        arguments = [instrument_id, _utc_text(start), _utc_text(end)]
        if as_of is not None:
            sql += " AND available_at<=?"
            arguments.append(_utc_text(as_of))
        rows = self._db.execute(sql+" ORDER BY effective_at,available_at", arguments).fetchall()
        events = [CorporateEvent.from_dict(json.loads(row["payload"])) for row in rows]
        return [event for event in events if event.account_id in {None, account_id}]

    def save_source_refresh(self, source: str, status: str, payload: dict, last_success_at: datetime | None = None,
                            retry_at: datetime | None = None, failures: int = 0) -> None:
        with self._transaction():
            self._db.execute(
                "INSERT INTO source_refreshes(source,status,payload,last_success_at,retry_at,failures) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET status=excluded.status,payload=excluded.payload,"
                "last_success_at=COALESCE(excluded.last_success_at,source_refreshes.last_success_at),"
                "retry_at=excluded.retry_at,failures=excluded.failures",
                (source, status, data_json(payload), _utc_text(last_success_at) if last_success_at else None,
                 _utc_text(retry_at) if retry_at else None, failures),
            )

    def get_source_refresh(self, source: str) -> dict | None:
        row = self._db.execute("SELECT * FROM source_refreshes WHERE source=?", (source,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def save_document(self, document: EvidenceDocument) -> None:
        with self._transaction():
            self._save_document(document)

    def _save_document(self, document: EvidenceDocument) -> None:
        self._insert_immutable("evidence_documents", {
            "id": document.id, "version": document.version, "source": document.source, "url": document.url,
            "retrieved_at": _utc_text(document.retrieved_at),
            "published_at": _utc_text(document.published_at) if document.published_at else None,
            "available_at": _utc_text(document.available_at) if document.available_at else None,
            "payload": data_json(document),
        }, ("id", "version"))

    def ingest_evidence(self, document: EvidenceDocument, canonical_url: str, fingerprint: str,
                        binding_confirmed_at: datetime | None = None) -> EvidenceDocument:
        """Атомарно объединить идентичность и добавить наблюдение исходного канала."""
        with self._transaction():
            row = self._db.execute("SELECT document_id FROM evidence_attributions WHERE canonical_url=? "
                                   "ORDER BY observed_at,document_id LIMIT 1", (canonical_url,)).fetchone()
            if row is None:
                row = self._db.execute("SELECT document_id FROM evidence_fingerprints WHERE fingerprint=? "
                                       "ORDER BY document_id LIMIT 1", (fingerprint,)).fetchone()
            identity = row["document_id"] if row else document.id
            prior = self.get_document(identity, document.version)
            versions = self.document_versions(identity)
            current = versions[-1] if versions else None
            fields = ("title", "content", "kind", "reporting_period", "published_at")
            if current and all(getattr(current, field) == getattr(document, field) for field in fields):
                prior = current
            elif prior and current and document.retrieved_at > current.retrieved_at:
                revision = sha256(data_json({"content_version": document.version, "after": current.version,
                                             "observed_at": document.retrieved_at}).encode()).hexdigest()
                document = replace(document, version=revision)
                prior = self.get_document(identity, revision)
            binding_at = binding_confirmed_at or document.retrieved_at
            initial_ids = document.instrument_ids if binding_at <= document.retrieved_at else []
            stored = prior or replace(document, id=identity, url=canonical_url, available_at=document.retrieved_at, instrument_ids=initial_ids)
            self._save_document(stored)
            self._db.execute("INSERT OR IGNORE INTO evidence_fingerprints VALUES(?,?,?)", (fingerprint, identity, stored.version))
            self._db.execute("INSERT OR IGNORE INTO evidence_attributions VALUES(?,?,?,?,?,?,?)",
                             (identity, stored.version, document.source, document.url, canonical_url,
                              _utc_text(document.retrieved_at), _utc_text(document.published_at) if document.published_at else None))
            self._db.executemany("INSERT OR IGNORE INTO evidence_instruments VALUES(?,?,?,?)",
                                 [(identity, stored.version, key, _utc_text(binding_at)) for key in set(document.instrument_ids)-{ "" }])
        return stored

    def evidence_instrument_time(self, document_id: str, version: str, instrument_id: str):
        row = self._db.execute("SELECT confirmed_at FROM evidence_instruments WHERE document_id=? AND document_version=? AND instrument_id=?",
                               (document_id, version, instrument_id)).fetchone()
        return timestamp(row["confirmed_at"]) if row else None

    def _document_at(self, payload: str, as_of: datetime) -> EvidenceDocument:
        document = EvidenceDocument.from_dict(json.loads(payload))
        rows = self._db.execute("SELECT instrument_id FROM evidence_instruments WHERE document_id=? AND document_version=? AND confirmed_at<=?",
                               (document.id, document.version, _utc_text(as_of))).fetchall()
        return replace(document, instrument_ids=sorted(set(document.instrument_ids) | {row["instrument_id"] for row in rows}))

    def evidence_attributions(self, document_id: str, version: str) -> list[dict]:
        rows = self._db.execute("SELECT source,original_url,canonical_url,observed_at,published_at "
                               "FROM evidence_attributions WHERE document_id=? AND document_version=? "
                               "ORDER BY observed_at,source,original_url", (document_id, version)).fetchall()
        return [dict(row) for row in rows]

    def document_versions(self, document_id: str) -> list[EvidenceDocument]:
        rows = self._db.execute("SELECT payload FROM evidence_documents WHERE id=? ORDER BY retrieved_at,rowid", (document_id,)).fetchall()
        return [EvidenceDocument.from_dict(json.loads(row["payload"])) for row in rows]

    def available_documents(self, instrument_ids: list[str], as_of: datetime) -> list[EvidenceDocument]:
        if not instrument_ids:
            return []
        at = _utc_text(as_of)
        placeholders = ",".join("?" for _ in instrument_ids)
        rows = self._db.execute(
            "SELECT d.payload FROM evidence_documents d WHERE d.available_at<=? "
            f"AND (EXISTS(SELECT 1 FROM json_each(d.payload,'$.instrument_ids') WHERE value IN ({placeholders})) "
            f"OR EXISTS(SELECT 1 FROM evidence_instruments i WHERE i.document_id=d.id AND i.document_version=d.version "
            f"AND i.instrument_id IN ({placeholders}) AND i.confirmed_at<=?)) "
            "AND d.version=(SELECT v.version FROM evidence_documents v WHERE v.id=d.id AND v.available_at<=? "
            "ORDER BY v.available_at DESC,v.retrieved_at DESC,v.version DESC LIMIT 1) ORDER BY d.available_at,d.id",
            (at, *instrument_ids, *instrument_ids, at, at)).fetchall()
        return [self._document_at(row["payload"], as_of) for row in rows]

    def get_document(self, document_id: str, version: str) -> EvidenceDocument | None:
        row = self._db.execute("SELECT payload FROM evidence_documents WHERE id=? AND version=?",
                               (document_id, version)).fetchone()
        return EvidenceDocument.from_dict(json.loads(row["payload"])) if row else None

    def search_evidence(self, match: str, instrument_ids: list[str], as_of: datetime, limit: int = 8) -> list[dict]:
        if not match.strip() or not instrument_ids or not 1 <= limit <= 100:
            return []
        at = _utc_text(as_of)
        placeholders = ",".join("?" for _ in instrument_ids)
        rows = self._db.execute(
            "SELECT c.payload AS chunk_payload,d.payload AS document_payload,bm25(evidence_search) AS score "
            "FROM evidence_search JOIN evidence_chunks c ON c.id=evidence_search.chunk_id "
            "JOIN evidence_documents d ON d.id=c.document_id AND d.version=c.document_version "
            "WHERE evidence_search MATCH ? AND d.available_at<=? "
            f"AND (EXISTS(SELECT 1 FROM json_each(d.payload,'$.instrument_ids') WHERE value IN ({placeholders})) "
            f"OR EXISTS(SELECT 1 FROM evidence_instruments i WHERE i.document_id=d.id AND i.document_version=d.version "
            f"AND i.instrument_id IN ({placeholders}) AND i.confirmed_at<=?)) "
            "AND d.version=(SELECT v.version FROM evidence_documents v WHERE v.id=d.id AND v.available_at<=? "
            "ORDER BY v.available_at DESC,v.retrieved_at DESC,v.version DESC LIMIT 1) "
            "ORDER BY score,c.id LIMIT ?", (match, at, *instrument_ids, *instrument_ids, at, at, limit)).fetchall()
        return [{"chunk": EvidenceChunk.from_dict(json.loads(row["chunk_payload"])),
                 "document": self._document_at(row["document_payload"], as_of), "score": row["score"], "as_of": timestamp(as_of)} for row in rows]

    def save_evidence_chunk(self, chunk: EvidenceChunk, normalized_text: str) -> None:
        if self.get_document(chunk.document_id, chunk.document_version) is None:
            raise ValueError("Версия исходного документа отсутствует")
        with self._transaction():
            inserted = self._insert_immutable("evidence_chunks", {
                "id": chunk.id, "document_id": chunk.document_id, "document_version": chunk.document_version,
                "payload": data_json(chunk),
            }, ("id",))
            if inserted:
                self._db.execute("INSERT INTO evidence_search(chunk_id,document_id,document_version,normalized_text) "
                                 "VALUES(?,?,?,?)", (chunk.id, chunk.document_id, chunk.document_version, normalized_text))

    def get_evidence_chunk(self, chunk_id: str) -> EvidenceChunk | None:
        row = self._db.execute("SELECT payload FROM evidence_chunks WHERE id=?", (chunk_id,)).fetchone()
        return EvidenceChunk.from_dict(json.loads(row["payload"])) if row else None

    def save_financial_fact(self, fact: FinancialFact) -> None:
        chunk = self.get_evidence_chunk(fact.chunk_id)
        document = self.get_document(fact.document_id, fact.document_version)
        if chunk is None or document is None or (chunk.document_id, chunk.document_version) != (fact.document_id, fact.document_version):
            raise ValueError("Показатель не связан с указанным документом и фрагментом")
        if document.available_at and (fact.available_at is None or fact.available_at < document.available_at):
            raise ValueError("Дата доступности показателя предшествует документу")
        with self._transaction():
            self._insert_immutable("financial_facts", {
                "id": fact.id, "document_version": fact.document_version, "instrument_id": fact.instrument_id,
                "metric": fact.metric, "available_at": _utc_text(fact.available_at) if fact.available_at else None,
                "payload": data_json(fact),
            }, ("id", "document_version"))

    def get_financial_facts(self, instrument_id: str, as_of: datetime) -> list[FinancialFact]:
        at = _utc_text(as_of)
        rows = self._db.execute(
            "SELECT f.payload FROM financial_facts f JOIN evidence_documents d "
            "ON d.id=json_extract(f.payload,'$.document_id') AND d.version=f.document_version "
            "WHERE f.instrument_id=? AND f.available_at<=? AND d.available_at<=? "
            "AND d.version=(SELECT v.version FROM evidence_documents v WHERE v.id=d.id AND v.available_at<=? "
            "ORDER BY v.available_at DESC,v.retrieved_at DESC,v.version DESC LIMIT 1) ORDER BY f.available_at",
            (instrument_id, at, at, at)).fetchall()
        return [FinancialFact.from_dict(json.loads(row["payload"])) for row in rows]

    def save_forecast_model(self, version: str, created_at: datetime, trained_until: datetime, payload: dict) -> None:
        if timestamp(trained_until) > timestamp(created_at):
            raise ValueError("Обучение использует будущие данные")
        with self._transaction():
            self._insert_immutable("forecast_models", {
                "version": version, "created_at": _utc_text(created_at), "trained_until": _utc_text(trained_until),
                "payload": data_json(payload),
            }, ("version",))

    def save_universe(self, user_id: int, account_id: str, universe: StockUniverse) -> None:
        if self.get_user(user_id) is None or (universe.user_id, universe.account_id) != (user_id, account_id):
            raise ValueError("Выборка принадлежит другому владельцу или счёту")
        for entry in universe.entries:
            if entry.series and not {"daily_series", "daily_end"}.issubset(entry.source_versions):
                raise ValueError("У дневной истории отсутствует неизменяемая версия")
            if any(item.available_at > universe.as_of for item in entry.series):
                raise ValueError("Дневная история содержит сведения после даты выборки")
            if entry.series:
                end = timestamp(entry.source_versions["daily_end"])
                records = self.get_historical_prices(entry.instrument_id, end, end+timedelta(microseconds=1))
                record = next((item["payload"] for item in records if item["source"] == "mcp-daily-series-v1"
                               and item["version"] == entry.source_versions["daily_series"]), None)
                if record is None or data_json(record["items"]) != data_json(entry.series):
                    raise ValueError("Дневная история не соответствует сохранённой версии")
        saved = replace(universe, entries=[replace(entry, series=[]) for entry in universe.entries])
        with self._transaction():
            self._insert_immutable("stock_universes", {"id": universe.id, "user_id": user_id, "account_id": account_id,
                                                       "as_of": _utc_text(universe.as_of), "methodology": universe.methodology,
                                                       "payload": data_json(saved)}, ("id",))

    def get_universe(self, user_id: int, account_id: str, universe_id: str) -> StockUniverse | None:
        row = self._db.execute("SELECT payload FROM stock_universes WHERE id=? AND user_id=? AND account_id=?",
                               (universe_id, user_id, account_id)).fetchone()
        if row is None:
            return None
        universe = StockUniverse.from_dict(json.loads(row["payload"]))
        for entry in universe.entries:
            if "daily_series" not in entry.source_versions:
                continue
            end = timestamp(entry.source_versions["daily_end"])
            rows = self.get_historical_prices(entry.instrument_id, end, end+timedelta(microseconds=1))
            record = next((item["payload"] for item in rows if item["source"] == "mcp-daily-series-v1"
                           and item["version"] == entry.source_versions["daily_series"]), None)
            if record is None:
                raise ValueError("Версия дневного ряда выборки недоступна")
            entry.series = [DailyObservation.from_dict(item) for item in record["items"]]
        return universe

    def save_broker_metadata(self, record: BrokerMetadataRecord) -> BrokerMetadataRecord:
        with self._transaction():
            current = self.get_broker_metadata(record.tool, record.identifier)
            if current and current.payload == record.payload:
                return current
            existing = self.get_broker_metadata(record.tool, record.identifier, record.version)
            if existing:
                if existing.payload != record.payload:
                    raise ValueError("Версия справочника уже существует с другими данными")
                record = replace(record, version=sha256(data_json({"content_version": record.version, "after": current.version,
                                                                   "observed_at": record.observed_at}).encode()).hexdigest())
            self._insert_immutable("broker_metadata", {"tool": record.tool, "identifier": record.identifier, "version": record.version,
                                                       "observed_at": _utc_text(record.observed_at), "payload": data_json(record)},
                                   ("tool", "identifier", "version"))
        return record

    def get_broker_metadata(self, tool: str, identifier: str, version: str | None = None, as_of: datetime | None = None):
        query = "SELECT payload FROM broker_metadata WHERE tool=? AND identifier=?"
        args = [tool, identifier]
        if version is not None:
            query += " AND version=?"
            args.append(version)
        if as_of is not None:
            query += " AND observed_at<=?"
            args.append(_utc_text(as_of))
        row = self._db.execute(query+" ORDER BY observed_at DESC,rowid DESC LIMIT 1", args).fetchone()
        return BrokerMetadataRecord.from_dict(json.loads(row["payload"])) if row else None

    def get_forecast_model(self, version: str) -> dict | None:
        row = self._db.execute("SELECT * FROM forecast_models WHERE version=?", (version,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def save_evaluation_dataset(self, version: str, scenarios: list[dict]) -> None:
        if len({item["id"] for item in scenarios}) != len(scenarios):
            raise ValueError("Сценарии повторяются")
        with self._transaction():
            for item in scenarios:
                self._insert_immutable("evaluation_scenarios", {"dataset_version": version, "id": item["id"],
                    "as_of": _utc_text(timestamp(item["as_of"])), "payload": data_json(item)}, ("dataset_version", "id"))

    def save_evaluation_run(self, identity: str, dataset_version: str, created_at: datetime, payload: dict):
        rows = self._db.execute("SELECT id FROM evaluation_scenarios WHERE dataset_version=?", (dataset_version,)).fetchall()
        if not rows or set(payload.get("scenario_ids", [])) != {row["id"] for row in rows}:
            raise ValueError("Результаты не соответствуют зафиксированному набору")
        with self._transaction():
            self._insert_immutable("evaluation_runs", {"id": identity, "dataset_version": dataset_version,
                "created_at": _utc_text(created_at), "payload": data_json(payload)}, ("id",))

    def get_evaluation_run(self, identity: str):
        row = self._db.execute("SELECT * FROM evaluation_runs WHERE id=?", (identity,)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def save_expert_review(self, review):
        run = self.get_evaluation_run(review.run_id)
        if not run or review.proposal_id not in {item["id"] for item in run["payload"].get("proposals", [])}:
            raise ValueError("Предложение отсутствует в эксперименте")
        if review.reviewed_at < timestamp(run["created_at"]):
            raise ValueError("Экспертная оценка предшествует эксперименту")
        with self._transaction():
            rows = self._db.execute("SELECT revision,payload FROM expert_reviews WHERE run_id=? AND proposal_id=? AND reviewer=? ORDER BY revision DESC LIMIT 1",
                                   (review.run_id, review.proposal_id, review.reviewer)).fetchone()
            if rows and rows["revision"] == review.revision and rows["payload"] == data_json(review):
                return
            if review.revision != (rows["revision"]+1 if rows else 1):
                raise ValueError("Ревизия экспертной оценки изменилась")
            self._insert_immutable("expert_reviews", {"run_id": review.run_id, "proposal_id": review.proposal_id,
                "reviewer": review.reviewer, "revision": review.revision, "payload": data_json(review)},
                ("run_id", "proposal_id", "reviewer", "revision"))

    def get_expert_reviews(self, run_id: str):
        from .recommendation_evaluation import ExpertReview
        rows = self._db.execute("SELECT payload FROM expert_reviews WHERE run_id=? ORDER BY proposal_id,reviewer,revision", (run_id,)).fetchall()
        return [ExpertReview.from_dict(json.loads(row["payload"])) for row in rows]

    def save_forecast_observation(self, user_id, account_id, forecast_id, observed_at, payload):
        if self.get_forecast(user_id, account_id, forecast_id) is None or payload.get("forecast_id") != forecast_id:
            raise ValueError("Прогноз недоступен для выбранного счёта")
        with self._transaction():
            self._insert_immutable("forecast_observations", {"forecast_id": forecast_id, "observed_at": _utc_text(observed_at),
                "payload": data_json(payload)}, ("forecast_id", "observed_at"))

    def latest_forecast_observation(self, user_id, account_id, forecast_id):
        if self.get_forecast(user_id, account_id, forecast_id) is None:
            return None
        row = self._db.execute("SELECT payload FROM forecast_observations WHERE forecast_id=? ORDER BY observed_at DESC LIMIT 1", (forecast_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_screening(self, user_id: int, account_id: str, screening: dict) -> None:
        if (screening.get("user_id"), screening.get("account_id")) != (user_id, account_id):
            raise ValueError("Подборка принадлежит другому владельцу или счёту")
        if self.get_universe(user_id, account_id, screening["universe_id"]) is None:
            raise ValueError("Выборка подборки недоступна")
        if screening.get("mode") not in {"fundamental", "technical", "combined"}:
            raise ValueError("Неизвестный режим подборки")
        with self._transaction():
            self._insert_immutable("stock_screenings", {"id": screening["id"], "user_id": user_id, "account_id": account_id,
                "as_of": _utc_text(timestamp(screening["as_of"])), "universe_id": screening["universe_id"],
                "mode": screening["mode"], "payload": data_json(screening)}, ("id",))

    def get_screening(self, user_id: int, account_id: str, screening_id: str):
        row = self._db.execute("SELECT payload FROM stock_screenings WHERE id=? AND user_id=? AND account_id=?",
                              (screening_id, user_id, account_id)).fetchone()
        return json.loads(row["payload"]) if row else None

    def screening_history(self, user_id: int, account_id: str, limit: int = 10):
        rows = self._db.execute("SELECT id,created_at,manifest,payload FROM report_runs WHERE user_id=? AND account_id=? "
                               "AND report_kind='screening' AND state='delivered' ORDER BY created_at DESC,rowid DESC LIMIT ?",
                               (user_id, account_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def save_forecast(self, user_id: int, account_id: str, forecast: GrowthForecast) -> None:
        model = self.get_forecast_model(forecast.model_version)
        if self.get_user(user_id) is None or not account_id or model is None:
            raise ValueError("Владелец, счёт или версия модели отсутствует")
        if timestamp(model["trained_until"]) > forecast.as_of or timestamp(model["created_at"]) > forecast.as_of:
            raise ValueError("Версия модели недоступна на дату прогноза")
        with self._transaction():
            self._insert_immutable("growth_forecasts", {
                "id": forecast.id, "user_id": user_id, "account_id": account_id, "instrument_id": forecast.instrument_id,
                "as_of": _utc_text(forecast.as_of), "target_at": _utc_text(forecast.target_at),
                "model_version": forecast.model_version, "payload": data_json(forecast),
            }, ("id",))

    def get_forecast(self, user_id: int, account_id: str, forecast_id: str) -> GrowthForecast | None:
        row = self._db.execute("SELECT payload FROM growth_forecasts WHERE id=? AND user_id=? AND account_id=?",
                               (forecast_id, user_id, account_id)).fetchone()
        return GrowthForecast.from_dict(json.loads(row["payload"])) if row else None

    def save_advice(self, user_id: int, account_id: str, proposal: AdviceProposal) -> None:
        with self._transaction():
            self._save_advice(user_id, account_id, proposal)

    def _save_advice(self, user_id: int, account_id: str, proposal: AdviceProposal) -> None:
        if (proposal.user_id, proposal.account_id) != (user_id, account_id):
            raise ValueError("Предложение принадлежит другому владельцу или счёту")
        if self.get_profile(user_id, proposal.profile_revision) is None:
            raise ValueError("Подтверждённая версия профиля отсутствует")
        if any(self.get_evidence_chunk(key) is None for key in proposal.evidence_ids):
            raise ValueError("Доказательства предложения отсутствуют")
        if proposal.report_id:
            run = self.get_run(user_id, proposal.report_id)
            if run is None or run["account_id"] != account_id or run["profile_revision"] != proposal.profile_revision:
                raise ValueError("Предложение не связано с выполнением выбранного счёта и профиля")
        self._insert_immutable("advice_proposals", {
            "id": proposal.id, "user_id": user_id, "account_id": account_id,
            "profile_revision": proposal.profile_revision, "created_at": _utc_text(proposal.created_at),
            "payload": data_json(proposal), "report_id": proposal.report_id,
        }, ("id",))

    def get_advice(self, user_id: int, account_id: str, proposal_id: str) -> AdviceProposal | None:
        row = self._db.execute("SELECT payload FROM advice_proposals WHERE id=? AND user_id=? AND account_id=?",
                               (proposal_id, user_id, account_id)).fetchone()
        proposal = AdviceProposal.from_dict(json.loads(row["payload"])) if row else None
        if proposal and proposal.report_id:
            run = self.get_run(user_id, proposal.report_id)
            if run is None or run["state"] != "delivered" or run["account_id"] != account_id:
                return None
        return proposal

    def list_advice(self, user_id: int, account_id: str, limit: int = 10) -> list[AdviceProposal]:
        rows = self._db.execute(
            "SELECT a.payload FROM advice_proposals a LEFT JOIN report_runs r ON r.id=a.report_id AND r.user_id=a.user_id "
            "AND r.account_id=a.account_id WHERE a.user_id=? AND a.account_id=? AND (a.report_id='' OR r.state='delivered') "
            "ORDER BY a.created_at DESC,a.rowid DESC LIMIT ?", (user_id, account_id, limit)).fetchall()
        return [AdviceProposal.from_dict(json.loads(row["payload"])) for row in rows]

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        with self._transaction():
            self._insert_immutable("portfolio_snapshots", {"id": snapshot.id, "user_id": snapshot.user_id,
                "account_id": snapshot.account_id, "observed_at": _utc_text(snapshot.observed_at),
                "currency": snapshot.currency, "payload": data_json(snapshot)}, ("id",))

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
            "WHERE r.user_id=? AND r.account_id=? AND r.origin=? AND r.state='delivered' AND r.report_kind='analysis' "
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

    def save_schedule(self, user_id: int, account_id: str, rule: dict, next_run_at: datetime,
                      analysis_policy=None, sections=None) -> dict:
        analysis_policy = {"kind": "legacy_delivered"} if analysis_policy is None else analysis_policy
        sections = ["analysis"] if sections is None else sections
        if analysis_policy.get("kind") not in {"rolling", "legacy_delivered"}:
            raise ValueError("Неизвестная политика периода")
        if analysis_policy["kind"] == "rolling" and (type(analysis_policy.get("days")) is not int or analysis_policy["days"] <= 0):
            raise ValueError("Укажите положительное число дней")
        if "analysis" not in sections or set(sections)-{"analysis", "advice", "screening"} or len(set(sections)) != len(sections):
            raise ValueError("Неизвестные или повторяющиеся разделы отчёта")
        with self._transaction():
            credentials = self.get_credentials(user_id)
            if credentials is None or credentials[1] != account_id:
                raise ValueError("Выбранный счёт изменился, откройте настройку заново")
            self._db.execute(
                "INSERT INTO report_schedules(user_id,account_id,rule,enabled,revision,next_run_at,analysis_policy,sections) "
                "VALUES(?,?,?,1,1,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "account_id=excluded.account_id,rule=excluded.rule,enabled=1,revision=revision+1,"
                "next_run_at=excluded.next_run_at,pause_reason='',analysis_policy=excluded.analysis_policy,sections=excluded.sections",
                (user_id, account_id, data_json(rule), _utc_text(next_run_at), data_json(analysis_policy), data_json(sections)),
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
                    revision: int, scheduled_at: datetime | None, now: datetime) -> bool:
        profile = self.get_profile(user_id)
        inserted = self._db.execute(
            "INSERT OR IGNORE INTO report_runs(id,user_id,account_id,origin,request_key,revision,scheduled_at,"
            "created_at,updated_at,profile_revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uuid4().hex, user_id, account_id, origin, request_key, revision,
             _utc_text(scheduled_at) if scheduled_at else None, _utc_text(now), _utc_text(now), profile.revision if profile else 0),
        )
        return inserted.rowcount > 0

    def create_run(self, user_id: int, account_id: str, origin: str, request_key: str,
                   now: datetime, revision: int = 0, scheduled_at: datetime | None = None,
                   period=None, period_request: dict | None = None, report_kind: str = "analysis", manifest: dict | None = None) -> dict:
        if origin not in {"manual", "scheduled"}:
            raise ValueError("Неизвестный источник отчёта")
        if report_kind not in {"analysis", "screening"}:
            raise ValueError("Неизвестный вид отчёта")
        with self._transaction():
            inserted = self._insert_run(user_id, account_id, origin, request_key, revision, scheduled_at, now)
            if inserted:
                self._db.execute("UPDATE report_runs SET report_kind=?,manifest=? WHERE user_id=? AND origin=? AND request_key=?",
                                 (report_kind, data_json(manifest or {}), user_id, origin, request_key))
            if inserted and period is not None:
                self._db.execute("UPDATE report_runs SET resolved_period=?,period_request=? "
                                 "WHERE user_id=? AND origin=? AND request_key=? AND resolved_period IS NULL AND state='queued'",
                                 (data_json(period), data_json(period_request or {}), user_id, origin, request_key))
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
        row = self._db.execute("SELECT * FROM report_runs WHERE user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
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

    def prepare_run(self, user_id: int, run_id: str, owner: str, snapshot_id: str | None,
                    baseline_id: str | None, payload: dict, parts: list[str], now: datetime,
                    proposals: list[AdviceProposal] | None = None, manifest: dict | None = None) -> None:
        with self._transaction():
            self._require_lease(user_id, run_id, owner, now)
            row = self.get_run(user_id, run_id)
            if row["report_kind"] == "screening":
                screening_id = payload.get("screening", {}).get("id")
                if snapshot_id is not None or baseline_id is not None or self.get_screening(user_id, row["account_id"], screening_id) is None:
                    raise ValueError("Сохранённая подборка недоступна")
            elif self.get_snapshot(user_id, row["account_id"], snapshot_id) is None:
                raise ValueError("Снимок отчёта недоступен")
            if baseline_id and self.get_snapshot(user_id, row["account_id"], baseline_id) is None:
                raise ValueError("Точка сравнения недоступна")
            if row["payload"] is not None or not parts:
                raise ValueError("Отчёт уже подготовлен или пуст")
            self._db.execute("UPDATE report_runs SET snapshot_id=?,baseline_id=?,payload=?,updated_at=? WHERE id=?",
                             (snapshot_id, baseline_id, data_json(payload), _utc_text(now), run_id))
            if manifest is not None:
                versions = manifest.get("model_versions", [])
                self._db.execute("UPDATE report_runs SET manifest=?,model_version=? WHERE id=?",
                                 (data_json(manifest), versions[0] if len(versions) == 1 else None, run_id))
            self._db.executemany("INSERT INTO report_parts(run_id,number,text) VALUES(?,?,?)",
                                 [(run_id, index, text) for index, text in enumerate(parts, 1)])
            for proposal in proposals or []:
                if proposal.report_id != run_id:
                    raise ValueError("Предложение относится к другому выполнению")
                self._save_advice(user_id, row["account_id"], proposal)

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

    def enqueue_schedule(self, schedule: dict, next_run_at: datetime, now: datetime, planned_at=None) -> None:
        with self._transaction():
            current = self.get_schedule(schedule["user_id"])
            if current is None or not current["enabled"] or current["revision"] != schedule["revision"]:
                return
            if current["next_run_at"] > now:
                return
            pending = self._db.execute("SELECT 1 FROM report_runs WHERE user_id=? AND origin='scheduled' "
                                       "AND state='queued' AND revision=?", (current["user_id"], current["revision"])).fetchone()
            if not pending:
                policy = json.loads(current["analysis_policy"])
                nominal = (planned_at or current["next_run_at"]) if policy["kind"] == "rolling" else current["next_run_at"]
                key = f"{current['revision']}:{_utc_text(nominal)}"
                inserted = self._insert_run(current["user_id"], current["account_id"], "scheduled", key, current["revision"], nominal, now)
                if inserted:
                    period, request = None, policy
                    if policy["kind"] == "rolling":
                        from .analysis_periods import rolling_period
                        zone = current["rule"]["timezone"]
                        period = rolling_period(policy["days"], zone, nominal)
                        request = {**policy, "zone": zone}
                    self._db.execute("UPDATE report_runs SET period_request=?,resolved_period=?,manifest=? WHERE user_id=? AND origin='scheduled' AND request_key=?",
                        (data_json(request), data_json(period) if period else None, data_json({"sections": json.loads(current["sections"]), "mode": "combined"}), current["user_id"], key))
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
        with self._transaction():
            return self._insert_decision(draft, entry_price, created_at)

    def _insert_decision(self, draft: DecisionDraft, entry_price: float, created_at: datetime) -> Decision:
        cur = self._db.execute(
            "INSERT INTO decisions (user_id, kind, ticker, hypothesis, entry_price, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (draft.user_id, draft.kind, draft.ticker.upper(), draft.hypothesis, entry_price, created_at.isoformat()),
        )
        return Decision(
            id=cur.lastrowid,
            user_id=draft.user_id,
            kind=draft.kind,
            ticker=draft.ticker.upper(),
            hypothesis=draft.hypothesis,
            entry_price=entry_price,
            created_at=created_at,
        )

    def linked_decision(self, user_id: int, account_id: str, request_key: str) -> Decision | None:
        row = self._db.execute("SELECT decision_id FROM hypothesis_evidence WHERE user_id=? AND account_id=? AND request_key=?",
                               (user_id, account_id, request_key)).fetchone()
        return self.get_decision(user_id, row["decision_id"]) if row else None

    def add_advice_decision(self, draft: DecisionDraft, entry_price: float, account_id: str,
                            proposal_id: str, request_key: str, created_at: datetime | None = None) -> Decision:
        if not request_key:
            raise ValueError("Не задан ключ подтверждения гипотезы")
        with self._transaction():
            previous = self.linked_decision(draft.user_id, account_id, request_key)
            if previous:
                return previous
            proposal = self.get_advice(draft.user_id, account_id, proposal_id)
            credentials = self.get_credentials(draft.user_id)
            if proposal is None or credentials is None or credentials[1] != account_id:
                raise ValueError("Предложение недоступно для выбранного счёта")
            proof = {"proposal": json.loads(data_json(proposal)), "metrics": {}, "evidence": []}
            if proposal.report_id:
                run = self.get_run(draft.user_id, proposal.report_id)
                context = json.loads(run["payload"])["advice"]["context"]
                proof["metrics"] = {key: context["metrics"][key] for key in proposal.metric_ids}
            for key in proposal.evidence_ids:
                chunk = self.get_evidence_chunk(key)
                document = self.get_document(chunk.document_id, chunk.document_version)
                proof["evidence"].append({"chunk": json.loads(data_json(chunk)), "url": document.url,
                                          "source": document.source, "version": document.version})
            decision = self._insert_decision(draft, entry_price, created_at or _now())
            self._db.execute("INSERT INTO hypothesis_evidence VALUES(?,?,?,?,?,?,?)",
                             (decision.id, draft.user_id, account_id, "advice", proposal.id, data_json(proof), request_key))
            return decision

    def hypothesis_sources(self, user_id: int, account_id: str, decision_id: int) -> list[dict]:
        rows = self._db.execute("SELECT h.source_kind,h.source_id,h.payload FROM hypothesis_evidence h "
                               "JOIN decisions d ON d.id=h.decision_id AND d.user_id=h.user_id "
                               "WHERE h.user_id=? AND h.account_id=? AND h.decision_id=?",
                               (user_id, account_id, decision_id)).fetchall()
        return [{"kind": row["source_kind"], "id": row["source_id"], "proof": json.loads(row["payload"])} for row in rows]

    def add_screening_decision(self, draft, entry_price, account_id, screening_id, request_key, created_at=None):
        if not request_key:
            raise ValueError("Не задан ключ подтверждения гипотезы")
        with self._transaction():
            previous = self.linked_decision(draft.user_id, account_id, request_key)
            if previous:
                return previous
            screening = self.get_screening(draft.user_id, account_id, screening_id)
            credentials = self.get_credentials(draft.user_id)
            if screening is None or credentials is None or credentials[1] != account_id:
                raise ValueError("Подборка недоступна для выбранного счёта")
            candidate = next((item for item in screening["candidates"] if item["entry"]["ticker"].upper() == draft.ticker.upper()), None)
            if candidate is None:
                raise ValueError("Бумага отсутствует среди кандидатов исходной подборки")
            proof = {"screening_id": screening_id, "as_of": screening["as_of"], "universe_id": screening["universe_id"],
                     "profile_revision": screening["profile_revision"], "mode": screening["mode"], "candidate": candidate}
            decision = self._insert_decision(draft, entry_price, created_at or _now())
            self._db.execute("INSERT INTO hypothesis_evidence VALUES(?,?,?,?,?,?,?)",
                             (decision.id, draft.user_id, account_id, "screening", screening_id, data_json(proof), request_key))
            return decision

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
