"""Версии справочника отраслей ограничены временем реального наблюдения."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
import unittest

import _support  # noqa: F401
from investment_journal.broker_metadata import BrokerMetadata, historical_sector
from investment_journal.models import OperationBatch, PortfolioSnapshot, SnapshotPosition
from investment_journal.portfolio_history import reconstruct_state
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW


class BrokerMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_observation_and_historical_sector_versions(self):
        repo = JournalRepository()
        self.addCleanup(repo.close)
        now = [NOW]
        sector = ["financial"]

        async def respond(tool, args):
            if tool == "invest_get_asset":
                return {"asset": {"uid": "asset", "brand": {"uid": "brand"}}}
            return {"uid": "brand", "sector": sector[0], "countryOfRisk": "RUS"}

        market = AsyncMock()
        market.report_call.side_effect = respond
        metadata = BrokerMetadata(repo, lambda: now[0])
        asset = await metadata.get(market, "invest_get_asset", "asset")
        first = await metadata.get(market, "invest_get_brand", "brand")
        self.assertEqual(historical_sector(repo, "asset", NOW)["sector"], "financial")
        self.assertEqual(historical_sector(repo, "asset", NOW-timedelta(seconds=1)), {})
        now[0] += timedelta(days=1)
        sector[0] = "technology"
        repeated_asset = await metadata.get(market, "invest_get_asset", "asset")
        second = await metadata.get(market, "invest_get_brand", "brand")
        self.assertEqual(asset.observed_at, repeated_asset.observed_at)
        self.assertNotEqual(first.version, second.version)
        self.assertEqual(historical_sector(repo, "asset", NOW)["sector"], "financial")
        self.assertEqual(historical_sector(repo, "asset", now[0])["sector"], "technology")
        now[0] += timedelta(days=1)
        sector[0] = "financial"
        restored = await metadata.get(market, "invest_get_brand", "brand")
        self.assertNotEqual(restored.version, first.version)
        self.assertEqual(historical_sector(repo, "asset", now[0])["sector"], "financial")
        self.assertEqual(historical_sector(repo, "asset", NOW+timedelta(days=1))["sector"], "technology")
        self.assertEqual((await metadata.get(market, "invest_get_brand", "brand")).version, restored.version)

    def test_current_sector_does_not_leak_into_prior_portfolio(self):
        anchor = PortfolioSnapshot("s", 1, "account", NOW, "rub", Decimal(100), Decimal(0), [
            SnapshotPosition("share", "TEST", "share", Decimal(1), "rub", value=Decimal(100),
                             sector="technology", sector_as_of=NOW, sector_source_version="v2")])
        state = reconstruct_state(anchor, NOW-timedelta(days=1), OperationBatch([], True))
        self.assertEqual(state.positions[0].sector, "")
        self.assertIsNone(state.positions[0].sector_as_of)


if __name__ == "__main__":
    unittest.main()
