"""Обработчики Telegram-бота на заглушках Message/FSMContext."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from _support import TEST_TOKEN, FakeToolClient, make_connections
from investment_journal import bot
from investment_journal.connections import NotConnected
from investment_journal.llm import LlmError


def message(text: str = "", chat_id: int = 7):
    return SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id), answer=AsyncMock(), delete=AsyncMock())


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
        self.connections = make_connections(self.tools)
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=7, user_id=7))

    async def test_start(self):
        msg = message()
        await bot.cmd_start(msg, self.connections)
        self.assertIn("/idea", last_answer(msg))
        self.assertNotIn(bot.NOT_CONNECTED, last_answer(msg))
        msg = message(chat_id=8)
        await bot.cmd_start(msg, self.connections)
        self.assertIn(bot.NOT_CONNECTED, last_answer(msg))

    async def test_price(self):
        msg = message()
        await bot.cmd_price(msg, command("siz6"), self.connections)
        self.assertEqual(last_answer(msg), "SIZ6: 90000")

    async def test_price_errors(self):
        msg = message()
        await bot.cmd_price(msg, command(None), self.connections)
        self.assertIn("Укажите тикер", last_answer(msg))
        await bot.cmd_price(msg, command("SBER"), self.connections)
        self.assertIn("Не удалось", last_answer(msg))

    async def test_stats(self):
        msg = message()
        await bot.cmd_stats(msg, command("SiZ6 1d 6"), self.connections)
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
                await bot.cmd_stats(msg, command(args), self.connections)
                self.assertIn(expected, last_answer(msg))

    async def test_stats_not_enough_candles(self):
        async def one_candle(name, arguments=None):
            if name == "invest_find_instrument":
                return {"instruments": [{"ticker": "SiZ6", "classCode": "SPBFUT"}]}
            return {"candles": [{"time": "2026-11-01", "close": {"value": "1"}, "volume": "1"}]}
        self.tools.call = one_candle
        msg = message()
        await bot.cmd_stats(msg, command("SiZ6"), self.connections)
        self.assertIn("Недостаточно", last_answer(msg))

    async def test_portfolio(self):
        msg = message()
        await bot.cmd_portfolio(msg, self.connections)
        self.assertIn("SiZ6", last_answer(msg))

    async def test_idea_dialog_records_decision(self):
        await bot.cmd_idea(message(), self.state, self.connections)
        msg = message("BUY")
        await bot.idea_kind(msg, self.state)
        await bot.idea_ticker(message("siz6"), self.state)
        msg = message("Рубль ослабнет из-за снижения ставки")
        await bot.idea_hypothesis(msg, self.state, self.connections)
        self.assertEqual(last_answer(msg), "Записано #1: buy SIZ6 по 90000")
        self.assertIsNone(await self.state.get_state())

    async def test_idea_dialog_validation(self):
        await bot.cmd_idea(message(), self.state, self.connections)
        msg = message("maybe")
        await bot.idea_kind(msg, self.state)
        self.assertIn("Выберите", last_answer(msg))
        await bot.idea_kind(message("hold"), self.state)
        msg = message("SI-Z6")
        await bot.idea_ticker(msg, self.state)
        self.assertIn("букв и цифр", last_answer(msg))
        await bot.idea_ticker(message("SIZ6"), self.state)
        msg = message("мало")
        await bot.idea_hypothesis(msg, self.state, self.connections)
        self.assertIn("подробнее", last_answer(msg))
        self.assertEqual(await self.state.get_state(), bot.IdeaForm.hypothesis.state)

    async def test_idea_price_failure_clears_state(self):
        await self.state.update_data(kind="buy", ticker="SBER")
        await self.state.set_state(bot.IdeaForm.hypothesis)
        msg = message("Дивиденды будут выше ожиданий")
        await bot.idea_hypothesis(msg, self.state, self.connections)
        self.assertIn("не сохранена", last_answer(msg))
        self.assertIsNone(await self.state.get_state())

    async def test_cancel(self):
        await self.state.set_state(bot.IdeaForm.kind)
        await bot.cmd_cancel(message(), self.state)
        self.assertIsNone(await self.state.get_state())

    async def test_journal_review_close(self):
        await self.state.update_data(kind="buy", ticker="SIZ6")
        await bot.idea_hypothesis(message("Рубль ослабнет к концу года"), self.state, self.connections)

        msg = message()
        await bot.cmd_journal(msg, self.connections)
        self.assertIn("#1", last_answer(msg))

        self.tools.prices["SIZ6"] = 95000.0
        await bot.cmd_review(msg, self.connections)
        self.assertIn("подтверждается", last_answer(msg))

        await bot.cmd_close(msg, command("#1"), self.connections)
        self.assertTrue(last_answer(msg).startswith("Закрыто."))
        await bot.cmd_close(msg, command("1"), self.connections)
        self.assertIn("уже закрыто", last_answer(msg))
        await bot.cmd_close(msg, command("x"), self.connections)
        self.assertIn("Укажите номер", last_answer(msg))

    async def test_review_price_failure(self):
        await self.state.update_data(kind="buy", ticker="SIZ6")
        await bot.idea_hypothesis(message("Рубль ослабнет к концу года"), self.state, self.connections)
        del self.tools.prices["SIZ6"]
        msg = message()
        await bot.cmd_review(msg, self.connections)
        self.assertIn("Не удалось", last_answer(msg))

    async def test_free_text_without_llm(self):
        msg = message("привет")
        await bot.free_text(msg, self.connections, None)
        self.assertIn("не настроен", last_answer(msg))

    async def test_free_text_with_agent(self):
        agent = SimpleNamespace(ask=AsyncMock(return_value="ответ"), reset=lambda chat_id: None)
        msg = message("привет")
        await bot.free_text(msg, self.connections, agent)
        self.assertEqual(last_answer(msg), "ответ")
        agent.ask.side_effect = LlmError("timeout")
        await bot.free_text(msg, self.connections, agent)
        self.assertIn("недоступен", last_answer(msg))
        await bot.cmd_reset(msg, agent)
        self.assertIn("очищена", last_answer(msg))

    async def test_token_in_free_text_is_deleted_and_not_sent_to_llm(self):
        agent = SimpleNamespace(ask=AsyncMock(return_value="ответ"))
        msg = message(f"мой токен {TEST_TOKEN}")
        await bot.free_text(msg, self.connections, agent)
        msg.delete.assert_awaited_once()
        agent.ask.assert_not_awaited()
        self.assertEqual(last_answer(msg), bot.TOKEN_IN_CHAT)


class ConnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tools = FakeToolClient()
        self.connections = make_connections(self.tools, connected=())
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=7, user_id=7))

    async def test_commands_need_token(self):
        for call in (bot.cmd_price(message(), command("SBER"), self.connections),
                     bot.cmd_portfolio(message(), self.connections),
                     bot.cmd_review(message(), self.connections),
                     bot.cmd_idea(message(), self.state, self.connections)):
            with self.subTest(call=call.__name__), self.assertRaises(NotConnected):
                await call
        self.assertIsNone(await self.state.get_state())

    async def test_connect_flow(self):
        await bot.cmd_connect(message(), self.state)
        self.assertEqual(await self.state.get_state(), bot.ConnectForm.token.state)
        agent = SimpleNamespace(reset=Mock())
        msg = message(TEST_TOKEN)
        await bot.connect_token(msg, self.state, self.connections, agent)
        msg.delete.assert_awaited_once()
        agent.reset.assert_called_once_with(7)
        self.assertIsNone(await self.state.get_state())
        self.assertTrue(last_answer(msg).startswith("Токен подключён."))
        self.assertIn("право на сделки", last_answer(msg))
        self.assertTrue(self.connections.is_connected(7))

        msg = message()
        await bot.cmd_price(msg, command("SiZ6"), self.connections)
        self.assertEqual(last_answer(msg), "SIZ6: 90000")

    async def test_read_only_token_has_no_warning(self):
        self.tools.accounts[0]["accessLevel"] = "ACCOUNT_ACCESS_LEVEL_READ_ONLY"
        msg = message(TEST_TOKEN)
        await bot.connect_token(msg, self.state, self.connections)
        self.assertNotIn("право на сделки", last_answer(msg))

    async def test_rejected_token(self):
        msg = message("t." + "x" * 60)
        await bot.connect_token(msg, self.state, self.connections)
        msg.delete.assert_awaited_once()
        self.assertIn("не принял токен", last_answer(msg))
        self.assertFalse(self.connections.is_connected(7))

    async def test_not_a_token(self):
        msg = message("привет")
        await bot.connect_token(msg, self.state, self.connections)
        self.assertIn("не похоже на токен", last_answer(msg))

    async def test_disconnect(self):
        await bot.connect_token(message(TEST_TOKEN), self.state, self.connections)
        msg = message()
        await bot.cmd_disconnect(msg, self.connections)
        self.assertIn("удалён", last_answer(msg))
        self.assertFalse(self.connections.is_connected(7))
        await bot.cmd_disconnect(msg, self.connections)
        self.assertIn("не подключён", last_answer(msg))

    async def test_accounts_and_select(self):
        self.tools.accounts.append({"id": "2000000002", "type": "ACCOUNT_TYPE_TINKOFF_IIS", "name": "ИИС"})
        await bot.connect_token(message(TEST_TOKEN), self.state, self.connections)
        msg = message()
        await bot.cmd_accounts(msg, self.connections)
        self.assertIn("1. брокерский ← выбран", last_answer(msg))
        self.assertIn("2. ИИС\n", last_answer(msg))
        await bot.cmd_account(msg, command("2"), self.connections)
        self.assertIn("«ИИС»", last_answer(msg))
        self.assertEqual(self.connections.selected_account(7), "2000000002")
        for arg, expected in (("5", "Нет счёта №5"), (None, "Укажите номер")):
            await bot.cmd_account(msg, command(arg), self.connections)
            self.assertIn(expected, last_answer(msg))


if __name__ == "__main__":
    unittest.main()
