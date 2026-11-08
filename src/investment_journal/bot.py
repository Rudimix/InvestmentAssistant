"""Telegram-бот инвестиционного ассистента (aiogram 3)."""

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from .analytics import candle_stats
from .formatting import format_journal, format_portfolio, format_review, format_reviews, format_stats
from .journal import JournalError, JournalService
from .llm import InvestmentAgent, LlmError
from .market import INTERVALS
from .mcp_client import ToolError
from .models import DECISION_KINDS, DecisionDraft

HELP = (
    "Я веду журнал инвестиционных гипотез и беру данные из Т-Инвестиций через MCP.\n\n"
    "/price ТИКЕР — последняя цена\n"
    "/stats ТИКЕР [интервал] [свечей] — статистика по свечам (интервалы: 1m 5m 15m 1h 4h 1d 1w)\n"
    "/portfolio — портфель счёта\n"
    "/idea — записать гипотезу (buy/sell/hold) с текущей ценой\n"
    "/journal — все записи журнала\n"
    "/review — проверить открытые гипотезы по текущим ценам\n"
    "/close НОМЕР — закрыть гипотезу по текущей цене\n"
    "/reset — забыть историю диалога\n\n"
    "Любой другой текст — вопрос ассистенту в свободной форме (если подключена LLM)."
)


#: Значения по умолчанию для «/stats ТИКЕР [интервал] [свечей]».
STATS_DEFAULTS = ["", "1d", "30"]


class IdeaForm(StatesGroup):
    kind = State()
    ticker = State()
    hypothesis = State()


KIND_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=k) for k in DECISION_KINDS]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


async def cmd_start(message: Message, journal: JournalService) -> None:
    journal.user(message.chat.id)
    await message.answer(HELP)


async def cmd_price(message: Message, command: CommandObject, journal: JournalService) -> None:
    if not command.args:
        await message.answer("Укажите тикер: /price SBER")
        return
    ticker = command.args.split()[0].upper()
    try:
        price = await journal.market.last_price(ticker)
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


async def cmd_stats(message: Message, command: CommandObject, journal: JournalService) -> None:
    try:
        ticker, interval, bars = parse_stats_args(command.args)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    try:
        candles = await journal.market.candles(ticker, interval, bars)
    except ToolError as exc:
        await message.answer(f"Не удалось получить свечи {ticker}: {exc}")
        return
    if len(candles) < 2:
        await message.answer(f"Недостаточно свечей по {ticker}")
        return
    await message.answer(format_stats(ticker, interval, candle_stats(candles), len(candles)))


async def cmd_portfolio(message: Message, journal: JournalService) -> None:
    try:
        portfolio = await journal.market.portfolio()
    except ToolError as exc:
        await message.answer(f"Не удалось получить портфель: {exc}")
        return
    await message.answer(format_portfolio(portfolio))


async def cmd_idea(message: Message, state: FSMContext) -> None:
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


async def idea_hypothesis(message: Message, state: FSMContext, journal: JournalService) -> None:
    data = await state.get_data()
    user = journal.user(message.chat.id)
    draft = DecisionDraft(user.id, data["kind"], data["ticker"], message.text.strip())
    try:
        decision = await journal.record(draft)
    except JournalError as exc:
        await message.answer(str(exc))
        return
    except ToolError as exc:
        await state.clear()
        await message.answer(f"Не удалось получить цену {draft.ticker}: {exc}. Гипотеза не сохранена.")
        return
    await state.clear()
    await message.answer(f"Записано #{decision.id}: {decision.kind} {decision.ticker} по {decision.entry_price:g}")


async def cmd_journal(message: Message, journal: JournalService) -> None:
    user = journal.user(message.chat.id)
    await message.answer(format_journal(journal.repo.list_decisions(user.id)))


async def cmd_review(message: Message, journal: JournalService) -> None:
    user = journal.user(message.chat.id)
    try:
        reviews = await journal.review(user)
    except ToolError as exc:
        await message.answer(f"Не удалось получить цены: {exc}")
        return
    await message.answer(format_reviews(reviews))


async def cmd_close(message: Message, command: CommandObject, journal: JournalService) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer("Укажите номер записи: /close 3")
        return
    user = journal.user(message.chat.id)
    try:
        review = await journal.close(user, int(arg))
    except (JournalError, ToolError) as exc:
        await message.answer(str(exc))
        return
    await message.answer("Закрыто.\n" + format_review(review))


async def cmd_reset(message: Message, agent: InvestmentAgent | None = None) -> None:
    if agent is not None:
        agent.reset(message.chat.id)
    await message.answer("История диалога очищена.")


async def free_text(message: Message, agent: InvestmentAgent | None = None) -> None:
    if agent is None:
        await message.answer("Свободный диалог не настроен (нет LLM_API_KEY / LLM_BASE_URL).\n\n" + HELP)
        return
    try:
        answer = await agent.ask(message.chat.id, message.text)
    except (LlmError, ToolError) as exc:
        await message.answer(f"Ассистент недоступен: {exc}")
        return
    await message.answer(answer[:4000])


#: Порядок важен: команды раньше шагов диалога /idea, свободный текст — последним.
HANDLERS = (
    (cmd_start, (CommandStart(),)),
    (cmd_start, (Command("help"),)),
    (cmd_price, (Command("price"),)),
    (cmd_stats, (Command("stats"),)),
    (cmd_portfolio, (Command("portfolio"),)),
    (cmd_idea, (Command("idea"),)),
    (cmd_cancel, (Command("cancel"),)),
    (idea_kind, (IdeaForm.kind, F.text)),
    (idea_ticker, (IdeaForm.ticker, F.text)),
    (idea_hypothesis, (IdeaForm.hypothesis, F.text)),
    (cmd_journal, (Command("journal"),)),
    (cmd_review, (Command("review"),)),
    (cmd_close, (Command("close"),)),
    (cmd_reset, (Command("reset"),)),
    (free_text, (F.text,)),
)


def create_router() -> Router:
    """Новый Router со всеми обработчиками (Router можно подключить только к одному Dispatcher)."""
    router = Router(name="assistant")
    for handler, filters in HANDLERS:
        router.message.register(handler, *filters)
    return router
