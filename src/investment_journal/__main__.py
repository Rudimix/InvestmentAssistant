"""Запуск: ``python -m investment_journal``."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from .bot import COMMANDS, create_router
from .config import McpSettings, Settings, load_env, load_settings, mcp_settings
from .connections import Connections, TokenCipher, generate_key
from .llm import ChatCompletionsClient, InvestmentAgent
from .mcp_client import McpToolClient
from .storage import JournalRepository
from .portfolio_reports import PortfolioReportService
from .report_scheduling import ReportScheduler
from .evidence_service import EvidenceService
from .evidence_sources import EvidenceHttpClient, source_registry


def build_dispatcher(settings: Settings, connections: Connections,
                     llm: ChatCompletionsClient | None = None, evidence=None) -> Dispatcher:
    """Собрать сервисы и Dispatcher; зависимости попадают в обработчики по имени."""
    agent = None
    if llm is not None and settings.llm is not None:
        agent = InvestmentAgent(llm, settings.mcp.read_only_tools, settings.llm.max_steps)
    dp = Dispatcher(connections=connections, agent=agent, reports=PortfolioReportService(
        connections, llm, evidence=evidence, forecast_directory=settings.forecast_models_path))
    dp.include_router(create_router())
    return dp


async def cancel_update_tasks(dispatcher: Dispatcher) -> None:
    """Polling aiogram хранит задачи обновлений отдельно от своей задачи получения."""
    tasks = list(dispatcher._handle_update_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(settings: Settings) -> None:
    cipher = TokenCipher(settings.token_key)
    async with AsyncExitStack() as resources:
        repo = JournalRepository(settings.db_path)
        resources.callback(repo.close)
        llm = ChatCompletionsClient(settings.llm) if settings.llm else None
        if llm:
            resources.push_async_callback(llm.aclose)
        bot = Bot(settings.bot_token)
        resources.push_async_callback(bot.session.close)
        connections = Connections(repo, cipher, settings.mcp)
        evidence = EvidenceService(repo, EvidenceHttpClient(settings.evidence_ca_files), source_registry(settings.evidence_sources_path))
        resources.push_async_callback(evidence.aclose)
        dispatcher = build_dispatcher(settings, connections, llm, evidence)
        reports = dispatcher["reports"]
        resources.push_async_callback(reports.aclose)
        resources.push_async_callback(cancel_update_tasks, dispatcher)
        scheduler = ReportScheduler(reports, bot.send_message)
        resources.push_async_callback(scheduler.stop)
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
        scheduler.start()
        await dispatcher.start_polling(bot, close_bot_session=False)


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
