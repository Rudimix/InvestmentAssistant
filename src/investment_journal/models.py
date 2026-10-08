"""Модели инвестиционного журнала."""

from dataclasses import dataclass


@dataclass
class User:
    """Пользователь журнала.

    :param id: Идентификатор пользователя.
    :param chat_id: Идентификатор личного чата.
    """

    id: int
    chat_id: int


@dataclass
class DecisionDraft:
    """Черновик инвестиционной гипотезы.

    :param user_id: Идентификатор владельца.
    :param kind: Тип решения: buy, sell или hold.
    :param ticker: Тикер инструмента.
    :param hypothesis: Текст инвестиционной гипотезы.
    """

    user_id: int
    kind: str
    ticker: str
    hypothesis: str
