"""Страна бизнеса, доступность счёту, ликвидность и замороженный состав выборки."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import unittest

import _support  # noqa: F401
from investment_journal.broker_metadata import enrich_snapshot_sectors
from investment_journal.models import Account, PortfolioSnapshot, SnapshotPosition, data_json
from investment_journal.stock_universe import StockUniverseService, account_restrictions
from investment_journal.storage import JournalRepository
from test_analysis_models import NOW
from test_daily_prices import daily_row


def share(ticker="SBER", asset="asset", available=True):
    return {"ticker": ticker, "classCode": "TQBR", "assetUid": asset, "isin": "ISIN-"+ticker, "lot": 10, "currency": "rub",
            "apiTradeAvailableFlag": available, "forQualInvestorFlag": False, "forIisFlag": True, "requiredTests": []}


class UniverseMarket:
    def __init__(self):
        self.rows = [share(), share("FOREIGN", "foreign"), share("UNKNOWN", "unknown"), share("DELISTED", "old", False)]
        self.rows.append(share() | {"classCode": "SMAL"})
        self.calls = []

    async def account_id(self):
        return "account"

    async def accounts(self):
        return [Account("account", type="ACCOUNT_TYPE_TINKOFF")]

    async def report_call(self, name, args):
        self.calls.append((name, args))
        if name == "invest_list_shares":
            return {"instruments": deepcopy(self.rows)}
        if name == "invest_get_user_info":
            return {"qualStatus": False, "qualifiedForWorkWith": [], "userId": "must-not-be-persisted"}
        if name == "invest_get_asset":
            return {"asset": {"uid": args["id"], "brand": {"uid": "brand-"+args["id"]}}}
        if name == "invest_get_brand":
            country = {"brand-asset": "RUS", "brand-foreign": "USA", "brand-unknown": ""}[args["id"]]
            return {"uid": args["id"], "countryOfRisk": country, "sector": "financial"}
        if name == "invest_get_candles":
            return {"priceCurrency": "rub", "candles": [daily_row((NOW-timedelta(days=day)).date()) for day in range(31, 1, -1)]}
        if name == "invest_get_max_lots":
            return {"buyLimits": {"buyMaxLots": "10"}, "currency": "rub"}
        if name == "invest_get_trading_statuses":
            return {"tradingStatuses": [{"ticker": "SBER", "classCode": "TQBR", "limitOrderAvailableFlag": True}]}
        raise AssertionError(name)


class UniverseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = JournalRepository()
        self.addCleanup(self.repo.close)
        self.user = self.repo.get_or_create_user(7)
        self.market = UniverseMarket()

    async def test_scope_access_liquidity_and_replay(self):
        service = StockUniverseService(self.repo, lambda: NOW, call_interval=0)
        universe = await service.collect(self.user.id, self.market)
        entries = {entry.ticker: entry for entry in universe.entries}
        self.assertTrue(entries["SBER"].included)
        self.assertTrue(entries["SBER"].trading_now)
        self.assertEqual(entries["SBER"].median_turnover, Decimal(100000000))
        self.assertEqual(entries["SBER"].max_buy_lots, Decimal(10))
        self.assertEqual(entries["SBER"].class_code, "TQBR")
        self.assertFalse(entries["FOREIGN"].in_scope)
        self.assertTrue(any("RUS" in reason for reason in entries["FOREIGN"].reasons))
        self.assertTrue(any("не подтверждена" in reason for reason in entries["UNKNOWN"].reasons))
        self.assertFalse(entries["DELISTED"].included)
        self.assertEqual(universe.catalog_count, 5)
        self.assertEqual(data_json(universe), data_json(self.repo.get_universe(self.user.id, "account", universe.id)))
        self.assertIsNone(self.repo.get_universe(self.user.id+1, "account", universe.id))
        self.assertIsNone(self.repo.get_universe(self.user.id, "other", universe.id))
        self.assertNotIn("must-not-be-persisted", data_json(universe))

    def test_required_tests_qualification_and_iis(self):
        row = share() | {"forQualInvestorFlag": True, "requiredTests": ["test"], "forIisFlag": False}
        self.assertEqual(len(account_restrictions(row, {"qualStatus": False, "qualifiedForWorkWith": []}, True)), 3)
        self.assertFalse(account_restrictions(row, {"qualStatus": True, "qualifiedForWorkWith": ["test"]}, False))
        self.assertTrue(account_restrictions(share() | {"forQualInvestorFlag": 1}, {}, False))

    async def test_foreign_holding_stays_in_portfolio_analysis(self):
        snapshot = PortfolioSnapshot("s", self.user.id, "account", NOW, "rub", Decimal(100), Decimal(0), [
            SnapshotPosition("foreign", "FOREIGN", "share", Decimal(1), "rub", value=Decimal(100), asset_id="foreign")])
        await enrich_snapshot_sectors(self.repo, self.market, snapshot, lambda: NOW)
        self.assertEqual(snapshot.positions[0].ticker, "FOREIGN")
        self.assertEqual(snapshot.positions[0].sector, "financial")
        self.assertEqual(snapshot.total, 100)

    async def test_low_liquidity_is_excluded_with_reason(self):
        original = self.market.report_call

        async def limited(name, args):
            raw = await original(name, args)
            if name == "invest_get_candles":
                for candle in raw["candles"]:
                    candle["volume"] = "1"
            return raw

        self.market.report_call = limited
        universe = await StockUniverseService(self.repo, lambda: NOW, call_interval=0).collect(self.user.id, self.market)
        entry = next(entry for entry in universe.entries if entry.ticker == "SBER")
        self.assertFalse(entry.included)
        self.assertTrue(any("оборот" in reason for reason in entry.reasons))


if __name__ == "__main__":
    unittest.main()
