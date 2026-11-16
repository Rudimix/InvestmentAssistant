"""Публикация предложений после доставки и атомарная связь с гипотезой."""

from dataclasses import replace
from decimal import Decimal
import unittest

import _support  # noqa: F401
from investment_journal.market import snapshot_from
from investment_journal.models import AdviceProposal, DecisionDraft, EvidenceChunk, EvidenceDocument
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW, profile
from test_portfolio_market import portfolio_response


class AdviceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = JournalRepository()
        self.addCleanup(self.repo.close)
        self.user = self.repo.get_or_create_user(7)
        self.repo.save_credentials(self.user.id, "encrypted", "account")
        self.repo.save_profile(self.user.id, replace(profile(), user_id=self.user.id))
        document = EvidenceDocument("doc", "v1", "Issuer", "https://example.com/report", "Report", "Source", NOW, available_at=NOW)
        self.repo.save_document(document)
        self.repo.save_evidence_chunk(EvidenceChunk("chunk", "doc", "v1", "Source", page=1), "source")
        snapshot = snapshot_from(portfolio_response(), self.user.id, "account", NOW)
        self.repo.save_snapshot(snapshot)
        self.run = self.repo.create_run(self.user.id, "account", "manual", "history", NOW)
        self.repo.claim_run(self.user.id, self.run["id"], "owner", NOW)
        self.proposal = AdviceProposal("proposal", self.user.id, "account", 1, NOW, "Обоснованное предложение", ["m"], ["chunk"], ["scenario"],
                                       ["goal"], ["risk"], ["changed"], report_id=self.run["id"])
        self.payload = {"advice": {"context": {"metrics": {"m": {"value": "100", "unit": "rub"}}}}}
        self.snapshot = snapshot

    def prepare(self, proposal=None):
        self.repo.prepare_run(self.user.id, self.run["id"], "owner", self.snapshot.id, None, self.payload, ["part"], NOW,
                               proposals=[proposal or self.proposal])

    def deliver(self):
        self.prepare()
        self.repo.set_part_state(self.user.id, self.run["id"], "owner", 1, "sending", NOW)
        self.repo.set_part_state(self.user.id, self.run["id"], "owner", 1, "sent", NOW, 1)

    def test_proposal_is_visible_only_after_complete_delivery_and_scoped(self):
        self.prepare()
        self.assertIsNone(self.repo.get_advice(self.user.id, "account", "proposal"))
        self.assertEqual(self.repo.list_advice(self.user.id, "account"), [])
        self.repo.set_part_state(self.user.id, self.run["id"], "owner", 1, "sending", NOW)
        self.repo.set_part_state(self.user.id, self.run["id"], "owner", 1, "sent", NOW, 1)
        self.assertEqual(self.repo.get_advice(self.user.id, "account", "proposal"), self.proposal)
        self.assertIsNone(self.repo.get_advice(self.user.id+1, "account", "proposal"))
        self.assertIsNone(self.repo.get_advice(self.user.id, "other-account", "proposal"))
        self.assertEqual(len(self.repo.list_advice(self.user.id, "account")), 1)

    def test_invalid_proof_rolls_back_preparation_and_parts(self):
        with self.assertRaises(ValueError):
            self.prepare(replace(self.proposal, evidence_ids=["missing"]))
        self.assertEqual(self.repo.get_parts(self.user.id, self.run["id"]), [])
        self.assertIsNone(self.repo.get_run(self.user.id, self.run["id"])["payload"])
        self.prepare()

    def test_hypothesis_and_proof_saved_once_with_original_versions(self):
        self.deliver()
        draft = DecisionDraft(self.user.id, "buy", "SBER", "Ожидаю рост по исходным данным")
        first = self.repo.add_advice_decision(draft, 100.0, "account", "proposal", "callback")
        repeated = self.repo.add_advice_decision(draft, 101.0, "account", "proposal", "callback")
        self.assertEqual(first.id, repeated.id)
        self.assertEqual(len(self.repo.list_decisions(self.user.id)), 1)
        proof = self.repo.hypothesis_sources(self.user.id, "account", first.id)[0]["proof"]
        self.assertEqual(proof["proposal"]["profile_revision"], 1)
        self.assertEqual(proof["metrics"]["m"]["value"], "100")
        self.assertEqual(proof["evidence"][0]["version"], "v1")
        self.assertEqual(self.repo.hypothesis_sources(self.user.id+1, "account", first.id), [])
        self.assertEqual(self.repo.hypothesis_sources(self.user.id, "other", first.id), [])
        self.repo.save_profile(self.user.id, replace(profile(), user_id=self.user.id, revision=2, minimum_cash=Decimal(1)))
        self.assertEqual(self.repo.get_advice(self.user.id, "account", "proposal").profile_revision, 1)

    def test_account_change_prevents_orphaned_hypothesis(self):
        self.deliver()
        self.repo.set_account(self.user.id, "other")
        with self.assertRaises(ValueError):
            self.repo.add_advice_decision(DecisionDraft(self.user.id, "buy", "SBER", "Исходная гипотеза"), 100, "account", "proposal", "new")
        self.assertEqual(self.repo.list_decisions(self.user.id), [])


if __name__ == "__main__":
    unittest.main()
