"""Клиент официального MCP-сервера Т-Инвестиций (Streamable HTTP)."""

import json
import ssl
from contextlib import AsyncExitStack
from typing import Any, Protocol

import certifi
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
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
    data = result.structuredContent if result.structuredContent is not None else _from_text(texts)
    return _unwrap(data)


def _from_text(texts: list[str]) -> Any:
    """Текстовый ответ: JSON, если получается разобрать, иначе строка."""
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except ValueError:
        return joined


def _unwrap(data: Any) -> Any:
    """Снять обёртку FastMCP ``{"result": ...}`` и превратить ``{"error": ...}`` в исключение."""
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    if isinstance(data, dict) and set(data) == {"error"}:
        raise ToolError(str(data["error"]))
    return data


#: Как у клиента mcp по умолчанию: 30 с на соединение, 300 с на чтение потока ответа.
HTTP_TIMEOUT = httpx.Timeout(30, read=300)


def ssl_context(ca_files: tuple[str, ...] = ()) -> ssl.SSLContext:
    """Сертификаты certifi плюс дополнительные (для T-Bank — корневой и промежуточный сертификаты Минцифры)."""
    context = ssl.create_default_context(cafile=certifi.where())
    for path in ca_files:
        context.load_verify_locations(cafile=path)
    return context


def _leaf_errors(exc: BaseException) -> list[BaseException]:
    """Исключения внутри (возможно вложенных) ``ExceptionGroup`` из anyio."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaf_errors(sub)]
    return [exc]


def connection_error(exc: Exception) -> ToolError:
    """Понятная ошибка вместо ``ExceptionGroup`` при открытии сессии."""
    leaves = _leaf_errors(exc)
    for leaf in leaves:
        if isinstance(leaf, httpx.HTTPStatusError) and leaf.response.status_code in (401, 403):
            return ToolError("сервер Т-Инвестиций не принял токен — проверьте его или выпустите новый")
    cause = next((leaf for leaf in leaves if isinstance(leaf, (httpx.HTTPError, OSError))), leaves[-1])
    return ToolError(f"MCP-сервер недоступен: {cause}")


class McpToolClient:
    """Долгоживущая сессия с MCP-сервером по Streamable HTTP."""

    def __init__(self, settings: McpSettings):
        self._settings = settings
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "McpToolClient":
        self._stack = AsyncExitStack()
        try:
            read, write = await self._open_transport(self._stack)
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
        except BaseException as exc:
            # при сбое транспорта anyio отменяет initialize(), а настоящую причину
            # (401, нет соединения) отдаёт только при закрытии стека
            try:
                await self.__aexit__(None, None, None)
            except Exception as cleanup:  # pylint: disable=broad-exception-caught
                raise connection_error(cleanup) from cleanup
            if isinstance(exc, Exception):
                raise connection_error(exc) from exc
            raise
        return self

    async def _open_transport(self, stack: AsyncExitStack):
        http = await stack.enter_async_context(httpx.AsyncClient(
            headers=dict(self._settings.headers),
            timeout=HTTP_TIMEOUT,
            verify=ssl_context(self._settings.ca_files),
        ))
        read, write, _ = await stack.enter_async_context(streamable_http_client(self._settings.url, http_client=http))
        return read, write

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
