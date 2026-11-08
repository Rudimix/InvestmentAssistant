"""MCP-клиент: разбор ответов и настоящая stdio-сессия с тестовым сервером."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import _support  # noqa: F401
from investment_journal.config import McpSettings
from investment_journal.mcp_client import McpToolClient, ToolError, parse_tool_result

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


class StdioSessionTests(unittest.IsolatedAsyncioTestCase):
    """Сессия открывается и закрывается в одной задаче — так требует anyio."""

    async def test_session_with_fake_server(self):
        settings = McpSettings(command=sys.executable, args=(str(SERVER),), env=_env())
        async with McpToolClient(settings) as client:
            names = {t["name"] for t in await client.list_tools()}
            self.assertEqual(names, {"get_last_price", "get_candles", "get_portfolio"})

            data = await client.call("get_last_price", {"ticker": "SiZ6"})
            self.assertEqual(data, {"ticker": "SiZ6", "price": 91234.5})

            candles = await client.call("get_candles", {"ticker": "SiZ6", "interval": "1d", "bars": 3})
            self.assertEqual([c["c"] for c in candles["candles"]], [1, 2, 3])

            with self.assertRaisesRegex(ToolError, "not found"):
                await client.call("get_last_price", {"ticker": "NOPE"})
            with self.assertRaisesRegex(ToolError, "TINKOFF_TOKEN"):
                await client.call("get_portfolio")


class SessionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_without_session_fails(self):
        with self.assertRaises(RuntimeError):
            await McpToolClient(McpSettings()).call("get_last_price", {"ticker": "SiZ6"})


def _env() -> dict[str, str]:
    import os

    keep = ("SYSTEMROOT", "PATH", "TEMP", "TMP", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA")
    return {k: os.environ[k] for k in keep if k in os.environ}


if __name__ == "__main__":
    unittest.main()
