"""Ввод исторического диапазона и часового пояса через обычную команду анализа."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from .analysis_periods import explicit_period, period_description, rolling_period
from .connections import NotConnected
from .mcp_client import ToolError


class AnalysisForm(StatesGroup):
    zone = State()
    first = State()
    last = State()


async def send_period(message: Message, reports, period, request):
    await message.answer(period_description(period))
    await message.answer("Готовлю исторический анализ выбранного счёта.")
    try:
        run = await reports.request(message.chat.id, str(message.message_id), message.bot.send_message,
                                    period=period, period_request=request)
    except ToolError:
        await message.answer("Источники временно недоступны. Повторите запрос позже.")
        return
    if run["state"] != "delivered":
        await message.answer({"retry": "Запрос сохранён. Повтор запланирован автоматически.",
                              "delivery_unknown": "Исход доставки неизвестен. Проверьте полученные сообщения.",
                              "cancelled": "Счёт, подключение или профиль изменились. Повторите запрос.",
                              "queued": "Запрос сохранён в очереди."}.get(run["state"], "Отчёт подготовить не удалось."))


async def start_period(message: Message, state: FSMContext, connections, reports, arguments: str):
    if not connections.is_connected(message.chat.id):
        raise NotConnected
    words = arguments.split()
    if words and words[0].lower() in {"период", "period"}:
        await state.clear()
        await state.set_state(AnalysisForm.zone)
        await message.answer("Укажите часовой пояс, например Europe/Moscow или UTC. Отмена — /cancel.")
        return
    try:
        if len(words) == 2 and words[0].isdigit():
            period = rolling_period(int(words[0]), words[1], reports.clock())
            request = {"kind": "rolling", "days": int(words[0]), "zone": words[1]}
        elif len(words) == 3:
            period = explicit_period(words[0], words[1], words[2], reports.clock())
            request = {"kind": "explicit", "first": words[0], "last": words[1], "zone": words[2]}
        else:
            raise ValueError("Используйте /analysis период либо /analysis 01.10.2026 31.10.2026 Europe/Moscow. Пресет — /analysis 30 Europe/Moscow.")
    except (ValueError, ZoneInfoNotFoundError) as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await send_period(message, reports, period, request)


async def period_zone(message: Message, state: FSMContext):
    zone = message.text.strip()
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        await message.answer("Часовой пояс не найден. Введите Europe/Moscow, UTC или другой пояс IANA.")
        return
    await state.update_data(analysis_zone=zone)
    await state.set_state(AnalysisForm.first)
    await message.answer("Введите начальную дату в формате ДД.ММ.ГГГГ.")


async def period_first(message: Message, state: FSMContext, reports):
    data = await state.get_data()
    first = message.text.strip()
    try:
        explicit_period(first, first, data["analysis_zone"], reports.clock())
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(analysis_first=first)
    await state.set_state(AnalysisForm.last)
    await message.answer("Введите конечную дату в формате ДД.ММ.ГГГГ. Она включается в период целиком.")


async def period_last(message: Message, state: FSMContext, reports, connections):
    if not connections.is_connected(message.chat.id):
        raise NotConnected
    data = await state.get_data()
    request = {"kind": "explicit", "first": data["analysis_first"], "last": message.text.strip(), "zone": data["analysis_zone"]}
    try:
        period = explicit_period(request["first"], request["last"], request["zone"], reports.clock())
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await send_period(message, reports, period, request)


def register_analysis_handlers(router):
    router.message.register(period_zone, AnalysisForm.zone, F.text)
    router.message.register(period_first, AnalysisForm.first, F.text)
    router.message.register(period_last, AnalysisForm.last, F.text)
