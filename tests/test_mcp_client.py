"""MCP-клиент: разбор ответов и настоящая HTTP-сессия с тестовым сервером."""

import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import certifi
import httpx

import _support  # noqa: F401
from investment_journal.config import McpSettings
from investment_journal.mcp_client import (McpToolClient, ToolError, connection_error, parse_tool_result,
                                           ssl_context)

SERVER = Path(__file__).resolve().parent / "fake_tinvest_server.py"


def text(value: str):
    return SimpleNamespace(type="text", text=value)


class ParseToolResultTests(unittest.TestCase):
    def test_structured_content_is_preferred(self):
        result = SimpleNamespace(isError=False, structuredContent={"price": 1.5}, content=[text("{}")])
        self.assertEqual(parse_tool_result(result), {"price": 1.5})

    def test_json_text_fallback(self):
        result = SimpleNamespace(isError=False, structuredContent=None, content=[text('{"price": 2}')])
        self.assertEqual(parse_tool_result(result), {"price": 2})

    def test_plain_text_returned_as_is(self):
        result = SimpleNamespace(isError=False, structuredContent=None, content=[text("hello")])
        self.assertEqual(parse_tool_result(result), "hello")

    def test_result_wrapper_is_unwrapped(self):
        result = SimpleNamespace(isError=False, structuredContent={"result": [1, 2]}, content=[])
        self.assertEqual(parse_tool_result(result), [1, 2])

    def test_is_error_raises(self):
        result = SimpleNamespace(isError=True, structuredContent=None, content=[text("TINKOFF_TOKEN not set")])
        with self.assertRaisesRegex(ToolError, "TINKOFF_TOKEN"):
            parse_tool_result(result)

    def test_error_payload_raises(self):
        result = SimpleNamespace(isError=False, structuredContent={"error": "XYZ not found"}, content=[])
        with self.assertRaisesRegex(ToolError, "XYZ not found"):
            parse_tool_result(result)


class HttpSessionTests(unittest.IsolatedAsyncioTestCase):
    """Streamable HTTP, как у официального сервера: тестовый сервер на локальном порту.

    Сессия открывается и закрывается в одной задаче — так требует anyio.
    """

    def setUp(self):
        self.port = _free_port()
        self.server = subprocess.Popen([sys.executable, str(SERVER), "--http", str(self.port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _wait_for_port(self.port)

    def tearDown(self):
        self.server.terminate()
        self.server.wait(timeout=10)

    async def test_http_session_sends_token(self):
        settings = McpSettings(url=f"http://127.0.0.1:{self.port}/mcp",
                               headers={"Authorization": "Bearer test-token"})
        async with McpToolClient(settings) as client:
            names = {t["name"] for t in await client.list_tools()}
            self.assertEqual(names, {"get_last_price", "get_candles", "get_portfolio", "whoami"})
            self.assertEqual(await client.call("whoami"), {"authorization": "Bearer test-token"})

            data = await client.call("get_last_price", {"ticker": "SiZ6"})
            self.assertEqual(data, {"ticker": "SiZ6", "price": 91234.5})
            candles = await client.call("get_candles", {"ticker": "SiZ6", "interval": "1d", "bars": 3})
            self.assertEqual([c["c"] for c in candles["candles"]], [1, 2, 3])

            with self.assertRaisesRegex(ToolError, "not found"):
                await client.call("get_last_price", {"ticker": "NOPE"})
            with self.assertRaisesRegex(ToolError, "TINKOFF_TOKEN"):
                await client.call("get_portfolio")

    async def test_unreachable_server_fails_on_enter(self):
        settings = McpSettings(url=f"http://127.0.0.1:{_free_port()}/mcp")
        with self.assertRaisesRegex(ToolError, "недоступен"):
            async with McpToolClient(settings):
                pass


class ConnectionErrorTests(unittest.TestCase):
    def test_rejected_token(self):
        request = httpx.Request("POST", "https://invest-public-api.tbank.ru/mcp")
        status = httpx.HTTPStatusError("401", request=request, response=httpx.Response(401, request=request))
        error = connection_error(ExceptionGroup("task group", [ExceptionGroup("inner", [status])]))
        self.assertIn("не принял токен", str(error))

    def test_other_error(self):
        error = connection_error(ExceptionGroup("task group", [OSError("connection refused")]))
        self.assertEqual(str(error), "MCP-сервер недоступен: connection refused")


class SslContextTests(unittest.TestCase):
    def test_extra_ca_file_is_loaded(self):
        base = ssl_context()
        with tempfile.TemporaryDirectory() as tmp:
            pem = Path(tmp) / "ca.pem"
            # любой корректный сертификат из certifi подходит для проверки загрузки
            pem.write_text(Path(certifi.where()).read_text(encoding="utf-8").split("\n\n")[0] + "\n",
                           encoding="utf-8")
            extended = ssl_context((str(pem),))
        self.assertGreaterEqual(len(extended.get_ca_certs()), len(base.get_ca_certs()))

    def test_missing_ca_file_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            ssl_context(("definitely-missing.cer",))


class SessionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_without_session_fails(self):
        with self.assertRaises(RuntimeError):
            await McpToolClient(McpSettings()).call("get_last_price", {"ticker": "SiZ6"})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"Тестовый MCP-сервер не запустился на порту {port}")


if __name__ == "__main__":
    unittest.main()
