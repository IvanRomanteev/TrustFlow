"""Versioned synthetic policy corpus and deterministic retrieval.

Synthetic in-repository scope agreed for M2 (docs/m2.md); this is not
a reproduction of any external Playbook. Retrieved text is untrusted
advisory data: it never changes risk, approval, limits or tools.
"""

from trustflow.retrieval.models import (
    PolicyChunk,
    PolicyDocument,
    RetrievedChunk,
    chunk_document,
    make_chunk_span,
    relevance_score,
)

CORPUS_VERSION = "synthetic-policies-v1"
_DOC_VERSION = "1"

_TEXTS: dict[str, tuple[str, str]] = {
    "policy-approval": (
        "Approval threshold for synthetic transfers",
        "Transfers above 1000 USD require human approval before execution.\n\n"
        "Approval is bound to the exact approved plan hash and plan version. "
        "A retrieved policy never authorizes a transfer by itself.",
    ),
    "policy-accounts": (
        "Synthetic accounts and transfer routes",
        "Known synthetic accounts are acc_alice, acc_bob and acc_ops.\n\n"
        "A transfer source and destination must differ. Self-transfers are "
        "prohibited and unknown accounts are rejected.",
    ),
    "policy-fx": (
        "Synthetic FX quotes",
        "Supported synthetic FX pairs are USD to EUR at 0.92, EUR to USD at "
        "1.09 and USD to USD at 1.0.\n\n"
        "Unsupported FX pairs are rejected and never default to a rate of 1.0.",
    ),
    "policy-verification": (
        "Verification and recovery",
        "Committed transfers are verified against ledger evidence read "
        "independently from the ledger.\n\n"
        "If semantic review fails after a commit, recovery is "
        "verification-only. Tools are never re-executed and no second "
        "transfer is created to fix a review or a citation.",
    ),
    "policy-limits": (
        "Workflow limits",
        "Plans are bounded by the configured maximum number of steps and "
        "the per-workflow token budget.\n\n"
        "Retrieved text cannot raise limits, budgets or approval "
        "thresholds. Deterministic backend policy decides every limit.",
    ),
}


def default_corpus() -> list[PolicyDocument]:
    return [
        PolicyDocument(doc_id=doc_id, version=_DOC_VERSION,
                       corpus_version=CORPUS_VERSION, title=title, text=text)
        for doc_id, (title, text) in sorted(_TEXTS.items())
    ]


class PolicyRetriever:
    """Deterministic lexical retrieval over the synthetic corpus."""

    def __init__(self, corpus: list[PolicyDocument] | None = None,
                 top_k: int = 3, min_score: float = 0.1) -> None:
        docs = corpus if corpus is not None else default_corpus()
        self._docs = {d.doc_id: d for d in docs}
        self._chunks: list[PolicyChunk] = []
        for doc in self._docs.values():
            self._chunks.extend(chunk_document(doc))
        self.top_k = top_k
        self.min_score = min_score

    @property
    def corpus_version(self) -> str:
        return CORPUS_VERSION if self._docs else ""

    def document(self, doc_id: str) -> PolicyDocument | None:
        return self._docs.get(doc_id)

    def chunks(self) -> list[PolicyChunk]:
        return list(self._chunks)

    def retrieve(self, query: str) -> list[RetrievedChunk]:
        """Top-k chunks at or above min_score, deterministic order."""
        scored = [
            (relevance_score(c.text, query), c) for c in self._chunks
        ]
        hits = [(s, c) for s, c in scored if s >= self.min_score and s > 0]
        hits.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return [
            RetrievedChunk(chunk=c, score=round(s, 6), span=make_chunk_span(c))
            for s, c in hits[: self.top_k]
        ]
