"""Общий сценарий подготовки и сохраняемой доставки портфельных отчётов."""

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import logging
import json
from typing import Any
from uuid import uuid4
from hashlib import sha256

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from .connections import Connections, NotConnected
from .llm import ChatCompletionsClient, portfolio_explanation
from .mcp_client import ToolError
from .models import AnalysisPeriod, data_json
from .historical_formatting import historical_report_parts
from .profile_metrics import analysis_profile_assessment
from .advice_service import AdviceService, proposal_records
from .portfolio_analysis import PortfolioAnalysisService
from .portfolio_events import event_window, report_context, select_events
from .portfolio_formatting import portfolio_report_parts
from .portfolio_sources import fetch_events
from .storage import LeaseLost
from .stock_screening import StockScreeningService
from .stock_formatting import screening_parts

Sender = Callable[..., Awaitable[Any]]
logger = logging.getLogger(__name__)


class PortfolioReportService:
    """Один путь отчёта с арендой, повтором и изоляцией пользователя."""

    def __init__(self, connections: Connections, llm: ChatCompletionsClient | None = None,
                 clock: Callable[[], datetime] | None = None, evidence=None, forecast_directory=None):
        self.connections = connections
        self.repo = connections.repo
        self.llm = llm
        self.evidence = evidence
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.analysis = PortfolioAnalysisService(self.repo, self.clock)
        self.advice = AdviceService(self.repo, self.llm, self.clock)
        self.screening = StockScreeningService(self.repo, self.clock, evidence, forecast_directory=forecast_directory)
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

    async def request(self, chat_id: int, request_key: str, send: Sender,
                      period: AnalysisPeriod | None = None, period_request: dict | None = None,
                      report_kind: str = "analysis", mode: str = "combined") -> dict:
        async with self._operation():
            user = self.connections.user(chat_id)
            async with self.settings_lock(user.id):
                account_id = self.connections.selected_account(chat_id)
                if not account_id:
                    async with self.connections.market(chat_id) as market:
                        account_id = await market.account_id()
                    self.connections.select_account(chat_id, account_id)
                run = self.repo.create_run(user.id, account_id, "manual", request_key, self.clock(),
                                           period=period, period_request=period_request, report_kind=report_kind,
                                           manifest={"mode": mode} if report_kind == "screening" else None)
            return await self.execute_run(run, send)

    def _checkpoint(self, run: dict, owner: str) -> None:
        user_id, run_id = run["user_id"], run["id"]
        if not self.repo.renew_run(user_id, run_id, owner, self.clock()):
            raise LeaseLost("Аренда выполнения потеряна")
        credentials = self.repo.get_credentials(user_id)
        current = credentials is not None and credentials[1] == run["account_id"]
        if run.get("profile_revision") is not None:
            profile = self.repo.get_profile(user_id)
            current = current and (profile.revision if profile else 0) == run["profile_revision"]
        if run["origin"] == "scheduled":
            schedule = self.repo.get_schedule(user_id)
            current = current and schedule is not None and schedule["enabled"] and schedule["revision"] == run["revision"]
        if not current:
            self.repo.cancel_run(user_id, run_id)
            raise LeaseLost("Выбранный счёт, подключение, профиль или расписание изменились")

    async def _heartbeat(self, run: dict, owner: str, runner) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                self._checkpoint(run, owner)
            except LeaseLost:
                current = self.repo.get_run(run["user_id"], run["id"])
                if current and current["payload"] is None:
                    runner.cancel()
                return

    async def _prepare(self, run: dict, owner: str, chat_id: int) -> None:
        sections = json.loads(run["manifest"]).get("sections", ["analysis", "advice"])
        if run.get("report_kind") == "screening":
            profile = self.repo.get_profile(run["user_id"], run["profile_revision"]) if run.get("profile_revision") else None
            mode = json.loads(run["manifest"])["mode"]
            async with self.connections.market(chat_id) as market:
                screening = await self.screening.prepare(run["user_id"], market, mode, profile)
            self._checkpoint(run, owner)
            universe = self.repo.get_universe(run["user_id"], run["account_id"], screening["universe_id"])
            await self._save_prepared(run, owner, chat_id, None, None, {"screening": screening}, screening_parts(screening, universe))
            return
        if run.get("resolved_period"):
            period = AnalysisPeriod.from_dict(json.loads(run["resolved_period"]))
            async with self.connections.market(chat_id) as market:
                analysis = await self.analysis.analyze(run["user_id"], market, run["origin"], period)
                positions = analysis.start_state.positions+analysis.end_state.positions
                events = await self.evidence.collect(market, positions, period.start, period.end, as_of=period.end) if self.evidence else None
            self._checkpoint(run, owner)
            profile = self.repo.get_profile(run["user_id"], run["profile_revision"]) if run.get("profile_revision") else None
            inputs = self.repo.get_historical_inputs(run["user_id"], run["account_id"], analysis.end_state.id)
            assessment = analysis_profile_assessment(analysis, profile, inputs)
            advice = await self.advice.prepare(analysis, profile, assessment) if "advice" in sections else {}
            proposals = proposal_records(advice, run, self.clock())
            self._checkpoint(run, owner)
            parts = historical_report_parts(analysis, run["id"], profile_revision=run.get("profile_revision"), assessment=assessment, events=events, advice=advice)
            await self._save_prepared(run, owner, chat_id, analysis.end_state.anchor_id, None,
                                  {"historical_analysis": analysis, "profile_revision": run.get("profile_revision"),
                                   "profile_assessment": assessment, "events": events, "advice": advice}, parts, proposals)
            return
        async with self.connections.market(chat_id) as market:
            analysis = await self.analysis.analyze(run["user_id"], market, run["origin"])
            self._checkpoint(run, owner)
            positions = (analysis.baseline.positions if analysis.baseline else []) + analysis.snapshot.positions
            start, end = event_window(analysis)
            events = await self.evidence.collect(market, positions, start, end) if self.evidence else select_events(
                await fetch_events(market, positions, start, end), positions, start, end)
        self._checkpoint(run, owner)
        try:
            context = report_context(analysis, events)
            context["profile_revision"] = run.get("profile_revision")
            explanation = await portfolio_explanation(self.llm, context)
        except ValueError:
            context = {}
            explanation = {"available": False, "reason": "Объём данных превышает контекст AI, показаны рассчитанные разделы"}
        self._checkpoint(run, owner)
        profile = self.repo.get_profile(run["user_id"], run["profile_revision"]) if run.get("profile_revision") else None
        assessment = analysis_profile_assessment(analysis, profile)
        context["profile_assessment"] = assessment
        advice = await self.advice.prepare(analysis, profile, assessment) if "advice" in sections else {}
        proposals = proposal_records(advice, run, self.clock())
        self._checkpoint(run, owner)
        context["advice"] = advice
        parts = portfolio_report_parts(analysis, events, explanation, context, run["id"])
        await self._save_prepared(run, owner, chat_id, analysis.snapshot.id,
                              analysis.baseline.id if analysis.baseline else None,
                              {"analysis": analysis, "events": events, "explanation": explanation,
                               "profile_revision": run.get("profile_revision"), "profile_assessment": assessment, "advice": advice}, parts, proposals)

    async def _save_prepared(self, run, owner, chat_id, snapshot_id, baseline_id, payload, parts, proposals=None):
        manifest = json.loads(run["manifest"])
        if run["report_kind"] == "analysis" and "screening" in manifest.get("sections", []):
            profile = self.repo.get_profile(run["user_id"], run["profile_revision"]) if run.get("profile_revision") else None
            async with self.connections.market(chat_id) as market:
                screening = await self.screening.prepare(run["user_id"], market, manifest.get("mode", "combined"), profile)
            payload["screening"] = screening
            universe = self.repo.get_universe(run["user_id"], run["account_id"], screening["universe_id"])
            parts.extend(screening_parts(screening, universe))
        self._checkpoint(run, owner)
        screening = payload.get("screening", {})
        models = sorted({item["forecast"]["forecast"]["model_version"] for item in screening.get("candidates", []) if item.get("forecast", {}).get("available")})
        events = payload.get("events")
        manifest.update(profile_revision=run.get("profile_revision"), period=json.loads(run["resolved_period"]) if run.get("resolved_period") else None,
                        inputs_version=sha256(data_json(payload).encode()).hexdigest(), screening_id=screening.get("id"),
                        universe_version=screening.get("universe_id"), model_versions=models,
                        evidence_versions=[item.id for item in events.items] if events else [])
        self.repo.prepare_run(run["user_id"], run["id"], owner, snapshot_id, baseline_id, payload, parts, self.clock(), proposals, manifest)

    async def _deliver(self, run: dict, owner: str, chat_id: int, send: Sender) -> bool:
        user_id, run_id = run["user_id"], run["id"]
        parts = self.repo.get_parts(user_id, run_id)
        for part in parts:
            if part["state"] == "sent":
                continue
            async with self.settings_lock(user_id):
                if not await self._send_part(run, owner, chat_id, send, part, part["number"] == len(parts)):
                    return False
                if part["number"] == len(parts):
                    self.repo.finish_run(user_id, run_id, owner, self.clock())
                    return True
        async with self.settings_lock(user_id):
            self.repo.finish_run(user_id, run_id, owner, self.clock())
        return True

    async def _send_part(self, run: dict, owner: str, chat_id: int, send: Sender, part: dict, last: bool = False) -> bool:
        user_id, run_id = run["user_id"], run["id"]
        self._checkpoint(run, owner)
        options = {}
        if last:
            from .advice_ui import advice_links
            current = self.repo.get_run(user_id, run_id)
            proposals = json.loads(current["payload"]).get("advice", {}).get("result", {}).get("proposals", [])
            if proposals:
                options["reply_markup"] = advice_links(chat_id, [(f"Основания предложения {index}", proposal["id"])
                                                                 for index, proposal in enumerate(proposals, 1)])
        self.repo.set_part_state(user_id, run_id, owner, part["number"], "sending", self.clock())
        try:
            message = await send(chat_id=chat_id, text=part["text"], parse_mode="HTML",
                                 link_preview_options=LinkPreviewOptions(is_disabled=True), **options)
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
            heartbeat = asyncio.create_task(self._heartbeat(run, owner, asyncio.current_task()))
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
                current = self.repo.get_run(user_id, run_id)
                if current and current["state"] == "cancelled":
                    return current
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
