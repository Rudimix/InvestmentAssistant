"""Загрузка настроек."""

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
from investment_journal.config import DEFAULT_READ_ONLY_TOOLS, load_settings, read_dotenv


class ConfigTests(unittest.TestCase):
    def test_bot_token_required(self):
        with self.assertRaises(ValueError):
            load_settings({}, dotenv=None)

    def test_defaults(self):
        s = load_settings({"TELEGRAM_BOT_TOKEN": "t", "TINKOFF_TOKEN": "x", "SECRET": "s"}, dotenv=None)
        self.assertEqual(s.mcp.command, "tinvest-mcp")
        self.assertEqual(s.mcp.read_only_tools, DEFAULT_READ_ONLY_TOOLS)
        self.assertEqual(s.mcp.env, {"TINKOFF_TOKEN": "x", "TINVEST_ALLOW_TRADING": "0"})
        self.assertIsNone(s.llm)
        self.assertEqual(s.db_path, Path("journal.sqlite3"))

    def test_trading_is_always_disabled(self):
        s = load_settings({"TELEGRAM_BOT_TOKEN": "t", "TINVEST_ALLOW_TRADING": "1"}, dotenv=None)
        self.assertEqual(s.mcp.env["TINVEST_ALLOW_TRADING"], "0")

    def test_custom_server_and_llm(self):
        s = load_settings({
            "TELEGRAM_BOT_TOKEN": "t",
            "TINVEST_MCP_COMMAND": "docker",
            "TINVEST_MCP_ARGS": 'run -i --rm "my image"',
            "TINVEST_MCP_PRICE_TOOL": "price",
            "TINVEST_MCP_READ_TOOLS": "price, candles ,",
            "LLM_BASE_URL": "http://localhost:11434/v1",
            "LLM_MODEL": "qwen3",
        }, dotenv=None)
        self.assertEqual(s.mcp.args, ("run", "-i", "--rm", "my image"))
        self.assertEqual((s.mcp.price_tool, s.mcp.read_only_tools), ("price", ("price", "candles")))
        self.assertEqual((s.llm.base_url, s.llm.model, s.llm.api_key), ("http://localhost:11434/v1", "qwen3", ""))

    def test_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('# c\nTELEGRAM_BOT_TOKEN="from-file"\nJOURNAL_DB=a.db\nbroken\n\n', encoding="utf-8")
            self.assertEqual(read_dotenv(path), {"TELEGRAM_BOT_TOKEN": "from-file", "JOURNAL_DB": "a.db"})
            s = load_settings({"JOURNAL_DB": "env.db"}, dotenv=path)
        self.assertEqual((s.bot_token, s.db_path), ("from-file", Path("env.db")))

    def test_missing_dotenv(self):
        self.assertEqual(read_dotenv(Path("definitely-missing.env")), {})


if __name__ == "__main__":
    unittest.main()
