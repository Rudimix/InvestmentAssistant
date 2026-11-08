"""Загрузка настроек."""

import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
from investment_journal.config import (
    DEFAULT_READ_ONLY_TOOLS,
    OFFICIAL_MCP_URL,
    SANDBOX_MCP_URL,
    load_env,
    load_settings,
    read_dotenv,
)


class ConfigTests(unittest.TestCase):
    def test_bot_token_required(self):
        with self.assertRaises(ValueError):
            load_settings({}, dotenv=None)

    def test_defaults(self):
        s = load_settings({"TELEGRAM_BOT_TOKEN": "t", "TINKOFF_TOKEN": "x", "SECRET": "s"}, dotenv=None)
        self.assertEqual(s.mcp.read_only_tools, DEFAULT_READ_ONLY_TOOLS)
        self.assertEqual((s.mcp.ca_files, s.token_key), ((), ""))
        self.assertIsNone(s.llm)
        self.assertEqual(s.db_path, Path("journal.sqlite3"))

    def test_read_only_tools_exclude_trading(self):
        for name in ("invest_create_order", "invest_replace_order", "invest_cancel_order",
                     "invest_create_stoporder", "invest_cancel_stoporder", "invest_deposit_sandbox_account",
                     "invest_open_sandbox_account", "invest_close_sandbox_account",
                     "invest_edit_favorite_instruments", "invest_create_favorite_group",
                     "invest_delete_favorite_group"):
            self.assertNotIn(name, DEFAULT_READ_ONLY_TOOLS)
        self.assertEqual(len(DEFAULT_READ_ONLY_TOOLS), len(set(DEFAULT_READ_ONLY_TOOLS)))

    def test_official_server_by_default(self):
        s = load_settings({"TELEGRAM_BOT_TOKEN": "t", "TINKOFF_TOKEN": "x",
                           "TINVEST_CA_FILES": "certs/root.cer, certs/sub.cer"}, dotenv=None)
        self.assertEqual(s.mcp.url, OFFICIAL_MCP_URL)
        self.assertEqual(s.mcp.headers, {"Authorization": "Bearer x"})
        self.assertEqual(s.mcp.ca_files, ("certs/root.cer", "certs/sub.cer"))

    def test_sandbox_and_explicit_url(self):
        env = {"TELEGRAM_BOT_TOKEN": "t", "TINVEST_SANDBOX": "1", "TINVEST_TOKEN": "y"}
        s = load_settings(env, dotenv=None)
        self.assertEqual((s.mcp.url, s.mcp.headers["Authorization"]), (SANDBOX_MCP_URL, "Bearer y"))
        s = load_settings({**env, "TINVEST_MCP_URL": "https://example.test/mcp"}, dotenv=None)
        self.assertEqual(s.mcp.url, "https://example.test/mcp")

    def test_no_token_no_header(self):
        self.assertEqual(load_settings({"TELEGRAM_BOT_TOKEN": "t"}, dotenv=None).mcp.headers, {})

    def test_load_env_without_bot_token(self):
        self.assertEqual(load_env({"TINKOFF_TOKEN": "x"}, dotenv=None), {"TINKOFF_TOKEN": "x"})

    def test_custom_tools_key_and_llm(self):
        s = load_settings({
            "TELEGRAM_BOT_TOKEN": "t",
            "TOKEN_ENCRYPTION_KEY": "key",
            "TINVEST_MCP_READ_TOOLS": "invest_get_candles, invest_get_portfolio ,",
            "LLM_BASE_URL": "http://localhost:11434/v1",
            "LLM_MODEL": "qwen3",
        }, dotenv=None)
        self.assertEqual(s.token_key, "key")
        self.assertEqual(s.mcp.read_only_tools, ("invest_get_candles", "invest_get_portfolio"))
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
