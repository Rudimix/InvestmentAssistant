"""Показатели по подтверждённой отчётности и сопоставимые отраслевые оценки.

Мультипликаторы пересчитываются из капитализации и FinancialFact. Поля
брокерского TTM без периода и стандарта остаются справочными данными.
"""

from dataclasses import dataclass, field
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from statistics import median

from .market import optional_decimal
from .mcp_client import ToolError
from .models import AnalyticalRecord, BrokerMetadataRecord, data_json, timestamp


METHODOLOGY = "confirmed-fundamentals-v1"
BANK_SECTORS = {"financial", "banks", "banking"}


@dataclass
class FundamentalMetric(AnalyticalRecord):
    value: Decimal
    unit: str
    period_start: str
    period_end: str
    currency: str
    standard: str
    fact_ids: list[str]
    capitalization_version: str = ""
    capitalization_scope: str = "issuer"


@dataclass
class FundamentalAnalysis(AnalyticalRecord):
    instrument_id: str
    asset_id: str
    sector: str
    template: str
    metrics: dict[str, FundamentalMetric] = field(default_factory=dict)
    comparisons: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    score: Decimal | None = None
    methodology: str = METHODOLOGY


def _annual(fact):
    start, end = date.fromisoformat(fact.period_start), date.fromisoformat(fact.period_end)
    return (start.month == end.month % 12+1 and start.day == 1 and end.day == monthrange(end.year, end.month)[1]
            and 365 <= (end-start).days+1 <= 366)


async def load_capitalizations(repo, market, asset_ids, clock):
    """Запросить публичные оценки по 100 активов с суточным датированным кэшем.

    Согласно контракту брокера нулевое значение означает отсутствие данных.
    Периоды TTM и MRQ без дат не преобразуются в подтверждённые финансовые факты.
    """
    records, missing, quality = {}, [], []
    now = timestamp(clock())
    tool = "invest_get_asset_fundamentals"
    for asset_id in sorted(set(asset_ids)-{""}):
        record = repo.get_broker_metadata(tool, asset_id)
        if record and timedelta(0) <= now-record.observed_at < timedelta(days=1):
            records[asset_id] = record
        else:
            missing.append(asset_id)
    for offset in range(0, len(missing), 100):
        identifiers = missing[offset:offset+100]
        try:
            raw = await market.report_call(tool, {"assets": identifiers})
            observed_at = timestamp(clock())
            for row in raw.get("fundamentals", []):
                asset_id = row.get("assetUid")
                if asset_id not in identifiers:
                    continue
                payload = {"assetUid": asset_id, "currency": row.get("currency"),
                           "marketCapitalization": row.get("marketCapitalization"), "checked_at": observed_at.isoformat(), "capitalization_scope": "asset"}
                version = sha256(data_json(payload).encode()).hexdigest()
                records[asset_id] = repo.save_broker_metadata(BrokerMetadataRecord(tool, asset_id, version, observed_at, payload))
        except (ToolError, ValueError, TypeError, AttributeError):
            quality.append("Капитализации части эмитентов недоступны")
    return records, list(dict.fromkeys(quality))


def issuer_capitalizations(entries, records):
    """Объединить известные классы одного бренда, сохранив ограничение покрытия."""
    groups = {}
    for entry in entries:
        if entry.issuer_id and entry.in_scope:
            groups.setdefault(entry.issuer_id, {})[entry.asset_id] = records.get(entry.asset_id)
    result = {}
    for entry in entries:
        members = groups.get(entry.issuer_id, {})
        if not members or any(record is None for record in members.values()):
            continue
        values = [optional_decimal(record.payload.get("marketCapitalization")) for record in members.values()]
        currencies = {str(record.payload.get("currency") or "").lower() for record in members.values()}
        if any(value is None or value <= 0 for value in values) or len(currencies) != 1 or "" in currencies:
            continue
        payload = {"marketCapitalization": sum(values), "currency": next(iter(currencies)), "issuer_id": entry.issuer_id,
                   "capitalization_scope": "catalog_classes", "components": {asset: record.version for asset, record in members.items()}}
        version = sha256(data_json(payload).encode()).hexdigest()
        result[entry.asset_id] = BrokerMetadataRecord("calculated_issuer_capitalization", entry.asset_id, version,
                                                     max(record.observed_at for record in members.values()), payload)
    return result


def fundamental_analysis(entry, facts, capitalization, as_of):
    """Сохранить пропуски, исключить будущие и несовместимые финансовые строки."""
    as_of = timestamp(as_of)
    bank = entry.sector.casefold() in BANK_SECTORS
    result = FundamentalAnalysis(entry.instrument_id, entry.issuer_id or entry.asset_id, entry.sector, "bank" if bank else "corporate")
    identifiers = {entry.instrument_id, entry.asset_id, entry.ticker, f"{entry.ticker}_{entry.class_code}"}
    identifiers.update(entry.inputs.get("issuer_financial_ids", []))
    eligible = [fact for fact in facts if fact.instrument_id in identifiers and fact.value is not None
                and fact.value.is_finite() and fact.unit == "money" and fact.currency
                and fact.standard in {"IFRS", "RAS"} and fact.consolidated is True
                and fact.available_at is not None and timestamp(fact.available_at) <= as_of
                and date.fromisoformat(fact.period_end) <= as_of.date()
                and (as_of.date()-date.fromisoformat(fact.period_end)).days <= 1100]
    by_key = {}
    for fact in eligible:
        key = (fact.metric, fact.period_start, fact.period_end, fact.currency.lower(), fact.standard)
        by_key.setdefault(key, []).append(fact)
    clean = []
    for rows in by_key.values():
        if len({row.value for row in rows}) == 1:
            clean.append(max(rows, key=lambda row: timestamp(row.available_at)))
        else:
            result.reasons.append("Конфликтующие значения отчётности исключены")
    cap = None
    if (capitalization is not None and capitalization.identifier == entry.asset_id
            and capitalization.tool in {"invest_get_asset_fundamentals", "calculated_issuer_capitalization"} and capitalization.observed_at <= as_of
            and as_of-capitalization.observed_at <= timedelta(days=7)):
        cap = optional_decimal(capitalization.payload.get("marketCapitalization"))
        if cap is not None and cap <= 0:
            cap = None
    cap_currency = str(capitalization.payload.get("currency") or "").lower() if capitalization else ""
    cap_scope = capitalization.payload.get("capitalization_scope", "asset") if capitalization else ""
    if cap_scope not in {"issuer", "catalog_classes"}:
        cap = None
        result.reasons.append("Капитализация отдельного класса не подтверждает капитализацию эмитента")
    if cap_scope == "catalog_classes":
        result.reasons.append("Капитализация объединяет известные классы одного эмитента, полнота его структуры капитала не подтверждена")

    def add(name, value, rows, unit="ratio", uses_cap=False):
        source = rows[0]
        result.metrics[name] = FundamentalMetric(value, unit, source.period_start, source.period_end,
            source.currency.lower(), source.standard, [row.id for row in rows], capitalization.version if uses_cap else "", cap_scope if uses_cap else "issuer")

    def latest(metric, annual=False):
        rows = [fact for fact in clean if fact.metric == metric and (not annual or _annual(fact))
                and (as_of.date()-date.fromisoformat(fact.period_end)).days <= 550]
        if not rows:
            return None
        end = max(fact.period_end for fact in rows)
        current = [fact for fact in rows if fact.period_end == end]
        if len(current) != 1:
            result.reasons.append(f"Несопоставимые варианты показателя {metric}")
            return None
        return current[0]

    def matching(metric, source, stock=False):
        rows = [fact for fact in clean if fact.metric == metric and fact.period_end == source.period_end
                and (stock or fact.period_start == source.period_start)
                and fact.currency.lower() == source.currency.lower() and fact.standard == source.standard]
        return rows[0] if len(rows) == 1 else None

    for metric, ratio, annual in (("net_profit", "pe", True), ("equity", "pb", False)):
        fact = latest(metric, annual)
        if fact is None:
            result.reasons.append(f"Подтверждённый показатель {metric} недоступен")
        elif fact.value <= 0:
            result.reasons.append(f"Показатель {ratio} неприменим при неположительном {metric}")
        elif cap is not None and fact.currency.lower() == cap_currency:
            add(ratio, cap/fact.value, [fact], uses_cap=True)
        else:
            result.reasons.append(f"Для {ratio} отсутствует свежая капитализация в валюте отчётности")

    if not bank:
        ebitda = latest("ebitda", True)
        debt = matching("total_debt", ebitda, stock=True) if ebitda else None
        cash = matching("cash", ebitda, stock=True) if ebitda else None
        if ebitda and ebitda.value > 0 and debt and debt.value >= 0 and cash and cash.value >= 0:
            net_debt = debt.value-cash.value
            add("net_debt_to_ebitda", net_debt/ebitda.value, [ebitda, debt, cash])
            if cap is not None and cap_currency == ebitda.currency.lower() and cap+net_debt > 0:
                add("ev_ebitda", (cap+net_debt)/ebitda.value, [ebitda, debt, cash], uses_cap=True)
                result.reasons.append("EV рассчитана как капитализация плюс долг минус денежные средства, без корректировок на неконтролирующие доли")
        else:
            result.reasons.append("Для долговой нагрузки и EV/EBITDA недостаточно сопоставимых данных")

    for metric in ("revenue", "net_profit"):
        current = latest(metric, True)
        if current is None:
            continue
        previous = [fact for fact in clean if fact.metric == metric and _annual(fact)
                    and fact.currency.lower() == current.currency.lower() and fact.standard == current.standard
                    and date.fromisoformat(fact.period_end).year == date.fromisoformat(current.period_end).year-1
                    and fact.period_end[4:] == current.period_end[4:] and fact.period_start[4:] == current.period_start[4:]]
        if len(previous) == 1 and previous[0].value > 0:
            add(metric+"_growth", (current.value/previous[0].value-1)*100, [current, previous[0]], "percent")
        else:
            result.reasons.append(f"Изменение {metric} не рассчитано из сопоставимых положительных базовых данных")
    result.reasons = list(dict.fromkeys(result.reasons))
    return result


def compare_industry(analyses):
    """Минимум два других эмитента, точные периоды, валюта и стандарт.

Доли одного актива учитываются один раз. Ранг равен доле сопоставимых
эмитентов с более высоким мультипликатором с половинным весом равенства.
"""
    for analysis in analyses:
        analysis.comparisons = {}
        ranks = []
        required = ("pe", "pb") if analysis.template == "bank" else ("pe", "ev_ebitda")
        for name in required:
            metric = analysis.metrics.get(name)
            if metric is None or not analysis.sector or not analysis.asset_id:
                continue
            peers = {}
            for other in analyses:
                candidate = other.metrics.get(name)
                if (other.asset_id and other.asset_id != analysis.asset_id and other.sector == analysis.sector
                        and other.template == analysis.template and candidate is not None
                        and (candidate.period_start, candidate.period_end, candidate.currency, candidate.standard, candidate.capitalization_scope)
                        == (metric.period_start, metric.period_end, metric.currency, metric.standard, metric.capitalization_scope)):
                    peers.setdefault(other.asset_id, []).append((other.instrument_id, candidate.value))
            # Расхождение значений классов одного актива делает эмитента непригодным для сравнения.
            peers = {asset: rows[0] for asset, rows in peers.items() if len({value for _, value in rows}) == 1}
            if len(peers) < 2:
                continue
            values = [value for _, value in peers.values()]
            middle = median(values)
            rank = sum(Decimal(1) if value > metric.value else Decimal("0.5") if value == metric.value else Decimal(0)
                       for value in values)/len(values)*100
            analysis.comparisons[name] = {"peer_ids": [key for key, _ in peers.values()], "median": middle,
                "discount_percent": (1-metric.value/middle)*100, "percentile": rank,
                "potential_undervaluation": metric.value < middle and metric.capitalization_scope == "issuer"}
            ranks.append(rank)
        analysis.score = sum(ranks)/len(ranks) if len(ranks) == len(required) else None
        if analysis.score is None:
            analysis.reasons = list(dict.fromkeys(analysis.reasons+["Полный набор сопоставимых отраслевых оценок недоступен"]))
    return analyses
