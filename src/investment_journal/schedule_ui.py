"""Настройка, предпросмотр и управление расписанием в личном чате Telegram."""

from datetime import timedelta
from secrets import token_hex
import json

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .connections import Connections, NotConnected
from .models import timestamp
from .portfolio_reports import PortfolioReportService
from .report_scheduling import (
    initial_run, parse_calendar_rule, parse_interval_rule, rule_from_dict, schedule_preview, schedule_zone,
)

RUN_LABELS = {"queued": "ожидает выполнения", "running": "выполняется", "retry": "назначен повтор",
              "delivered": "доставлен", "failed": "ошибка", "delivery_unknown": "исход доставки неизвестен",
              "cancelled": "отменён"}


class ScheduleForm(StatesGroup):
    zone = State()
    details = State()
    period = State()
    confirmation = State()


def keyboard(chat_id: int, rows: list[list[tuple[str, str]]], nonce: str = "") -> InlineKeyboardMarkup:
    suffix = f":{nonce}" if nonce else ""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=f"schedule:{chat_id}:{action}{suffix}")
        for text, action in row
    ] for row in rows])


async def selected_account(connections: Connections, chat_id: int) -> str:
    account_id = connections.selected_account(chat_id)
    if not account_id:
        async with connections.market(chat_id) as market:
            account_id = await market.account_id()
        connections.select_account(chat_id, account_id)
    return account_id


async def show_status(message: Message, connections: Connections, reports: PortfolioReportService) -> None:
    user = connections.user(message.chat.id)
    schedule = connections.repo.get_schedule(user.id)
    rows = [[("Интервал", "interval"), ("Календарь", "calendar")]]
    text = "Выберите способ автоматической доставки отчёта по выбранному счёту."
    if schedule:
        rule = rule_from_dict(schedule["rule"])
        next_at = schedule["next_run_at"] if schedule["enabled"] else initial_run(rule, reports.clock())
        text = schedule_preview(schedule["account_id"], rule, reports.clock(), next_at,
                                json.loads(schedule["analysis_policy"]), json.loads(schedule["sections"]))
        if schedule["enabled"]:
            text += "\nДоставка включена"
            rows.append([("Пауза", "pause")])
        else:
            text = text.replace("Ближайший запуск", "Плановый момент после возобновления")
            text += f"\nДоставка приостановлена — {schedule['pause_reason']}"
            rows.append([("Возобновить", "resume")])
    last = connections.repo.last_run(user.id)
    if last:
        text += f"\nПоследний отчёт — {RUN_LABELS.get(last['state'], last['state'])}"
        if last["last_error"]:
            text += f"\n{last['last_error']}"
        if last["retry_at"]:
            text += f"\nПовтор — {timestamp(last['retry_at']).isoformat()}"
    await message.answer(text, reply_markup=keyboard(message.chat.id, rows))


async def cmd_schedule(message: Message, state: FSMContext, connections: Connections,
                       reports: PortfolioReportService) -> None:
    if not connections.is_connected(message.chat.id):
        raise NotConnected
    await state.clear()
    await show_status(message, connections, reports)


async def show_confirmation(message: Message, state: FSMContext, connections: Connections,
                            reports: PortfolioReportService, rule) -> None:
    account_id = await selected_account(connections, message.chat.id)
    next_at = initial_run(rule, reports.clock())
    nonce = token_hex(4)
    previous = await state.get_data()
    policy = previous.get("schedule_policy", {"kind": "rolling", "days": 30})
    sections = previous.get("schedule_sections", ["analysis", "advice"])
    preview = schedule_preview(account_id, rule, reports.clock(), next_at, policy, sections)
    await state.set_data({"schedule_nonce": nonce, "schedule_rule": rule.to_dict(),
                          "schedule_account": account_id, "schedule_next": next_at.isoformat(),
                          "schedule_policy": policy, "schedule_sections": sections})
    await state.set_state(ScheduleForm.confirmation)
    await message.answer(preview,
                         reply_markup=keyboard(message.chat.id, [[("Период анализа", "period"), ("Разделы", "sections")],
                                                                 [("Сохранить", "save"), ("Отмена", "cancel")]], nonce))


async def period_text(message: Message, state: FSMContext, connections: Connections, reports: PortfolioReportService):
    text = message.text.strip().lower()
    if text in {"сравнение", "legacy"}:
        policy = {"kind": "legacy_delivered"}
    elif text.isdigit() and int(text) > 0:
        policy = {"kind": "rolling", "days": int(text)}
    else:
        await message.answer("Введите положительное число календарных дней, например 30, или слово сравнение.")
        return
    data = await state.get_data()
    await state.update_data(schedule_policy=policy)
    try:
        await show_confirmation(message, state, connections, reports, rule_from_dict(data["schedule_rule"]))
    except ValueError as exc:
        await message.answer(str(exc))


async def accept_zone(message: Message, state: FSMContext, reports: PortfolioReportService, zone: str) -> None:
    schedule_zone(zone)
    await state.update_data(schedule_zone=zone)
    await state.set_state(ScheduleForm.details)
    data = await state.get_data()
    if data["schedule_mode"] == "interval":
        tomorrow = (reports.clock().astimezone(schedule_zone(zone))+timedelta(days=1)).replace(hour=9, minute=0)
        text = (f"Укажите интервал — например 6ч или 3д. Первый отчёт придёт через этот интервал.\n"
                f"Для своего первого запуска добавьте дату и время — например 6ч {tomorrow:%d.%m.%Y %H:%M}.\n"
                "День в интервальном режиме равен 24 часам.")
    else:
        text = "Укажите дни и местное время — например ежедневно 09:00, будни 19:00 или пн,ср,пт 12:00."
    await message.answer(text, reply_markup=keyboard(message.chat.id, [[("Отмена", "cancel")]], data["schedule_nonce"]))


async def zone_text(message: Message, state: FSMContext, reports: PortfolioReportService) -> None:
    try:
        await accept_zone(message, state, reports, message.text.strip())
    except ValueError as exc:
        await message.answer(str(exc))


async def details_text(message: Message, state: FSMContext, connections: Connections,
                       reports: PortfolioReportService) -> None:
    data = await state.get_data()
    try:
        if data["schedule_mode"] == "interval":
            rule = parse_interval_rule(message.text, data["schedule_zone"], reports.clock())
        else:
            rule = parse_calendar_rule(message.text, data["schedule_zone"])
        await show_confirmation(message, state, connections, reports, rule)
    except (ValueError, OverflowError) as exc:
        await message.answer(str(exc))


async def save_draft(query: CallbackQuery, state: FSMContext, connections: Connections,
                     reports: PortfolioReportService, nonce: str) -> None:
    user = connections.user(query.message.chat.id)
    async with reports.settings_lock(user.id):
        data = await state.get_data()
        if await state.get_state() != ScheduleForm.confirmation.state or data.get("schedule_nonce") != nonce:
            await query.message.answer("Настройка устарела. Откройте /schedule.")
            return
        rule = rule_from_dict(data["schedule_rule"])
        account_id = await selected_account(connections, query.message.chat.id)
        next_at = initial_run(rule, reports.clock())
        if account_id != data["schedule_account"] or next_at != timestamp(data["schedule_next"]):
            await show_confirmation(query.message, state, connections, reports, rule)
            return
        connections.repo.save_schedule(user.id, account_id, rule.to_dict(), next_at,
                                       data["schedule_policy"], data["schedule_sections"])
        await state.clear()
    await query.message.answer("Расписание сохранено.")
    await show_status(query.message, connections, reports)


async def schedule_callback(query: CallbackQuery, state: FSMContext, connections: Connections,
                            reports: PortfolioReportService) -> None:
    parts = (query.data or "").split(":")
    message = query.message
    if (len(parts) not in {3, 4} or not isinstance(message, Message) or message.chat.type != "private"
            or message.chat.id != query.from_user.id or parts[1] != str(query.from_user.id)):
        await query.answer("Эта настройка недоступна.", show_alert=True)
        return
    await query.answer()
    if not connections.is_connected(message.chat.id):
        await message.answer("Подключите токен через /connect, затем откройте /schedule.")
        return
    action = parts[2]
    user = connections.user(message.chat.id)
    try:
        if len(parts) == 3:
            if action in {"interval", "calendar"}:
                nonce = token_hex(4)
                schedule = connections.repo.get_schedule(user.id)
                draft = {"schedule_mode": action, "schedule_nonce": nonce}
                if schedule:
                    draft.update(schedule_policy=json.loads(schedule["analysis_policy"]), schedule_sections=json.loads(schedule["sections"]))
                await state.set_data(draft)
                await state.set_state(ScheduleForm.zone)
                await message.answer("Выберите часовой пояс или введите его IANA-имя, например Asia/Yekaterinburg.",
                                     reply_markup=keyboard(message.chat.id, [[("Москва", "moscow"), ("UTC", "utc")],
                                                                            [("Отмена", "cancel")]], nonce))
            elif action == "pause":
                async with reports.settings_lock(user.id):
                    connections.repo.pause_schedule(user.id, "пауза пользователя")
                    await state.clear()
                await show_status(message, connections, reports)
            elif action == "resume":
                schedule = connections.repo.get_schedule(user.id)
                if schedule:
                    await state.update_data(schedule_policy=json.loads(schedule["analysis_policy"]),
                                            schedule_sections=json.loads(schedule["sections"]))
                    await show_confirmation(message, state, connections, reports, rule_from_dict(schedule["rule"]))
            return
        data = await state.get_data()
        if data.get("schedule_nonce") != parts[3]:
            await message.answer("Настройка устарела. Откройте /schedule.")
            return
        if action == "cancel":
            await state.clear()
            await message.answer("Настройка отменена.")
        elif action in {"moscow", "utc"} and await state.get_state() == ScheduleForm.zone.state:
            await accept_zone(message, state, reports, "Europe/Moscow" if action == "moscow" else "UTC")
        elif action == "save":
            await save_draft(query, state, connections, reports, parts[3])
        elif action == "period" and await state.get_state() == ScheduleForm.confirmation.state:
            await state.set_state(ScheduleForm.period)
            await message.answer("За сколько завершённых календарных дней показывать динамику? Введите число, например 30. Для прежнего сравнения введите сравнение.")
        elif action == "sections" and await state.get_state() == ScheduleForm.confirmation.state:
            await message.answer("Выберите разделы автоматического отчёта.", reply_markup=keyboard(message.chat.id,
                [[("Только анализ", "only"), ("Анализ и советы", "withadvice")],
                 [("Анализ и акции", "withstocks"), ("Все разделы", "allsections")]], data["schedule_nonce"]))
        elif action in {"only", "withadvice", "withstocks", "allsections"} and await state.get_state() == ScheduleForm.confirmation.state:
            sections = ["analysis"]
            if action in {"withadvice", "allsections"}:
                sections.append("advice")
            if action in {"withstocks", "allsections"}:
                sections.append("screening")
            await state.update_data(schedule_sections=sections)
            await show_confirmation(message, state, connections, reports, rule_from_dict(data["schedule_rule"]))
    except (ValueError, NotConnected) as exc:
        await message.answer(str(exc) or "Подключите токен через /connect.")


def register_schedule_handlers(router: Router) -> None:
    router.message.register(zone_text, ScheduleForm.zone, F.text)
    router.message.register(details_text, ScheduleForm.details, F.text)
    router.message.register(period_text, ScheduleForm.period, F.text, ~F.text.startswith("/"))
    router.callback_query.register(schedule_callback, F.data.startswith("schedule:"))
