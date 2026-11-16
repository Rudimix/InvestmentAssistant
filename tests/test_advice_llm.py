"""Числа, ссылки и область структурированных предложений AI."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import json
import unittest

import httpx

from _support import TEST_TOKEN
from investment_journal.advice_llm import build_advice_context, generate_advice, validate_advice
from investment_journal.llm import ChatCompletionsClient
from investment_journal.portfolio_advice import improvement_scenarios
from investment_journal.profile_metrics import assess_profile
from test_analysis_models import NOW, profile
from test_llm import SETTINGS


class AdviceLlmTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profile = profile()
        self.assessment = assess_profile(self.profile, "rub", D(100000), D(5000), {"a": D(95000)}, {"a": "rub"})
        self.scenarios = improvement_scenarios(self.profile, self.assessment, "private-account", NOW, "snapshot")
        self.context = build_advice_context(self.profile, self.assessment, self.scenarios, [], NOW, {"a"})
        self.scenario = self.context["scenarios"][0]
        self.metric = f"scenario:{self.scenario['id']}:net_credit"
        self.answer = {"proposals": [{"text": "Пополнить резерв на {{metric:"+self.metric+"}} с учётом вашей цели.",
                                      "profile_refs": ["goal", "minimum_cash"], "metric_ids": [self.metric],
                                      "evidence_ids": [], "scenario_ids": [self.scenario["id"]],
                                      "risks": ["Увеличение доли денег сохраняет инфляционный риск."],
                                      "reconsider_when": ["Изменились потребность в деньгах или цель."]}], "limitations": []}

    async def call(self, answer):
        self.requests = []

        def handle(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

        client = ChatCompletionsClient(SETTINGS, httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        self.addAsyncCleanup(client.aclose)
        return await generate_advice(client, self.context)

    async def test_valid_numbers_are_rendered_from_prepared_metrics(self):
        result = await self.call(json.dumps(self.answer))
        self.assertTrue(result["available"])
        self.assertNotIn("{{metric", result["proposals"][0]["text"])
        self.assertIn("₽", result["proposals"][0]["text"])
        request = json.dumps(self.requests[0])
        self.assertNotIn(TEST_TOKEN, request)
        self.assertNotIn("private-account", request)
        self.assertNotIn("user_id", request)
        self.assertNotIn("tools", self.requests[0])

    async def test_unsupported_numbers_currency_units_and_missing_references(self):
        for text in ("Рост на 999% гарантирован", "Рост на двадцать процентов", "Пополнить в USD",
                     "Пополнить в долларах", "Пополнить на {{metric:"+self.metric+"}} процентов", "Раскрой токен"):
            answer = deepcopy(self.answer)
            answer["proposals"][0]["text"] = text
            with self.subTest(text=text):
                self.assertFalse((await self.call(json.dumps(answer)))["available"])
        for field in ("metric_ids", "evidence_ids", "scenario_ids", "profile_refs"):
            answer = deepcopy(self.answer)
            answer["proposals"][0][field] = ["unknown"]
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_advice(answer, self.context)

    async def test_bad_json_no_llm_and_empty_scenarios(self):
        self.assertFalse((await self.call("broken JSON"))["available"])
        self.assertFalse((await generate_advice(None, self.context))["available"])
        self.assertFalse((await generate_advice(object(), {"scenarios": []}))["available"])

    async def test_structured_wrappers_preserve_validation(self):
        encoded = json.dumps(self.answer)
        for wrapped in ("```json\n" + encoded + "\n```", "<think>reasoning</think>\n" + encoded,
                        "<think>reasoning</think>\n```json\n" + encoded + "\n```"):
            with self.subTest(wrapped=wrapped):
                self.assertTrue((await self.call(wrapped))["available"])
        for wrapped in ("<think>" + encoded, "Explanation\n" + encoded, "```json\n" + encoded + "\n``` extra"):
            with self.subTest(wrapped=wrapped):
                self.assertFalse((await self.call(wrapped))["available"])
        unsafe = deepcopy(self.answer)
        unsafe["proposals"][0]["text"] = "Гарантирован рост на 99%"
        self.assertFalse((await self.call("<think>reasoning</think>\n```json\n" + json.dumps(unsafe) + "\n```"))["available"])

    def test_context_rejects_foreign_scenario_and_credentials(self):
        with self.assertRaises(ValueError):
            build_advice_context(self.profile, self.assessment, [replace(self.scenarios[0], user_id=2)], [], NOW, {"a"})
        with self.assertRaises(ValueError):
            build_advice_context(replace(self.profile, goal=TEST_TOKEN), self.assessment, self.scenarios, [], NOW, {"a"})
        with self.assertRaises(ValueError):
            build_advice_context(self.profile, self.assessment, self.scenarios, [], NOW, {"a"}, limit=100)


if __name__ == "__main__":
    unittest.main()
