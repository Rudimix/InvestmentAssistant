"""Версионированная выборка российского бизнеса и проверяемая доступность счёту."""

import asyncio
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import partial
from hashlib import sha256
import json
import math
from statistics import median

from .daily_prices import daily_prices
from .broker_metadata import BrokerMetadata
from .historical_prices import currency_pairs, currency_rates
from .market import PREFERRED_CLASS_CODES, optional_decimal
from .mcp_client import ToolError
from .models import StockUniverse, StockUniverseEntry, data_json, decimal_value, timestamp


@dataclass(frozen=True)
class UniversePolicy:
    version: str = "russian-business-v1"
    minimum_turnover_rub: Decimal = Decimal(10_000_000)
    liquidity_days: int = 20
    maximum_price_age_days: int = 7
    lookback_days: int = 550

    def __post_init__(self):
        object.__setattr__(self, "minimum_turnover_rub", decimal_value(self.minimum_turnover_rub))
        if (self.minimum_turnover_rub < 0 or self.liquidity_days <= 0 or self.lookback_days < self.liquidity_days
                or self.maximum_price_age_days < 0 or not self.version):
            raise ValueError("Некорректные параметры методики выборки")


def account_restrictions(row: dict, rights: dict, is_iis: bool) -> list[str]:
    reasons = []
    if row.get("apiTradeAvailableFlag") is not True:
        reasons.append("Доступ к инструменту через API не подтверждён")
    qualified_only = row.get("forQualInvestorFlag")
    if qualified_only is True and rights.get("qualStatus") is not True:
        reasons.append("Для инструмента требуется подтверждённая квалификация инвестора")
    if type(qualified_only) is not bool:
        reasons.append("Ограничение квалификации в справочнике неизвестно")
    if is_iis and row.get("forIisFlag") is not True:
        reasons.append("Доступность инструмента на ИИС не подтверждена")
    tests = row.get("requiredTests")
    passed = rights.get("qualifiedForWorkWith", [])
    valid_passed = isinstance(passed, list) and all(isinstance(item, str) for item in passed)
    passed_values = passed if valid_passed else []
    if not isinstance(tests, list) or any(not isinstance(item, str) for item in tests):
        reasons.append("Требуемые тесты инструмента неизвестны")
    elif tests and not valid_passed:
        reasons.append("Пройденные тесты пользователя неизвестны")
    elif {item.casefold() for item in tests}-{item.casefold() for item in passed_values}:
        reasons.append("Не подтверждено прохождение требуемых тестов")
    return reasons


def canonical_shares(rows: list[dict], rights: dict, is_iis: bool) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict) or not row.get("ticker") or not row.get("classCode"):
            continue
        key = row.get("isin") or row.get("assetUid") or f"{row['ticker']}_{row['classCode']}"
        grouped[key].append(row)

    def priority(row):
        board = row["classCode"]
        preferred = PREFERRED_CLASS_CODES.index(board) if board in PREFERRED_CLASS_CODES else len(PREFERRED_CLASS_CODES)
        return bool(account_restrictions(row, rights, is_iis)), preferred, row["ticker"], board

    return sorted((min(group, key=priority) for group in grouped.values()), key=lambda row: (row["ticker"], row["classCode"]))


def liquidity_metrics(entry: StockUniverseEntry, policy: UniversePolicy, as_of: datetime, rate=None) -> list[str]:
    recent = entry.series[-policy.liquidity_days:]
    if len(recent) < policy.liquidity_days:
        return ["Недостаточно завершённых дневных свечей для ликвидности"]
    if entry.lot is None or entry.lot <= 0:
        return ["Размер лота не подтверждён"]
    last = recent[-1]
    entry.price = last.close
    entry.price_at = datetime.combine(date.fromisoformat(last.day), time(), timezone.utc)
    entry.median_daily_lots = median(item.volume for item in recent)
    if last.currency != entry.currency or any(item.currency != entry.currency for item in recent):
        return ["Валюта свечей расходится со справочником инструмента"]
    if as_of-entry.price_at > timedelta(days=policy.maximum_price_age_days):
        return ["Цена устарела для подбора"]
    if entry.currency == "rub":
        rate = Decimal(1)
    if rate is None:
        return ["Для ликвидности отсутствует подтверждённый курс в RUB"]
    # Исторический оборот в валюте бумаги оценивается по курсу на дату подбора.
    entry.median_turnover = median(item.close*item.volume*entry.lot for item in recent)*decimal_value(rate)
    return ["Медианный дневной оборот ниже порога методики"] if entry.median_turnover < policy.minimum_turnover_rub else []


class StockUniverseService:
    def __init__(self, repo, clock=None, policy: UniversePolicy | None = None, call_interval: float = 0.2):
        if not math.isfinite(call_interval) or call_interval < 0:
            raise ValueError("Интервал запросов должен быть конечным и неотрицательным")
        self.repo = repo
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy = policy or UniversePolicy()
        self.call_interval = call_interval
        self._rate_lock = asyncio.Lock()
        self._next_call = 0.0
        self._capacity = asyncio.Semaphore(4)
        self.metadata = BrokerMetadata(repo, self.clock)

    async def _call(self, market, name, arguments):
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            delay = self._next_call-loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_call = loop.time()+self.call_interval
        return await market.report_call(name, arguments)

    async def _entry(self, market, row, rights, is_iis, as_of, fx_catalog):
        async with self._capacity:
            identifier = row.get("uid") or f"{row['ticker']}_{row['classCode']}"
            entry = StockUniverseEntry(identifier, row["ticker"], row["classCode"], str(row.get("assetUid") or ""),
                                        currency=str(row.get("currency") or "").lower(), lot=optional_decimal(row.get("lot")))
            entry.source_versions["catalog_row"] = sha256(data_json(row).encode()).hexdigest()
            entry.inputs["catalog"] = row
            entry.reasons = account_restrictions(row, rights, is_iis)
            entry.account_access = not entry.reasons
            if not entry.account_access:
                return entry
            try:
                asset = await self.metadata.get(market, "invest_get_asset", entry.asset_id, partial(self._call, market))
                entry.source_versions["asset"] = asset.version
                entry.inputs["asset"] = json.loads(data_json(asset))
                brand_id = asset.payload.get("brand", {}).get("uid")
                if not brand_id:
                    raise ValueError("Бренд актива отсутствует")
                brand = await self.metadata.get(market, "invest_get_brand", brand_id, partial(self._call, market))
                entry.issuer_id = brand_id
                entry.source_versions["brand"] = brand.version
                entry.inputs["brand"] = json.loads(data_json(brand))
                entry.country_of_risk = str(brand.payload.get("countryOfRisk") or "").upper()
                if len(entry.country_of_risk) != 3 or not entry.country_of_risk.isalpha():
                    entry.country_of_risk = ""
                entry.sector = str(brand.payload.get("sector") or "")
                entry.in_scope = entry.country_of_risk == "RUS"
                if not entry.in_scope:
                    entry.reasons.append("Страна риска бизнеса отличается от RUS" if entry.country_of_risk else "Страна риска бизнеса не подтверждена")
                    return entry
                series, version, quality = await daily_prices(market, identifier, as_of-timedelta(days=self.policy.lookback_days),
                                                               as_of, self.repo, call=partial(self._call, market))
                entry.series = series
                entry.source_versions.update(daily_series=version, daily_end=as_of.replace(hour=0, minute=0, second=0, microsecond=0).isoformat())
                entry.reasons.extend(quality)
                rate = None
                if entry.currency != "rub":
                    rates, proofs = await currency_rates(market, self.repo, [entry.currency], "rub", as_of, self.clock(), fx_catalog)
                    rate = rates.get(entry.currency)
                    entry.source_versions["fx"] = sha256(data_json(proofs).encode()).hexdigest()
                entry.reasons.extend(liquidity_metrics(entry, self.policy, as_of, rate))
                if entry.price is not None:
                    lots = await self._call(market, "invest_get_max_lots", {"accountId": await market.account_id(),
                        "instrumentId": f"{entry.ticker}_{entry.class_code}", "price": {"value": str(entry.price)}})
                    entry.max_buy_lots = optional_decimal(lots.get("buyLimits", {}).get("buyMaxLots"))
                    limits = {"buyMaxLots": lots.get("buyLimits", {}).get("buyMaxLots"), "currency": lots.get("currency"),
                              "price": str(entry.price), "observed_at": timestamp(self.clock()).isoformat()}
                    entry.inputs["account_limits"] = limits
                    entry.source_versions["account_limits"] = sha256(data_json(limits).encode()).hexdigest()
                    if entry.max_buy_lots is None or entry.max_buy_lots < 0:
                        entry.reasons.append("Лимит покупки на собственные средства не подтверждён")
                entry.included = not entry.reasons
            except (ToolError, ValueError, TypeError, AttributeError, KeyError):
                entry.reasons.append("Данные инструмента или доступность покупки получены частично")
            return entry

    async def collect(self, user_id: int, market) -> StockUniverse:
        requested_at = timestamp(self.clock())
        account_id = await market.account_id()
        accounts = await market.accounts()
        account = next((item for item in accounts if item.id == account_id), None)
        if account is None:
            raise ToolError("Выбранный счёт недоступен")
        raw = await self._call(market, "invest_list_shares", {"instrumentStatus": "INSTRUMENT_STATUS_ALL", "responseView": ["FULL"]})
        rows = raw.get("instruments", raw.get("shares"))
        if not isinstance(rows, list):
            raise ToolError("Каталог акций недоступен")
        fields = ("uid", "ticker", "classCode", "isin", "lot", "currency", "name", "apiTradeAvailableFlag", "forQualInvestorFlag",
                  "forIisFlag", "requiredTests", "positionUid", "assetUid", "first1dayCandleDate")
        catalog = [{name: row[name] for name in fields if name in row} for row in rows if isinstance(row, dict)]
        quality, rights = [], {}
        try:
            info = await self._call(market, "invest_get_user_info", {})
            rights = {name: info[name] for name in ("qualStatus", "qualifiedForWorkWith") if name in info}
        except (ToolError, ValueError, TypeError):
            quality.append("Квалификация и пройденные тесты получены частично")
        is_iis = account.type.endswith("IIS")
        selected = canonical_shares(catalog, rights, is_iis)
        fx_catalog = []
        if any(row.get("currency") not in {"rub", "", None} for row in selected):
            try:
                fx_catalog = currency_pairs(await self._call(market, "invest_list_currencies", {"instrumentStatus": "INSTRUMENT_STATUS_ALL", "responseView": ["FULL"]}))
            except (ToolError, ValueError, TypeError, AttributeError):
                quality.append("Справочник валютных пар недоступен")
        entries = await asyncio.gather(*(self._entry(market, row, rights, is_iis, requested_at, fx_catalog) for row in selected))
        eligible = [entry for entry in entries if entry.included]
        for offset in range(0, len(eligible), 100):
            batch = eligible[offset:offset+100]
            try:
                statuses = await self._call(market, "invest_get_trading_statuses", {"instrumentId": [entry.instrument_id for entry in batch]})
                by_code = {f"{item.get('ticker')}_{item.get('classCode')}": item for item in statuses.get("tradingStatuses", [])}
                for entry in batch:
                    status = by_code.get(f"{entry.ticker}_{entry.class_code}", {})
                    entry.trading_now = status.get("limitOrderAvailableFlag") is True or status.get("marketOrderAvailableFlag") is True
                    entry.source_versions["trading_status"] = sha256(data_json(status).encode()).hexdigest()
                    entry.inputs["trading_status"] = status
                    if not entry.trading_now:
                        entry.reasons.append("Приём заявок в текущем торговом статусе не подтверждён")
            except (ToolError, ValueError, TypeError, AttributeError):
                quality.append("Торговые статусы получены частично")
        finished_at = timestamp(self.clock())
        inputs = {"policy": asdict(self.policy), "rights": rights, "account_type": account.type,
                  "catalog": catalog, "catalog_version": sha256(data_json(catalog).encode()).hexdigest(), "historical_membership": "partial"}
        identity = sha256(data_json({"user": user_id, "account": account_id, "as_of": finished_at,
                                    "entries": entries, "inputs": inputs}).encode()).hexdigest()
        universe = StockUniverse(identity, user_id, account_id, requested_at, finished_at, entries, len(rows),
                                  self.policy.version, quality, json.loads(data_json(inputs)))
        self.repo.save_universe(user_id, account_id, universe)
        return universe
