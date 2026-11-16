"""Полные пользовательские сценарии с MCP, LLM HTTP и Telegram, подменёнными на границах."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from itertools import count
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from _support import TEST_KEY, FakeToolClient, value
from investment_journal.__main__ import build_dispatcher
from investment_journal.config import McpSettings, load_settings
from investment_journal.connections import Connections, TokenCipher
from investment_journal.llm import ChatCompletionsClient
from investment_journal.mcp_client import ToolError
from investment_journal.portfolio_reports import PortfolioReportService
from investment_journal.report_scheduling import ReportScheduler
from investment_journal.storage import JournalRepository
from test_portfolio_market import NOW, portfolio_response
from test_portfolio_sources import FIXTURE

SHARE_UID = "e6123145-9665-43e0-8413-cd61b8aa9b13"
BOND_UID = "11111111-1111-4111-8111-111111111111"
TOKEN_A = "t."+"A"*64
TOKEN_B = "t."+"B"*64


class ScenarioTools(FakeToolClient):
    def __init__(self, clock, owner="a", extra_cash=0):
        super().__init__()
        self.clock = clock
        self.extra_cash = extra_cash
        self.news_unavailable = False
        self.accounts = [{"id": f"owner-{owner}-{suffix}", "name": suffix, "accessLevel": "ACCOUNT_ACCESS_LEVEL_READ_ONLY"}
                         for suffix in ("first", "second")]

    async def list_tools(self):
        return FIXTURE["tools"]

    def portfolio(self, account_id):
        elapsed = self.clock()-NOW
        price, cash = 100, 20
        if elapsed >= timedelta(hours=6):
            price, cash = 110, 120
        if elapsed >= timedelta(hours=19):
            price, cash = 120, 124
        cash += self.extra_cash
        raw = portfolio_response()
        raw["accountId"] = account_id
        raw["totalAmountPortfolio"]["value"] = str(price*2+20+cash)
        raw["totalAmountCurrencies"]["value"] = str(cash)
        raw["totalAmountShares"]["value"] = str(price*2)
        raw["positions"][0].update(instrumentUid=SHARE_UID, ticker="SBER", currentPrice={"value": str(price), "currency": "rub"})
        raw["positions"][1]["instrumentUid"] = BOND_UID
        raw["positions"][2]["quantity"] = value(cash)
        return raw

    async def call(self, name, arguments=None):
        args = arguments or {}
        if name not in {"invest_get_portfolio", "invest_get_operations", "invest_get_news", "invest_get_instrument_reports"}:
            return await super().call(name, args)
        self.calls.append((name, args))
        if name in {"invest_get_portfolio", "invest_get_operations"}:
            if args["accountId"] not in {account["id"] for account in self.accounts}:
                raise ToolError("foreign account", authorization=True, temporary=False)
        if name == "invest_get_portfolio":
            return self.portfolio(args["accountId"])
        if name == "invest_get_operations":
            items = []
            for number, hour, kind, amount in (("deposit", 3, "INPUT", 100), ("income", 12, "DIVIDEND", 5),
                                               ("fee", 13, "BROKER_FEE", -1)):
                items.append({"id": number, "date": (NOW+timedelta(hours=hour)).isoformat(),
                              "type": "OPERATION_TYPE_"+kind, "state": "OPERATION_STATE_EXECUTED",
                              "payment": {"value": str(amount), "currency": "rub"}})
            return {"items": items, "hasNext": False}
        if name == "invest_get_news":
            if self.news_unavailable:
                raise ToolError("news unavailable")
            items = []
            if args["instrumentId"] == "SBER":
                items = [{"id": self.clock().isoformat(), "title": "Событие эмитента", "content": "Материал источника",
                          "source": "test", "url": "https://example.com/event", "ts": (self.clock()-timedelta(minutes=30)).isoformat(),
                          "instrumentId": [{"instrument": {"instrumentUid": SHARE_UID, "ticker": "SBER"}}]}]
            return {"items": items, "hasNext": False}
        events = []
        if args["instrumentId"] == SHARE_UID:
            events = [{"instrumentId": SHARE_UID, "reportDate": self.clock().date().isoformat(),
                       "periodYear": 2026, "periodNum": 3, "periodType": "PERIOD_TYPE_QUARTER"}]
        return {"events": events}


class PortfolioEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = NOW
        self.tools_a = ScenarioTools(lambda: self.now)
        self.tools_b = ScenarioTools(lambda: self.now, "b", extra_cash=1000)
        sources = {TOKEN_A: self.tools_a, TOKEN_B: self.tools_b}

        @asynccontextmanager
        async def open_source(settings):
            token = settings.headers["Authorization"].removeprefix("Bearer ")
            yield sources[token]

        self.repo = JournalRepository()
        self.connections = Connections(self.repo, TokenCipher(TEST_KEY), McpSettings(), open_source)
        self.settings = load_settings({"TELEGRAM_BOT_TOKEN": "42:TEST", "TOKEN_ENCRYPTION_KEY": TEST_KEY,
                                       "LLM_BASE_URL": "https://llm.example/v1", "LLM_MODEL": "test"}, dotenv=None)
        self.model_inputs = []
        self.llm_down = False

        def model(request):
            payload = json.loads(request.content)
            self.model_inputs.append(payload)
            if self.llm_down:
                return httpx.Response(503, text="unavailable")
            context = json.loads(payload["messages"][-1]["content"])
            metric = "change_after_flows" if "change_after_flows" in context["metrics"] else "largest_weight"
            answer = {"summary": "Структура портфеля и наблюдаемые изменения.", "limitations": [], "findings": [
                {"text": "Вывод основан на рассчитанном показателе с учётом доступных данных.", "metric_ids": [metric],
                 "event_ids": [], "confidence": "medium", "kind": "observation"},
            ]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]})

        self.llm = ChatCompletionsClient(self.settings.llm, httpx.AsyncClient(transport=httpx.MockTransport(model)))
        self.dp = build_dispatcher(self.settings, self.connections, self.llm)
        self.reports = PortfolioReportService(self.connections, self.llm, lambda: self.now)
        self.dp["reports"] = self.reports
        self.bot = Bot("42:TEST")
        self.sent = []
        self.numbers = count(1000)

        async def telegram(method, request_timeout=None):
            if isinstance(method, SendMessage):
                self.sent.append(method)
                return Message(message_id=next(self.numbers), date=self.now,
                               chat=Chat(id=method.chat_id, type="private"), text=method.text, reply_markup=method.reply_markup)
            return True

        self.patch = patch.object(Bot, "__call__", AsyncMock(side_effect=telegram))
        self.patch.start()
        self.scheduler = ReportScheduler(self.reports, self.bot.send_message)

    async def asyncTearDown(self):
        await self.scheduler.stop()
        await self.reports.aclose()
        await self.llm.aclose()
        await self.bot.session.close()
        self.repo.close()
        self.patch.stop()

    async def send(self, text, chat_id=7):
        number = next(self.numbers)
        message = Message(message_id=number, date=self.now, chat=Chat(id=chat_id, type="private"),
                          from_user=User(id=chat_id, is_bot=False, first_name="Test"), text=text)
        await self.dp.feed_update(self.bot, Update(update_id=number, message=message))

    async def click(self, label, chat_id=7):
        method = next(item for item in reversed(self.sent) if item.chat_id == chat_id and item.reply_markup is not None
                      and any(button.text == label for row in item.reply_markup.inline_keyboard for button in row))
        button = next(button for row in method.reply_markup.inline_keyboard for button in row if button.text == label)
        number = next(self.numbers)
        message = Message(message_id=number, date=self.now, chat=Chat(id=chat_id, type="private"), text=method.text,
                          reply_markup=method.reply_markup)
        callback = CallbackQuery(id=str(number), from_user=User(id=chat_id, is_bot=False, first_name="Test"),
                                 chat_instance="test", message=message, data=button.callback_data)
        await self.dp.feed_update(self.bot, Update(update_id=number, callback_query=callback))

    async def connect(self, chat_id=7, token=TOKEN_A):
        await self.send("/connect", chat_id)
        await self.send(token, chat_id)

    async def tick(self):
        await self.scheduler.tick()
        tasks = list(self.scheduler._jobs.values())
        if tasks:
            await asyncio.gather(*tasks)
        await asyncio.sleep(0)

    async def test_connect_select_manual_interval_and_calendar_reports(self):
        await self.connect()
        await self.send("/account 2")
        await self.send("/analysis")
        user = self.connections.user(7)
        first = self.repo.last_run(user.id)
        self.assertEqual(first["state"], "delivered")
        self.assertEqual(first["account_id"], "owner-a-second")
        self.assertTrue(json.loads(first["payload"])["explanation"]["available"])
        await self.send("/schedule")
        await self.click("Интервал")
        await self.click("UTC")
        await self.send("6ч")
        await self.click("Период анализа")
        await self.send("сравнение")
        await self.click("Сохранить")
        self.now += timedelta(hours=6)
        await self.tick()
        interval = self.repo.last_run(user.id)
        result = json.loads(interval["payload"])
        self.assertEqual(interval["origin"], "scheduled")
        self.assertEqual(result["analysis"]["metrics"]["external_flow"], "100")
        self.assertEqual(Decimal(result["analysis"]["metrics"]["change_after_flows"]), Decimal(20))
        self.assertEqual(interval["baseline_id"], first["snapshot_id"])
        self.assertTrue(any(event["url"] == "https://example.com/event" for event in result["events"]["items"]))
        await self.send("/schedule")
        await self.click("Календарь")
        await self.click("UTC")
        await self.send("ежедневно 19:00")
        await self.click("Сохранить")
        self.now = NOW+timedelta(hours=19)
        await self.tick()
        calendar = self.repo.last_run(user.id)
        result = json.loads(calendar["payload"])
        self.assertEqual(calendar["state"], "delivered")
        self.assertEqual(calendar["baseline_id"], interval["snapshot_id"])
        self.assertEqual(Decimal(result["analysis"]["metrics"]["change_after_flows"]), Decimal(24))
        self.assertEqual(Decimal(result["analysis"]["metrics"]["unallocated_change"]), Decimal(0))
        self.assertTrue(all(part["state"] == "sent" for part in self.repo.get_parts(user.id, calendar["id"])))
        self.assertTrue(any("Выводы AI" in item.text for item in self.sent))
        account_queries = [args["accountId"] for name, args in self.tools_a.calls if name in {"invest_get_portfolio", "invest_get_operations"}]
        self.assertEqual(set(account_queries), {"owner-a-second"})

    async def test_two_tokens_and_fallback_after_source_and_llm_failure(self):
        await self.connect(7, TOKEN_A)
        await self.connect(8, TOKEN_B)
        await asyncio.gather(self.send("/analysis", 7), self.send("/analysis", 8))
        first_user, second_user = self.connections.user(7), self.connections.user(8)
        first = json.loads(self.repo.last_run(first_user.id)["payload"])
        second = json.loads(self.repo.last_run(second_user.id)["payload"])
        self.assertEqual(first["analysis"]["metrics"]["total"], "240")
        self.assertEqual(second["analysis"]["metrics"]["total"], "1240")
        self.assertNotIn(TOKEN_A, json.dumps(self.model_inputs))
        self.assertNotIn(TOKEN_B, json.dumps(self.model_inputs))
        self.llm_down = True
        self.tools_a.news_unavailable = True
        self.now += timedelta(hours=6)
        await self.send("/analysis", 7)
        run = self.repo.last_run(first_user.id)
        saved = json.loads(run["payload"])
        self.assertEqual(run["state"], "delivered")
        self.assertFalse(saved["explanation"]["available"])
        self.assertTrue(any(state == "unavailable" for state in saved["events"]["sources"].values()))
        self.assertEqual(Decimal(saved["analysis"]["metrics"]["change_after_flows"]), Decimal(20))
        for tools, prefix in ((self.tools_a, "owner-a-"), (self.tools_b, "owner-b-")):
            self.assertTrue(all(args["accountId"].startswith(prefix) for name, args in tools.calls
                                if name in {"invest_get_portfolio", "invest_get_operations"}))
