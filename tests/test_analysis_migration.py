"""Миграция аналитики сохраняет данные версии 1 и полностью откатывает сбой."""

from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import _support  # noqa: F401
from investment_journal import storage
from investment_journal.storage import JournalRepository


def create_version_one(path):
    with closing(sqlite3.connect(path)) as db:
        db.executescript(storage.SCHEMA + storage.PORTFOLIO_SCHEMA + "\nPRAGMA user_version=1;")
        db.execute("INSERT INTO users VALUES(1,7)")
        db.execute("INSERT INTO credentials VALUES(1,?,'account','2026-11-01')", (b"encrypted-test-token",))
        db.execute("INSERT INTO decisions VALUES(1,1,'hold','TEST','Keep hypothesis',100,'2026-11-01',NULL,NULL)")
        db.execute("INSERT INTO report_schedules VALUES(1,'account','{}',1,3,'2026-11-20T00:00:00Z','')")
        db.execute("INSERT INTO report_runs(id,user_id,account_id,origin,request_key,state,attempts,payload,created_at,updated_at) "
                   "VALUES('run',1,'account','manual','message','retry',2,'{}','2026-11-01','2026-11-01')")
        db.execute("INSERT INTO report_parts VALUES('run',1,'Saved part','sent',42)")
        db.commit()


class AnalysisMigrationTests(unittest.TestCase):
    def test_migrate_copy_of_version_one_and_reopen_without_changing_old_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / "original.db"
            target = Path(temporary) / "copy.db"
            create_version_one(original)
            shutil.copyfile(original, target)
            for _ in range(2):
                repo = JournalRepository(target)
                try:
                    self.assertEqual(repo._db.execute("PRAGMA user_version").fetchone()[0], 2)
                    self.assertEqual(repo.get_credentials(1), (b"encrypted-test-token", "account"))
                    self.assertEqual(repo.list_decisions(1)[0].hypothesis, "Keep hypothesis")
                    run = repo.get_run(1, "run")
                    self.assertEqual((run["state"], run["attempts"], run["payload"]), ("retry", 2, "{}"))
                    self.assertEqual((run["report_kind"], run["manifest"], run["resolved_period"]), ("analysis", "{}", None))
                    schedule = repo.get_schedule(1)
                    self.assertEqual(schedule["analysis_policy"], '{"kind":"legacy_delivered"}')
                    self.assertEqual((schedule["enabled"], schedule["revision"]), (1, 3))
                    part = repo._db.execute("SELECT * FROM report_parts WHERE run_id='run'").fetchone()
                    self.assertEqual((part["state"], part["message_id"]), ("sent", 42))
                finally:
                    repo.close()
            with closing(sqlite3.connect(original)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_failed_second_migration_rolls_back_columns_tables_fts_and_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "journal.db"
            create_version_one(path)
            with patch.object(storage, "ANALYSIS_SCHEMA", storage.ANALYSIS_SCHEMA + "\nINVALID SQL;"):
                with self.assertRaises(sqlite3.OperationalError):
                    JournalRepository(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
                columns = {row[1] for row in db.execute("PRAGMA table_info(report_runs)")}
                self.assertNotIn("report_kind", columns)
                self.assertEqual(db.execute("SELECT count(*) FROM report_parts").fetchone()[0], 1)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='investor_profiles'").fetchone())
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'evidence_search%'").fetchone())
            repo = JournalRepository(path)
            repo.close()

    def test_new_database_has_search_and_analytical_tables(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        tables = {row[0] for row in repo._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"investor_profiles", "historical_states", "historical_inputs", "historical_prices",
                         "corporate_events", "evidence_documents", "evidence_chunks", "financial_facts",
                         "stock_screenings", "forecast_models", "growth_forecasts", "advice_proposals",
                         "evaluation_scenarios", "expert_reviews", "forecast_observations"}.issubset(tables))
        repo._db.execute("INSERT INTO evidence_search(chunk_id,document_id,document_version,normalized_text) "
                         "VALUES('c','d','v','Прибыль эмитента выросла')")
        self.assertEqual(repo._db.execute("SELECT chunk_id FROM evidence_search WHERE evidence_search MATCH 'эмитента'")
                         .fetchone()[0], "c")

    def test_newer_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "journal.db"
            create_version_one(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA user_version=3")
            with self.assertRaises(ValueError):
                JournalRepository(path)
