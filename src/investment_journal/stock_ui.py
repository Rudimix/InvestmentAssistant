"""Режим подбора, сохранённая история и основания для каждой проверенной акции."""

import json

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from .connections import NotConnected
from .mcp_client import ToolError
from .portfolio_formatting import plain_chunks
from .stock_formatting import candidate_lines
from .forecast_observation import refresh_forecast, observation_lines


MODES = {"фундаментальный": "fundamental", "технический": "technical", "совместный": "combined",
         "fundamental": "fundamental", "technical": "technical", "combined": "combined"}


class StocksForm(StatesGroup):
    mode = State()


async def request_stocks(message, reports, mode, request_key):
    await message.answer("Готовлю подборку по выбранному счёту. Первая проверка справочников может занять несколько минут.",
                         reply_markup=ReplyKeyboardRemove())
    try:
        run = await reports.request(message.chat.id, request_key, message.bot.send_message, report_kind="screening", mode=mode)
    except ToolError:
        await message.answer("Источник временно недоступен. Повторите запрос позже.")
        return
    if run["state"] != "delivered":
        await message.answer({"retry": "Запрос сохранён, повтор запланирован автоматически.",
                              "queued": "Запрос ожидает выполнения в очереди.",
                              "cancelled": "Профиль, счёт или подключение изменились. Повторите подбор.",
                              "delivery_unknown": "Исход доставки неизвестен. Проверьте полученные части."}.get(run["state"], "Подборку подготовить не удалось."))


async def cmd_stocks(message: Message, state: FSMContext, command: CommandObject, connections, reports):
    if not connections.is_connected(message.chat.id):
        raise NotConnected
    words = (command.args or "").split()
    await state.clear()
    user = connections.user(message.chat.id)
    account = connections.selected_account(message.chat.id)
    if words and words[0].lower() in {"история", "history"}:
        runs = connections.repo.screening_history(user.id, account)
        lines = ["Сохранённые подборки"]
        for run in runs:
            record = json.loads(run["payload"])["screening"]
            lines.append(f"{record['as_of']} · {record['mode']} · /stocks открыть {run['id']}")
        await message.answer("\n".join(lines) if runs else "Доставленных подборок для выбранного счёта пока нет.")
        return
    if len(words) == 2 and words[0].lower() in {"открыть", "open"}:
        run = connections.repo.get_run(user.id, words[1])
        if not run or run["account_id"] != account or run["report_kind"] != "screening" or run["state"] != "delivered":
            await message.answer("Сохранённая подборка недоступна для выбранного счёта.")
            return
        for part in connections.repo.get_parts(user.id, run["id"]):
            await message.answer(part["text"], parse_mode="HTML")
        return
    if len(words) == 3 and words[0].lower() in {"бумага", "stock", "кандидаты", "candidates", "гипотеза", "idea", "наблюдение", "observe"}:
        screening = connections.repo.get_screening(user.id, account, words[1])
        if not screening:
            await message.answer("Подборка недоступна для выбранного счёта.")
            return
        lines = [f"Зафиксированная оценка на {screening['as_of']}"]
        if words[0].lower() in {"наблюдение", "observe"}:
            candidate = next((item for item in screening["candidates"] if item["entry"]["ticker"].upper() == words[2].upper()), None)
            if not candidate or not candidate.get("forecast", {}).get("available"):
                await message.answer("У кандидата отсутствует сохранённый численный прогноз.")
                return
            async with connections.market(message.chat.id) as market:
                observation = await refresh_forecast(connections.repo, market, reports.screening.forecasts,
                    user.id, account, candidate["forecast"]["forecast"]["id"], reports.clock())
            await message.answer("\n".join(observation_lines(observation)))
            return
        if words[0].lower() in {"гипотеза", "idea"}:
            candidate = next((item for item in screening["candidates"] if item["entry"]["ticker"].upper() == words[2].upper()), None)
            if candidate is None:
                await message.answer("Бумага отсутствует среди кандидатов подборки.")
                return
            from .bot import IdeaForm, KIND_KEYBOARD
            ticker = candidate["entry"]["ticker"]
            request_key = f"screening-hypothesis:{screening['id']}:{ticker}"
            previous = connections.repo.linked_decision(user.id, account, request_key)
            if previous:
                await message.answer(f"Гипотеза уже записана · #{previous.id}")
                return
            await state.update_data(advice_source={"source_kind": "screening", "account_id": account,
                "screening_id": screening["id"], "ticker": ticker, "request_key": request_key})
            await state.set_state(IdeaForm.kind)
            await message.answer(f"Выберите buy, sell или hold для {ticker}. Основания подборки сохранятся с гипотезой.", reply_markup=KIND_KEYBOARD)
            return
        if words[0].lower() in {"кандидаты", "candidates"}:
            if not words[2].isdigit() or int(words[2]) < 1:
                await message.answer("Укажите номер страницы от 1.")
                return
            offset = (int(words[2])-1)*10
            candidates = screening["candidates"][offset:offset+10]
            for candidate in candidates:
                lines.extend(candidate_lines(candidate, screening["mode"]))
            if not candidates:
                lines.append("На этой странице кандидатов нет")
        else:
            ticker = words[2].upper()
            candidate = next((item for item in screening["candidates"] if item["entry"]["ticker"].upper() == ticker), None)
            if candidate:
                lines.extend(candidate_lines(candidate, screening["mode"]))
                if candidate.get("forecast", {}).get("available"):
                    observation = connections.repo.latest_forecast_observation(user.id, account, candidate["forecast"]["forecast"]["id"])
                    lines.extend(observation_lines(observation))
            else:
                universe = connections.repo.get_universe(user.id, account, screening["universe_id"])
                entry = next((item for item in universe.entries if item.ticker.upper() == ticker), None)
                lines.extend([entry.ticker]+entry.reasons if entry else ["Бумага отсутствует в проверенном составе каталога"])
        for chunk in plain_chunks("\n".join(lines), 3500):
            await message.answer(chunk)
        return
    if words and words[0].lower() in MODES and len(words) == 1:
        await request_stocks(message, reports, MODES[words[0].lower()], "stocks:"+str(message.message_id))
        return
    if words:
        await message.answer("Выберите /stocks fundamental, /stocks technical или /stocks combined. История — /stocks история.")
        return
    await state.set_state(StocksForm.mode)
    await state.update_data(stocks_request_key="stocks:"+str(message.message_id))
    await message.answer("Выберите режим подбора. Отмена — /cancel.", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=value)] for value in ("Фундаментальный", "Технический", "Совместный")], resize_keyboard=True))


async def stocks_mode(message: Message, state: FSMContext, connections, reports):
    if not connections.is_connected(message.chat.id):
        raise NotConnected
    mode = MODES.get(message.text.strip().lower())
    if mode is None:
        await message.answer("Выберите фундаментальный, технический или совместный режим.")
        return
    data = await state.get_data()
    await state.clear()
    await request_stocks(message, reports, mode, data["stocks_request_key"])


def register_stock_handlers(router):
    router.message.register(cmd_stocks, Command("stocks"))
    router.message.register(stocks_mode, StocksForm.mode, F.text, ~F.text.startswith("/"))
