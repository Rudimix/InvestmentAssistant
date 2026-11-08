"""Свободный диалог: LLM с OpenAI-совместимым API вызывает инструменты T-Invest MCP и журнала."""

import json
from typing import Any

import httpx

from .config import LlmSettings
from .formatting import format_journal, format_reviews
from .journal import JournalService
from .mcp_client import ToolError

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
    """Цикл «LLM → вызов инструментов → LLM», история диалога по каждому чату."""

    def __init__(self, llm: ChatCompletionsClient, journal: JournalService,
                 read_only_tools: tuple[str, ...], max_steps: int = 6):
        self._llm = llm
        self._journal = journal
        self._allowed = set(read_only_tools)
        self._max_steps = max_steps
        self._tools: list[dict[str, Any]] | None = None
        self._history: dict[int, list[dict[str, Any]]] = {}

    async def tools(self) -> list[dict[str, Any]]:
        """Схемы инструментов для LLM: разрешённые инструменты MCP + журнал."""
        if self._tools is None:
            mcp_tools = await self._journal.market._tools.list_tools()
            self._tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"][:1000],
                        "parameters": t["input_schema"] or {"type": "object", "properties": {}},
                    },
                }
                for t in mcp_tools
                if t["name"] in self._allowed
            ] + JOURNAL_TOOLS
        return self._tools

    def reset(self, chat_id: int) -> None:
        self._history.pop(chat_id, None)

    async def ask(self, chat_id: int, text: str) -> str:
        user = self._journal.user(chat_id)
        history = self._history.setdefault(chat_id, [])
        history.append({"role": "user", "content": text})
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
        tools = await self.tools()
        for _ in range(self._max_steps):
            reply = await self._llm.complete(messages, tools)
            calls = reply.get("tool_calls") or []
            if not calls:
                answer = reply.get("content") or "Не удалось получить ответ."
                history.append({"role": "assistant", "content": answer})
                del history[:-HISTORY_LIMIT]
                return answer
            messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                try:
                    if name == "journal_list":
                        output = format_journal(self._journal.repo.list_decisions(user.id))
                    elif name == "journal_review":
                        output = format_reviews(await self._journal.review(user))
                    elif name in self._allowed:
                        output = json.dumps(await self._journal.market.call_raw(name, args),
                                            ensure_ascii=False, default=str)[:8000]
                    else:
                        output = f"Инструмент {name} недоступен"
                except ToolError as exc:
                    output = f"Ошибка: {exc}"
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": output})
        history.append({"role": "assistant", "content": "Слишком много шагов, уточните вопрос."})
        return "Слишком много шагов, уточните вопрос."
