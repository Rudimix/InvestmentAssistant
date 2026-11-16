"""Фиксированный эксперимент со сконструированными портфелями и публичными котировками."""

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from .analysis_periods import explicit_period
from .connections import Connections, TokenCipher, generate_key
from .config import McpSettings
from .market import optional_decimal
from .mcp_client import ToolError
from .models import Account, InvestorProfile, PortfolioSnapshot, SnapshotPosition, data_json, timestamp
from .portfolio_reports import PortfolioReportService
from .stock_universe import StockUniverseService
from .storage import JournalRepository


DATASET_METHODOLOGY = "recorded-bank-cases-v1"
CONTROLLED_OVERRIDES = {
    "short_history": {"maximum_candles": 10}, "missing_prices": {"prices_available": False},
    "foreign_business": {"countryOfRisk": "USA"}, "account_restricted": {"forQualInvestorFlag": True},
    "low_liquidity": {"volume_lots": "1"},
}


class ReplayMarket:
    """Контракт рыночного сервиса с замороженными публичными ответами.

Портфель, права счёта и лимиты покупки задаются условиями сценария.
Частные данные брокерского счёта в эксперименте отсутствуют.
"""

    def __init__(self, records, case):
        self.records, self.case = records, case
        self.as_of = timestamp(case["as_of"])
        self.rows = [record["response"]["instrument"] for record in records if record["tool"] == "invest_get_share"]

    async def account_id(self):
        return "evaluation-account"

    async def accounts(self):
        return [Account("evaluation-account", type="ACCOUNT_TYPE_TINKOFF")]

    async def snapshot(self, user_id, observed_at):
        if "snapshot" in self.case:
            snapshot = PortfolioSnapshot.from_dict(self.case["snapshot"])
            snapshot.user_id = user_id
            snapshot.id = sha256(data_json({"user": user_id, "case": self.case["id"], "as_of": self.case["as_of"]}).encode()).hexdigest()
            return snapshot
        cash = Decimal(self.case.get("cash", "5000"))
        positions = []
        if self.case["state"] != "empty_portfolio":
            for index, row in enumerate(self.rows):
                identity = f"{row['ticker']}_{row['classCode']}"
                raw = next(record["response"] for record in self.records if record["tool"] == "invest_get_candles"
                           and record["arguments"]["instrumentId"] == identity and record["arguments"]["interval"] == "CANDLE_INTERVAL_DAY")
                candles = sorted(raw["candles"], key=lambda item: item["time"])
                price = optional_decimal(candles[-1]["close"])
                quantity = Decimal(100 if index == 0 else 10)
                positions.append(SnapshotPosition(identity, row["ticker"], "share", quantity, "rub", price*quantity, price,
                    identity, row["assetUid"], "rub", aliases=[identity]))
        total = cash+sum((item.value for item in positions), Decimal(0))
        return PortfolioSnapshot("evaluation-snapshot", user_id, "evaluation-account", self.as_of, "rub", total,
                                 None if self.case["state"] == "missing_cash" else cash, positions)

    async def report_call(self, tool, arguments):
        state = self.case["state"]
        if tool == "invest_list_shares":
            rows = deepcopy(self.rows)
            if state == "account_restricted":
                for row in rows:
                    row["forQualInvestorFlag"] = True
            return {"instruments": rows}
        if tool == "invest_get_user_info":
            return {"qualStatus": False, "qualifiedForWorkWith": []}
        if tool == "invest_get_max_lots":
            return {"buyLimits": {"buyMaxLots": "100"}, "currency": "rub"}
        if tool == "invest_get_trading_statuses":
            return {"tradingStatuses": [{"ticker": row["ticker"], "classCode": row["classCode"],
                                         "limitOrderAvailableFlag": True} for row in self.rows]}
        if tool == "invest_get_operations":
            return {"items": [], "hasNext": False}
        if tool == "invest_get_news":
            return {"items": [], "hasNext": False}
        if tool == "invest_get_instrument_reports":
            return {"events": []}
        if tool == "invest_get_asset_fundamentals":
            return {"fundamentals": [row for record in self.records if record["tool"] == tool
                                      for row in record["response"].get("fundamentals", []) if row["assetUid"] in arguments["assets"]]}
        if tool == "invest_get_candles":
            rows = {}
            for record in self.records:
                request = record["arguments"]
                if (record["tool"] == tool and request["instrumentId"] == arguments["instrumentId"]
                        and request["interval"] == arguments["interval"]):
                    for row in record["response"].get("candles", []):
                        rows[row["time"]] = row
            if state == "missing_prices":
                rows = {}
            if state == "short_history":
                rows = dict(sorted(rows.items())[-10:])
            start, end = timestamp(arguments["from"]), timestamp(arguments["to"])
            selected = []
            for key, row in sorted(rows.items()):
                at = timestamp(key+"T00:00:00Z" if len(key) == 10 else key)
                if start.date() <= at.date() <= end.date():
                    row = deepcopy(row)
                    if state == "low_liquidity":
                        row["volume"] = "1"
                    selected.append(row)
            return {"priceCurrency": "rub", "candles": selected}
        for record in self.records:
            if record["tool"] == tool and record["arguments"] == arguments:
                response = deepcopy(record["response"])
                if tool == "invest_get_brand":
                    if state == "foreign_business":
                        response["countryOfRisk"] = "USA"
                    if state == "unknown_country":
                        response["countryOfRisk"] = ""
                return response
        raise ToolError("В зафиксированном источнике инструмент недоступен", temporary=False)


async def build_dataset(market_file):
    path = Path(market_file)
    raw = path.read_bytes()
    market = json.loads(raw)
    as_of = market["recorded_at"]
    cases = []
    states = ("complete", "complete", "complete", "complete", "complete", "complete", "no_profile", "short_horizon",
              "concentration", "cash_sufficient", "empty_portfolio", "missing_cash")
    for index, state in enumerate(states):
        cases.append({"id": f"analysis-{index+1:02}", "kind": "analysis", "mode": None, "state": state,
                      "as_of": as_of, "risk": ("low", "medium", "high")[index % 3], "goal": ("Сохранение капитала", "Регулярные выплаты", "Долгосрочный рост")[index % 3],
                      "cash": "50000" if state == "cash_sufficient" else "5000"})
    for mode in ("fundamental", "technical", "combined"):
        for index, state in enumerate(("complete", "short_history", "missing_prices", "foreign_business", "account_restricted", "low_liquidity")):
            cases.append({"id": f"{mode}-{index+1:02}", "kind": "screening", "mode": mode, "state": state,
                          "as_of": as_of, "risk": ("low", "medium", "high")[index % 3], "goal": "Долгосрочный рост", "cash": "50000"})
    source_versions = {str(index): record["version"] for index, record in enumerate(market["records"])}
    source_times = {str(index): record["retrieved_at"] for index, record in enumerate(market["records"])}
    for case in cases:
        case["controlled_overrides"] = CONTROLLED_OVERRIDES.get(case["state"], {})
        snapshot = await ReplayMarket(market["records"], case).snapshot(1, timestamp(as_of))
        case["snapshot"] = asdict(snapshot)
        case["profile"] = None if case["state"] == "no_profile" else asdict(InvestorProfile(1, 1, timestamp(as_of)-timedelta(days=1),
            case["goal"], 6 if case["state"] == "short_horizon" else 36, case["risk"], Decimal(20), Decimal(10000),
            Decimal(10) if case["state"] == "concentration" else Decimal(25), Decimal(40)))
        case["period"] = {"first": "01.10.2026", "last": timestamp(as_of).strftime("%d.%m.%Y"), "zone": "UTC"}
        case["source_versions"], case["source_available_at"] = source_versions, source_times
        case["methodologies"] = ["history-valuation-v1", "profile-v1", "russian-business-v1", "confirmed-fundamentals-v1", "sector-percentiles-v1"]
    result = {"schema_version": 1, "methodology": DATASET_METHODOLOGY, "market_file": path.name,
              "market_sha256": sha256(raw).hexdigest(), "data_kind": "constructed_accounts_recorded_public_market",
              "created_at": datetime.now(timezone.utc), "scenarios": cases, "limitations": [
                  "Портфели, профили, права и лимиты счёта сконструированы для эксперимента",
                  "Контрольные отказы используют явно заданные изменения страны риска, доступности и качества ценовых данных",
                  "Публичная выборка ограничена тремя банками, каталог рынка представлен частично",
                  "Котировки получены ретроспективно, исходные версии до даты получения не подтверждены",
                  "В наборе отсутствует подтверждённая финансовая отчётность, фундаментальные ранги ожидаются неполными",
                  "Рабочая прогнозная модель на рыночных примерах ещё не обучена",
                  "Основная метрика оценивает обоснованность сформированных предложений по зафиксированным входам"]}
    result["version"] = sha256(data_json(result).encode()).hexdigest()
    return json.loads(data_json(result))


def validate_dataset(dataset, directory):
    body = {key: value for key, value in dataset.items() if key != "version"}
    if dataset.get("version") != sha256(data_json(body).encode()).hexdigest():
        raise ValueError("Версия набора повреждена")
    cases = dataset["scenarios"]
    if len(cases) != 30 or len({case["id"] for case in cases}) != 30:
        raise ValueError("Набор должен содержать 30 разных сценариев")
    path = Path(directory)/dataset["market_file"]
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != dataset["market_sha256"]:
        raise ValueError("Версия публичных источников изменилась")
    market = json.loads(raw)
    for case in cases:
        at = timestamp(case["as_of"])
        if (case["kind"] not in {"analysis", "screening"} or not case["source_versions"]
                or any(timestamp(value) > at for value in case["source_available_at"].values())):
            raise ValueError("Непригодный вид сценария или будущая доступность источника")
        if case.get("controlled_overrides") != CONTROLLED_OVERRIDES.get(case["state"], {}):
            raise ValueError("Контрольное изменение входов не соответствует методике")
        for index, record in enumerate(market["records"]):
            if (record["version"] != sha256(data_json(record["response"]).encode()).hexdigest()
                    or case["source_versions"].get(str(index)) != record["version"]
                    or case["source_available_at"].get(str(index)) != record["retrieved_at"]):
                raise ValueError("Источники сценария не соответствуют замороженным ответам")
        profile = InvestorProfile.from_dict(case["profile"]) if case["profile"] else None
        if profile and profile.confirmed_at > at:
            raise ValueError("Профиль подтверждён после даты сценария")
    if {case["mode"] for case in cases if case["kind"] == "screening"} != {"fundamental", "technical", "combined"}:
        raise ValueError("Предусмотренные режимы подбора представлены не полностью")
    if not {"no_profile", "missing_prices", "short_history", "account_restricted"}.issubset({case["state"] for case in cases}):
        raise ValueError("Отказы представлены не полностью")
    return market


class EvaluationLlm:
    def __init__(self, client):
        self.client = client
        self.attempts, self.responses = 0, 0

    async def complete(self, messages, tools):
        self.attempts += 1
        result = await self.client.complete(messages, tools)
        self.responses += 1
        return result


async def run_evaluation(dataset, directory, repo, llm, model_name, provider_mode="test_double"):
    """Пройти общий путь отчёта с фиксированными внешними данными и реальным LLM-клиентом."""
    market_source = validate_dataset(dataset, directory)
    repo.save_evaluation_dataset(dataset["version"], dataset["scenarios"])
    identity = uuid4().hex
    probe = EvaluationLlm(llm) if llm else None
    outputs, proposals, refusals = [], [], []
    for case in dataset["scenarios"]:
        local = JournalRepository()
        reports = None
        try:
            user = local.get_or_create_user(1)
            local.save_credentials(user.id, b"evaluation-placeholder", "evaluation-account")
            if case["profile"]:
                profile = InvestorProfile.from_dict(case["profile"])
                local.save_profile(user.id, profile)
            market = ReplayMarket(market_source["records"], case)
            connections = Connections(local, TokenCipher(generate_key()), McpSettings())

            @asynccontextmanager
            async def session(chat_id):
                yield market

            connections.market = session
            at = timestamp(case["as_of"])
            reports = PortfolioReportService(connections, probe, clock=lambda: at)
            reports.screening.universe = StockUniverseService(local, lambda: at, call_interval=0)
            request = case["period"]
            period = explicit_period(request["first"], request["last"], request["zone"], at) if case["kind"] == "analysis" else None
            sent = []

            async def send(**kwargs):
                sent.append(kwargs["text"])
                return SimpleNamespace(message_id=len(sent))

            run = await reports.request(1, case["id"], send, period=period, report_kind=case["kind"], mode=case["mode"] or "combined")
            payload = json.loads(run["payload"]) if run["payload"] else {}
            output = {"scenario_id": case["id"], "state": run["state"], "manifest": json.loads(run["manifest"]), "payload": payload, "parts": sent}
            outputs.append(output)
            rows = payload.get("advice", {}).get("result", {}).get("proposals", [])
            if case["kind"] == "screening":
                rows = [row for row in payload.get("screening", {}).get("candidates", []) if row["score"] is not None]
            for index, row in enumerate(rows):
                proposals.append({"id": f"{case['id']}:{index+1}", "scenario_id": case["id"], "kind": case["kind"], "content": row})
            if not rows:
                if run["state"] != "delivered":
                    reason = run["last_error"] or "Ошибка подготовки или доставки отчёта"
                elif case["kind"] == "analysis":
                    reason = payload.get("advice", {}).get("result", {}).get("reason") or "AI воздержался от предложений"
                else:
                    reason = "Полный обязательный аналитический блок недоступен"
                refusals.append({"scenario_id": case["id"], "reason": reason, "report_state": run["state"]})
        finally:
            if reports:
                await reports.aclose()
            local.close()
    result = {"id": identity, "dataset_version": dataset["version"], "scenario_ids": [case["id"] for case in dataset["scenarios"]],
              "llm_model": model_name, "outputs": outputs, "proposals": proposals, "refusals": refusals,
              "limitations": dataset["limitations"], "data_kind": dataset["data_kind"], "expert_reviews": "pending"}
    result["llm_calls"] = {"provider_mode": provider_mode, "attempts": probe.attempts if probe else 0, "responses": probe.responses if probe else 0}
    modules = ("evaluation_replay.py", "portfolio_reports.py", "historical_service.py", "advice_llm.py", "portfolio_advice.py",
               "stock_universe.py", "stock_fundamentals.py", "stock_technicals.py", "growth_forecast.py")
    result["implementation_versions"] = {name: sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in modules}
    repo.save_evaluation_run(identity, dataset["version"], datetime.now(timezone.utc), result)
    return result
