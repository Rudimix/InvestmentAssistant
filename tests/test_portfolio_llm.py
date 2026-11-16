"""Структура AI-ответа и привязка объяснения к доступным данным."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import unittest

import httpx

from _support import TEST_TOKEN
from investment_journal.analytics import apply_portfolio_flows, portfolio_structure
from investment_journal.llm import ChatCompletionsClient, portfolio_explanation
from investment_journal.market import snapshot_from
from investment_journal.models import EventBatch, InstrumentEvent, OperationBatch, PortfolioOperation
from investment_journal.portfolio_events import report_context
from test_llm import SETTINGS
from test_portfolio_market import NOW, portfolio_response


def explanation():
    return {"summary": "Портфель требует внимания к концентрации.", "limitations": [], "findings": [
        {"text": "Крупнейшая позиция определяет значимую часть структуры.", "metric_ids": ["largest_weight"],
         "event_ids": [], "confidence": "high", "kind": "observation"},
    ]}


class PortfolioLlmTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, response, context=None):
        self.requests = []

        def handler(request):
            self.requests.append(json.loads(request.content))
            if isinstance(response, Exception):
                raise response
            return httpx.Response(200, json={"choices": [{"message": {"content": response}}]})

        client = ChatCompletionsClient(SETTINGS, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            return await portfolio_explanation(client, context or {"metrics": {"largest_weight": "70"}, "events": []})
        finally:
            await client.aclose()

    async def test_valid_and_unknown_references(self):
        valid = await self.call(json.dumps(explanation()))
        self.assertTrue(valid["available"])
        self.assertNotIn("tools", self.requests[0])
        bad = explanation()
        bad["findings"][0]["metric_ids"] = ["invented"]
        self.assertFalse((await self.call(json.dumps(bad)))["available"])

    async def test_bad_json_timeout_and_unconfigured_llm(self):
        self.assertFalse((await self.call("invalid JSON"))["available"])
        self.assertFalse((await self.call(httpx.ReadTimeout("timeout")))["available"])
        self.assertFalse((await portfolio_explanation(None, {}))["available"])

    async def test_report_numbers_are_bound_to_known_metric_templates(self):
        answer = explanation()
        answer["findings"][0]["text"] = "Доля крупнейшей позиции {{metric:largest_weight}}."
        result = await self.call(json.dumps(answer))
        self.assertTrue(result["available"])
        self.assertIn("70%", result["findings"][0]["text"])
        answer["findings"][0]["text"] = "Гарантированный рост на 999%."
        self.assertFalse((await self.call(json.dumps(answer)))["available"])

    async def test_possible_explanation_needs_both_sources_and_limited_confidence(self):
        answer = explanation()
        answer["findings"][0].update(kind="possible_explanation", event_ids=["news"], confidence="medium")
        context = {"metrics": {"largest_weight": "70"}, "events": [{"id": "news"}]}
        self.assertTrue((await self.call(json.dumps(answer), context))["available"])
        answer["findings"][0]["confidence"] = "high"
        self.assertFalse((await self.call(json.dumps(answer), context))["available"])

    async def test_evaluation_scenarios_use_known_metrics_and_scoped_context(self):
        snapshot = snapshot_from(portfolio_response(), 1, "account", NOW)
        concentration = portfolio_structure(snapshot)
        before = replace(snapshot, id="before", total=Decimal(100000), observed_at=NOW-timedelta(days=1))
        after = replace(snapshot, total=Decimal(110000))
        deposit = PortfolioOperation("deposit", NOW-timedelta(hours=1), "OPERATION_TYPE_INPUT",
                                     "OPERATION_STATE_EXECUTED", Decimal(10000), "rub")
        flows = apply_portfolio_flows(portfolio_structure(after), before, OperationBatch([deposit], True))
        calendar = InstrumentEvent("calendar", "report_calendar", ["share"], "Публикация отчётности",
                                   "source", event_at=NOW, period="2026 Q3")
        news = InstrumentEvent("news", "news", ["share"], "Событие эмитента", "source",
                               url="https://example.com/news", published_at=NOW-timedelta(hours=1))
        scenarios = [
            (report_context(concentration, EventBatch()), "largest_weight", [], "observation",
             "Крупнейшая позиция влияет на концентрацию портфеля."),
            (report_context(flows, EventBatch()), "change_after_flows", [], "observation",
             "Изменение общей стоимости объясняется внешним пополнением."),
            (report_context(concentration, EventBatch([calendar], found=1)), None, ["calendar"], "observation",
             "В календаре указана публикация отчётности, содержание документа недоступно."),
            (report_context(concentration, EventBatch([news], found=1)), "largest_weight", ["news"], "possible_explanation",
             "Событие может быть связано с восприятием крупнейшей позиции, причинная связь не установлена."),
        ]
        for context, metric, events, kind, text in scenarios:
            with self.subTest(kind=kind, metric=metric, events=events):
                answer = explanation()
                answer["findings"][0].update(text=text, metric_ids=[metric] if metric else [], event_ids=events,
                                             kind=kind, confidence="medium")
                self.assertTrue((await self.call(json.dumps(answer), context))["available"])
                payload = self.requests[0]
                sent = json.loads(payload["messages"][1]["content"])
                self.assertNotIn(TEST_TOKEN, json.dumps(payload))
                self.assertNotIn("account_id", sent)
                self.assertEqual(len(payload["messages"]), 2)
                if metric == "change_after_flows":
                    self.assertEqual(sent["metrics"][metric], "0")
                    self.assertEqual(sent["metrics"]["external_flow"], "10000")
                if events == ["calendar"]:
                    self.assertEqual(sent["events"][0]["kind"], "report_calendar")
                    self.assertEqual(sent["events"][0]["content"], "")
