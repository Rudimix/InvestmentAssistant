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
        result = []
        for c in data.get("candles", []):
            result.append(
                Candle(
                    time=str(c.get("t", "")),
                    open=float(c.get("o") or 0),
                    high=float(c.get("h") or 0),
                    low=float(c.get("l") or 0),
                    close=float(c.get("c") or 0),
                    volume=int(c.get("v") or 0),
                )
            )
        return result

    async def portfolio(self) -> Portfolio:
        data = await self._tools.call(self._settings.portfolio_tool)
        raw = data.get("raw", {}) if isinstance(data, dict) else {}
        positions = []
        for p in raw.get("positions", []):
            qty = p.get("quantity") or {}
            if isinstance(qty, dict):
                quantity = float(qty.get("units", 0) or 0) + float(qty.get("nano", 0) or 0) / 1e9
            else:
                quantity = float(qty or 0)
            avg = p.get("averagePositionPrice") or {}
            if isinstance(avg, dict):
                average_price = float(avg.get("units", 0) or 0) + float(avg.get("nano", 0) or 0) / 1e9
                currency = avg.get("currency", "rub")
            else:
                average_price = float(avg or 0)
                currency = "rub"
            cur = p.get("currentPrice") or {}
            if isinstance(cur, dict):
                current_price = float(cur.get("units", 0) or 0) + float(cur.get("nano", 0) or 0) / 1e9
            else:
                current_price = float(cur or 0)
            yld = p.get("expectedYield") or {}
            if isinstance(yld, dict):
                expected_yield = float(yld.get("units", 0) or 0) + float(yld.get("nano", 0) or 0) / 1e9
            else:
                expected_yield = float(yld or 0)
            if p.get("instrumentType") == "currency" and quantity == 0:
                continue
            positions.append(
                Position(
                    ticker=p.get("ticker") or p.get("figi") or "?",
                    instrument_type=p.get("instrumentType", ""),
                    quantity=quantity,
                    average_price=average_price,
                    current_price=current_price,
                    expected_yield=expected_yield,
                    currency=currency,
                )
            )
        return Portfolio(
            total=float(data.get("total_portfolio") or 0),
            cash=float(data.get("cash") or 0),
            unrealized_pnl=float(data.get("unrealized_pnl") or 0),
            positions=positions,
        )

    async def call_raw(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Вызвать произвольный инструмент (используется LLM-агентом)."""
        return await self._tools.call(name, arguments)
