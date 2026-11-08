"""Настройки приложения из переменных окружения и файла ``.env``."""

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

#: Инструменты MCP-сервера, которые разрешено вызывать LLM (только чтение).
DEFAULT_READ_ONLY_TOOLS = (
    "get_last_price",
    "get_candles",
    "get_orderbook",
    "get_instrument_specs",
    "get_portfolio",
    "get_active_orders",
    "get_free_deposit",
    "get_operations",
)

#: Переменные, которые передаются процессу MCP-сервера.
FORWARDED_ENV = (
    "TINKOFF_TOKEN",
    "TINVEST_TOKEN",
    "TINKOFF_ACCOUNT_ID",
    "TINVEST_ACCOUNT_ID",
    "TINVEST_SANDBOX",
    "TINVEST_DROP_WEEKENDS",
    "TINVEST_STATE_DIR",
    # Без них Python-процесс на Windows/Linux может не запуститься.
    "SYSTEMROOT",
    "PATH",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
)


@dataclass(frozen=True)
class McpSettings:
    """Как запускать MCP-сервер T-Invest и как называются его инструменты."""

    command: str = "tinvest-mcp"
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    price_tool: str = "get_last_price"
    candles_tool: str = "get_candles"
    portfolio_tool: str = "get_portfolio"
    read_only_tools: tuple[str, ...] = DEFAULT_READ_ONLY_TOOLS


@dataclass(frozen=True)
class LlmSettings:
    """Подключение к LLM с OpenAI-совместимым API (OpenAI, DeepSeek, OpenRouter, Ollama, ...)."""

    base_url: str
    api_key: str
    model: str
    max_steps: int = 6


@dataclass(frozen=True)
class Settings:
    """Все настройки ассистента."""

    bot_token: str
    db_path: Path
    mcp: McpSettings
    llm: LlmSettings | None = None


def read_dotenv(path: Path) -> dict[str, str]:
    """Прочитать простой файл ``KEY=VALUE``; комментарии и пустые строки пропускаются."""
    if not path.is_file():
        return {}
    pairs = (_parse_line(line) for line in path.read_text(encoding="utf-8").splitlines())
    return dict(pair for pair in pairs if pair)


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if line.startswith("#") or "=" not in line:
        return None
    key, value = (part.strip() for part in line.split("=", 1))
    return key, _unquote(value)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_settings(environ: dict[str, str] | None = None, dotenv: Path | None = Path(".env")) -> Settings:
    """Собрать настройки. Переменные окружения важнее значений из ``.env``."""
    env = dict(read_dotenv(dotenv)) if dotenv else {}
    env.update(os.environ if environ is None else environ)

    bot_token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        raise ValueError("Не задан TELEGRAM_BOT_TOKEN")
    return Settings(
        bot_token=bot_token,
        db_path=Path(env.get("JOURNAL_DB", "journal.sqlite3")),
        mcp=mcp_settings(env),
        llm=llm_settings(env),
    )


def split_list(value: str) -> tuple[str, ...]:
    """``"a, b ,"`` → ``("a", "b")``."""
    return tuple(item.strip() for item in value.split(",") if item.strip())


def mcp_settings(env: dict[str, str]) -> McpSettings:
    server_env = {k: env[k] for k in FORWARDED_ENV if k in env}
    # Ассистент никогда не торгует: торговые инструменты сервера выключены.
    server_env["TINVEST_ALLOW_TRADING"] = "0"
    defaults = McpSettings()
    return McpSettings(
        command=env.get("TINVEST_MCP_COMMAND", defaults.command),
        args=tuple(shlex.split(env.get("TINVEST_MCP_ARGS", ""))),
        env=server_env,
        price_tool=env.get("TINVEST_MCP_PRICE_TOOL", defaults.price_tool),
        candles_tool=env.get("TINVEST_MCP_CANDLES_TOOL", defaults.candles_tool),
        portfolio_tool=env.get("TINVEST_MCP_PORTFOLIO_TOOL", defaults.portfolio_tool),
        read_only_tools=split_list(env.get("TINVEST_MCP_READ_TOOLS", "")) or defaults.read_only_tools,
    )


def llm_settings(env: dict[str, str]) -> LlmSettings | None:
    """Настройки LLM или ``None``, если свободный диалог не настроен."""
    if not (env.get("LLM_API_KEY") or env.get("LLM_BASE_URL")):
        return None
    return LlmSettings(
        base_url=env.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=env.get("LLM_API_KEY", ""),
        model=env.get("LLM_MODEL", "gpt-4o-mini"),
        max_steps=int(env.get("LLM_MAX_STEPS", "6")),
    )
