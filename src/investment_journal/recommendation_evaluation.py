"""Вердикты людей, согласование разногласий и процент обоснованных предложений."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .models import AnalyticalRecord, timestamp


CRITERIA = {"goal", "horizon", "risk", "evidence", "limitations"}


@dataclass
class ExpertReview(AnalyticalRecord):
    run_id: str
    proposal_id: str
    reviewer: str
    revision: int
    reviewed_at: datetime
    criteria: dict[str, bool]
    reason: str
    review_kind: str = "individual"
    resolves: dict[str, int] = field(default_factory=dict)
    reviewer_type: str = "human"

    def __post_init__(self):
        super().__post_init__()
        self.reviewed_at = timestamp(self.reviewed_at)
        if (not self.run_id or not self.proposal_id or not self.reviewer.strip() or not self.reason.strip()
                or type(self.revision) is not int or self.revision <= 0 or self.reviewer_type != "human"
                or self.review_kind not in {"individual", "consensus"} or set(self.criteria) != CRITERIA
                or any(type(value) is not bool for value in self.criteria.values())):
            raise ValueError("Нужна полная человеческая оценка по обязательной рубрике")
        if self.review_kind == "individual" and self.resolves:
            raise ValueError("Индивидуальная оценка не разрешает разногласия")
        if self.review_kind == "consensus" and (len(self.resolves) < 2 or any(not key.strip() or type(value) is not int or value <= 0 for key, value in self.resolves.items())):
            raise ValueError("Согласование должно ссылаться на ревизии исходных оценок")

    @property
    def justified(self):
        return all(self.criteria.values())


def evaluation_metric(proposal_ids, reviews, scenario_count=0, refusals=None):
    """Все критерии обязательны, незавершённые оценки сохраняют промежуточный статус."""
    proposal_ids = list(proposal_ids)
    if len(set(proposal_ids)) != len(proposal_ids):
        raise ValueError("Идентификаторы предложений повторяются")
    identities = {(review.proposal_id, review.reviewer, review.revision) for review in reviews}
    if (len(identities) != len(reviews) or len({review.run_id for review in reviews}) > 1
            or any(review.proposal_id not in proposal_ids for review in reviews)):
        raise ValueError("Повтор или неизвестное предложение в оценках")
    checked, justified, pending, disagreements = 0, 0, [], []
    for proposal_id in proposal_ids:
        selected = [review for review in reviews if review.proposal_id == proposal_id]
        latest = {}
        for review in selected:
            key = (review.reviewer, review.review_kind)
            if key not in latest or latest[key].revision < review.revision:
                latest[key] = review
        individual = [review for review in latest.values() if review.review_kind == "individual"]
        outcomes = {review.justified for review in individual}
        verdict = None
        if len(outcomes) == 1:
            verdict = next(iter(outcomes))
        elif len(outcomes) > 1:
            versions = {review.reviewer: review.revision for review in individual}
            consensuses = [review for review in latest.values() if review.review_kind == "consensus"
                           and review.resolves == versions and all(review.reviewed_at >= item.reviewed_at for item in individual)]
            if consensuses:
                consensus = max(consensuses, key=lambda review: (review.reviewed_at, review.revision, review.reviewer))
                verdict = consensus.justified
            else:
                disagreements.append(proposal_id)
        if verdict is None:
            pending.append(proposal_id)
        else:
            checked += 1
            justified += int(verdict)
    value = Decimal(justified)/checked*100 if checked else None
    complete = bool(proposal_ids) and not pending
    return {"scenario_count": scenario_count, "proposal_count": len(proposal_ids), "checked": checked,
            "justified": justified, "pending": len(pending), "pending_ids": pending, "disagreements": disagreements,
            "percent": value, "threshold": Decimal(85), "success": value >= 85 if complete else None,
            "status": "complete" if complete else "intermediate" if proposal_ids else "no_proposals",
            "refusals": list(refusals or [])}
