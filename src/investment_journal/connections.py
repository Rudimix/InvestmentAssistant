"""Подключения пользователей к T-Invest MCP: у каждого пользователя бота свой токен.

Токены хранятся в базе зашифрованными (Fernet, ключ — ``TOKEN_ENCRYPTION_KEY``).
Сессия MCP открывается на время одного запроса с токеном того, кто спросил,
поэтому пользователи видят только свои счета и портфели.
"""

import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace

from cryptography.fernet import Fernet, InvalidToken

from .config import McpSettings
from .journal import JournalService
from .market import NO_ACCOUNTS, MarketDataService
from .mcp_client import McpToolClient, ToolClient, ToolError
from .models import Account, User
from .storage import JournalRepository

#: Токен T-Invest API: «t.» и длинная строка из букв, цифр, «-» и «_».
TOKEN_PATTERN = re.compile(r"t\.[A-Za-z0-9_-]{30,}")

#: Открыть сессию MCP по настройкам (в тестах подменяется заглушкой).
Opener = Callable[[McpSettings], AbstractAsyncContextManager[ToolClient]]


class NotConnected(Exception):
    """Пользователь ещё не подключил токен T-Invest API."""


def looks_like_token(text: str) -> bool:
    """В тексте есть что-то похожее на токен T-Invest API."""
    return bool(TOKEN_PATTERN.search(text or ""))


def generate_key() -> str:
    """Новый ключ для ``TOKEN_ENCRYPTION_KEY``."""
    return Fernet.generate_key().decode()


class TokenCipher:
    """Шифрование токенов перед записью в базу."""

    def __init__(self, key: str):
        if not key:
            raise ValueError("Не задан TOKEN_ENCRYPTION_KEY (создайте: python -m investment_journal --generate-key)")
        try:
            self._fernet = Fernet(key.encode())
        except ValueError as exc:
            raise ValueError("TOKEN_ENCRYPTION_KEY должен быть ключом Fernet: "
                             "python -m investment_journal --generate-key") from exc

    def encrypt(self, token: str) -> bytes:
        return self._fernet.encrypt(token.encode())

    def decrypt(self, data: bytes) -> str:
        """Расшифровать; при смене ключа старые токены становятся недействительными."""
        try:
            return self._fernet.decrypt(data).decode()
        except InvalidToken as exc:
            raise NotConnected("Сохранённый токен не расшифровывается — подключите его заново") from exc


class Connections:
    """Токены пользователей и сессии MCP с ними."""

    def __init__(self, repo: JournalRepository, cipher: TokenCipher, settings: McpSettings,
                 opener: Opener = McpToolClient):
        self.repo = repo
        self._cipher = cipher
        self._settings = settings
        self._opener = opener
        #: Тикер → instrumentId: справочник общий для всех, кэшируем его один раз.
        self._instrument_ids: dict[str, str] = {}

    def user(self, chat_id: int) -> User:
        return self.repo.get_or_create_user(chat_id)

    def is_connected(self, chat_id: int) -> bool:
        return self.repo.get_credentials(self.user(chat_id).id) is not None

    def _open(self, token: str) -> AbstractAsyncContextManager[ToolClient]:
        return self._opener(replace(self._settings, headers={"Authorization": f"Bearer {token}"}))

    async def connect(self, chat_id: int, token: str) -> list[Account]:
        """Проверить токен на сервере и сохранить его; выбранным становится первый счёт."""
        token = token.strip()
        if not looks_like_token(token) or " " in token:
            raise ToolError("это не похоже на токен T-Invest API (он начинается с «t.»)")
        async with self._open(token) as tools:
            accounts = await MarketDataService(tools).accounts()
        if not accounts:
            raise ToolError(NO_ACCOUNTS)
        self.repo.save_credentials(self.user(chat_id).id, self._cipher.encrypt(token), accounts[0].id)
        return accounts

    def disconnect(self, chat_id: int) -> bool:
        """Удалить токен пользователя; журнал гипотез остаётся."""
        return self.repo.delete_credentials(self.user(chat_id).id)

    def selected_account(self, chat_id: int) -> str:
        return self._credentials(chat_id)[1]

    def select_account(self, chat_id: int, account_id: str) -> None:
        self._credentials(chat_id)
        self.repo.set_account(self.user(chat_id).id, account_id)

    def _credentials(self, chat_id: int) -> tuple[bytes, str]:
        credentials = self.repo.get_credentials(self.user(chat_id).id)
        if credentials is None:
            raise NotConnected("Токен T-Invest API не подключён")
        return credentials

    @asynccontextmanager
    async def market(self, chat_id: int) -> AsyncIterator[MarketDataService]:
        """Рыночные данные с токеном пользователя на время ``async with``."""
        encrypted, account_id = self._credentials(chat_id)
        async with self._open(self._cipher.decrypt(encrypted)) as tools:
            yield MarketDataService(tools, account_id, self._instrument_ids)

    @asynccontextmanager
    async def journal(self, chat_id: int) -> AsyncIterator[JournalService]:
        """Журнал пользователя с доступом к его рыночным данным."""
        async with self.market(chat_id) as market:
            yield JournalService(self.repo, market)
