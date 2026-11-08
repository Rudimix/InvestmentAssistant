"""Настройки приложения из переменных окружения и файла ``.env``."""

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Официальный MCP-сервер Т-Инвестиций (Streamable HTTP) и его песочница.
OFFICIAL_MCP_URL = "https://invest-public-api.tbank.ru/mcp"
SANDBOX_MCP_URL = "https://sandbox-invest-public-api.tbank.ru/mcp"

#: Инструменты официального сервера, которые разрешено вызывать LLM: только чтение.
#: Заявки, стоп-заявки, избранное и операции со счетами песочницы сюда не входят;
#: инструменты, появившиеся на сервере позже, тоже не попадут к LLM, пока их не добавить.
DEFAULT_READ_ONLY_TOOLS = (
    # инструменты и справочники
    "invest_find_instrument", "invest_get_share", "invest_get_bond", "invest_get_etf", "invest_get_future",
    "invest_get_option", "invest_get_currency", "invest_get_dfa", "invest_get_structured_note",
    "invest_get_asset", "invest_get_asset_fundamentals", "invest_get_brand", "invest_list_assets",
    "invest_list_brands", "invest_list_countries", "invest_list_shares", "invest_list_bonds",
    "invest_list_etfs", "invest_list_futures", "invest_list_options", "invest_list_currencies",
    "invest_list_dfas", "invest_list_indicatives", "invest_list_structured_notes",
    "invest_get_bond_accrued", "invest_get_bond_events", "invest_get_share_dividends",
    "invest_get_instrument_reports", "invest_get_insider_deals", "invest_get_risk_rates",
    "invest_get_trading_schedule", "invest_get_favorite_groups", "invest_get_favorite_instruments",
    # рыночные данные и аналитика
    "invest_get_market_values", "invest_get_candles", "invest_get_last_trades", "invest_get_orderbook",
    "invest_get_tech_analysis", "invest_get_trading_statuses", "invest_get_forecast",
    "invest_list_consensus_forecasts", "invest_list_signals", "invest_list_strategies", "invest_get_news",
    # счета, портфель, операции (без изменений)
    "invest_list_accounts", "invest_list_broker_accounts", "invest_get_user_info", "invest_get_portfolio",
    "invest_get_positions", "invest_get_operations", "invest_get_blocked_guarantee",
    "invest_get_broker_account_margin", "invest_get_orders", "invest_get_order_state", "invest_get_stoporders",
    "invest_get_max_lots", "invest_get_order_price",
)


@dataclass(frozen=True)
class McpSettings:
    """Подключение к MCP-серверу Т-Инвестиций по Streamable HTTP."""

    url: str = OFFICIAL_MCP_URL
    headers: dict[str, str] = field(default_factory=dict)
    ca_files: tuple[str, ...] = ()
    account_id: str = ""
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


def load_env(environ: dict[str, str] | None = None, dotenv: Path | None = Path(".env")) -> dict[str, str]:
    """Значения из ``.env``, поверх которых — переменные окружения."""
    env = dict(read_dotenv(dotenv)) if dotenv else {}
    env.update(os.environ if environ is None else environ)
    return env


def load_settings(environ: dict[str, str] | None = None, dotenv: Path | None = Path(".env")) -> Settings:
    """Собрать настройки. Переменные окружения важнее значений из ``.env``."""
    env = load_env(environ, dotenv)
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


def mcp_url(env: dict[str, str]) -> str:
    """Адрес MCP-сервера: явный ``TINVEST_MCP_URL``, иначе песочница или боевой сервер."""
    if env.get("TINVEST_MCP_URL"):
        return env["TINVEST_MCP_URL"]
    sandbox = env.get("TINVEST_SANDBOX", "0").strip().lower() in ("1", "true", "yes")
    return SANDBOX_MCP_URL if sandbox else OFFICIAL_MCP_URL


def auth_headers(env: dict[str, str]) -> dict[str, str]:
    token = env.get("TINKOFF_TOKEN") or env.get("TINVEST_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def mcp_settings(env: dict[str, str]) -> McpSettings:
    return McpSettings(
        url=mcp_url(env),
        headers=auth_headers(env),
        ca_files=split_list(env.get("TINVEST_CA_FILES", "")),
        account_id=env.get("TINKOFF_ACCOUNT_ID") or env.get("TINVEST_ACCOUNT_ID", ""),
        read_only_tools=split_list(env.get("TINVEST_MCP_READ_TOOLS", "")) or DEFAULT_READ_ONLY_TOOLS,
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
