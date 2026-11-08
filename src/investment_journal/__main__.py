"""Запуск: ``python -m investment_journal``."""

import argparse
import asyncio
import json
import logging
import sys

from aiogram import Bot, Dispatcher

from .bot import create_router
from .config import McpSettings, Settings, load_env, load_settings, mcp_settings
from .journal import JournalService
from .llm import ChatCompletionsClient, InvestmentAgent
from .market import MarketDataService
from .mcp_client import ToolClient, McpToolClient
from .storage import JournalRepository


def build_dispatcher(settings: Settings, tools: ToolClient, repo: JournalRepository,
                     llm: ChatCompletionsClient | None = None) -> Dispatcher:
    """Собрать сервисы и Dispatcher; зависимости попадают в обработчики по имени."""
    journal = JournalService(repo, MarketDataService(tools, settings.mcp.account_id))
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


async def list_tools(settings: McpSettings) -> list[dict]:
    """Инструменты MCP-сервера с их схемами — для проверки подключения и настройки имён."""
    async with McpToolClient(settings) as tools:
        return await tools.list_tools()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="investment-assistant", description="Telegram-бот инвестиционного ассистента")
    parser.add_argument("--list-tools", action="store_true",
                        help="подключиться к MCP-серверу, вывести его инструменты в JSON и выйти")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.list_tools:
        tools = asyncio.run(list_tools(mcp_settings(load_env())))
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(tools, ensure_ascii=False, indent=2))
        return
    asyncio.run(run(load_settings()))


if __name__ == "__main__":
    main()
