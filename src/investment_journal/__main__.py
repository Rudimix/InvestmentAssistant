"""Запуск: ``python -m investment_journal``."""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from .bot import router
from .config import load_settings
from .journal import JournalService
from .llm import ChatCompletionsClient, InvestmentAgent
from .market import MarketDataService
from .mcp_client import McpToolClient
from .storage import JournalRepository


async def run() -> None:
    settings = load_settings()
    repo = JournalRepository(settings.db_path)
    async with McpToolClient(settings.mcp) as tools:
        market = MarketDataService(tools, settings.mcp)
        journal = JournalService(repo, market)
        llm = ChatCompletionsClient(settings.llm) if settings.llm else None
        agent = (
            InvestmentAgent(llm, journal, settings.mcp.read_only_tools, settings.llm.max_steps)
            if llm else None
        )
        dp = Dispatcher(journal=journal, agent=agent)
        dp.include_router(router)
        bot = Bot(settings.bot_token)
        try:
            await dp.start_polling(bot)
        finally:
            await bot.session.close()
            if llm:
                await llm.aclose()
            repo.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
