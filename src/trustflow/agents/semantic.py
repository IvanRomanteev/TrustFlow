"""Independent semantic review. Advisory: never overrides deterministic FAIL."""

from typing import Annotated, Any

from pydantic import BaseModel, Field, ValidationError

from trustflow.core.errors import LLMResponseError
from trustflow.domain.models import Plan, SemanticReview, WorkflowGoal
from trustflow.llm.provider import LLMProvider
from trustflow.llm.usage import UsageTracker
from trustflow.retrieval.corpus import PolicyRetriever
from trustflow.retrieval.models import (
    EvidenceSpan,
    LedgerSnapshot,
    SpanSourceType,
    build_ledger_snapshot,
    canonical_snapshot_errors,
    canonical_transactions,
    make_ledger_span,
    span_in_text,
    validate_policy_span,
    validate_span,
)
from trustflow.tools.ledger import StrictJsonNumber, SyntheticLedger


class SemanticReviewInput(BaseModel):
    """Explicit validation contract for model review output.

    Mirrors REVIEW_SCHEMA exactly: every field is required (no defaults),
    so an incomplete provider response is malformed, never a valid PASS.
    """

    verdict: str = Field(pattern="^(PASS|FAIL)$")
    # Strict JSON number: bool/string coercion would accept output the
    # published REVIEW_SCHEMA (type: number) forbids.
    confidence: Annotated[StrictJsonNumber, Field(ge=0.0, le=1.0)]
    issues: list[str]
    summary: str


class SemanticReviewer:
    """Reviews goal vs approved plan vs independently obtained ledger evidence."""

    def __init__(self, llm: LLMProvider, ledger: SyntheticLedger,
                 max_output_tokens: int = 1200,
                 retriever: PolicyRetriever | None = None,
                 retrieval_min_score: float = 0.1) -> None:
        self._llm = llm
        self._ledger = ledger
        self._reserve_tokens = max_output_tokens
        self._retriever = retriever
        self._min_score = retrieval_min_score
        # Findings from the last advisory validation (never affect the
        # outcome in advisory mode; the engine audits them).
        self.last_advisory_findings: list[str] = []

    def _accounts(self, plan: Plan) -> set[str]:
        accounts: set[str] = set()
        for step in plan.steps:
            for key in ("account_id", "from_account", "to_account"):
                if step.args.get(key):
                    accounts.add(str(step.args[key]))
        return accounts

    def ledger_snapshot(self, plan: Plan) -> LedgerSnapshot:
        """Deterministic snapshot of the evidence for this plan."""
        return build_ledger_snapshot(self._ledger, sorted(self._accounts(plan)))

    def expected_transactions(self, plan: Plan) -> dict[str, Any]:
        """Backend-owned operation -> ledger transaction for this plan.

        Identity comes from the step's operation/intent id, never from
        ordering: the ledger is queried by that exact key (R3).
        """
        expected: dict[str, Any] = {}
        for step in plan.steps:
            if step.tool_name != "simulate_transfer":
                continue
            key = step.operation_id or step.intent_id
            if not key:
                continue
            tx = self._ledger.find_by_key(key)
            if tx is not None:
                expected[key] = tx
        return expected

    def ledger_spans(self, snapshot: LedgerSnapshot,
                     plan: Plan) -> list[EvidenceSpan]:
        """One span per operation of this plan, citing its own tx."""
        spans: list[EvidenceSpan] = []
        for tx in self.expected_transactions(plan).values():
            needle = f'"tx_id":"{tx.tx_id}"'
            if needle in snapshot.canonical_text:
                start, end = span_in_text(snapshot.canonical_text, needle)
                spans.append(make_ledger_span(snapshot, start, end))
        return spans

    def validate_evidence_spans(
        self, spans: list[EvidenceSpan], goal: WorkflowGoal, plan: Plan,
        snapshot: LedgerSnapshot | None = None,
    ) -> list[str]:
        """Integrity and relevance, checked separately (docs/m2.md).

        Integrity: bounds/exact text/hash/source/version against the
        cited source. Relevance: lexical overlap with the goal (policy
        spans) or the planned operation (ledger spans). Neither check
        grants authority; deterministic verification remains decisive.
        """
        errors: list[str] = []
        goal_query = f"{goal.title} {goal.description}"
        expected = self.expected_transactions(plan)
        for span in spans:
            if span.source_type == SpanSourceType.POLICY:
                doc = (
                    self._retriever.document(span.source_id)
                    if self._retriever is not None else None
                )
                if doc is None:
                    errors.append("span_source_mismatch")
                    continue
                chunk = self._retriever.chunk(span.chunk_id) \
                    if self._retriever is not None else None
                errors.extend(validate_policy_span(
                    span, doc, chunk, goal_query, self._min_score))
            else:
                if snapshot is None or span.snapshot_id != snapshot.snapshot_id:
                    errors.append("span_source_mismatch")
                    continue
                errors.extend(validate_span(
                    span, snapshot.canonical_text,
                    expected_source_id=snapshot.snapshot_id,
                    expected_snapshot_hash=snapshot.snapshot_hash,
                ))
                # The canonical text is the only authoritative
                # representation: its schema/numerics are validated
                # and the duplicate views must equal it exactly.
                # Malformed snapshot data is a typed span error here,
                # never an escaping numeric exception (N1/R3).
                canonical_errors = canonical_snapshot_errors(
                    snapshot.canonical_text, snapshot.balances,
                    snapshot.transactions)
                if canonical_errors:
                    errors.extend(canonical_errors)
                    continue
                # Provenance: the cited canonical transactions must
                # contain this workflow's operations, read from this
                # ledger by their backend-owned keys. A foreign
                # canonical ledger fails here even though its own
                # hash matches (R3).
                by_key = {t.get("idempotency_key"): t
                          for t in canonical_transactions(snapshot)}
                for key, tx in expected.items():
                    snap_tx = by_key.get(key)
                    if snap_tx is None or not (
                        snap_tx.get("tx_id") == tx.tx_id
                        and snap_tx.get("from_account") == tx.from_account
                        and snap_tx.get("to_account") == tx.to_account
                        and float(snap_tx.get("amount", -1)) == float(tx.amount)
                        and snap_tx.get("currency") == tx.currency
                    ):
                        errors.append("span_source_mismatch")
                        break
                # Relevance: the citation must cite one of this plan's
                # own transactions; an arbitrary intact slice of the
                # snapshot is not evidence for the operation (R3).
                if expected and not any(
                    f'"tx_id":"{tx.tx_id}"' in span.quoted_text
                    for tx in expected.values()
                ):
                    errors.append("span_irrelevant")
        # Coverage: every committed operation of the plan needs its
        # own ledger citation when a snapshot is supplied.
        if snapshot is not None and expected:
            cited = {
                tx.tx_id for tx in expected.values()
                if any(s.source_type == SpanSourceType.LEDGER
                       and f'"tx_id":"{tx.tx_id}"' in s.quoted_text
                       for s in spans)
            }
            if len(cited) < len(expected):
                errors.append("span_coverage_missing")
        return errors

    def evidence(self, plan: Plan) -> dict[str, Any]:
        """Evidence read directly from the ledger, not from executor output."""
        accounts = self._accounts(plan)
        balances = {}
        transactions: list[dict] = []
        for acct in sorted(accounts):
            try:
                balances[acct] = self._ledger.get_balance(acct).model_dump()
                transactions.extend(
                    t.model_dump() for t in self._ledger.list_transactions(acct, 50)
                )
            except KeyError:
                continue
        unique = {t["tx_id"]: t for t in transactions}
        return {"balances": balances, "transactions": list(unique.values())}

    def review(self, goal: WorkflowGoal, plan: Plan,
               tracker: UsageTracker | None = None,
               evidence_spans: list[EvidenceSpan] | None = None,
               mandatory_citations: bool = False,
               ledger_snapshot: LedgerSnapshot | None = None) -> SemanticReview:
        # Span validation runs before any model call. Mandatory-mode
        # failures raise LLMResponseError so the engine follows the
        # existing bounded verification-only recovery (no re-execution).
        # Advisory findings are only recorded for audit.
        self.last_advisory_findings = []
        if mandatory_citations and not evidence_spans:
            # Mandatory evidence can never be silently absent (R2).
            raise LLMResponseError(
                "Evidence span validation failed: retrieval_unavailable")
        if mandatory_citations and not any(
                s.source_type == SpanSourceType.POLICY
                for s in (evidence_spans or [])):
            # Ledger citations never satisfy the separate mandatory
            # policy-evidence obligation (F1/R2).
            raise LLMResponseError(
                "Evidence span validation failed: span_coverage_missing")
        if evidence_spans:
            findings = self.validate_evidence_spans(
                evidence_spans, goal, plan, snapshot=ledger_snapshot)
            if findings and mandatory_citations:
                raise LLMResponseError(
                    "Evidence span validation failed: "
                    + ",".join(sorted(set(findings)))
                )
            self.last_advisory_findings = findings
        payload = {
            "goal": {"title": goal.title, "description": goal.description},
            "plan_steps": [
                {"tool_name": s.tool_name, "args": s.args} for s in plan.steps
            ],
            "evidence": self.evidence(plan),
            "evidence_spans": [
                s.model_dump(mode="json") for s in (evidence_spans or [])
            ],
            "ledger_snapshot": (
                {"snapshot_id": ledger_snapshot.snapshot_id,
                 "snapshot_hash": ledger_snapshot.snapshot_hash}
                if ledger_snapshot is not None else None
            ),
        }
        estimated = 0
        if tracker is not None:
            import json as _json

            from trustflow.core.errors import TokenBudgetExceededError

            def _estimate(candidate: dict[str, Any]) -> int:
                return len(_json.dumps(candidate, default=str)) // 4 + 128 \
                    + self._reserve_tokens

            estimated = _estimate(payload)
            try:
                tracker.reserve(estimated)
            except TokenBudgetExceededError:
                if mandatory_citations:
                    # Mandatory evidence is never dropped for budget;
                    # the honest reservation failure propagates (R5).
                    raise
                # Advisory context is optional: drop it and reserve for
                # the actually transmitted smaller payload, so a
                # previously successful M1 outcome cannot change (R5).
                # Incurred usage accounting is untouched.
                payload = {**payload, "evidence_spans": [],
                           "ledger_snapshot": None}
                estimated = _estimate(payload)
                tracker.reserve(estimated)
        try:
            review_call = self._llm.semantic_review
            if tracker is not None and getattr(
                    self._llm, "supports_attempt_tracker", False):
                raw, record = review_call(  # type: ignore[call-arg]
                    payload, tracker=tracker, retry_reserve_tokens=estimated)
            else:
                raw, record = review_call(payload)
        except LLMResponseError as exc:
            if tracker is not None and exc.record is not None:
                tracker.record(exc.record)  # type: ignore[arg-type]
            raise
        if tracker is not None:
            tracker.record(record)
        try:
            parsed = SemanticReviewInput.model_validate(raw)
        except ValidationError as exc:
            raise LLMResponseError(
                f"Malformed semantic review output: {exc}", record=record
            ) from exc
        from trustflow.core.logging import redact

        return SemanticReview(
            verdict=parsed.verdict, confidence=parsed.confidence,
            issues=[redact(str(i)) for i in parsed.issues],
            summary=redact(parsed.summary), model=record.model,
        )
