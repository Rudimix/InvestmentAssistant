"""Подтверждаемый профиль целей и ограничений пользователя."""

from decimal import Decimal
import re

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from .models import InvestorProfile, decimal_value
from .connections import looks_like_token
from .portfolio_formatting import plain_chunks

QUESTIONS = (
    ("goal", "Опишите цель инвестирования."),
    ("horizon_months", "Укажите горизонт в месяцах, например 36."),
    ("risk_level", "Укажите допустимый риск — низкий, средний или высокий."),
    ("max_drawdown_pct", "Укажите максимально допустимую просадку в процентах, например 20%."),
    ("minimum_cash", "Укажите минимальный денежный остаток и валюту, например 10000 RUB."),
    ("max_position_pct", "Укажите максимальную долю одной позиции в процентах, например 25%."),
    ("max_sector_pct", "Укажите максимальную долю одного сектора в процентах, например 40%."),
)
RISK_LABELS = {"low": "низкий", "medium": "средний", "high": "высокий"}


class ProfileForm(StatesGroup):
    value = State()
    confirm = State()


def parse_profile_value(field: str, text: str) -> dict:
    text = text.strip()
    if field == "goal":
        if not text or looks_like_token(text):
            raise ValueError("Укажите цель инвестирования")
        return {field: text}
    if field == "risk_level":
        risks = {value: key for key, value in RISK_LABELS.items()} | {key: key for key in RISK_LABELS}
        if text.lower() not in risks:
            raise ValueError("Выберите низкий, средний или высокий риск")
        return {field: risks[text.lower()]}
    if field == "horizon_months":
        match = re.fullmatch(r"([0-9]+)\s*(?:мес\.?|месяцев|месяца)?", text)
        if not match or int(match[1]) <= 0:
            raise ValueError("Горизонт должен быть положительным целым числом месяцев")
        return {field: int(match[1])}
    if field == "minimum_cash":
        match = re.fullmatch(r"([0-9][0-9 .,]*)\s+([A-Za-z]{3})", text)
        if not match:
            raise ValueError("Укажите неотрицательную сумму и трёхбуквенную валюту, например 10000 RUB")
        amount = decimal_value(match[1].replace(" ", "").replace(",", "."))
        return {field: str(amount), "currency": match[2].lower()}
    if field not in {"max_drawdown_pct", "max_position_pct", "max_sector_pct"}:
        raise ValueError("Неизвестное поле профиля")
    amount = decimal_value(text.removesuffix("%").strip().replace(",", "."))
    if not Decimal(0) < amount <= Decimal(100):
        raise ValueError("Укажите процент больше 0 и не больше 100")
    return {field: str(amount)}


def profile_description(profile: InvestorProfile) -> str:
    return (f"Профиль · версия {profile.revision}\nЦель — {profile.goal}\n"
            f"Горизонт — {profile.horizon_months} мес.\nРиск — {RISK_LABELS[profile.risk_level]}\n"
            f"Допустимая просадка — {profile.max_drawdown_pct}%\n"
            f"Минимум денег — {profile.minimum_cash} {profile.currency.upper()}\n"
            f"Одна позиция — до {profile.max_position_pct}%\nСектор — до {profile.max_sector_pct}%")


async def send_profile_text(message, text, markup=None):
    parts = list(plain_chunks(text, 3500))
    for index, part in enumerate(parts):
        await message.answer(part, reply_markup=markup if index == len(parts)-1 else None)


async def cmd_profile(message: Message, state: FSMContext, connections, reports):
    user = connections.user(message.chat.id)
    current = connections.repo.get_profile(user.id)
    if current:
        await send_profile_text(message, profile_description(current))
    await state.clear()
    await state.update_data(profile_values={}, profile_step=0, profile_revision=current.revision if current else 0)
    await state.set_state(ProfileForm.value)
    await message.answer(QUESTIONS[0][1]+" Отмена — /cancel.", reply_markup=ReplyKeyboardRemove())


async def profile_value(message: Message, state: FSMContext, connections, reports):
    data = await state.get_data()
    step = data["profile_step"]
    try:
        values = data["profile_values"] | parse_profile_value(QUESTIONS[step][0], message.text)
    except (ValueError, TypeError):
        await message.answer("Значение не подходит. "+QUESTIONS[step][1])
        return
    await state.update_data(profile_values=values, profile_step=step+1)
    if step+1 < len(QUESTIONS):
        await message.answer(QUESTIONS[step+1][1])
        return
    user = connections.user(message.chat.id)
    profile = InvestorProfile.from_dict({"user_id": user.id, "revision": data["profile_revision"]+1,
                                        "confirmed_at": reports.clock(), **values})
    await state.set_state(ProfileForm.confirm)
    await send_profile_text(message, profile_description(profile)+"\nПодтвердите сохранение или отправьте /cancel.",
                            ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Сохранить профиль")]], resize_keyboard=True))


async def profile_confirm(message: Message, state: FSMContext, connections, reports):
    if message.text.strip() != "Сохранить профиль":
        await message.answer("Для подтверждения нажмите «Сохранить профиль». Отмена — /cancel.")
        return
    data = await state.get_data()
    user = connections.user(message.chat.id)
    async with reports.settings_lock(user.id):
        current = connections.repo.get_profile(user.id)
        if (current.revision if current else 0) != data["profile_revision"]:
            await state.clear()
            await message.answer("Профиль уже изменился. Повторите /profile.", reply_markup=ReplyKeyboardRemove())
            return
        profile = InvestorProfile.from_dict({"user_id": user.id, "revision": data["profile_revision"]+1,
                                            "confirmed_at": reports.clock(), **data["profile_values"]})
        connections.repo.save_profile(user.id, profile)
    await state.clear()
    await message.answer(f"Профиль сохранён · версия {profile.revision}", reply_markup=ReplyKeyboardRemove())


def register_profile_handlers(router):
    router.message.register(cmd_profile, Command("profile"))
    router.message.register(profile_value, ProfileForm.value, F.text, ~F.text.startswith("/"))
    router.message.register(profile_confirm, ProfileForm.confirm, F.text, ~F.text.startswith("/"))
