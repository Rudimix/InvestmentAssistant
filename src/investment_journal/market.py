"""Рыночные данные через официальный MCP-сервер Т-Инвестиций (инструменты ``invest_*``)."""

from datetime import datetime, timezone
from typing import Any

from .mcp_client import ToolClient, ToolError
from .models import Account, Candle, Portfolio, Position

#: Интервал команды бота → интервал ``invest_get_candles``.
INTERVALS = {
    "1m": "CANDLE_INTERVAL_1_MIN",
    "5m": "CANDLE_INTERVAL_5_MIN",
    "15m": "CANDLE_INTERVAL_15_MIN",
    "1h": "CANDLE_INTERVAL_HOUR",
    "4h": "CANDLE_INTERVAL_4_HOUR",
    "1d": "CANDLE_INTERVAL_DAY",
    "1w": "CANDLE_INTERVAL_WEEK",
}

#: Основные режимы торгов: по ним выбирается инструмент, если тикер торгуется в нескольких.
PREFERRED_CLASS_CODES = ("TQBR", "SPBFUT", "TQTF", "TQCB", "TQOB", "CETS", "SPBXM")


class MarketDataService:
    """Цена, свечи и портфель по тикеру; тикер переводится в ``instrumentId`` вида ``SBER_TQBR``."""

    def __init__(self, tools: ToolClient, account_id: str = "", instrument_ids: dict[str, str] | None = None):
        self._tools = tools
        self._account_id = account_id
        #: Тикер → instrumentId; словарь можно разделять между сессиями разных пользователей.
        self._instrument_ids = instrument_ids if instrument_ids is not None else {}

    async def instrument_id(self, ticker: str) -> str:
        """``SBER`` → ``SBER_TQBR``; ``SBER_TQBR`` и UID возвращаются как есть."""
        key = ticker.strip().upper()
        if "_" in key or "-" in key:
            return key if "_" in key else ticker.strip()
        if key not in self._instrument_ids:
            data = await self._tools.call("invest_find_instrument",
                                          {"query": key, "responseView": ["IDENTIFIERS"]})
            self._instrument_ids[key] = pick_instrument(key, data.get("instruments", []))
        return self._instrument_ids[key]

    async def last_price(self, ticker: str) -> float:
        instrument = await self.instrument_id(ticker)
        data = await self._tools.call("invest_get_market_values", {
            "instrumentId": [instrument], "values": ["INSTRUMENT_VALUE_LAST_PRICE"],
        })
        price = last_price_from(data)
        if price <= 0:
            raise ToolError(f"Нет цены для {ticker.upper()}")
        return price

    async def candles(self, ticker: str, interval: str = "1d", bars: int = 30) -> list[Candle]:
        if interval not in INTERVALS:
            raise ValueError(f"Интервал должен быть одним из: {', '.join(INTERVALS)}")
        data = await self._tools.call("invest_get_candles", {
            "instrumentId": await self.instrument_id(ticker),
            "interval": INTERVALS[interval],
            "to": datetime.now(timezone.utc).isoformat(),
            "limit": bars,
        })
        return [parse_candle(c) for c in data.get("candles", [])]

    async def accounts(self) -> list[Account]:
        """Открытые брокерские счета пользователя (без банковских)."""
        data = await self._tools.call("invest_list_accounts", {"status": "ACCOUNT_STATUS_OPEN"})
        return parse_accounts(data)

    async def account_id(self) -> str:
        """Выбранный счёт или первый открытый брокерский счёт пользователя."""
        if not self._account_id:
            accounts = await self.accounts()
            if not accounts:
                raise ToolError(NO_ACCOUNTS)
            self._account_id = accounts[0].id
        return self._account_id

    async def portfolio(self) -> Portfolio:
        data = await self._tools.call("invest_get_portfolio", {
            "accountId": await self.account_id(), "responseView": ["FULL"],
        })
        return parse_portfolio(data)

    async def call_raw(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Вызвать произвольный инструмент (используется LLM-агентом)."""
        return await self._tools.call(name, arguments)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Описание инструментов MCP-сервера."""
        return await self._tools.list_tools()


def quotation(value: Any) -> float:
    """Число из ответа: ``{"value": "294.35"}``, ``{"units": "12", "nano": 5e8}``, строка или число."""
    if isinstance(value, dict):
        if "value" in value:
            return quotation(value["value"])
        return float(value.get("units") or 0) + float(value.get("nano") or 0) / 1e9
    return float(value or 0)


def currency_of(value: Any, default: str = "rub") -> str:
    return value.get("currency") or default if isinstance(value, dict) else default


def pick_instrument(ticker: str, instruments: list[dict[str, Any]]) -> str:
    """Выбрать ``TICKER_CLASSCODE`` с точным совпадением тикера, предпочитая основные режимы торгов."""
    exact = [i for i in instruments if str(i.get("ticker", "")).upper() == ticker]
    if not exact:
        raise ToolError(f"Инструмент {ticker} не найден")
    rank = {code: n for n, code in enumerate(PREFERRED_CLASS_CODES)}
    best = min(exact, key=lambda i: rank.get(i.get("classCode", ""), len(rank)))
    return f"{best['ticker']}_{best['classCode']}"


def last_price_from(data: dict[str, Any]) -> float:
    for instrument in data.get("instruments", []):
        for item in instrument.get("values", []):
            if item.get("type") == "INSTRUMENT_VALUE_LAST_PRICE":
                return quotation(item.get("value"))
    return 0.0


NO_ACCOUNTS = "Нет открытых брокерских счетов"


def parse_accounts(data: dict[str, Any]) -> list[Account]:
    """Ответ ``invest_list_accounts`` → счета; банковские счета пропускаются."""
    accounts = []
    for raw in data.get("accounts") or []:
        account_id = raw.get("id") or raw.get("accountId")
        if account_id and raw.get("type", "") != "ACCOUNT_TYPE_BANK":
            accounts.append(Account(
                id=account_id, name=raw.get("name", ""), type=raw.get("type", ""),
                read_only=raw.get("accessLevel") == "ACCOUNT_ACCESS_LEVEL_READ_ONLY",
            ))
    return accounts


def first_account_id(data: dict[str, Any]) -> str:
    accounts = parse_accounts(data)
    if not accounts:
        raise ToolError(NO_ACCOUNTS)
    return accounts[0].id


def parse_candle(raw: dict[str, Any]) -> Candle:
    """Свеча ``invest_get_candles``: open/high/low/close как ``{"value": "..."}``, объём строкой."""
    return Candle(
        time=str(raw.get("time", "")),
        open=quotation(raw.get("open")),
        high=quotation(raw.get("high")),
        low=quotation(raw.get("low")),
        close=quotation(raw.get("close")),
        volume=int(float(raw.get("volume") or 0)),
    )


def parse_position(raw: dict[str, Any]) -> Position:
    """Позиция из ``positions`` ответа ``invest_get_portfolio``."""
    avg = raw.get("averagePositionPrice")
    return Position(
        ticker=raw.get("ticker") or raw.get("figi") or raw.get("instrumentUid") or "?",
        instrument_type=raw.get("instrumentType", ""),
        quantity=quotation(raw.get("quantity")),
        average_price=quotation(avg),
        current_price=quotation(raw.get("currentPrice")),
        expected_yield=quotation(raw.get("expectedYield")),
        currency=currency_of(avg),
    )


#: Рубли на счёте приходят позицией с этим тикером; они уже учтены в свободных деньгах.
RUB_CASH_TICKER = "RUB000UTSTOM"


def parse_portfolio(data: dict[str, Any]) -> Portfolio:
    """Ответ ``invest_get_portfolio`` (responseView FULL): итоги и позиции без рублёвого остатка."""
    positions = [parse_position(p) for p in data.get("positions") or []]
    return Portfolio(
        total=quotation(data.get("totalAmountPortfolio")),
        cash=quotation(data.get("totalAmountCurrencies")),
        unrealized_pnl=sum(p.expected_yield for p in positions),
        positions=[p for p in positions if p.ticker != RUB_CASH_TICKER and not p.is_empty_cash],
    )
