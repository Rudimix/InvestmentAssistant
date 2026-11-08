"""Общие заглушки для тестов."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment_journal.config import McpSettings  # noqa: E402
from investment_journal.mcp_client import ToolError  # noqa: E402

#: Ответ invest_get_portfolio в формате официального сервера.
PORTFOLIO_RESPONSE = {
    "totalAmountPortfolio": {"value": "125000.5", "currency": "rub"},
    "totalAmountCurrencies": {"value": "20000", "currency": "rub"},
    "expectedYield": {"value": "1.21"},
    "positions": [
        {
            "ticker": "SiZ6",
            "instrumentType": "futures",
            "quantity": {"value": "2"},
            "averagePositionPrice": {"value": "90000", "currency": "rub"},
            "currentPrice": {"value": "91000.5", "currency": "rub"},
            "expectedYield": {"value": "2000"},
        },
        {
            "figi": "RUB000UTSTOM",
            "instrumentType": "currency",
            "quantity": {"value": "0"},
        },
    ],
}

#: Тикер → (тикер как на бирже, основной режим торгов). SBER есть ещё и в режиме SPEQ.
INSTRUMENTS = {"SIZ6": ("SiZ6", "SPBFUT"), "BRZ6": ("BRZ6", "SPBFUT"), "SBER": ("SBER", "TQBR")}


def value(number) -> dict:
    return {"value": str(number)}


class FakeToolClient:
    """Имитация официального T-Invest MCP (инструменты invest_*) в памяти."""

    def __init__(self, prices: dict[str, float] | None = None):
        self.prices = prices if prices is not None else {"SIZ6": 90000.0, "BRZ6": 80.0}
        self.accounts = [{"id": "2000000001", "type": "ACCOUNT_TYPE_TINKOFF", "status": "ACCOUNT_STATUS_OPEN"}]
        self.calls: list[tuple[str, dict]] = []

    def _ticker(self, instrument_id: str) -> str:
        ticker = instrument_id.split("_")[0].upper()
        if ticker not in self.prices:
            raise ToolError(f"{instrument_id} not found")
        return ticker

    async def call(self, name, arguments=None):
        arguments = arguments or {}
        self.calls.append((name, arguments))
        handler = getattr(self, "_" + name, None)
        if handler is None:
            raise ToolError(f"Unknown tool: {name}")
        return handler(arguments)

    def _invest_find_instrument(self, args):
        found = []
        query = args["query"].upper()
        if query in INSTRUMENTS:
            ticker, class_code = INSTRUMENTS[query]
            found.append({"ticker": ticker, "classCode": "SPEQ", "uid": "other"})
            found.append({"ticker": ticker, "classCode": class_code, "uid": f"uid-{ticker}"})
        return {"instruments": found}

    def _invest_get_market_values(self, args):
        try:
            ticker = self._ticker(args["instrumentId"][0])
        except ToolError:
            return {"instruments": []}
        return {"instruments": [{"values": [{"type": "INSTRUMENT_VALUE_LAST_PRICE",
                                             "value": value(self.prices[ticker])}],
                                 "ticker": ticker, "priceCurrency": "rub"}]}

    def _invest_get_candles(self, args):
        self._ticker(args["instrumentId"])
        closes = [100, 102, 101, 105, 103, 108]
        return {"priceCurrency": "rub", "candles": [
            {"open": value(c - 1), "high": value(c + 2), "low": value(c - 2), "close": value(c),
             "volume": str(10 * (i + 1)), "time": f"2026-11-0{i + 1}"}
            for i, c in enumerate(closes)
        ]}

    def _invest_list_accounts(self, args):
        return {"accounts": self.accounts}

    def _invest_get_portfolio(self, args):
        if args.get("accountId") not in {a["id"] for a in self.accounts}:
            raise ToolError("account not found")
        return PORTFOLIO_RESPONSE

    async def list_tools(self):
        return [
            {"name": "invest_get_market_values", "description": "Рыночные значения",
             "input_schema": {"type": "object", "properties": {"instrumentId": {"type": "array"}}}},
            {"name": "invest_get_portfolio", "description": "Портфель", "input_schema": {"type": "object"}},
            {"name": "invest_create_order", "description": "Создать заявку", "input_schema": {"type": "object"}},
        ]


def mcp_settings() -> McpSettings:
    return McpSettings()
