"""Рыночные данные T-Invest, полученные через MCP."""

from typing import Any

from .config import McpSettings
from .mcp_client import ToolClient, ToolError
from .models import Candle, Portfolio, Position

INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")


class MarketDataService:
    """Обёртка над инструментами MCP-сервера: цена, свечи, портфель."""

    def __init__(self, tools: ToolClient, settings: McpSettings):
        self._tools = tools
        self._settings = settings

    async def last_price(self, ticker: str) -> float:
        data = await self._tools.call(self._settings.price_tool, {"ticker": ticker.upper()})
        price = float(data.get("price") or 0)
        if price <= 0:
            raise ToolError(f"Нет цены для {ticker.upper()}")
        return price

    async def candles(self, ticker: str, interval: str = "1d", bars: int = 30) -> list[Candle]:
        if interval not in INTERVALS:
            raise ValueError(f"Интервал должен быть одним из: {', '.join(INTERVALS)}")
        data = await self._tools.call(
            self._settings.candles_tool,
            {"ticker": ticker.upper(), "interval": interval, "bars": bars},
        )
        return [parse_candle(c) for c in data.get("candles", [])]

    async def portfolio(self) -> Portfolio:
        data = await self._tools.call(self._settings.portfolio_tool)
        positions = (parse_position(p) for p in data.get("raw", {}).get("positions", []))
        return Portfolio(
            total=float(data.get("total_portfolio") or 0),
            cash=float(data.get("cash") or 0),
            unrealized_pnl=float(data.get("unrealized_pnl") or 0),
            positions=[p for p in positions if not p.is_empty_cash],
        )

    async def call_raw(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Вызвать произвольный инструмент (используется LLM-агентом)."""
        return await self._tools.call(name, arguments)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Описание инструментов MCP-сервера."""
        return await self._tools.list_tools()


def parse_candle(raw: dict[str, Any]) -> Candle:
    """Свеча из ответа ``get_candles`` (ключи t/o/h/l/c/v)."""
    return Candle(
        time=str(raw.get("t", "")),
        open=quotation(raw.get("o")),
        high=quotation(raw.get("h")),
        low=quotation(raw.get("l")),
        close=quotation(raw.get("c")),
        volume=int(raw.get("v") or 0),
    )


def quotation(value: Any) -> float:
    """Число T-Invest API: ``{"units": "12", "nano": 500000000}`` → 12.5; обычные числа — как есть."""
    if isinstance(value, dict):
        return float(value.get("units") or 0) + float(value.get("nano") or 0) / 1e9
    return float(value or 0)


def parse_position(raw: dict[str, Any]) -> Position:
    """Позиция из ``raw.positions`` ответа GetPortfolio."""
    avg = raw.get("averagePositionPrice")
    return Position(
        ticker=raw.get("ticker") or raw.get("figi") or "?",
        instrument_type=raw.get("instrumentType", ""),
        quantity=quotation(raw.get("quantity")),
        average_price=quotation(avg),
        current_price=quotation(raw.get("currentPrice")),
        expected_yield=quotation(raw.get("expectedYield")),
        currency=avg.get("currency", "rub") if isinstance(avg, dict) else "rub",
    )
