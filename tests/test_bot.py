"""Обработчики Telegram-бота на заглушках Message/FSMContext."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from _support import FakeToolClient
from investment_journal import bot
from investment_journal.journal import JournalService
from investment_journal.llm import LlmError
from investment_journal.market import MarketDataService
from investment_journal.storage import JournalRepository


def message(text: str = "", chat_id: int = 7):
    return SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id), answer=AsyncMock())


def command(args: str | None):
    return CommandObject(prefix="/", command="x", args=args)


def last_answer(msg) -> str:
    return msg.answer.call_args.args[0]


class ParseStatsArgsTests(unittest.TestCase):
    def test_defaults_and_values(self):
        self.assertEqual(bot.parse_stats_args("siz6"), ("SIZ6", "1d", 30))
        self.assertEqual(bot.parse_stats_args("siz6 1h"), ("SIZ6", "1h", 30))
        self.assertEqual(bot.parse_stats_args("siz6 4h 100 extra"), ("SIZ6", "4h", 100))

    def test_errors(self):
        for args in (None, "", "SIZ6 2d", "SIZ6 1d 1", "SIZ6 1d -5"):
            with self.subTest(args=args), self.assertRaises(ValueError):
                bot.parse_stats_args(args)


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tools = FakeToolClient()
        self.journal = JournalService(JournalRepository(), MarketDataService(self.tools))
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=7, user_id=7))

    async def test_start_registers_user(self):
        msg = message()
        await bot.cmd_start(msg, self.journal)
        self.assertIn("/idea", last_answer(msg))
        self.assertEqual(self.journal.repo.get_or_create_user(7).id, 1)

    async def test_price(self):
        msg = message()
        await bot.cmd_price(msg, command("siz6"), self.journal)
        self.assertEqual(last_answer(msg), "SIZ6: 90000")

    async def test_price_errors(self):
        msg = message()
        await bot.cmd_price(msg, command(None), self.journal)
        self.assertIn("Укажите тикер", last_answer(msg))
        await bot.cmd_price(msg, command("SBER"), self.journal)
        self.assertIn("Не удалось", last_answer(msg))

    async def test_stats(self):
        msg = message()
        await bot.cmd_stats(msg, command("SiZ6 1d 6"), self.journal)
        self.assertIn("Тренд: восходящий", last_answer(msg))

    async def test_stats_validation(self):
        cases = {
            None: "Укажите тикер",
            "SiZ6 2d": "Интервал",
            "SiZ6 1d abc": "от 2 до 500",
            "SiZ6 1d 1000": "от 2 до 500",
            "SBER": "Не удалось",
        }
        for args, expected in cases.items():
            with self.subTest(args=args):
                msg = message()
                await bot.cmd_stats(msg, command(args), self.journal)
                self.assertIn(expected, last_answer(msg))

    async def test_stats_not_enough_candles(self):
        async def one_candle(name, arguments=None):
            if name == "invest_find_instrument":
                return {"instruments": [{"ticker": "SiZ6", "classCode": "SPBFUT"}]}
            return {"candles": [{"time": "2026-11-01", "close": {"value": "1"}, "volume": "1"}]}
        self.tools.call = one_candle
        msg = message()
        await bot.cmd_stats(msg, command("SiZ6"), self.journal)
        self.assertIn("Недостаточно", last_answer(msg))

    async def test_portfolio(self):
        msg = message()
        await bot.cmd_portfolio(msg, self.journal)
        self.assertIn("SiZ6", last_answer(msg))

    async def test_idea_dialog_records_decision(self):
        await bot.cmd_idea(message(), self.state)
        msg = message("BUY")
        await bot.idea_kind(msg, self.state)
        await bot.idea_ticker(message("siz6"), self.state)
        msg = message("Рубль ослабнет из-за снижения ставки")
        await bot.idea_hypothesis(msg, self.state, self.journal)
        self.assertEqual(last_answer(msg), "Записано #1: buy SIZ6 по 90000")
        self.assertIsNone(await self.state.get_state())

    async def test_idea_dialog_validation(self):
        await bot.cmd_idea(message(), self.state)
        msg = message("maybe")
        await bot.idea_kind(msg, self.state)
        self.assertIn("Выберите", last_answer(msg))
        await bot.idea_kind(message("hold"), self.state)
        msg = message("SI-Z6")
        await bot.idea_ticker(msg, self.state)
        self.assertIn("букв и цифр", last_answer(msg))
        await bot.idea_ticker(message("SIZ6"), self.state)
        msg = message("мало")
        await bot.idea_hypothesis(msg, self.state, self.journal)
        self.assertIn("подробнее", last_answer(msg))
        self.assertEqual(await self.state.get_state(), bot.IdeaForm.hypothesis.state)

    async def test_idea_price_failure_clears_state(self):
        await self.state.update_data(kind="buy", ticker="SBER")
        await self.state.set_state(bot.IdeaForm.hypothesis)
        msg = message("Дивиденды будут выше ожиданий")
        await bot.idea_hypothesis(msg, self.state, self.journal)
        self.assertIn("не сохранена", last_answer(msg))
        self.assertIsNone(await self.state.get_state())

    async def test_cancel(self):
        await self.state.set_state(bot.IdeaForm.kind)
        await bot.cmd_cancel(message(), self.state)
        self.assertIsNone(await self.state.get_state())

    async def test_journal_review_close(self):
        await self.state.update_data(kind="buy", ticker="SIZ6")
        await bot.idea_hypothesis(message("Рубль ослабнет к концу года"), self.state, self.journal)

        msg = message()
        await bot.cmd_journal(msg, self.journal)
        self.assertIn("#1", last_answer(msg))

        self.tools.prices["SIZ6"] = 95000.0
        await bot.cmd_review(msg, self.journal)
        self.assertIn("подтверждается", last_answer(msg))

        await bot.cmd_close(msg, command("#1"), self.journal)
        self.assertTrue(last_answer(msg).startswith("Закрыто."))
        await bot.cmd_close(msg, command("1"), self.journal)
        self.assertIn("уже закрыто", last_answer(msg))
        await bot.cmd_close(msg, command("x"), self.journal)
        self.assertIn("Укажите номер", last_answer(msg))

    async def test_review_price_failure(self):
        await self.state.update_data(kind="buy", ticker="SIZ6")
        await bot.idea_hypothesis(message("Рубль ослабнет к концу года"), self.state, self.journal)
        del self.tools.prices["SIZ6"]
        msg = message()
        await bot.cmd_review(msg, self.journal)
        self.assertIn("Не удалось", last_answer(msg))

    async def test_free_text_without_llm(self):
        msg = message("привет")
        await bot.free_text(msg, None)
        self.assertIn("не настроен", last_answer(msg))

    async def test_free_text_with_agent(self):
        agent = SimpleNamespace(ask=AsyncMock(return_value="ответ"), reset=lambda chat_id: None)
        msg = message("привет")
        await bot.free_text(msg, agent)
        self.assertEqual(last_answer(msg), "ответ")
        agent.ask.side_effect = LlmError("timeout")
        await bot.free_text(msg, agent)
        self.assertIn("недоступен", last_answer(msg))
        await bot.cmd_reset(msg, agent)
        self.assertIn("очищена", last_answer(msg))


if __name__ == "__main__":
    unittest.main()
