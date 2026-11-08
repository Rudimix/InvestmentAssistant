"""Свободный диалог: LLM с OpenAI-совместимым API вызывает инструменты T-Invest MCP и журнала."""

import json
from typing import Any, NamedTuple

import httpx

from .config import LlmSettings
from .formatting import format_journal, format_reviews
from .journal import JournalService
from .market import MarketDataService
from .mcp_client import ToolError
from .models import User

SYSTEM_PROMPT = (
    "Ты — инвестиционный ассистент пользователя брокера Т-Инвестиции. "
    "Отвечай по-русски, кратко и по делу. Для любых цен, свечей и данных портфеля "
    "вызывай инструменты — не придумывай числа. Ты не можешь совершать сделки. "
    "Не давай персональных инвестиционных рекомендаций: помогай анализировать данные "
    "и проверять гипотезы пользователя из его журнала."
)

JOURNAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "journal_list",
            "description": "Список решений (гипотез) пользователя из журнала.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "journal_review",
            "description": "Оценка открытых гипотез пользователя по текущим ценам.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

HISTORY_LIMIT = 20
TOOL_OUTPUT_LIMIT = 8000
TOO_MANY_STEPS = "Слишком много шагов, уточните вопрос."


class ToolContext(NamedTuple):
    """Чей журнал и чьи данные видит LLM при вызове инструментов."""

    journal: JournalService
    user: User


class LlmError(RuntimeError):
    """Ошибка обращения к LLM."""


class ChatCompletionsClient:
    """Минимальный клиент ``POST {base_url}/chat/completions``."""

    def __init__(self, settings: LlmSettings, http: httpx.AsyncClient | None = None):
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=60)

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._settings.api_key}"} if self._settings.api_key else {}
        payload: dict[str, Any] = {"model": self._settings.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        try:
            response = await self._http.post(
                self._settings.base_url.rstrip("/") + "/chat/completions", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise LlmError(f"LLM недоступна: {exc}") from exc
        if response.status_code >= 400:
            raise LlmError(f"LLM ответила {response.status_code}: {response.text[:300]}")
        try:
            return response.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LlmError("Неожиданный ответ LLM") from exc

    async def aclose(self) -> None:
        await self._http.aclose()


class InvestmentAgent:
    """Цикл «LLM → вызов инструментов → LLM», история диалога по каждому чату.

    Журнал и рыночные данные передаются в :meth:`ask` для каждого вопроса —
    с токеном того пользователя, который спрашивает.
    """

    def __init__(self, llm: ChatCompletionsClient, read_only_tools: tuple[str, ...], max_steps: int = 6):
        self._llm = llm
        self._allowed = set(read_only_tools)
        self._max_steps = max_steps
        self._tools: list[dict[str, Any]] | None = None
        self._history: dict[int, list[dict[str, Any]]] = {}

    async def tools(self, market: MarketDataService) -> list[dict[str, Any]]:
        """Схемы инструментов для LLM: разрешённые инструменты MCP + журнал (одни для всех)."""
        if self._tools is None:
            mcp_tools = await market.list_tools()
            self._tools = [as_function(t) for t in mcp_tools if t["name"] in self._allowed] + JOURNAL_TOOLS
        return self._tools

    def reset(self, chat_id: int) -> None:
        self._history.pop(chat_id, None)

    async def ask(self, chat_id: int, text: str, journal: JournalService) -> str:
        """Ответить на вопрос, давая LLM вызывать инструменты не больше ``max_steps`` раз."""
        history = self._history.setdefault(chat_id, [])
        history.append({"role": "user", "content": text})
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
        tools = await self.tools(journal.market)
        context = ToolContext(journal, journal.user(chat_id))
        for _ in range(self._max_steps):
            reply = await self._llm.complete(messages, tools)
            calls = reply.get("tool_calls") or []
            if not calls:
                return self._remember(history, reply.get("content") or "Не удалось получить ответ.")
            messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": calls})
            for call in calls:
                messages.append(await self._run_tool(call, context))
        return self._remember(history, TOO_MANY_STEPS)

    @staticmethod
    def _remember(history: list[dict[str, Any]], answer: str) -> str:
        history.append({"role": "assistant", "content": answer})
        del history[:-HISTORY_LIMIT]
        return answer

    async def _run_tool(self, call: dict[str, Any], context: "ToolContext") -> dict[str, Any]:
        """Выполнить один вызов инструмента и вернуть сообщение ``role=tool``."""
        name = call["function"]["name"]
        try:
            output = await self._dispatch(name, parse_arguments(call), context)
        except ToolError as exc:
            output = f"Ошибка: {exc}"
        return {"role": "tool", "tool_call_id": call.get("id", name), "content": output}

    async def _dispatch(self, name: str, args: dict[str, Any], context: "ToolContext") -> str:
        journal, user = context
        if name == "journal_list":
            return format_journal(journal.repo.list_decisions(user.id))
        if name == "journal_review":
            return format_reviews(await journal.review(user))
        if name not in self._allowed:
            return f"Инструмент {name} недоступен"
        data = await journal.market.call_raw(name, args)
        return json.dumps(data, ensure_ascii=False, default=str)[:TOOL_OUTPUT_LIMIT]


def as_function(tool: dict[str, Any]) -> dict[str, Any]:
    """Описание инструмента MCP → схема function calling OpenAI."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"][:1000],
            "parameters": tool["input_schema"] or {"type": "object", "properties": {}},
        },
    }


def parse_arguments(call: dict[str, Any]) -> dict[str, Any]:
    """Аргументы вызова; некорректный JSON от модели превращается в пустой словарь."""
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}
