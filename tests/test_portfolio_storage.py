"""Миграция и восстановление истории портфеля и доставки."""

from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import _support  # noqa: F401
from investment_journal import storage
from investment_journal.storage import JournalRepository, LeaseLost, SCHEMA
from investment_journal.market import snapshot_from
from investment_journal.portfolio_sources import operation_from
from test_portfolio_market import NOW, portfolio_response
from test_portfolio_sources import operation


class SnapshotStorageTests(unittest.TestCase):
    def test_snapshot_reopen_and_owner_account_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "journal.db"
            snapshot = snapshot_from(portfolio_response(), 1, "account", NOW)
            repo = JournalRepository(path)
            repo.save_snapshot(snapshot)
            repo.close()
            repo = JournalRepository(path)
            self.assertEqual(repo.get_snapshot(1, "account", snapshot.id), snapshot)
            self.assertIsNone(repo.get_snapshot(2, "account", snapshot.id))
            self.assertIsNone(repo.get_snapshot(1, "other", snapshot.id))
            repo.close()

    def test_operations_upsert_state_and_scoped_period(self):
        repo = JournalRepository()
        item = operation_from(operation())
        repo.save_operations(1, "account", [item, item])
        repo.save_operations(1, "account", [replace(item, state="OPERATION_STATE_CANCELED")])
        repo.save_operations(2, "account", [item])
        repo.save_operations(1, "other", [item])
        result = repo.list_operations(1, "account", NOW-timedelta(days=1), NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].state, "OPERATION_STATE_CANCELED")
        self.assertEqual(repo.list_operations(1, "account", NOW, NOW+timedelta(days=1)), [])
        repo.close()


class RunStorageTests(unittest.TestCase):
    def setUp(self):
        self.repo = JournalRepository()
        self.user = self.repo.get_or_create_user(7)
        self.repo.save_credentials(self.user.id, b"encrypted", "account")
        self.snapshot = snapshot_from(portfolio_response(), self.user.id, "account", NOW)
        self.repo.save_snapshot(self.snapshot)

    def tearDown(self):
        self.repo.close()

    def deliver(self, snapshot, origin):
        self.repo.save_snapshot(snapshot)
        run = self.repo.create_run(self.user.id, "account", origin, snapshot.id, snapshot.observed_at)
        self.repo.claim_run(self.user.id, run["id"], "worker", snapshot.observed_at)
        self.repo.prepare_run(self.user.id, run["id"], "worker", snapshot.id, None, {}, ["one"], snapshot.observed_at)
        self.repo.set_part_state(self.user.id, run["id"], "worker", 1, "sending", snapshot.observed_at)
        self.repo.set_part_state(self.user.id, run["id"], "worker", 1, "sent", snapshot.observed_at, 42)
        self.repo.finish_run(self.user.id, run["id"], "worker", snapshot.observed_at)

    def test_baselines_separate_origins_and_require_full_delivery(self):
        self.assertIsNone(self.repo.baseline_snapshot(self.user.id, "account", "manual", NOW))
        scheduled = replace(self.snapshot, id="scheduled", observed_at=NOW+timedelta(hours=1))
        manual = replace(self.snapshot, id="manual", observed_at=NOW+timedelta(hours=2))
        self.deliver(scheduled, "scheduled")
        self.deliver(manual, "manual")
        later = NOW+timedelta(hours=3)
        self.assertEqual(self.repo.baseline_snapshot(self.user.id, "account", "scheduled", later), scheduled)
        self.assertEqual(self.repo.baseline_snapshot(self.user.id, "account", "manual", later), manual)
        pending = replace(self.snapshot, id="pending", observed_at=later)
        self.repo.save_snapshot(pending)
        run = self.repo.create_run(self.user.id, "account", "scheduled", "pending", later)
        self.repo.claim_run(self.user.id, run["id"], "worker", later)
        self.repo.prepare_run(self.user.id, run["id"], "worker", pending.id, scheduled.id, {}, ["one", "two"], later)
        self.repo.set_part_state(self.user.id, run["id"], "worker", 1, "sending", later)
        self.repo.set_part_state(self.user.id, run["id"], "worker", 1, "sent", later, 43)
        with self.assertRaises(ValueError):
            self.repo.finish_run(self.user.id, run["id"], "worker", later)
        self.repo.fail_run(self.user.id, run["id"], "worker", later, "unknown", unknown=True)
        self.assertEqual(self.repo.baseline_snapshot(self.user.id, "account", "scheduled", later+timedelta(hours=1)), scheduled)
        self.assertIsNone(self.repo.baseline_snapshot(self.user.id+1, "account", "scheduled", later))
        self.assertIsNone(self.repo.baseline_snapshot(self.user.id, "other", "scheduled", later))

    def test_schedule_revision_and_cancelled_old_run(self):
        rule = {"mode": "interval", "value": 6, "unit": "hours"}
        first = self.repo.save_schedule(self.user.id, "account", rule, NOW)
        run = self.repo.create_run(self.user.id, "account", "scheduled", "first", NOW, first["revision"])
        second = self.repo.save_schedule(self.user.id, "account", rule, NOW+timedelta(hours=6))
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["rule"], rule)
        self.assertEqual(self.repo.get_run(self.user.id, run["id"])["state"], "cancelled")

    def test_schedule_survives_database_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"journal.db"
            repo = JournalRepository(path)
            user = repo.get_or_create_user(7)
            repo.save_credentials(user.id, b"encrypted", "account")
            rule = {"mode": "calendar", "weekdays": [0, 2, 4], "local_time": "19:00", "timezone": "Europe/Moscow"}
            saved = repo.save_schedule(user.id, "account", rule, NOW+timedelta(hours=1))
            repo.close()
            repo = JournalRepository(path)
            self.assertEqual(repo.get_schedule(user.id), saved)
            repo.close()

    def test_repeated_request_and_expired_lease_restore_saved_progress(self):
        run = self.repo.create_run(self.user.id, "account", "manual", "message", NOW)
        repeated = self.repo.create_run(self.user.id, "account", "manual", "message", NOW)
        self.assertEqual(run["id"], repeated["id"])
        self.assertTrue(self.repo.claim_run(self.user.id, run["id"], "first", NOW))
        self.repo.prepare_run(self.user.id, run["id"], "first", self.snapshot.id, None, {"total": "240"}, ["one", "two"], NOW)
        self.repo.set_part_state(self.user.id, run["id"], "first", 1, "sending", NOW)
        self.repo.set_part_state(self.user.id, run["id"], "first", 1, "sent", NOW, message_id=42)
        later = NOW+timedelta(seconds=181)
        self.assertTrue(self.repo.claim_run(self.user.id, run["id"], "second", later))
        self.assertFalse(self.repo.renew_run(self.user.id, run["id"], "first", later))
        parts = self.repo.get_parts(self.user.id, run["id"])
        self.assertEqual((parts[0]["state"], parts[0]["message_id"], parts[1]["state"]), ("sent", 42, "queued"))
        self.assertEqual(self.repo.get_parts(self.user.id+1, run["id"]), [])
        with self.assertRaises(LeaseLost):
            self.repo.finish_run(self.user.id, run["id"], "first", later)

    def test_interrupted_send_is_marked_unknown(self):
        run = self.repo.create_run(self.user.id, "account", "manual", "message", NOW)
        self.repo.claim_run(self.user.id, run["id"], "first", NOW)
        self.repo.prepare_run(self.user.id, run["id"], "first", self.snapshot.id, None, {}, ["one"], NOW)
        self.repo.set_part_state(self.user.id, run["id"], "first", 1, "sending", NOW)
        self.assertFalse(self.repo.claim_run(self.user.id, run["id"], "second", NOW+timedelta(seconds=181)))
        self.assertEqual(self.repo.get_run(self.user.id, run["id"])["state"], "delivery_unknown")

    def test_two_connections_claim_one_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "journal.db"
            repo = JournalRepository(path)
            user = repo.get_or_create_user(7)
            run = repo.create_run(user.id, "account", "manual", "message", NOW)
            repo.close()
            barrier = Barrier(2)

            def claim(owner):
                connection = JournalRepository(path)
                try:
                    barrier.wait(timeout=5)
                    return connection.claim_run(user.id, run["id"], owner, NOW)
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(claim, ("first", "second")))
            self.assertEqual(sorted(results), [False, True])


class MigrationTests(unittest.TestCase):
    def test_current_database_and_repeated_open_preserve_existing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "journal.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(SCHEMA)
                db.execute("INSERT INTO users(id, chat_id) VALUES(1, 7)")
                db.execute("INSERT INTO credentials VALUES(1, ?, 'account', 'date')", (b"encrypted",))
                db.execute("INSERT INTO decisions(user_id,kind,ticker,hypothesis,entry_price,created_at) "
                           "VALUES(1,'buy','TEST','Existing hypothesis',10,'2026-01-01T00:00:00+00:00')")
            for _ in range(2):
                repo = JournalRepository(path)
                self.assertEqual(repo.get_credentials(1), (b"encrypted", "account"))
                self.assertEqual(len(repo.list_decisions(1)), 1)
                self.assertEqual(repo._db.execute("PRAGMA user_version").fetchone()[0], 1)
                repo.close()

    def test_interrupted_migration_rolls_back_all_new_tables_and_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "journal.db"
            with patch.object(storage, "PORTFOLIO_SCHEMA", storage.PORTFOLIO_SCHEMA + "\nINVALID SQL;"):
                with self.assertRaises(sqlite3.OperationalError):
                    JournalRepository(path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='portfolio_snapshots'").fetchone())
            repo = JournalRepository(path)
            self.assertEqual(repo._db.execute("PRAGMA user_version").fetchone()[0], 1)
            repo.close()

    def test_new_database_has_full_schema(self):
        repo = JournalRepository()
        tables = {row[0] for row in repo._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"portfolio_snapshots", "portfolio_operations", "report_schedules", "report_runs", "report_parts"} <= tables)
        repo.close()
