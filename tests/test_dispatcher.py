"""Сквозная проверка: апдейты Telegram проходят через Dispatcher aiogram (без сети)."""

import itertools
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import Chat, Message, Update, User

from _support import TEST_TOKEN, FakeToolClient, make_connections
from investment_journal.__main__ import build_dispatcher
from investment_journal.config import load_settings
from investment_journal.llm import ChatCompletionsClient, InvestmentAgent

_ids = itertools.count(1)


def update(text: str, chat: Chat = Chat(id=7, type="private")) -> Update:
    n = next(_ids)
    return Update(
        update_id=n,
        message=Message(
            message_id=n,
            date=datetime.now(),
            chat=chat,
            from_user=User(id=7, is_bot=False, first_name="Test"),
            text=text,
        ),
    )


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tools = FakeToolClient()
        settings = load_settings({"TELEGRAM_BOT_TOKEN": "42:TEST"}, dotenv=None)
        self.dp = build_dispatcher(settings, make_connections(self.tools, connected=()))
        self.bot = Bot("42:TEST")
        self.sent: list[str] = []
        self.deleted: list[int] = []

        async def fake_call(method, request_timeout=None):
            if isinstance(method, SendMessage):
                self.sent.append(method.text)
            if isinstance(method, DeleteMessage):
                self.deleted.append(method.message_id)
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
        self.assertIn("/connect", await self.send("/start"))
        self.assertIn("подключите", await self.send("/price SiZ6"))
        self.assertIn("Только чтение", await self.send("/connect"))
        self.assertIn("Токен подключён", await self.send(TEST_TOKEN))
        self.assertEqual(len(self.deleted), 1)
        self.assertEqual(await self.send("/price SiZ6"), "SIZ6: 90000")
        await self.send("/idea")
        await self.send("buy")
        await self.send("SiZ6")
        self.assertEqual(await self.send("Рубль ослабнет после снижения ставки"), "Записано #1: buy SIZ6 по 90000")
        self.tools.prices["SIZ6"] = 93000.0
        self.assertIn("подтверждается 1", await self.send("/review"))
        self.assertIn("не настроен", await self.send("какой у меня портфель?"))

    async def test_group_chats_are_ignored(self):
        await self.dp.feed_update(self.bot, update("/start", Chat(id=-100, type="group")))
        self.assertEqual(self.sent, [])


class BuildDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_created_only_with_llm(self):
        env = {"TELEGRAM_BOT_TOKEN": "42:TEST", "LLM_BASE_URL": "http://localhost:11434/v1"}
        settings = load_settings(env, dotenv=None)
        llm = ChatCompletionsClient(settings.llm)
        dp = build_dispatcher(settings, make_connections(FakeToolClient()), llm)
        self.assertIsInstance(dp["agent"], InvestmentAgent)
        self.assertIsNone(build_dispatcher(settings, make_connections(FakeToolClient()))["agent"])
        await llm.aclose()


if __name__ == "__main__":
    unittest.main()
