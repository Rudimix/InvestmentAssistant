"""Рыночные данные через официальный MCP-сервер Т-Инвестиций (инструменты ``invest_*``)."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from .mcp_client import ToolClient, ToolError
from .models import (
    Account, Candle, Portfolio, PortfolioSnapshot, Position, SnapshotPosition, decimal_value, timestamp,
)

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

    def __init__(self, tools: ToolClient, account_id: str = "", instrument_ids: dict[str, str] | None = None,
                 allowed_tools: tuple[str, ...] | None = None):
        self._tools = tools
        self._account_id = account_id
        #: Тикер → instrumentId; словарь можно разделять между сессиями разных пользователей.
        self._instrument_ids = instrument_ids if instrument_ids is not None else {}
        self._allowed_tools = allowed_tools
        self._catalog: dict[str, dict] | None = None

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
        raw, details = await self._portfolio_data()
        currency = str(raw.get("totalAmountPortfolio", {}).get("currency", "")).lower()
        for item in raw.get("positions", []):
            if item.get("instrumentType") == "bond":
                normalized = snapshot_position(item, currency, details)
                if normalized.price is not None:
                    item["currentPrice"] = {"value": str(normalized.price), "currency": normalized.price_currency}
        return parse_portfolio(raw)

    async def snapshot(self, user_id: int, observed_at: datetime | None = None) -> PortfolioSnapshot:
        """Получить полный ответ портфеля и сохранить точные поля аналитического снимка."""
        account_id = await self.account_id()
        raw, details = await self._portfolio_data()
        try:
            return snapshot_from(raw, user_id, account_id, observed_at or datetime.now(timezone.utc), details)
        except (ValueError, TypeError, KeyError) as exc:
            raise ToolError("Ответ портфеля непригоден для анализа") from exc

    async def _portfolio_data(self) -> tuple[dict, dict]:
        """Дополнить FULL-портфель справочником и котировками, которые MCP возвращает отдельно."""
        raw = await self._tools.call("invest_get_portfolio", {
            "accountId": await self.account_id(), "responseView": ["FULL"],
        })
        if not isinstance(raw, dict) or not isinstance(raw.get("positions", []), list):
            raise ToolError("Ответ портфеля непригоден для анализа")
        raw = dict(raw)
        if "positions" in raw:
            raw["positions"] = [dict(item) if isinstance(item, dict) else item for item in raw["positions"]]
        details = {}
        for position in raw.get("positions") or []:
            if not isinstance(position, dict):
                continue
            ticker = position.get("ticker")
            if ticker == RUB_CASH_TICKER:
                position.setdefault("instrumentType", "currency")
            elif ticker and (not position.get("instrumentType") or not (position.get("instrumentUid") or position.get("figi"))):
                try:
                    found = await self.report_call("invest_find_instrument", {"query": ticker, "responseView": ["FULL"]})
                    matches = [item for item in found.get("instruments", [])
                               if item.get("ticker") == ticker and
                               (not position.get("classCode") or item.get("classCode") == position["classCode"])]
                    if len(matches) == 1:
                        instrument = matches[0]
                        for target, source in (("instrumentUid", "uid"), ("positionUid", "positionUid"), ("assetUid", "assetUid")):
                            if not position.get(target) and instrument.get(source):
                                position[target] = instrument[source]
                        if not position.get("instrumentType") and instrument.get("instrumentKind"):
                            position["instrumentType"] = instrument["instrumentKind"].removeprefix("INSTRUMENT_TYPE_").lower()
                except (ToolError, ValueError, TypeError, AttributeError):
                    pass  # Отсутствующие дополнительные данные отмечаются в снимке.
        needed = [item for item in raw.get("positions", []) if isinstance(item, dict) and
                  optional_decimal(item.get("currentPrice")) is None and item.get("ticker") != RUB_CASH_TICKER]
        identifiers = {portfolio_instrument_id(item) for item in needed} - {""}
        if identifiers:
            try:
                quotes = await self.report_call("invest_get_market_values", {
                    "instrumentId": sorted(identifiers), "values": ["INSTRUMENT_VALUE_LAST_PRICE"],
                })
                for position in needed:
                    matches = [item for item in quotes.get("instruments", []) if same_instrument(position, item)]
                    if len(matches) != 1:
                        continue
                    quote = matches[0]
                    for price in quote.get("values", []):
                        if price.get("type") == "INSTRUMENT_VALUE_LAST_PRICE" and optional_decimal(price.get("value")) is not None:
                            position["currentPrice"] = {"value": str(decimal_value(price["value"])),
                                                        "currency": quote.get("priceCurrency", "")}
                            position["lastPriceDate"] = price.get("time")
                            position["_priceFromMarket"] = True
                            break
            except (ToolError, ValueError, TypeError, AttributeError):
                pass
        for position in raw.get("positions", []):
            if not isinstance(position, dict):
                continue
            price = position.get("currentPrice") or {}
            if position.get("instrumentType") == "bond" and (
                    price.get("currency") in {"pt", "pt."} or optional_decimal(position.get("currentNkd")) is None):
                instrument = portfolio_instrument_id(position)
                if instrument:
                    try:
                        details[instrument] = await self.report_call("invest_get_bond", {
                            "instrumentId": instrument, "idType": "INSTRUMENT_ID_TYPE_ID",
                            "responseView": ["FULL"],
                        })
                        bond = details[instrument].get("instrument", details[instrument])
                        if optional_decimal(position.get("currentNkd")) is None and bond.get("aciValue") is not None:
                            position["currentNkd"] = bond["aciValue"]
                    except ToolError:
                        pass  # Неполная оценка позиции отражается в снимке.
        return raw, details

    async def supports_tool(self, name: str) -> bool:
        """Проверить наличие разрешённого инструмента в схеме текущего подключения."""
        if self._allowed_tools is not None and name not in self._allowed_tools:
            return False
        if self._catalog is None:
            self._catalog = {tool["name"]: tool for tool in await self._tools.list_tools()}
        return name in self._catalog

    async def report_call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Вызов адаптера отчётов с проверкой доступности и обязательных аргументов."""
        if not await self.supports_tool(name):
            raise ToolError(f"Источник {name} недоступен")
        schema = self._catalog[name].get("input_schema") or {}
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if any(key not in arguments for key in required):
            raise ToolError(f"Изменилась схема источника {name}")
        supplied = {key: value for key, value in arguments.items() if key in properties}
        return await self._tools.call(name, supplied)

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


def optional_decimal(raw: Any) -> Decimal | None:
    """Отсутствующая или некорректная дополнительная оценка."""
    try:
        return decimal_value(raw)
    except (ValueError, TypeError):
        return None


def portfolio_instrument_id(position: dict) -> str:
    """UID либо точная пара тикера и режима торгов для дополнительных запросов."""
    if position.get("instrumentUid"):
        return position["instrumentUid"]
    if position.get("ticker") and position.get("classCode"):
        return f"{position['ticker']}_{position['classCode']}"
    return ""


def same_instrument(position: dict, quote: dict) -> bool:
    if position.get("ticker") and position.get("classCode"):
        return (position["ticker"], position["classCode"]) == (quote.get("ticker"), quote.get("classCode"))
    uid = position.get("instrumentUid")
    return bool(uid) and uid in {quote.get("instrumentUid"), quote.get("uid"), quote.get("instrumentId")}


def snapshot_position(raw: dict, base_currency: str, details: dict) -> SnapshotPosition:
    """Нормализовать позицию, сохранив границы доступной денежной оценки."""
    ticker = raw.get("ticker") or raw.get("figi") or raw.get("instrumentUid") or "?"
    instrument_id = raw.get("instrumentUid") or raw.get("figi") or ""
    key = raw.get("positionUid") or instrument_id
    quality = []
    if not key:
        key = f"{ticker}_{raw.get('classCode', '')}"
        quality.append("Составной идентификатор позиции")
    kind = raw.get("instrumentType", "")
    if ticker == RUB_CASH_TICKER:
        kind = "currency"
    price_data = raw.get("currentPrice") or {}
    price_currency = str(price_data.get("currency") or "").lower()
    if price_currency == "pt.":
        price_currency = "pt"
    price = optional_decimal(price_data)
    quantity = decimal_value(raw["quantity"])
    currency = str(raw.get("currency") or price_currency).lower()
    if kind == "currency" and ticker[:3].lower() in {"rub", "usd", "eur", "cny", "hkd", "gbp", "chf", "jpy"}:
        currency = ticker[:3].lower()
    if kind == "currency" and currency == base_currency and price is None:
        price, price_currency = Decimal(1), base_currency
    if kind == "bond" and price_currency == "pt":
        instrument = details.get(portfolio_instrument_id(raw), details.get(instrument_id, {}))
        nominal = instrument.get("nominal") or instrument.get("instrument", {}).get("nominal") or {}
        amount = optional_decimal(nominal)
        if amount is not None and price is not None:
            price = price * amount / 100
            price_currency = nominal.get("currency", "").lower()
            currency = price_currency
    value = None
    if kind in {"share", "etf", "bond", "currency"} and price is not None and price_currency == base_currency:
        accrued = Decimal(0)
        if kind == "bond":
            nkd = raw.get("currentNkd")
            accrued = optional_decimal(nkd)
            if isinstance(nkd, dict) and nkd.get("currency", base_currency).lower() != base_currency:
                accrued = None
        if accrued is not None:
            value = quantity * (price + accrued)
    if value is None:
        quality.append("Денежная оценка в валюте портфеля недоступна")
    return SnapshotPosition(
        key=key, ticker=ticker, instrument_type=kind, quantity=quantity, currency=currency,
        value=value, price=price, instrument_id=instrument_id or key,
        asset_id=raw.get("assetUid", ""), price_currency=price_currency,
        source_time=raw.get("lastPriceDate") or raw.get("priceTime"), quality=quality,
        aliases=[f"{ticker}_{raw['classCode']}"] if raw.get("classCode") else [],
        sector=str(raw.get("sector") or ""),
    )


def snapshot_from(raw: dict, user_id: int, account_id: str, observed_at: datetime,
                  details: dict | None = None) -> PortfolioSnapshot:
    """Проверить основной ответ и нормализовать валидный снимок выбранного счёта."""
    if raw.get("accountId", account_id) != account_id:
        raise ValueError("Получен другой счёт")
    total_data = raw["totalAmountPortfolio"]
    currency = str(total_data["currency"]).lower()
    if not currency:
        raise ValueError("Валюта портфеля отсутствует")
    if not isinstance(raw.get("positions", []), list):
        raise ValueError("Некорректный состав портфеля")
    positions = []
    quality = []
    for item in raw.get("positions", []):
        try:
            positions.append(snapshot_position(item, currency, details or {}))
        except (ValueError, KeyError, TypeError, AttributeError):
            quality.append("Часть позиций не удалось прочитать")
    if "positions" not in raw:
        quality.append("Источник не вернул состав позиций")
    if any(isinstance(item, dict) and item.get("_priceFromMarket") for item in raw.get("positions", [])):
        quality.append("Оценки позиций получены из отдельных котировок; их время может отличаться от оценки брокера")
    totals = {}
    for suffix, kind in (("Shares", "share"), ("Bonds", "bond"), ("Etf", "etf"),
                         ("Currencies", "currency"), ("Futures", "futures"), ("Options", "option")):
        money = raw.get("totalAmount" + suffix) or {}
        value = optional_decimal(money)
        if value is not None and money.get("currency", currency).lower() == currency:
            totals[kind] = value
    return PortfolioSnapshot(
        id=uuid4().hex, user_id=user_id, account_id=account_id, observed_at=timestamp(observed_at),
        currency=currency, total=decimal_value(total_data), cash=totals.get("currency"),
        positions=positions, type_totals=totals, quality=quality,
        source_time=raw.get("asOf") or raw.get("date"),
    )
