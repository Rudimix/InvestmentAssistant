"""Клиент MCP-сервера T-Invest (транспорт stdio)."""

import json
from contextlib import AsyncExitStack
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from .config import McpSettings


class ToolError(RuntimeError):
    """Инструмент MCP вернул ошибку."""


class ToolClient(Protocol):
    """То, что нужно сервисам от MCP: вызвать инструмент и узнать список инструментов."""

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any: ...

    async def list_tools(self) -> list[dict[str, Any]]: ...


def parse_tool_result(result: Any) -> Any:
    """Достать данные из ``CallToolResult``: структурированный ответ или JSON из текста."""
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    if result.isError:
        raise ToolError("; ".join(texts) or "MCP tool error")
    data = result.structuredContent
    if data is None:
        joined = "\n".join(texts)
        try:
            data = json.loads(joined)
        except ValueError:
            return joined
    # FastMCP оборачивает не-словари в {"result": ...}
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if isinstance(data, dict) and "error" in data and len(data) == 1:
        raise ToolError(str(data["error"]))
    return data


class McpToolClient:
    """Долгоживущая сессия с MCP-сервером, запущенным как дочерний процесс."""

    def __init__(self, settings: McpSettings):
        self._settings = settings
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "McpToolClient":
        params = StdioServerParameters(
            command=self._settings.command,
            args=list(self._settings.args),
            env=dict(self._settings.env),
        )
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCP-сессия не открыта: используйте 'async with McpToolClient(...)'")
        return self._session

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        try:
            result = await self._require_session().call_tool(name, arguments or {})
        except McpError as exc:
            raise ToolError(f"MCP-сервер недоступен: {exc}") from exc
        return parse_tool_result(result)

    async def list_tools(self) -> list[dict[str, Any]]:
        listed = await self._require_session().list_tools()
        return [
            {"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
            for t in listed.tools
        ]
