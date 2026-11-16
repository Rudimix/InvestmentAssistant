"""Календарные периоды анализа с зафиксированными полуоткрытыми UTC-границами."""

from datetime import date, datetime, time, timedelta

from .models import AnalysisPeriod, timestamp
from .report_scheduling import local_datetime_to_utc, schedule_zone


def analysis_date(value: str | date) -> date:
    """Дата пользователя ДД.ММ.ГГГГ или ISO-дата сохранённого запроса."""
    if type(value) is date:
        return value
    if not isinstance(value, str) or len(value.strip()) != 10:
        raise ValueError("Введите дату в формате ДД.ММ.ГГГГ")
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    raise ValueError("Введите существующую дату в формате ДД.ММ.ГГГГ")


def explicit_period(first: str | date, last: str | date, zone_name: str, now: datetime,
                    policy: str = "explicit") -> AnalysisPeriod:
    """Все местные дни от first до last, сегодняшний день ограничен временем запроса."""
    zone = schedule_zone(zone_name)
    requested_at = timestamp(now)
    today = requested_at.astimezone(zone).date()
    first, last = analysis_date(first), analysis_date(last)
    if first > last:
        raise ValueError("Начальная дата должна предшествовать конечной или совпадать с ней")
    if last > today:
        raise ValueError("Период не должен включать будущие даты")
    try:
        start = local_datetime_to_utc(datetime.combine(first, time.min), zone)
        end = requested_at if last == today else local_datetime_to_utc(datetime.combine(last+timedelta(days=1), time.min), zone)
    except (OverflowError, ValueError) as exc:
        raise ValueError("Границы периода недоступны в выбранном часовом поясе") from exc
    if start >= end:
        raise ValueError("Период не содержит прошедшего времени в выбранном часовом поясе")
    return AnalysisPeriod(first.isoformat(), last.isoformat(), zone_name, start, end, requested_at, policy)


def rolling_period(days: int, zone_name: str, now: datetime) -> AnalysisPeriod:
    """Последние days завершённых календарных дней до местной даты запуска."""
    if type(days) is not int or days <= 0:
        raise ValueError("Количество календарных дней должно быть положительным целым числом")
    now = timestamp(now)
    today = now.astimezone(schedule_zone(zone_name)).date()
    try:
        last = today-timedelta(days=1)
        first = last-timedelta(days=days-1)
    except OverflowError as exc:
        raise ValueError("Выбранный период выходит за пределы допустимых дат") from exc
    return explicit_period(first, last, zone_name, now, policy="rolling")


def resolve_period_request(request: dict, now: datetime) -> AnalysisPeriod:
    """Разрешить сохраняемую политику относительно момента подтверждения или срока расписания."""
    if not isinstance(request, dict):
        raise ValueError("Некорректный запрос периода")
    kind = request.get("kind")
    if kind == "rolling":
        return rolling_period(request.get("days"), request.get("zone", "Europe/Moscow"), now)
    if kind == "explicit":
        return explicit_period(request.get("first"), request.get("last"), request.get("zone", "Europe/Moscow"), now)
    raise ValueError("Укажите явный диапазон дат или скользящий период")


def period_description(period: AnalysisPeriod) -> str:
    """Период предпросмотра с местными датами, зоной и фактическими UTC-границами."""
    first = analysis_date(period.local_start).strftime("%d.%m.%Y")
    last = analysis_date(period.local_end).strftime("%d.%m.%Y")
    today = period.requested_at.astimezone(schedule_zone(period.zone)).date()
    partial = "\nПоследний день ещё не завершён, оценка ограничена временем запроса" if analysis_date(period.local_end) == today else ""
    return (f"Период {first} — {last}, {period.zone}\n"
            f"Границы UTC [{period.start:%d.%m.%Y %H:%M}, {period.end:%d.%m.%Y %H:%M}){partial}")
