"""Подключения пользователей: шифрование токенов и сессии MCP с токеном того, кто спросил."""

import unittest

from _support import TEST_KEY, TEST_TOKEN, FakeOpener, FakeToolClient
from investment_journal.config import McpSettings
from investment_journal.connections import Connections, NotConnected, TokenCipher, generate_key, looks_like_token
from investment_journal.market import parse_accounts
from investment_journal.mcp_client import ToolError
from investment_journal.storage import JournalRepository

OTHER_TOKEN = "t." + "OtherToken" * 6


class TokenCipherTests(unittest.TestCase):
    def test_roundtrip_and_wrong_key(self):
        encrypted = TokenCipher(TEST_KEY).encrypt(TEST_TOKEN)
        self.assertNotIn(TEST_TOKEN.encode(), encrypted)
        self.assertEqual(TokenCipher(TEST_KEY).decrypt(encrypted), TEST_TOKEN)
        with self.assertRaises(NotConnected):
            TokenCipher(generate_key()).decrypt(encrypted)

    def test_bad_key(self):
        for key in ("", "not-a-key"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "TOKEN_ENCRYPTION_KEY"):
                TokenCipher(key)


class LooksLikeTokenTests(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(looks_like_token(TEST_TOKEN))
        self.assertTrue(looks_like_token(f"вот: {TEST_TOKEN}"))
        for text in ("", "t.short", "SBER растёт", None):
            with self.subTest(text=text):
                self.assertFalse(looks_like_token(text))


class ConnectionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tools = FakeToolClient()
        self.opener = FakeOpener(self.tools, valid_tokens=(TEST_TOKEN, OTHER_TOKEN))
        self.repo = JournalRepository()
        self.connections = Connections(self.repo, TokenCipher(TEST_KEY), McpSettings(), self.opener)

    async def test_connect_stores_encrypted_token_and_first_account(self):
        accounts = await self.connections.connect(1, f"  {TEST_TOKEN}\n")
        self.assertEqual([a.id for a in accounts], ["2000000001"])
        encrypted, account_id = self.repo.get_credentials(self.connections.user(1).id)
        self.assertNotIn(TEST_TOKEN.encode(), encrypted)
        self.assertEqual(account_id, "2000000001")

    async def test_each_user_gets_own_token(self):
        await self.connections.connect(1, TEST_TOKEN)
        await self.connections.connect(2, OTHER_TOKEN)
        self.opener.tokens.clear()
        async with self.connections.market(2) as market:
            await market.last_price("SiZ6")
        async with self.connections.market(1) as market:
            await market.last_price("SiZ6")
        self.assertEqual(self.opener.tokens, [OTHER_TOKEN, TEST_TOKEN])
        # справочник инструментов общий: второй запрос не ищет тикер заново
        self.assertEqual([c[0] for c in self.tools.calls].count("invest_find_instrument"), 1)

    async def test_selected_account_is_used_for_portfolio(self):
        self.tools.accounts.append({"id": "2000000002"})
        await self.connections.connect(1, TEST_TOKEN)
        self.connections.select_account(1, "2000000002")
        async with self.connections.market(1) as market:
            await market.portfolio()
        self.assertEqual(self.tools.calls[-1][1]["accountId"], "2000000002")

    async def test_not_connected(self):
        with self.assertRaises(NotConnected):
            async with self.connections.market(1):
                pass
        with self.assertRaises(NotConnected):
            self.connections.select_account(1, "x")
        self.assertFalse(self.connections.disconnect(1))

    async def test_rejected_and_malformed_tokens_are_not_saved(self):
        for token in ("t." + "x" * 40, "hello", f"{TEST_TOKEN} extra"):
            with self.subTest(token=token), self.assertRaises(ToolError):
                await self.connections.connect(1, token)
        self.assertFalse(self.connections.is_connected(1))

    async def test_no_accounts(self):
        self.tools.accounts.clear()
        with self.assertRaisesRegex(ToolError, "Нет открытых"):
            await self.connections.connect(1, TEST_TOKEN)
        self.assertFalse(self.connections.is_connected(1))

    async def test_disconnect_keeps_journal_user(self):
        await self.connections.connect(1, TEST_TOKEN)
        user = self.connections.user(1)
        self.assertTrue(self.connections.disconnect(1))
        self.assertFalse(self.connections.is_connected(1))
        self.assertEqual(self.connections.user(1), user)


class ParseAccountsTests(unittest.TestCase):
    def test_bank_accounts_skipped_and_access_level(self):
        data = {"accounts": [
            {"id": "b", "type": "ACCOUNT_TYPE_BANK"},
            {"id": "1", "type": "ACCOUNT_TYPE_TINKOFF_IIS", "name": "ИИС",
             "accessLevel": "ACCOUNT_ACCESS_LEVEL_READ_ONLY"},
            {"id": "2", "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"},
            {"name": "без id"},
        ]}
        accounts = parse_accounts(data)
        self.assertEqual([(a.id, a.name, a.read_only) for a in accounts], [("1", "ИИС", True), ("2", "", False)])
        self.assertEqual(parse_accounts({"accounts": None}), [])


if __name__ == "__main__":
    unittest.main()
