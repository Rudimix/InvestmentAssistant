"""Подготовка общего контекста профиля, сценариев и доказательств для отчёта."""

from hashlib import sha256

from .advice_llm import build_advice_context, generate_advice
from .evidence_search import search_evidence
from .market_evidence import position_identifiers
from .models import AdviceProposal, HistoricalAnalysis
from .portfolio_advice import improvement_scenarios


class AdviceService:
    def __init__(self, repo, client, clock):
        self.repo, self.client, self.clock = repo, client, clock

    async def prepare(self, analysis, profile, assessment):
        if profile is None:
            return {"scenarios": [], "context": {}, "result": {"available": False, "proposals": [],
                                                                  "reason": "Для персональных предложений заполните /profile"}}
        if isinstance(analysis, HistoricalAnalysis):
            positions = analysis.start_state.positions+analysis.end_state.positions
            state = analysis.end_state
            account_id, source, as_of = state.account_id, state.id, state.effective_at
        else:
            positions = (analysis.baseline.positions if analysis.baseline else [])+analysis.snapshot.positions
            state = analysis.snapshot
            account_id, source, as_of = state.account_id, state.id, self.clock()
        if state.user_id != profile.user_id:
            raise ValueError("Профиль и анализ относятся к разным владельцам")
        scenarios = improvement_scenarios(profile, assessment, account_id, as_of, source)
        instruments = position_identifiers(positions)
        evidence, used, per_document = [], set(), {}
        for topic in ("прибыль", "дивиденды", "долг", "риски"):
            for row in search_evidence(self.repo, topic, instruments, as_of):
                chunk, document = row["chunk"], row["document"]
                if chunk.id in used or per_document.get(document.id, 0) >= 2:
                    continue
                evidence.append(row)
                used.add(chunk.id)
                per_document[document.id] = per_document.get(document.id, 0)+1
        try:
            context = build_advice_context(profile, assessment, scenarios, evidence, as_of, instruments)
            result = await generate_advice(self.client, context)
        except ValueError:
            context, result = {}, {"available": False, "proposals": [], "reason": "Контекст предложений не прошёл проверку данных"}
        return {"scenarios": scenarios, "context": context, "result": result}


def proposal_records(bundle: dict, run: dict, created_at) -> list[AdviceProposal]:
    records = []
    for index, item in enumerate(bundle.get("result", {}).get("proposals", [])):
        identity = sha256(f"{run['id']}:{index}".encode()).hexdigest()[:24]
        item["id"] = identity
        records.append(AdviceProposal(identity, run["user_id"], run["account_id"], run["profile_revision"], created_at,
                                       item["text"], item["metric_ids"], item["evidence_ids"], item["scenario_ids"],
                                       item["profile_refs"], item["risks"], item["reconsider_when"], report_id=run["id"]))
    return records
