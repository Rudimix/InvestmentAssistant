"""Запуск: ``python -m investment_journal``."""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from .bot import create_router
from .config import Settings, load_settings
from .journal import JournalService
from .llm import ChatCompletionsClient, InvestmentAgent
from .market import MarketDataService
from .mcp_client import ToolClient, McpToolClient
from .storage import JournalRepository


def build_dispatcher(settings: Settings, tools: ToolClient, repo: JournalRepository,
                     llm: ChatCompletionsClient | None = None) -> Dispatcher:
    """Собрать сервисы и Dispatcher; зависимости попадают в обработчики по имени."""
    journal = JournalService(repo, MarketDataService(tools, settings.mcp))
    agent = None
    if llm is not None and settings.llm is not None:
        agent = InvestmentAgent(llm, journal, settings.mcp.read_only_tools, settings.llm.max_steps)
    dp = Dispatcher(journal=journal, agent=agent)
    dp.include_router(create_router())
    return dp


async def run(settings: Settings) -> None:
    repo = JournalRepository(settings.db_path)
    llm = ChatCompletionsClient(settings.llm) if settings.llm else None
    bot = Bot(settings.bot_token)
    try:
        async with McpToolClient(settings.mcp) as tools:
            await build_dispatcher(settings, tools, repo, llm).start_polling(bot)
    finally:
        await bot.session.close()
        if llm:
            await llm.aclose()
        repo.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(load_settings()))


if __name__ == "__main__":
    main()
