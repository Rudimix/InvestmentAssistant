"""Telegram-бот инвестиционного ассистента (aiogram 3).

Каждый пользователь подключает свой токен T-Invest API командой /connect и
работает только со своими счетами. Бот отвечает только в личных чатах.
"""

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart, ExceptionTypeFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ErrorEvent, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from .analytics import candle_stats
from .connections import Connections, NotConnected, looks_like_token
from .formatting import (format_accounts, format_journal, format_portfolio, format_review, format_reviews,
                         format_stats)
from .journal import JournalError, JournalService
from .llm import InvestmentAgent, LlmError
from .market import INTERVALS
from .mcp_client import ToolError
from .models import DECISION_KINDS, DecisionDraft

HELP = (
    "Я веду журнал инвестиционных гипотез и беру данные из ваших Т-Инвестиций через MCP.\n\n"
    "/connect — подключить свой токен T-Invest API\n"
    "/accounts — счета; /account НОМЕР — выбрать счёт для /portfolio\n"
    "/disconnect — удалить токен из бота\n\n"
    "/price ТИКЕР — последняя цена\n"
    "/stats ТИКЕР [интервал] [свечей] — статистика по свечам (интервалы: 1m 5m 15m 1h 4h 1d 1w)\n"
    "/portfolio — портфель выбранного счёта\n"
    "/idea — записать гипотезу (buy/sell/hold) с текущей ценой\n"
    "/journal — все записи журнала\n"
    "/review — проверить открытые гипотезы по текущим ценам\n"
    "/close НОМЕР — закрыть гипотезу по текущей цене\n"
    "/reset — забыть историю диалога\n\n"
    "Любой другой текст — вопрос ассистенту в свободной форме (если подключена LLM)."
)

NOT_CONNECTED = "Сначала подключите свой токен T-Invest API: /connect"

CONNECT_HELP = (
    "Пришлите токен T-Invest API следующим сообщением.\n\n"
    "Где взять: приложение или сайт Т-Инвестиций → Настройки → Токены T-Invest API → Создать токен.\n"
    "Выберите доступ «Только чтение»: бот не торгует, и такой токен безопаснее.\n\n"
    "Сообщение с токеном я сразу удалю, а сам токен сохраню в зашифрованном виде. "
    "Отозвать его можно в тех же настройках или командой /disconnect. Отмена — /cancel."
)

TOKEN_IN_CHAT = ("Похоже, вы прислали токен T-Invest API. Сообщение я удалил и никуда его не передавал. "
                 "Чтобы подключить токен, отправьте /connect и затем токен.")


#: Значения по умолчанию для «/stats ТИКЕР [интервал] [свечей]».
STATS_DEFAULTS = ["", "1d", "30"]


class IdeaForm(StatesGroup):
    kind = State()
    ticker = State()
    hypothesis = State()


class ConnectForm(StatesGroup):
    token = State()


KIND_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=k) for k in DECISION_KINDS]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


async def delete_quietly(message: Message) -> None:
    """Удалить сообщение пользователя; если Telegram не дал — не страшно."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass


async def cmd_start(message: Message, connections: Connections) -> None:
    text = HELP if connections.is_connected(message.chat.id) else f"{HELP}\n\n{NOT_CONNECTED}"
    await message.answer(text)


async def cmd_connect(message: Message, state: FSMContext) -> None:
    await state.set_state(ConnectForm.token)
    await message.answer(CONNECT_HELP)


async def connect_token(message: Message, state: FSMContext, connections: Connections,
                        agent: InvestmentAgent | None = None) -> None:
    await delete_quietly(message)
    await state.clear()
    try:
        accounts = await connections.connect(message.chat.id, message.text)
    except ToolError as exc:
        await message.answer(f"Не удалось подключить токен: {exc}.\nПопробуйте ещё раз: /connect")
        return
    if agent is not None:
        agent.reset(message.chat.id)
    lines = ["Токен подключён.", "", format_accounts(accounts, accounts[0].id)]
    if not all(a.read_only for a in accounts):
        lines += ["", "У токена есть право на сделки. Бот им не пользуется, но надёжнее выпустить "
                      "токен «Только чтение» и подключить его заново."]
    await message.answer("\n".join(lines))


async def cmd_disconnect(message: Message, connections: Connections,
                         agent: InvestmentAgent | None = None) -> None:
    removed = connections.disconnect(message.chat.id)
    if agent is not None:
        agent.reset(message.chat.id)
    if not removed:
        await message.answer("Токен и так не подключён.")
        return
    await message.answer("Токен удалён из бота. Журнал гипотез сохранён. "
                         "Сам токен можно отозвать в настройках Т-Инвестиций.")


async def cmd_accounts(message: Message, connections: Connections) -> None:
    try:
        async with connections.market(message.chat.id) as market:
            accounts = await market.accounts()
    except ToolError as exc:
        await message.answer(f"Не удалось получить счета: {exc}")
        return
    await message.answer(format_accounts(accounts, connections.selected_account(message.chat.id)))


async def cmd_account(message: Message, command: CommandObject, connections: Connections) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Укажите номер счёта из /accounts: /account 2")
        return
    try:
        async with connections.market(message.chat.id) as market:
            accounts = await market.accounts()
    except ToolError as exc:
        await message.answer(f"Не удалось получить счета: {exc}")
        return
    if not 1 <= int(arg) <= len(accounts):
        await message.answer(f"Нет счёта №{arg}. Список: /accounts")
        return
    account = accounts[int(arg) - 1]
    connections.select_account(message.chat.id, account.id)
    await message.answer(f"Для /portfolio выбран счёт «{account.name or account.id}».")


async def cmd_price(message: Message, command: CommandObject, connections: Connections) -> None:
    if not command.args:
        await message.answer("Укажите тикер: /price SBER")
        return
    ticker = command.args.split()[0].upper()
    try:
        async with connections.market(message.chat.id) as market:
            price = await market.last_price(ticker)
    except ToolError as exc:
        await message.answer(f"Не удалось получить цену {ticker}: {exc}")
        return
    await message.answer(f"{ticker}: {price:g}")


def parse_stats_args(args: str | None) -> tuple[str, str, int]:
    """``"SiZ6 1h 50"`` → ``("SIZ6", "1h", 50)``; интервал и число свечей необязательны."""
    parts = (args or "").split()
    ticker, interval, bars = (parts + STATS_DEFAULTS[len(parts):])[:3]
    if not ticker:
        raise ValueError("Укажите тикер: /stats SBER 1d 30")
    if interval not in INTERVALS:
        raise ValueError(f"Интервал должен быть одним из: {', '.join(INTERVALS)}")
    if not bars.isdigit() or not 2 <= int(bars) <= 500:
        raise ValueError("Число свечей — от 2 до 500")
    return ticker.upper(), interval, int(bars)


async def cmd_stats(message: Message, command: CommandObject, connections: Connections) -> None:
    try:
        ticker, interval, bars = parse_stats_args(command.args)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    try:
        async with connections.market(message.chat.id) as market:
            candles = await market.candles(ticker, interval, bars)
    except ToolError as exc:
        await message.answer(f"Не удалось получить свечи {ticker}: {exc}")
        return
    if len(candles) < 2:
        await message.answer(f"Недостаточно свечей по {ticker}")
        return
    await message.answer(format_stats(ticker, interval, candle_stats(candles), len(candles)))


async def cmd_portfolio(message: Message, connections: Connections) -> None:
    try:
        async with connections.market(message.chat.id) as market:
            portfolio = await market.portfolio()
    except ToolError as exc:
        await message.answer(f"Не удалось получить портфель: {exc}")
        return
    await message.answer(format_portfolio(portfolio))


async def cmd_idea(message: Message, state: FSMContext, connections: Connections) -> None:
    if not connections.is_connected(message.chat.id):
        raise NotConnected
    await state.set_state(IdeaForm.kind)
    await message.answer("Какое решение? buy, sell или hold", reply_markup=KIND_KEYBOARD)


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


async def idea_kind(message: Message, state: FSMContext) -> None:
    kind = message.text.strip().lower()
    if kind not in DECISION_KINDS:
        await message.answer("Выберите buy, sell или hold", reply_markup=KIND_KEYBOARD)
        return
    await state.update_data(kind=kind)
    await state.set_state(IdeaForm.ticker)
    await message.answer("Тикер инструмента?", reply_markup=ReplyKeyboardRemove())


async def idea_ticker(message: Message, state: FSMContext) -> None:
    ticker = message.text.strip().upper()
    if not ticker.isalnum():
        await message.answer("Тикер должен состоять из букв и цифр")
        return
    await state.update_data(ticker=ticker)
    await state.set_state(IdeaForm.hypothesis)
    await message.answer("Опишите гипотезу: почему вы ожидаете такое движение?")


async def idea_hypothesis(message: Message, state: FSMContext, connections: Connections) -> None:
    data = await state.get_data()
    user = connections.user(message.chat.id)
    draft = DecisionDraft(user.id, data["kind"], data["ticker"], message.text.strip())
    try:
        JournalService.validate(draft)
    except JournalError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    try:
        async with connections.journal(message.chat.id) as journal:
            decision = await journal.record(draft)
    except ToolError as exc:
        await message.answer(f"Не удалось получить цену {draft.ticker}: {exc}. Гипотеза не сохранена.")
        return
    await message.answer(f"Записано #{decision.id}: {decision.kind} {decision.ticker} по {decision.entry_price:g}")


async def cmd_journal(message: Message, connections: Connections) -> None:
    user = connections.user(message.chat.id)
    await message.answer(format_journal(connections.repo.list_decisions(user.id)))


async def cmd_review(message: Message, connections: Connections) -> None:
    user = connections.user(message.chat.id)
    try:
        async with connections.journal(message.chat.id) as journal:
            reviews = await journal.review(user)
    except ToolError as exc:
        await message.answer(f"Не удалось получить цены: {exc}")
        return
    await message.answer(format_reviews(reviews))


async def cmd_close(message: Message, command: CommandObject, connections: Connections) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer("Укажите номер записи: /close 3")
        return
    user = connections.user(message.chat.id)
    try:
        async with connections.journal(message.chat.id) as journal:
            review = await journal.close(user, int(arg))
    except (JournalError, ToolError) as exc:
        await message.answer(str(exc))
        return
    await message.answer("Закрыто.\n" + format_review(review))


async def cmd_reset(message: Message, agent: InvestmentAgent | None = None) -> None:
    if agent is not None:
        agent.reset(message.chat.id)
    await message.answer("История диалога очищена.")


async def free_text(message: Message, connections: Connections, agent: InvestmentAgent | None = None) -> None:
    if looks_like_token(message.text):
        # токен не должен уйти ни в историю диалога, ни к LLM-провайдеру
        await delete_quietly(message)
        await message.answer(TOKEN_IN_CHAT)
        return
    if agent is None:
        await message.answer("Свободный диалог не настроен (нет LLM_API_KEY / LLM_BASE_URL).\n\n" + HELP)
        return
    try:
        async with connections.journal(message.chat.id) as journal:
            answer = await agent.ask(message.chat.id, message.text, journal)
    except (LlmError, ToolError) as exc:
        await message.answer(f"Ассистент недоступен: {exc}")
        return
    await message.answer(answer[:4000])


async def on_not_connected(event: ErrorEvent) -> None:
    """Команде нужен токен, а пользователь его ещё не подключил."""
    message = event.update.message
    if message is not None:
        detail = str(event.exception)
        await message.answer(f"{detail}.\n{NOT_CONNECTED}" if detail else NOT_CONNECTED)


#: Порядок важен: команды раньше шагов диалогов /connect и /idea, свободный текст — последним.
HANDLERS = (
    (cmd_start, (CommandStart(),)),
    (cmd_start, (Command("help"),)),
    (cmd_connect, (Command("connect"),)),
    (cmd_disconnect, (Command("disconnect"),)),
    (cmd_accounts, (Command("accounts"),)),
    (cmd_account, (Command("account"),)),
    (cmd_price, (Command("price"),)),
    (cmd_stats, (Command("stats"),)),
    (cmd_portfolio, (Command("portfolio"),)),
    (cmd_idea, (Command("idea"),)),
    (cmd_cancel, (Command("cancel"),)),
    (cmd_journal, (Command("journal"),)),
    (cmd_review, (Command("review"),)),
    (cmd_close, (Command("close"),)),
    (cmd_reset, (Command("reset"),)),
    (connect_token, (ConnectForm.token, F.text)),
    (idea_kind, (IdeaForm.kind, F.text)),
    (idea_ticker, (IdeaForm.ticker, F.text)),
    (idea_hypothesis, (IdeaForm.hypothesis, F.text)),
    (free_text, (F.text,)),
)


def create_router() -> Router:
    """Новый Router со всеми обработчиками (Router можно подключить только к одному Dispatcher)."""
    router = Router(name="assistant")
    # у каждого пользователя свой токен и журнал: в группах бот молчит
    router.message.filter(F.chat.type == "private")
    for handler, filters in HANDLERS:
        router.message.register(handler, *filters)
    router.errors.register(on_not_connected, ExceptionTypeFilter(NotConnected))
    return router
