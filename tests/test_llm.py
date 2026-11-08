"""LLM-агент с OpenAI-совместимым API (HTTP подменён MockTransport)."""

import json
import unittest

import httpx

from _support import FakeToolClient, mcp_settings
from investment_journal.config import LlmSettings
from investment_journal.journal import JournalService
from investment_journal.llm import ChatCompletionsClient, InvestmentAgent, LlmError, parse_arguments
from investment_journal.market import MarketDataService
from investment_journal.models import DecisionDraft
from investment_journal.storage import JournalRepository

SETTINGS = LlmSettings(base_url="https://llm.example/v1/", api_key="k", model="test-model")


def tool_call(name, args, call_id="c1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    ]}


class ScriptedLlm:
    """Отдаёт заранее заданные ответы и запоминает запросы."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": self.replies.pop(0)}]})


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def make_agent(self, replies, max_steps=6):
        self.llm = ScriptedLlm(replies)
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.llm))
        self.tools = FakeToolClient()
        self.journal = JournalService(JournalRepository(), MarketDataService(self.tools))
        client = ChatCompletionsClient(SETTINGS, http)
        return InvestmentAgent(client, mcp_settings().read_only_tools, max_steps)

    async def test_plain_answer(self):
        agent = self.make_agent([{"role": "assistant", "content": "Привет!"}])
        self.assertEqual(await agent.ask(1, "привет", self.journal), "Привет!")
        request = self.llm.requests[0]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["messages"][0]["role"], "system")

    async def test_only_read_only_tools_are_exposed(self):
        agent = self.make_agent([{"role": "assistant", "content": "ok"}])
        await agent.ask(1, "что есть?", self.journal)
        names = {t["function"]["name"] for t in self.llm.requests[0]["tools"]}
        self.assertEqual(names, {"invest_get_market_values", "invest_get_portfolio", "journal_list", "journal_review"})

    async def test_mcp_tool_call_roundtrip(self):
        agent = self.make_agent([
            tool_call("invest_get_market_values", {"instrumentId": ["SiZ6_SPBFUT"]}),
            {"role": "assistant", "content": "SiZ6 стоит 90000"},
        ])
        self.assertEqual(await agent.ask(1, "цена SiZ6?", self.journal), "SiZ6 стоит 90000")
        tool_msg = self.llm.requests[1]["messages"][-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertEqual(json.loads(tool_msg["content"])["instruments"][0]["values"][0]["value"], {"value": "90000.0"})

    async def test_trading_tool_is_refused(self):
        agent = self.make_agent([
            tool_call("invest_create_order", {"instrumentId": "SiZ6_SPBFUT", "direction": "ORDER_DIRECTION_BUY"}),
            {"role": "assistant", "content": "Не могу"},
        ])
        await agent.ask(1, "купи", self.journal)
        self.assertIn("недоступен", self.llm.requests[1]["messages"][-1]["content"])
        self.assertEqual(self.tools.calls, [])

    async def test_journal_tools(self):
        agent = self.make_agent([
            tool_call("journal_list", {}),
            tool_call("journal_review", {}, "c2"),
            {"role": "assistant", "content": "готово"},
        ])
        user = self.journal.user(1)
        await self.journal.record(DecisionDraft(user.id, "buy", "SIZ6", "Ожидаю ослабления рубля"))
        await agent.ask(1, "как мои идеи?", self.journal)
        self.assertIn("«Ожидаю ослабления рубля»", self.llm.requests[1]["messages"][-1]["content"])
        self.assertIn("Итого", self.llm.requests[2]["messages"][-1]["content"])

    async def test_tool_error_is_passed_to_llm(self):
        agent = self.make_agent([
            tool_call("invest_get_portfolio", {"accountId": "nope"}),
            {"role": "assistant", "content": "нет данных"},
        ])
        await agent.ask(1, "цена SBER?", self.journal)
        self.assertIn("Ошибка", self.llm.requests[1]["messages"][-1]["content"])

    async def test_bad_arguments_json(self):
        reply = tool_call("journal_list", {})
        reply["tool_calls"][0]["function"]["arguments"] = "{oops"
        agent = self.make_agent([reply, {"role": "assistant", "content": "ок"}])
        self.assertEqual(await agent.ask(1, "?", self.journal), "ок")

    def test_parse_arguments(self):
        def call(arguments):
            return {"function": {"name": "x", "arguments": arguments}}
        self.assertEqual(parse_arguments(call('{"ticker": "SiZ6"}')), {"ticker": "SiZ6"})
        self.assertEqual(parse_arguments(call("[1, 2]")), {})
        self.assertEqual(parse_arguments(call("{oops")), {})
        self.assertEqual(parse_arguments(call(None)), {})

    async def test_step_limit(self):
        agent = self.make_agent([tool_call("journal_list", {})] * 2, max_steps=2)
        self.assertIn("Слишком много шагов", await agent.ask(1, "?", self.journal))

    async def test_history_kept_per_chat_and_reset(self):
        agent = self.make_agent([{"role": "assistant", "content": "a"}] * 3)
        await agent.ask(1, "первый", self.journal)
        await agent.ask(1, "второй", self.journal)
        self.assertEqual([m["content"] for m in self.llm.requests[1]["messages"][1:]], ["первый", "a", "второй"])
        agent.reset(1)
        await agent.ask(1, "заново", self.journal)
        self.assertEqual(len(self.llm.requests[2]["messages"]), 2)


class ClientErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_status(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key")))
        with self.assertRaisesRegex(LlmError, "401"):
            await ChatCompletionsClient(SETTINGS, http).complete([], [])

    async def test_malformed_body(self):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        with self.assertRaises(LlmError):
            await ChatCompletionsClient(SETTINGS, http).complete([], [])

    async def test_network_error(self):
        def fail(request):
            raise httpx.ConnectError("down")
        http = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        with self.assertRaisesRegex(LlmError, "недоступна"):
            await ChatCompletionsClient(SETTINGS, http).complete([], [])

    async def test_url_and_auth_header(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await ChatCompletionsClient(SETTINGS, http).complete([], [])
        self.assertEqual(str(seen[0].url), "https://llm.example/v1/chat/completions")
        self.assertEqual(seen[0].headers["authorization"], "Bearer k")
        self.assertNotIn("tools", json.loads(seen[0].content))


if __name__ == "__main__":
    unittest.main()
