"""Новые сценарии отчётов через штатный Dispatcher Telegram."""

import asyncio
from datetime import timedelta
from itertools import count
from unittest.mock import AsyncMock, patch
import unittest

from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from _support import TEST_TOKEN, make_connections
from investment_journal.__main__ import build_dispatcher
from investment_journal.config import load_settings
from investment_journal.portfolio_reports import PortfolioReportService
from investment_journal.report_scheduling import IntervalRule
from test_dispatcher import update
from test_portfolio_market import NOW
from test_portfolio_reports import report_tools


class PortfolioDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = make_connections(report_tools(count=4), connected=(7, 8))
        self.dp = build_dispatcher(load_settings({"TELEGRAM_BOT_TOKEN": "42:TEST"}, dotenv=None), self.connections)
        self.dp["reports"] = PortfolioReportService(self.connections, clock=lambda: NOW)
        self.bot = Bot("42:TEST")
        self.sent = []
        self.callback_numbers = count(10000)
        numbers = count(100)

        async def fake_call(method, request_timeout=None):
            if isinstance(method, SendMessage):
                self.sent.append(method)
                return Message(message_id=next(numbers), date=NOW, chat=Chat(id=method.chat_id, type="private"), text=method.text)
            return True

        self.patch = patch.object(Bot, "__call__", AsyncMock(side_effect=fake_call))
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.bot.session.close()
        self.connections.repo.close()

    async def send(self, text, chat_id=7, chat_type="private"):
        await self.dp.feed_update(self.bot, update(text, Chat(id=chat_id, type=chat_type)))

    def button(self, label, chat_id=7):
        for method in reversed(self.sent):
            if method.chat_id == chat_id and method.reply_markup is not None:
                for row in method.reply_markup.inline_keyboard:
                    for button in row:
                        if button.text == label:
                            return method, button
        raise AssertionError(f"Button missing: {label}")

    async def click(self, label, actor_id=7, data=None):
        method, button = self.button(label)
        number = next(self.callback_numbers)
        message = Message(message_id=number, date=NOW, chat=Chat(id=method.chat_id, type="private"),
                          text=method.text, reply_markup=method.reply_markup)
        callback = CallbackQuery(id=str(number), from_user=User(id=actor_id, is_bot=False, first_name="Test"),
                                 chat_instance="test", data=data or button.callback_data, message=message)
        await self.dp.feed_update(self.bot, Update(update_id=number, callback_query=callback))

    async def test_analysis_normal_entrypoint_and_private_chat(self):
        await self.send("/analysis")
        self.assertIn("Готовлю анализ", self.sent[0].text)
        self.assertTrue(any("Стоимость портфеля" in item.text and item.parse_mode == "HTML" for item in self.sent))
        size = len(self.sent)
        await self.send("/analysis", -100, "group")
        self.assertEqual(len(self.sent), size)

    async def test_disconnected_user_gets_connect_hint(self):
        await self.send("/analysis", 9)
        self.assertIn("/connect", self.sent[-1].text)

    async def test_two_users_receive_their_own_reports(self):
        await asyncio.gather(self.send("/analysis", 7), self.send("/analysis", 8))
        reports = [item for item in self.sent if item.parse_mode == "HTML"]
        self.assertEqual({item.chat_id for item in reports}, {7, 8})
        users = [self.connections.user(chat_id) for chat_id in (7, 8)]
        self.assertTrue(all(self.connections.repo.last_run(user.id)["state"] == "delivered" for user in users))

    async def test_interval_wizard_preview_save_pause_and_resume(self):
        await self.send("/schedule")
        await self.click("Интервал")
        await self.click("Москва")
        await self.send("6ч 17.11.2026 09:00")
        self.assertIn("09:00 +0300", self.sent[-1].text)
        _, save = self.button("Сохранить")
        old_save = save.callback_data
        await self.click("Сохранить")
        user = self.connections.user(7)
        saved = self.connections.repo.get_schedule(user.id)
        self.assertEqual(saved["rule"]["value"], 6)
        self.assertEqual(saved["rule"]["timezone"], "Europe/Moscow")
        self.assertTrue(saved["enabled"])
        await self.click("Сохранить", data=old_save)
        self.assertEqual(self.connections.repo.get_schedule(user.id)["revision"], saved["revision"])
        await self.click("Пауза")
        self.assertFalse(self.connections.repo.get_schedule(user.id)["enabled"])
        await self.click("Возобновить")
        await self.click("Сохранить")
        self.assertTrue(self.connections.repo.get_schedule(user.id)["enabled"])

    async def test_calendar_cancel_and_callback_owner_isolation(self):
        await self.send("/schedule")
        before = len(self.sent)
        await self.click("Календарь", actor_id=8)
        self.assertEqual(len(self.sent), before)
        self.assertIsNone(self.connections.repo.get_schedule(self.connections.user(8).id))
        await self.click("Календарь")
        await self.send("Asia/Yekaterinburg")
        await self.send("пн,ср,пт 19:00")
        await self.click("Сохранить")
        user = self.connections.user(7)
        saved = self.connections.repo.get_schedule(user.id)
        self.assertEqual(saved["rule"]["weekdays"], [0, 2, 4])
        self.assertEqual(saved["rule"]["local_time"], "19:00")
        await self.click("Интервал")
        await self.click("UTC")
        await self.send("3д")
        await self.click("Отмена")
        self.assertEqual(self.connections.repo.get_schedule(user.id), saved)
        rebuilt = build_dispatcher(load_settings({"TELEGRAM_BOT_TOKEN": "42:TEST"}, dotenv=None), self.connections)
        self.dp = rebuilt
        await self.send("/schedule")
        self.assertIn("Asia/Yekaterinburg", self.sent[-1].text)

    async def test_switch_account_during_generation_cancels_old_report(self):
        user = self.connections.user(7)
        rule = IntervalRule(6, "hours", NOW+timedelta(hours=6), "Europe/Moscow")
        previous = self.connections.repo.save_schedule(user.id, "2000000001", rule.to_dict(), rule.first_at)
        tools = self.connections._opener.tools
        tools.accounts.append({"id": "new-account", "name": "Second"})
        original = tools.call
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(name, arguments=None):
            if name == "invest_get_portfolio":
                started.set()
                await release.wait()
            return await original(name, arguments)

        tools.call = delayed
        task = asyncio.create_task(self.send("/analysis"))
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(self.send("/account 2"), 2)
        release.set()
        await task
        schedule = self.connections.repo.get_schedule(user.id)
        self.assertEqual(schedule["account_id"], "new-account")
        self.assertTrue(schedule["enabled"])
        self.assertEqual(schedule["next_run_at"], previous["next_run_at"])
        self.assertGreater(schedule["revision"], previous["revision"])
        self.assertFalse(any(item.parse_mode == "HTML" for item in self.sent))
        self.assertEqual(self.connections.repo.last_run(user.id)["state"], "cancelled")

    async def test_disconnect_before_delivery_pauses_and_reconnect_requires_resume(self):
        user = self.connections.user(7)
        rule = IntervalRule(6, "hours", NOW+timedelta(hours=6), "Europe/Moscow")
        self.connections.repo.save_schedule(user.id, "2000000001", rule.to_dict(), rule.first_at)
        tools = self.connections._opener.tools
        original = tools.call
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(name, arguments=None):
            if name == "invest_get_portfolio":
                started.set()
                await release.wait()
            return await original(name, arguments)

        tools.call = delayed
        task = asyncio.create_task(self.send("/analysis"))
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(self.send("/disconnect"), 2)
        release.set()
        await task
        self.assertFalse(self.connections.repo.get_schedule(user.id)["enabled"])
        self.assertFalse(any(item.parse_mode == "HTML" for item in self.sent))
        await self.send("/connect")
        await self.send(TEST_TOKEN)
        self.assertFalse(self.connections.repo.get_schedule(user.id)["enabled"])
        await self.send("/schedule")
        await self.click("Возобновить")
        await self.click("Сохранить")
        self.assertTrue(self.connections.repo.get_schedule(user.id)["enabled"])
