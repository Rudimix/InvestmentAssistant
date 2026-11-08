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
    "/stats ТИКЕР [интервал] [свечей] — статистика по свечам (интервалы: 1m 5m 15m 1h 4h 1d)\n"
    "/portfolio — портфель счёта\n"
    "/idea — записать гипотезу (buy/sell/hold) с текущей ценой\n"
    "/journal — все записи журнала\n"
    "/review — проверить открытые гипотезы по текущим ценам\n"
    "/close НОМЕР — закрыть гипотезу по текущей цене\n"
    "/reset — забыть историю диалога\n\n"
    "Любой другой текст — вопрос ассистенту в свободной форме (если подключена LLM)."
)

router = Router(name="assistant")


class IdeaForm(StatesGroup):
    kind = State()
    ticker = State()
    hypothesis = State()


KIND_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=k) for k in DECISION_KINDS]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, journal: JournalService) -> None:
    journal.user(message.chat.id)
    await message.answer(HELP)


@router.message(Command("price"))
async def cmd_price(message: Message, command: CommandObject, journal: JournalService) -> None:
    if not command.args:
        await message.answer("Укажите тикер: /price SiZ6")
        return
    ticker = command.args.split()[0].upper()
    try:
        price = await journal.market.last_price(ticker)
    except ToolError as exc:
        await message.answer(f"Не удалось получить цену {ticker}: {exc}")
        return
    await message.answer(f"{ticker}: {price:g}")


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, journal: JournalService) -> None:
    parts = (command.args or "").split()
    if not parts:
        await message.answer("Укажите тикер: /stats SiZ6 1d 30")
        return
    ticker = parts[0].upper()
    interval = parts[1] if len(parts) > 1 else "1d"
    if interval not in INTERVALS:
        await message.answer(f"Интервал должен быть одним из: {', '.join(INTERVALS)}")
        return
    if len(parts) > 2:
        if not parts[2].isdigit() or not 2 <= int(parts[2]) <= 500:
            await message.answer("Число свечей — от 2 до 500")
            return
        bars = int(parts[2])
    else:
        bars = 30
    try:
        candles = await journal.market.candles(ticker, interval, bars)
    except ToolError as exc:
        await message.answer(f"Не удалось получить свечи {ticker}: {exc}")
        return
    if len(candles) < 2:
        await message.answer(f"Недостаточно свечей по {ticker}")
        return
    await message.answer(format_stats(ticker, interval, candle_stats(candles), len(candles)))


@router.message(Command("portfolio"))
async def cmd_portfolio(message: Message, journal: JournalService) -> None:
    try:
        portfolio = await journal.market.portfolio()
    except ToolError as exc:
        await message.answer(f"Не удалось получить портфель: {exc}")
        return
    await message.answer(format_portfolio(portfolio))


@router.message(Command("idea"))
async def cmd_idea(message: Message, state: FSMContext) -> None:
    await state.set_state(IdeaForm.kind)
    await message.answer("Какое решение? buy, sell или hold", reply_markup=KIND_KEYBOARD)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(IdeaForm.kind, F.text)
async def idea_kind(message: Message, state: FSMContext) -> None:
    kind = message.text.strip().lower()
    if kind not in DECISION_KINDS:
        await message.answer("Выберите buy, sell или hold", reply_markup=KIND_KEYBOARD)
        return
    await state.update_data(kind=kind)
    await state.set_state(IdeaForm.ticker)
    await message.answer("Тикер инструмента?", reply_markup=ReplyKeyboardRemove())


@router.message(IdeaForm.ticker, F.text)
async def idea_ticker(message: Message, state: FSMContext) -> None:
    ticker = message.text.strip().upper()
    if not ticker.isalnum():
        await message.answer("Тикер должен состоять из букв и цифр")
        return
    await state.update_data(ticker=ticker)
    await state.set_state(IdeaForm.hypothesis)
    await message.answer("Опишите гипотезу: почему вы ожидаете такое движение?")


@router.message(IdeaForm.hypothesis, F.text)
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


@router.message(Command("journal"))
async def cmd_journal(message: Message, journal: JournalService) -> None:
    user = journal.user(message.chat.id)
    await message.answer(format_journal(journal.repo.list_decisions(user.id)))


@router.message(Command("review"))
async def cmd_review(message: Message, journal: JournalService) -> None:
    user = journal.user(message.chat.id)
    try:
        reviews = await journal.review(user)
    except ToolError as exc:
        await message.answer(f"Не удалось получить цены: {exc}")
        return
    await message.answer(format_reviews(reviews))


@router.message(Command("close"))
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


@router.message(Command("reset"))
async def cmd_reset(message: Message, agent: InvestmentAgent | None = None) -> None:
    if agent is not None:
        agent.reset(message.chat.id)
    await message.answer("История диалога очищена.")


@router.message(F.text)
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
