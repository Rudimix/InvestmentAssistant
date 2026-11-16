"""Правила пользовательского расписания и устойчивое выполнение отчётов."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import asyncio
from contextlib import suppress
import logging
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import timestamp
from .portfolio_reports import PortfolioReportService, Sender


def schedule_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError("Укажите существующий часовой пояс IANA, например Europe/Moscow") from exc


def local_datetime_to_utc(naive: datetime, zone: ZoneInfo) -> datetime:
    """Первое вхождение повторяющегося времени или первая допустимая граница после разрыва."""
    def valid(value):
        candidate = value.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
        restored = candidate.astimezone(zone).replace(tzinfo=None)
        return candidate if restored == value else None

    result = valid(naive)
    if result is not None:
        return result
    possibilities = [naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
                     for fold in (0, 1)]
    lower, upper = naive, max(possibilities)
    while upper-lower > timedelta(microseconds=1):
        middle = lower+(upper-lower)//2
        if valid(middle) is not None:
            upper = middle
        else:
            lower = middle
    result = valid(upper)
    if result is None:
        raise ValueError("Не удалось определить допустимый момент в выбранном часовом поясе")
    return result


@dataclass(frozen=True)
class IntervalRule:
    """Фиксированный интервал реального времени от первого UTC-момента."""

    value: int
    unit: str
    first_at: datetime
    timezone: str = "UTC"

    def __post_init__(self):
        schedule_zone(self.timezone)
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value <= 0:
            raise ValueError("Интервал должен быть положительным целым числом")
        if self.unit not in {"hours", "days"}:
            raise ValueError("Выберите часы или дни")
        object.__setattr__(self, "first_at", timestamp(self.first_at))
        try:
            self.first_at + self.step
        except OverflowError as exc:
            raise ValueError("Интервал выходит за допустимый диапазон дат") from exc

    @property
    def step(self) -> timedelta:
        return timedelta(**{self.unit: self.value})

    def next_after(self, moment: datetime) -> datetime:
        moment = timestamp(moment)
        if moment < self.first_at:
            return self.first_at
        try:
            count = (moment-self.first_at)//self.step + 1
            return self.first_at + count*self.step
        except OverflowError as exc:
            raise ValueError("Следующий запуск выходит за допустимый диапазон дат") from exc

    def to_dict(self) -> dict:
        return {"mode": "interval", "value": self.value, "unit": self.unit,
                "first_at": self.first_at.isoformat(), "timezone": self.timezone}


@dataclass(frozen=True)
class CalendarRule:
    """Выбранные дни недели и местное время в пользовательском часовом поясе."""

    weekdays: tuple[int, ...]
    local_time: str
    timezone: str

    def __post_init__(self):
        schedule_zone(self.timezone)
        if not self.weekdays or any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in self.weekdays):
            raise ValueError("Выберите дни недели с понедельника по воскресенье")
        object.__setattr__(self, "weekdays", tuple(sorted(set(self.weekdays))))
        if not isinstance(self.local_time, str) or re.fullmatch(r"\d{2}:\d{2}", self.local_time) is None:
            raise ValueError("Укажите местное время в формате ЧЧ:ММ")
        try:
            time.fromisoformat(self.local_time)
        except ValueError as exc:
            raise ValueError("Некорректное местное время") from exc

    def next_after(self, moment: datetime) -> datetime:
        zone = schedule_zone(self.timezone)
        moment = timestamp(moment)
        try:
            day = moment.astimezone(zone).date()
            for offset in range(9):
                candidate_day = day+timedelta(days=offset)
                if candidate_day.weekday() not in self.weekdays:
                    continue
                naive = datetime.combine(candidate_day, time.fromisoformat(self.local_time))
                candidate = local_datetime_to_utc(naive, zone)
                if candidate > moment:
                    return candidate
        except OverflowError as exc:
            raise ValueError("Следующий запуск выходит за допустимый диапазон дат") from exc
        raise ValueError("Не удалось определить следующий запуск")

    def to_dict(self) -> dict:
        return {"mode": "calendar", "weekdays": list(self.weekdays), "local_time": self.local_time,
                "timezone": self.timezone}


def initial_run(rule: IntervalRule | CalendarRule, now: datetime) -> datetime:
    if isinstance(rule, IntervalRule) and rule.first_at >= now:
        return rule.first_at
    return rule.next_after(now)


def schedule_preview(account_id: str, rule: IntervalRule | CalendarRule, now: datetime,
                     next_run_at: datetime | None = None) -> str:
    """Показать понятное правило и фактический ближайший момент в выбранной зоне."""
    if isinstance(rule, IntervalRule):
        unit = "ч." if rule.unit == "hours" else "дн. по 24 ч."
        description = f"интервал {rule.value} {unit}"
    else:
        days = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
        description = f"{', '.join(days[index] for index in rule.weekdays)}, {rule.local_time}"
    next_at = next_run_at if next_run_at is not None else initial_run(rule, now)
    local = timestamp(next_at).astimezone(schedule_zone(rule.timezone))
    return (f"Счёт — {account_id}\nПравило — {description}\nЧасовой пояс — {rule.timezone}\n"
            f"Ближайший запуск — {local:%d.%m.%Y %H:%M %z}")


def rule_from_dict(raw: dict) -> IntervalRule | CalendarRule:
    if raw.get("mode") == "interval":
        return IntervalRule(raw["value"], raw["unit"], timestamp(raw["first_at"]), raw["timezone"])
    if raw.get("mode") == "calendar":
        return CalendarRule(tuple(raw["weekdays"]), raw["local_time"], raw["timezone"])
    raise ValueError("Неизвестный режим расписания")


def parse_interval_rule(text: str, zone_name: str, now: datetime) -> IntervalRule:
    """Интервал вида «6ч» или «3д 17.11.2026 09:00» в выбранной зоне."""
    match = re.fullmatch(r"(\d+)\s*(ч\.?|час(?:а|ов)?|h|д\.?|дн\.?|день|дня|дней|d)(?:\s+(.+))?", text.strip().lower())
    if match is None:
        raise ValueError("Укажите интервал, например 6ч или 3д. После него можно добавить ДД.ММ.ГГГГ ЧЧ:ММ")
    value = int(match[1])
    unit = "hours" if match[2].startswith("ч") or match[2] == "h" else "days"
    try:
        first_at = now+timedelta(**{unit: value})
        if match[3]:
            naive = datetime.strptime(match[3], "%d.%m.%Y %H:%M")
            first_at = local_datetime_to_utc(naive, schedule_zone(zone_name))
        return IntervalRule(value, unit, first_at, zone_name)
    except OverflowError as exc:
        raise ValueError("Интервал выходит за допустимый диапазон дат") from exc


def parse_calendar_rule(text: str, zone_name: str) -> CalendarRule:
    """Ежедневно, будни или перечисление дней «пн,ср,пт 19:00»."""
    parts = text.strip().lower().rsplit(maxsplit=1)
    if len(parts) != 2:
        raise ValueError("Укажите дни и время, например ежедневно 09:00 или пн,ср,пт 19:00")
    days, local_time = parts
    names = {name: index for index, name in enumerate(("пн", "вт", "ср", "чт", "пт", "сб", "вс"))}
    if days in {"ежедневно", "каждый день"}:
        weekdays = tuple(range(7))
    elif days in {"будни", "по будням"}:
        weekdays = (0, 1, 2, 3, 4)
    else:
        try:
            weekdays = tuple(names[name] for name in days.replace(",", " ").split())
        except KeyError as exc:
            raise ValueError("Используйте дни пн, вт, ср, чт, пт, сб, вс") from exc
    return CalendarRule(weekdays, local_time, zone_name)


class ReportScheduler:
    """Один фоновый обработчик с сохраняемой очередью и ограниченной конкуренцией."""

    def __init__(self, reports: PortfolioReportService, send: Sender):
        self.reports = reports
        self.repo = reports.repo
        self.send = send
        self.clock = reports.clock
        self._jobs: dict[int, asyncio.Task] = {}
        self._loop_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._closing = False

    def start(self) -> None:
        if self._loop_task is None:
            self._closing = False
            self._loop_task = asyncio.create_task(self._loop())

    def _finished(self, user_id: int, task: asyncio.Task) -> None:
        if self._jobs.get(user_id) is task:
            self._jobs.pop(user_id)
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger(__name__).warning("Report worker failed (%s)", type(task.exception()).__name__)
        if not self._closing:
            self._wake.set()

    async def tick(self) -> None:
        """Объединить наступившие сроки и запустить готовые выполнения из SQLite."""
        if self._closing:
            return
        now = self.clock()
        for schedule in self.repo.due_schedules(now):
            try:
                rule = rule_from_dict(schedule["rule"])
                next_at = rule.next_after(now)
                self.repo.enqueue_schedule(schedule, next_at, now)
            except (ValueError, KeyError, TypeError):
                self.repo.pause_schedule(schedule["user_id"], "правило требует повторной настройки через /schedule")
        for run in self.repo.ready_runs(now):
            if len(self._jobs) >= 4:
                break
            user_id = run["user_id"]
            if user_id in self._jobs:
                continue
            task = asyncio.create_task(self.reports.execute_run(run, self.send))
            self._jobs[user_id] = task
            task.add_done_callback(lambda completed, user=user_id: self._finished(user, completed))
        await asyncio.sleep(0)

    async def _loop(self) -> None:
        while not self._closing:
            self._wake.clear()
            try:
                await self.tick()
            except Exception as exc:
                logging.getLogger(__name__).warning("Scheduler tick failed (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        """Завершить свои задачи до закрытия сетевых клиентов и базы."""
        self._closing = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        jobs = list(self._jobs.values())
        for task in jobs:
            task.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._jobs.clear()
