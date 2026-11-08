"""Сценарии журнала: зафиксировать гипотезу, оценить, закрыть."""

from .analytics import DecisionReview, review_decision
from .market import MarketDataService
from .models import DECISION_KINDS, Decision, DecisionDraft, User
from .storage import JournalRepository


class JournalError(ValueError):
    """Ошибка пользовательского ввода."""


class JournalService:
    """Журнал инвестиционных решений поверх рыночных данных из MCP."""

    def __init__(self, repo: JournalRepository, market: MarketDataService):
        self.repo = repo
        self.market = market

    def user(self, chat_id: int) -> User:
        return self.repo.get_or_create_user(chat_id)

    @staticmethod
    def validate(draft: DecisionDraft) -> None:
        if draft.kind not in DECISION_KINDS:
            raise JournalError(f"Тип решения должен быть одним из: {', '.join(DECISION_KINDS)}")
        if not draft.ticker.strip() or not draft.ticker.strip().isalnum():
            raise JournalError("Тикер должен состоять из букв и цифр")
        if len(draft.hypothesis.strip()) < 10:
            raise JournalError("Опишите гипотезу подробнее (не меньше 10 символов)")

    async def record(self, draft: DecisionDraft) -> Decision:
        """Проверить черновик, получить текущую цену через MCP и сохранить решение."""
        self.validate(draft)
        price = await self.market.last_price(draft.ticker)
        return self.repo.add_decision(draft, price)

    async def review(self, user: User, open_only: bool = True) -> list[DecisionReview]:
        """Оценить решения пользователя по текущим ценам (закрытые — по цене закрытия)."""
        reviews = []
        cache: dict[str, float] = {}
        for d in self.repo.list_decisions(user.id, open_only=open_only):
            if not d.is_open:
                reviews.append(review_decision(d, d.exit_price))
                continue
            if d.ticker not in cache:
                cache[d.ticker] = await self.market.last_price(d.ticker)
            reviews.append(review_decision(d, cache[d.ticker]))
        return reviews

    async def close(self, user: User, decision_id: int) -> DecisionReview:
        decision = self.repo.get_decision(user.id, decision_id)
        if decision is None:
            raise JournalError(f"Решение #{decision_id} не найдено")
        if not decision.is_open:
            raise JournalError(f"Решение #{decision_id} уже закрыто")
        price = await self.market.last_price(decision.ticker)
        self.repo.close_decision(decision, price)
        return review_decision(decision, price)
