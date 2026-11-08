"""Общие заглушки для тестов."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment_journal.config import McpSettings  # noqa: E402
from investment_journal.mcp_client import ToolError  # noqa: E402

PORTFOLIO_RESPONSE = {
    "total_portfolio": 125000.5,
    "cash": 20000.0,
    "unrealized_pnl": 1500.25,
    "raw": {
        "positions": [
            {
                "figi": "FUTSI1225000",
                "ticker": "SiZ6",
                "instrumentType": "futures",
                "quantity": {"units": "2", "nano": 0},
                "averagePositionPrice": {"currency": "rub", "units": "90000", "nano": 0},
                "currentPrice": {"currency": "rub", "units": "91000", "nano": 500000000},
                "expectedYield": {"units": "2000", "nano": 0},
            },
            {
                "figi": "RUB000UTSTOM",
                "instrumentType": "currency",
                "quantity": {"units": "0", "nano": 0},
            },
        ]
    },
}


class FakeToolClient:
    """Имитация MCP-сервера t-invest-tinkoff-mcp в памяти."""

    def __init__(self, prices: dict[str, float] | None = None):
        self.prices = prices if prices is not None else {"SIZ6": 90000.0, "BRZ6": 80.0}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, arguments=None):
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if name == "get_last_price":
            ticker = arguments["ticker"].upper()
            if ticker not in self.prices:
                raise ToolError(f"{ticker} not found")
            return {"ticker": ticker, "price": self.prices[ticker]}
        if name == "get_candles":
            if arguments["ticker"].upper() not in self.prices:
                raise ToolError(f"{arguments['ticker']} not found")
            closes = [100, 102, 101, 105, 103, 108]
            return {
                "ticker": arguments["ticker"],
                "candles": [
                    {"t": f"2026-11-0{i + 1}T07:00:00Z", "o": c - 1, "h": c + 2, "l": c - 2, "c": c, "v": 10 * (i + 1)}
                    for i, c in enumerate(closes)
                ],
            }
        if name == "get_portfolio":
            return PORTFOLIO_RESPONSE
        raise ToolError(f"Unknown tool: {name}")

    async def list_tools(self):
        return [
            {"name": "get_last_price", "description": "Последняя цена",
             "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}}},
            {"name": "get_portfolio", "description": "Портфель", "input_schema": {"type": "object"}},
            {"name": "place_market_order", "description": "Рыночный ордер",
             "input_schema": {"type": "object"}},
        ]


def mcp_settings() -> McpSettings:
    return McpSettings()
