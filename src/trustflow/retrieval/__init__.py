"""M2 retrieval: synthetic policy corpus, evidence spans, ledger snapshots."""

from trustflow.retrieval.corpus import CORPUS_VERSION, PolicyRetriever, default_corpus
from trustflow.retrieval.models import (
    EvidenceSpan,
    LedgerSnapshot,
    PolicyChunk,
    PolicyDocument,
    RetrievedChunk,
    SpanSourceType,
    build_ledger_snapshot,
    make_ledger_span,
    make_policy_span,
    sha256_text,
    span_in_text,
    validate_relevance,
    validate_span,
)

__all__ = [
    "CORPUS_VERSION",
    "EvidenceSpan",
    "LedgerSnapshot",
    "PolicyChunk",
    "PolicyDocument",
    "PolicyRetriever",
    "RetrievedChunk",
    "SpanSourceType",
    "build_ledger_snapshot",
    "default_corpus",
    "make_ledger_span",
    "make_policy_span",
    "sha256_text",
    "span_in_text",
    "validate_relevance",
    "validate_span",
]
