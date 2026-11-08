"""Сквозная проверка: апдейты Telegram проходят через Dispatcher aiogram (без сети)."""

import itertools
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User

from _support import FakeToolClient
from investment_journal.__main__ import build_dispatcher
from investment_journal.config import load_settings
from investment_journal.llm import ChatCompletionsClient, InvestmentAgent
from investment_journal.storage import JournalRepository

_ids = itertools.count(1)


def update(text: str) -> Update:
    n = next(_ids)
    return Update(
        update_id=n,
        message=Message(
            message_id=n,
            date=datetime.now(),
            chat=Chat(id=7, type="private"),
            from_user=User(id=7, is_bot=False, first_name="Test"),
            text=text,
        ),
    )


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = FakeToolClient()
        settings = load_settings({"TELEGRAM_BOT_TOKEN": "42:TEST"}, dotenv=None)
        self.dp = build_dispatcher(settings, self.tools, JournalRepository())
        self.bot = Bot("42:TEST")
        self.sent: list[str] = []

        async def fake_call(method, request_timeout=None):
            if isinstance(method, SendMessage):
                self.sent.append(method.text)
            return True

        self.patch = patch.object(Bot, "__call__", AsyncMock(side_effect=fake_call))
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.bot.session.close()

    async def send(self, text: str) -> str:
        await self.dp.feed_update(self.bot, update(text))
        return self.sent[-1]

    async def test_full_idea_flow(self):
        self.assertIn("/portfolio", await self.send("/start"))
        self.assertEqual(await self.send("/price SiZ6"), "SIZ6: 90000")
        await self.send("/idea")
        await self.send("buy")
        await self.send("SiZ6")
        self.assertEqual(await self.send("Рубль ослабнет после снижения ставки"), "Записано #1: buy SIZ6 по 90000")
        self.tools.prices["SIZ6"] = 93000.0
        self.assertIn("подтверждается 1", await self.send("/review"))
        self.assertIn("не настроен", await self.send("какой у меня портфель?"))


class BuildDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_created_only_with_llm(self):
        env = {"TELEGRAM_BOT_TOKEN": "42:TEST", "LLM_BASE_URL": "http://localhost:11434/v1"}
        settings = load_settings(env, dotenv=None)
        llm = ChatCompletionsClient(settings.llm)
        dp = build_dispatcher(settings, FakeToolClient(), JournalRepository(), llm)
        self.assertIsInstance(dp["agent"], InvestmentAgent)
        self.assertIsNone(build_dispatcher(settings, FakeToolClient(), JournalRepository())["agent"])
        await llm.aclose()


if __name__ == "__main__":
    unittest.main()
