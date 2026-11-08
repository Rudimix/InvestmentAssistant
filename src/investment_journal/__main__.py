"""Запуск: ``python -m investment_journal``."""

import argparse
import asyncio
import json
import logging
import sys

from aiogram import Bot, Dispatcher

from .bot import create_router
from .config import McpSettings, Settings, load_env, load_settings, mcp_settings
from .connections import Connections, TokenCipher, generate_key
from .llm import ChatCompletionsClient, InvestmentAgent
from .mcp_client import McpToolClient
from .storage import JournalRepository


def build_dispatcher(settings: Settings, connections: Connections,
                     llm: ChatCompletionsClient | None = None) -> Dispatcher:
    """Собрать сервисы и Dispatcher; зависимости попадают в обработчики по имени."""
    agent = None
    if llm is not None and settings.llm is not None:
        agent = InvestmentAgent(llm, settings.mcp.read_only_tools, settings.llm.max_steps)
    dp = Dispatcher(connections=connections, agent=agent)
    dp.include_router(create_router())
    return dp


async def run(settings: Settings) -> None:
    cipher = TokenCipher(settings.token_key)
    repo = JournalRepository(settings.db_path)
    llm = ChatCompletionsClient(settings.llm) if settings.llm else None
    bot = Bot(settings.bot_token)
    try:
        connections = Connections(repo, cipher, settings.mcp)
        await build_dispatcher(settings, connections, llm).start_polling(bot)
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
                        help="подключиться к MCP-серверу с TINKOFF_TOKEN из .env, вывести инструменты в JSON и выйти")
    parser.add_argument("--generate-key", action="store_true",
                        help="вывести новый ключ для TOKEN_ENCRYPTION_KEY и выйти")
    args = parser.parse_args(argv)
    if args.generate_key:
        print(generate_key())
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.list_tools:
        tools = asyncio.run(list_tools(mcp_settings(load_env())))
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(tools, ensure_ascii=False, indent=2))
        return
    asyncio.run(run(load_settings()))


if __name__ == "__main__":
    main()
