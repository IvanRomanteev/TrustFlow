"""Evidence-span and ledger-snapshot contracts (see docs/m2.md).

Offsets are Unicode code points (Python ``str`` indices), ``start``
inclusive / ``end`` exclusive. Hashes are lowercase hex SHA-256 of the
UTF-8 encoding. Integrity, relevance and authority are separate
checks: a valid span proves only that an exact fragment was cited.
"""

import hashlib
import json
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trustflow.tools.ledger import (
    StrictJsonInt,
    SyntheticLedger,
    strict_json_number,
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SpanSourceType(StrEnum):
    POLICY = "policy"
    LEDGER = "ledger"


class PolicyDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_id: str
    version: str
    corpus_version: str
    title: str
    text: str


class PolicyChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: str
    doc_id: str
    version: str
    corpus_version: str
    text: str
    start: int  # offset in the document text, inclusive
    end: int  # exclusive


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_type: SpanSourceType
    source_id: str  # doc_id for policy, snapshot_id for ledger
    chunk_id: str = ""
    version: str = ""  # document version (policy spans)
    corpus_version: str = ""  # policy spans
    snapshot_id: str = ""  # ledger spans
    snapshot_hash: str = ""  # ledger spans
    # Strict JSON integers: bool/string/fraction coercion is rejected;
    # integer-valued JSON numbers (0.0) stay valid per the M1 contract.
    start: StrictJsonInt
    end: StrictJsonInt
    quoted_text: str
    text_hash: str


class RetrievedChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk: PolicyChunk
    score: float
    span: EvidenceSpan


def canonical_snapshot_errors(canonical_text: str, balances: dict,
                              transactions: list[dict],
                              unique: bool = True) -> list[str]:
    """Validate the single authoritative representation (R3/N1).

    The canonical text is the only source of truth. Its JSON must
    parse, every numeric field must be a strict finite JSON number,
    and the duplicate ``balances``/``transactions`` views must be
    exactly equivalent to it — provenance is never read from a
    representation different from the one that was hashed.
    """
    try:
        parsed = json.loads(canonical_text)
    except (ValueError, TypeError):
        return ["span_malformed"]
    if not isinstance(parsed, dict) \
            or not isinstance(parsed.get("balances"), dict) \
            or not isinstance(parsed.get("transactions"), list):
        return ["span_malformed"]
    try:
        for account in parsed["balances"].values():
            if not isinstance(account, dict):
                return ["span_malformed"]
            strict_json_number(account.get("balance"))
        for tx in parsed["transactions"]:
            if not isinstance(tx, dict):
                return ["span_malformed"]
            for key in ("tx_id", "from_account", "to_account",
                        "currency", "idempotency_key"):
                if not isinstance(tx.get(key), str):
                    return ["span_malformed"]
            strict_json_number(tx.get("amount"))
    except (ValueError, TypeError, OverflowError):
        return ["span_malformed"]
    if parsed["balances"] != balances or parsed["transactions"] != transactions:
        return ["span_malformed"]
    # Canonical transaction identity is unique: conflicting or exact
    # duplicate tx_id / operation-key rows make every citation in the
    # snapshot ambiguous (F1).
    if unique:
        tx_ids = [tx.get("tx_id") for tx in parsed["transactions"]]
        op_keys = [tx.get("idempotency_key") for tx in parsed["transactions"]]
        if len(set(tx_ids)) != len(tx_ids) or len(set(op_keys)) != len(op_keys):
            return ["span_malformed"]
    return []


def snapshot_validation_errors(snapshot: "LedgerSnapshot") -> list[str]:
    """The one snapshot contract for fresh and stored objects (F2).

    Works on any object carrying the fields, including instances
    assembled with ``model_copy`` that bypassed model validation:
    full hash, deterministic ID derivation and canonical/duplicate
    consistency are all rechecked, never silently repaired.
    """
    errors: list[str] = []
    if sha256_text(snapshot.canonical_text) != snapshot.snapshot_hash:
        errors.append("span_version_mismatch")
    if snapshot.snapshot_id != f"ledger-{snapshot.snapshot_hash[:16]}":
        errors.append("span_source_mismatch")
    errors.extend(canonical_snapshot_errors(
        snapshot.canonical_text, snapshot.balances, snapshot.transactions))
    return errors


def canonical_transactions(snapshot: "LedgerSnapshot") -> list[dict]:
    """Transactions parsed from the authoritative canonical text."""
    parsed = json.loads(snapshot.canonical_text)
    rows = parsed.get("transactions") if isinstance(parsed, dict) else None
    return rows if isinstance(rows, list) else []


class LedgerSnapshot(BaseModel):
    """Immutable deterministic snapshot; later commits cannot change it.

    ``canonical_text`` is authoritative; ``balances``/``transactions``
    are duplicate views validated as exactly equivalent to it, with
    strict finite numeric fields and a matching SHA-256/id.
    """

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    snapshot_hash: str
    canonical_text: str
    balances: dict = Field(default_factory=dict)
    transactions: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _canonical_is_authoritative(self) -> "LedgerSnapshot":
        if sha256_text(self.canonical_text) != self.snapshot_hash:
            raise ValueError("snapshot hash does not match canonical text")
        if self.snapshot_id != f"ledger-{self.snapshot_hash[:16]}":
            raise ValueError("snapshot id does not match canonical hash")
        # Construction validates schema/hash/equivalence. Identity
        # uniqueness is enforced by snapshot_validation_errors at
        # every acceptance boundary (semantic validation, snapshot
        # pairing), so an ambiguous object can be constructed for
        # diagnosis but can never be accepted as evidence (F1).
        errors = canonical_snapshot_errors(
            self.canonical_text, self.balances, self.transactions,
            unique=False)
        if errors:
            raise ValueError("snapshot canonical data is malformed: "
                             + ",".join(errors))
        return self


def chunk_document(doc: PolicyDocument) -> list[PolicyChunk]:
    """Deterministic paragraph chunking preserving exact offsets."""
    chunks: list[PolicyChunk] = []
    pos = 0
    idx = 0
    for part in doc.text.split("\n\n"):
        start = doc.text.index(part, pos) if part else pos
        end = start + len(part)
        if part.strip():
            idx += 1
            chunks.append(PolicyChunk(
                chunk_id=f"{doc.doc_id}#{idx}", doc_id=doc.doc_id,
                version=doc.version, corpus_version=doc.corpus_version,
                text=part, start=start, end=end,
            ))
        pos = end + 2
    return chunks


def _span_for(source_type: SpanSourceType, source_id: str, chunk_id: str,
              source_text: str, start: int, end: int, **kw: str) -> EvidenceSpan:
    if not (0 <= start < end <= len(source_text)):
        raise ValueError("span_out_of_bounds")
    quoted = source_text[start:end]
    return EvidenceSpan(
        source_type=source_type, source_id=source_id, chunk_id=chunk_id,
        start=start, end=end, quoted_text=quoted, text_hash=sha256_text(quoted),
        **kw,
    )


def span_in_text(source_text: str, needle: str, start_from: int = 0) -> tuple[int, int]:
    """Exact offsets of ``needle`` in ``source_text`` (code points)."""
    start = source_text.index(needle, start_from)
    return start, start + len(needle)


def _chunk_binding_errors(span: EvidenceSpan, chunk: PolicyChunk) -> list[str]:
    """The span must belong to its declared chunk (docs/m2.md R7)."""
    errors: list[str] = []
    if (
        chunk.doc_id != span.source_id
        or chunk.version != span.version
        or chunk.corpus_version != span.corpus_version
    ):
        errors.append("span_chunk_mismatch")
    if not (chunk.start <= span.start < span.end <= chunk.end):
        errors.append("span_chunk_mismatch")
    return errors


def make_policy_span(doc: PolicyDocument, chunk: PolicyChunk,
                     start: int, end: int) -> EvidenceSpan:
    """Span over the document text; offsets are document-level and
    must lie inside the declared chunk of the authoritative document."""
    if chunk.doc_id != doc.doc_id or doc.text[chunk.start:chunk.end] != chunk.text:
        raise ValueError("span_chunk_mismatch")
    if not (chunk.start <= start < end <= chunk.end):
        raise ValueError("span_chunk_mismatch")
    return _span_for(
        SpanSourceType.POLICY, doc.doc_id, chunk.chunk_id, doc.text, start, end,
        version=doc.version, corpus_version=doc.corpus_version,
    )


def make_chunk_span(chunk: PolicyChunk) -> EvidenceSpan:
    """Span covering exactly one retrieved chunk of its document."""
    return EvidenceSpan(
        source_type=SpanSourceType.POLICY, source_id=chunk.doc_id,
        chunk_id=chunk.chunk_id, version=chunk.version,
        corpus_version=chunk.corpus_version, start=chunk.start, end=chunk.end,
        quoted_text=chunk.text, text_hash=sha256_text(chunk.text),
    )


def build_ledger_snapshot(ledger: SyntheticLedger,
                          accounts: list[str] | None = None) -> LedgerSnapshot:
    """Deterministic snapshot read directly from the ledger."""
    names = sorted(accounts) if accounts else sorted(ledger.accounts)
    balances: dict = {}
    txs: dict[str, dict] = {}
    for name in names:
        try:
            balances[name] = ledger.get_balance(name).model_dump()
        except KeyError:
            continue
        for tx in ledger.list_transactions(name, 100):
            txs[tx.tx_id] = tx.model_dump()
    transactions = [txs[k] for k in sorted(txs)]
    canonical = json.dumps(
        {"balances": balances, "transactions": transactions},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    digest = sha256_text(canonical)
    return LedgerSnapshot(
        snapshot_id=f"ledger-{digest[:16]}", snapshot_hash=digest,
        canonical_text=canonical, balances=balances, transactions=transactions,
    )


def make_ledger_span(snapshot: LedgerSnapshot, start: int, end: int) -> EvidenceSpan:
    return _span_for(
        SpanSourceType.LEDGER, snapshot.snapshot_id, "", snapshot.canonical_text,
        start, end, snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.snapshot_hash,
    )


def validate_span(span: EvidenceSpan, source_text: str, *,
                  expected_source_id: str = "",
                  expected_version: str = "",
                  expected_corpus_version: str = "",
                  expected_snapshot_hash: str = "",
                  expected_chunk: PolicyChunk | None = None) -> list[str]:
    """Integrity only: bounds, exact text, hash, source and version.

    Returns stable error codes (docs/m2.md); empty means intact. This
    says nothing about relevance or truth.
    """
    errors: list[str] = []
    if expected_source_id and span.source_id != expected_source_id:
        errors.append("span_source_mismatch")
    if span.source_type == SpanSourceType.POLICY:
        if expected_version and span.version != expected_version:
            errors.append("span_version_mismatch")
        if expected_corpus_version and span.corpus_version != expected_corpus_version:
            errors.append("span_version_mismatch")
    if span.source_type == SpanSourceType.LEDGER:
        if span.snapshot_id != span.source_id:
            errors.append("span_source_mismatch")
        if expected_snapshot_hash and span.snapshot_hash != expected_snapshot_hash:
            errors.append("span_version_mismatch")
        if span.snapshot_hash and sha256_text(source_text) != span.snapshot_hash:
            errors.append("span_version_mismatch")
    if expected_chunk is not None:
        if span.chunk_id != expected_chunk.chunk_id:
            errors.append("span_chunk_mismatch")
        else:
            errors.extend(_chunk_binding_errors(span, expected_chunk))
    if not (0 <= span.start < span.end <= len(source_text)):
        errors.append("span_out_of_bounds")
        return errors
    if source_text[span.start:span.end] != span.quoted_text:
        errors.append("span_text_mismatch")
    if sha256_text(span.quoted_text) != span.text_hash:
        errors.append("span_hash_mismatch")
    return errors


def validate_policy_span(span: EvidenceSpan, doc: PolicyDocument,
                         chunk: PolicyChunk | None, query: str,
                         min_score: float) -> list[str]:
    """Full policy-span check: integrity (incl. chunk binding), then
    relevance. ``chunk`` is resolved by the caller from the
    authoritative corpus; ``None`` means the chunk_id is unknown."""
    if chunk is None:
        return ["span_chunk_mismatch"]
    errors = validate_span(
        span, doc.text, expected_source_id=doc.doc_id,
        expected_version=doc.version,
        expected_corpus_version=doc.corpus_version,
        expected_chunk=chunk,
    )
    errors.extend(validate_relevance(span, query, min_score))
    return errors


_TOKEN = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def relevance_score(cited_text: str, query: str) -> float:
    """Deterministic lexical overlap: |shared| / |query tokens|."""
    q = tokens(query)
    if not q:
        return 0.0
    return len(q & tokens(cited_text)) / len(q)


def validate_relevance(span: EvidenceSpan, query: str,
                       min_score: float) -> list[str]:
    """Relevance only: intact but unrelated citations fail here."""
    if relevance_score(span.quoted_text, query) < min_score:
        return ["span_irrelevant"]
    return []
