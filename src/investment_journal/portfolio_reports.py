"""Общий сценарий подготовки и сохраняемой доставки портфельных отчётов."""

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import logging
from typing import Any
from uuid import uuid4

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from .connections import Connections, NotConnected
from .llm import ChatCompletionsClient, portfolio_explanation
from .mcp_client import ToolError
from .portfolio_analysis import PortfolioAnalysisService
from .portfolio_events import event_window, report_context, select_events
from .portfolio_formatting import portfolio_report_parts
from .portfolio_sources import fetch_events
from .storage import LeaseLost

Sender = Callable[..., Awaitable[Any]]
logger = logging.getLogger(__name__)


class PortfolioReportService:
    """Один путь отчёта с арендой, повтором и изоляцией пользователя."""

    def __init__(self, connections: Connections, llm: ChatCompletionsClient | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.connections = connections
        self.repo = connections.repo
        self.llm = llm
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.analysis = PortfolioAnalysisService(self.repo, self.clock)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._settings_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._capacity = asyncio.Semaphore(4)
        self._active: set[asyncio.Task] = set()
        self._closed = False

    @asynccontextmanager
    async def _operation(self):
        if self._closed:
            raise ToolError("Сервис отчётов завершает работу")
        task = asyncio.current_task()
        outermost = task not in self._active
        self._active.add(task)
        try:
            yield
        finally:
            if outermost:
                self._active.discard(task)

    async def aclose(self) -> None:
        """Остановить все ручные и фоновые обращения до закрытия зависимостей."""
        self._closed = True
        current = asyncio.current_task()
        tasks = [task for task in self._active if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def user_lock(self, user_id: int) -> asyncio.Lock:
        return self._locks[user_id]

    def settings_lock(self, user_id: int) -> asyncio.Lock:
        """Изменения подключения и отправка сериализуются отдельно от долгого сбора данных."""
        return self._settings_locks[user_id]

    async def request(self, chat_id: int, request_key: str, send: Sender) -> dict:
        async with self._operation():
            user = self.connections.user(chat_id)
            async with self.settings_lock(user.id):
                account_id = self.connections.selected_account(chat_id)
                if not account_id:
                    async with self.connections.market(chat_id) as market:
                        account_id = await market.account_id()
                    self.connections.select_account(chat_id, account_id)
                run = self.repo.create_run(user.id, account_id, "manual", request_key, self.clock())
            return await self.execute_run(run, send)

    def _checkpoint(self, run: dict, owner: str) -> None:
        user_id, run_id = run["user_id"], run["id"]
        if not self.repo.renew_run(user_id, run_id, owner, self.clock()):
            raise LeaseLost("Аренда выполнения потеряна")
        credentials = self.repo.get_credentials(user_id)
        current = credentials is not None and credentials[1] == run["account_id"]
        if run["origin"] == "scheduled":
            schedule = self.repo.get_schedule(user_id)
            current = current and schedule is not None and schedule["enabled"] and schedule["revision"] == run["revision"]
        if not current:
            self.repo.cancel_run(user_id, run_id)
            raise LeaseLost("Выбранный счёт, подключение или расписание изменились")

    async def _heartbeat(self, run: dict, owner: str) -> None:
        while True:
            await asyncio.sleep(30)
            if not self.repo.renew_run(run["user_id"], run["id"], owner, self.clock()):
                return

    async def _prepare(self, run: dict, owner: str, chat_id: int) -> None:
        async with self.connections.market(chat_id) as market:
            analysis = await self.analysis.analyze(run["user_id"], market, run["origin"])
            self._checkpoint(run, owner)
            positions = (analysis.baseline.positions if analysis.baseline else []) + analysis.snapshot.positions
            start, end = event_window(analysis)
            events = select_events(await fetch_events(market, positions, start, end), positions, start, end)
        self._checkpoint(run, owner)
        try:
            context = report_context(analysis, events)
            explanation = await portfolio_explanation(self.llm, context)
        except ValueError:
            context = {}
            explanation = {"available": False, "reason": "Объём данных превышает контекст AI, показаны рассчитанные разделы"}
        self._checkpoint(run, owner)
        parts = portfolio_report_parts(analysis, events, explanation, context, run["id"])
        self.repo.prepare_run(run["user_id"], run["id"], owner, analysis.snapshot.id,
                              analysis.baseline.id if analysis.baseline else None,
                              {"analysis": analysis, "events": events, "explanation": explanation}, parts, self.clock())

    async def _deliver(self, run: dict, owner: str, chat_id: int, send: Sender) -> bool:
        user_id, run_id = run["user_id"], run["id"]
        parts = self.repo.get_parts(user_id, run_id)
        for part in parts:
            if part["state"] == "sent":
                continue
            async with self.settings_lock(user_id):
                if not await self._send_part(run, owner, chat_id, send, part):
                    return False
                if part["number"] == len(parts):
                    self.repo.finish_run(user_id, run_id, owner, self.clock())
                    return True
        async with self.settings_lock(user_id):
            self.repo.finish_run(user_id, run_id, owner, self.clock())
        return True

    async def _send_part(self, run: dict, owner: str, chat_id: int, send: Sender, part: dict) -> bool:
        user_id, run_id = run["user_id"], run["id"]
        self._checkpoint(run, owner)
        self.repo.set_part_state(user_id, run_id, owner, part["number"], "sending", self.clock())
        try:
            message = await send(chat_id=chat_id, text=part["text"], parse_mode="HTML",
                                 link_preview_options=LinkPreviewOptions(is_disabled=True))
            message_id = getattr(message, "message_id", None)
            if not isinstance(message_id, int) or isinstance(message_id, bool):
                raise ValueError("Не получено подтверждение сообщения")
        except (TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest):
            self.repo.set_part_state(user_id, run_id, owner, part["number"], "queued", self.clock())
            raise
        except asyncio.CancelledError:
            self.repo.fail_run(user_id, run_id, owner, self.clock(), "Исход отправки неизвестен", unknown=True)
            raise
        except Exception:
            self.repo.fail_run(user_id, run_id, owner, self.clock(), "Исход отправки неизвестен", unknown=True)
            return False
        self.repo.set_part_state(user_id, run_id, owner, part["number"], "sent", self.clock(), message_id)
        return True

    def _record_failure(self, run: dict, owner: str, error: Exception) -> str | None:
        user_id, run_id = run["user_id"], run["id"]
        current = self.repo.get_run(user_id, run_id)
        authorization = isinstance(error, NotConnected) or isinstance(error, ToolError) and error.authorization
        blocked = isinstance(error, TelegramForbiddenError)
        permanent = authorization or blocked or isinstance(error, TelegramBadRequest)
        if isinstance(error, ToolError) and not error.temporary:
            permanent = True
        retry_at = None
        if not permanent and current["attempts"] < 3:
            delay = 30 if current["attempts"] == 1 else 120
            if isinstance(error, TelegramRetryAfter):
                delay = max(delay, error.retry_after)
            retry_at = self.clock()+timedelta(seconds=delay)
        reason = "Ошибка подготовки или доставки отчёта"
        if authorization:
            reason = "Подключение недействительно, подключите токен заново"
        if blocked:
            reason = "Telegram отклонил доставку, проверьте доступность бота"
        self.repo.fail_run(user_id, run_id, owner, self.clock(), reason, retry_at=retry_at)
        if authorization or blocked:
            self.repo.pause_schedule(user_id, reason)
        logger.warning("Report %s failed (%s)", run_id, type(error).__name__)
        return reason+". Откройте /connect, затем возобновите /schedule." if authorization else None

    async def execute_run(self, run: dict, send: Sender) -> dict:
        """Захватить выполнение, подготовить один раз и возобновить подтверждённую доставку."""
        async with self._operation():
            return await self._execute_run(run, send)

    async def _execute_run(self, run: dict, send: Sender) -> dict:
        user_id, run_id = run["user_id"], run["id"]
        async with self.user_lock(user_id), self._capacity:
            owner = uuid4().hex
            if not self.repo.claim_run(user_id, run_id, owner, self.clock()):
                return self.repo.get_run(user_id, run_id)
            heartbeat = asyncio.create_task(self._heartbeat(run, owner))
            try:
                self._checkpoint(run, owner)
                user = self.repo.get_user(user_id)
                if user is None:
                    raise NotConnected("Пользователь недоступен")
                current = self.repo.get_run(user_id, run_id)
                if current["payload"] is None:
                    await self._prepare(run, owner, user.chat_id)
                await self._deliver(run, owner, user.chat_id, send)
            except LeaseLost:
                pass
            except asyncio.CancelledError:
                try:
                    self.repo.fail_run(user_id, run_id, owner, self.clock(), "Выполнение прервано, ожидает возобновления",
                                       retry_at=self.clock())
                except LeaseLost:
                    pass
                raise
            except Exception as exc:
                try:
                    notice = self._record_failure(run, owner, exc)
                    if notice:
                        user = self.repo.get_user(user_id)
                        try:
                            await send(chat_id=user.chat_id, text=notice, parse_mode=None)
                        except Exception:
                            logger.warning("Report connection notice could not be delivered")
                except LeaseLost:
                    pass
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            return self.repo.get_run(user_id, run_id)
