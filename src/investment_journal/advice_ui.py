"""История подтверждённых предложений и переход к инвестиционной гипотезе."""

import json

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .advice_formatting import EFFECT_LABELS
from .formatting import money
from .investor_profile import profile_description
from .models import decimal_value
from .portfolio_formatting import plain_chunks


def advice_links(chat_id: int, entries: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"advice:{chat_id}:view:{identity}")] for label, identity in entries]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def show_advice(message: Message, connections, proposal):
    lines = [proposal.text, f"Сохранено {proposal.created_at:%d.%m.%Y %H:%M UTC}"]
    profile = connections.repo.get_profile(proposal.user_id, proposal.profile_revision)
    if profile:
        lines.append(profile_description(profile))
    lines.extend(["Риски", *proposal.risks, "Условия пересмотра", *proposal.reconsider_when])
    if proposal.report_id:
        run = connections.repo.get_run(proposal.user_id, proposal.report_id)
        context = json.loads(run["payload"])["advice"]["context"]
        for key in proposal.metric_ids:
            metric = context["metrics"][key]
            value, unit = decimal_value(metric["value"]), metric["unit"]
            label = EFFECT_LABELS.get(key.rsplit(":", 1)[-1]) or {
                "profile:cash_shortfall": "Недостаток денежного резерва", "portfolio:total": "Стоимость портфеля",
                "profile:cash": "Денежный остаток", "profile:horizon_months": "Горизонт пользователя",
                "profile:max_drawdown_pct": "Допустимая просадка", "forecast:horizon_months": "Горизонт прогноза",
            }.get(key, "Подтверждённая доля" if key.endswith(":weight") else "Подтверждённый показатель")
            formatted = money(value, unit) if len(unit) == 3 else f"{value:f} {unit}"
            lines.append(f"{label} — {formatted}")
    for key in proposal.evidence_ids:
        chunk = connections.repo.get_evidence_chunk(key)
        document = connections.repo.get_document(chunk.document_id, chunk.document_version)
        page = f" · страница {chunk.page}" if chunk.page else ""
        lines.extend([document.title+page, document.source, document.url])
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Записать гипотезу",
                                    callback_data=f"advice:{message.chat.id}:idea:{proposal.id}")]])
    parts = list(plain_chunks("\n\n".join(lines), 3500))
    for index, text in enumerate(parts):
        await message.answer(text, reply_markup=markup if index == len(parts)-1 else None)


async def cmd_advice(message: Message, command: CommandObject, connections):
    user = connections.user(message.chat.id)
    account = connections.selected_account(message.chat.id)
    identity = (command.args or "").strip()
    if identity:
        proposal = connections.repo.get_advice(user.id, account, identity)
        if proposal is None:
            await message.answer("Предложение недоступно для выбранного счёта.")
            return
        await show_advice(message, connections, proposal)
        return
    proposals = connections.repo.list_advice(user.id, account)
    if not proposals:
        await message.answer("Для выбранного счёта сохранённых предложений пока нет. Запросите /analysis после заполнения /profile.")
        return
    for proposal in proposals:
        markup = advice_links(message.chat.id, [("Основания и риски", proposal.id)])
        await message.answer(proposal.text+f"\nПрофиль · версия {proposal.profile_revision}", reply_markup=markup)


async def advice_callback(query: CallbackQuery, state: FSMContext, connections):
    parts = (query.data or "").split(":")
    message = query.message
    if (len(parts) != 4 or not isinstance(message, Message) or message.chat.type != "private"
            or message.chat.id != query.from_user.id or parts[1] != str(query.from_user.id)):
        await query.answer("Предложение недоступно.", show_alert=True)
        return
    await query.answer()
    if not connections.is_connected(message.chat.id):
        await message.answer("Подключите счёт через /connect.")
        return
    user = connections.user(message.chat.id)
    account = connections.selected_account(message.chat.id)
    proposal = connections.repo.get_advice(user.id, account, parts[3])
    if proposal is None:
        await message.answer("Предложение недоступно для выбранного счёта.")
        return
    if parts[2] == "view":
        await show_advice(message, connections, proposal)
    elif parts[2] == "idea":
        from .bot import IdeaForm, KIND_KEYBOARD
        request_key = "advice-hypothesis:"+proposal.id
        previous = connections.repo.linked_decision(user.id, account, request_key)
        if previous:
            await message.answer(f"Гипотеза уже записана · #{previous.id}")
            return
        data = await state.get_data()
        if data.get("advice_source", {}).get("request_key") == request_key:
            await message.answer("Продолжите открытый ввод гипотезы. Отмена — /cancel.")
            return
        await state.clear()
        await state.update_data(advice_source={"account_id": account, "proposal_id": proposal.id, "request_key": request_key})
        await state.set_state(IdeaForm.kind)
        await message.answer("Выберите buy, sell или hold для гипотезы по выбранной бумаге. Основания предложения будут сохранены.",
                             reply_markup=KIND_KEYBOARD)


def register_advice_handlers(router):
    router.message.register(cmd_advice, Command("advice"))
    router.callback_query.register(advice_callback, F.data.startswith("advice:"))
