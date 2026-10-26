# TrustFlow Worklog

## M0 — Walking skeleton (860ce7d..7e744d3)
Planner/Executor/Verifier loop, hash-bound approvals, synthetic ledger,
23 offline tests. Pushed at the time with approval.

## M1 — Real Meta intelligence (7e744d3..4d2bc2d)
Live Responses API provider (Wonder plan + semantic review, Spark config),
validated plans with one repair, usage/cost records, 22 eval scenarios,
59 offline tests, bounded live E2E ($0.036 for the successful run).
Independent review then reproduced seven findings; M1 was not accepted.

## M1 stabilization (this candidate, after 4d2bc2d)
Finding-by-finding fixes, each with a regression test in
`tests/unit/test_m1_stabilization.py` (see docs/m1.md for the table):
operation identities are backend UUIDs retained by payload fingerprint +
occurrence ordinal; revised plans reconcile committed ledger evidence into
current-step results and refresh reads; timeouts produce EXECUTION_UNCERTAIN
with tracked workers and ledger reconciliation (no definitive failure, no
replacement while unresolved); semantic output is validated, the
deterministic report persists first, and failures land in
VERIFICATION_PENDING with verification-only retry (committed transfers are
never rolled back or re-executed); usage is attributed per workflow at call
time with pre-call reservation, unknown tokens stay unknown, and over-budget
records are preserved; LLM mode is a validated fake|live contract; plan
schema covers fx fields; errors/audit payloads are recursively sanitized;
approved plans execute from an isolated deep snapshot; the verifier matches
complete operations against the ledger itself.
Offline only: 75 tests, ruff and mypy clean. No paid API calls were made.
Second review reproduced three P1 and six P2 findings on that candidate.

## M1 remediation (this candidate, after 15833ea)
Payment-intent manifest separate from payload matching (changed-payload
re-plans keep the committed intent; new intents need explicit approval),
exact approved-snapshot binding rejected before mutation, authoritative
operation identity in verification, atomic semantic-attempt accounting
(initial call included, default max 3), claimed concurrent reconciliation,
provider/local schema alignment, usage preserved through all validation
failures with input-aware reservations, semantic sanitization, full
tool-schema parity (incl. limit), and resolved-error clearing with audit
history preserved. 14 behavioral probes in
`tests/unit/test_m1_remediation.py` failed on 15833ea (12) before the fix;
full suite is now 89 tests, ruff/mypy clean, offline only.
Status: candidate for independent re-review (not self-accepted).

## M1 remediation round 2 (this candidate, after 0da157e)
Adversarial self-audit BLOCKED 0da157e with N1-N6. Replaced position-based
intent inheritance with stable backend intent IDs (IntentRecord): order-
independent exact matching, explicit conflict for changed committed intents,
re-authorization for modified uncommitted intents, fail-closed ambiguity,
explicit cancellation, manifest-coverage verification before COMPLETED,
whitespace-credential redaction, explicit unsupported-FX failure, and the
restored low-budget exhaustion scenario. 18 round-2 probes
(tests/unit/test_m1_round2.py; 12 failed on 0da157e) plus real-HTTP
concurrent-approval and API-secret tests; full suite 107 tests, ruff/mypy
clean, offline/fake only. Four round-1 tests were updated to the superseding
conflict contract (changed committed intent is no longer a silent COMPLETED).
Status: candidate for independent re-review (not self-accepted).

## M1 remediation round 3 (this candidate, after a1a2ad8)
Round-2 audit BLOCKED with N1-N4. Cancellation by omission eliminated:
omitted AUTHORIZED intents produce an explicit conflict and remain
outstanding; only never-authorized PROPOSED intents may withdraw. Coverage
never accepts a bare CANCELLED status (explicit authorized cancellation
evidence required; no such workflow exists in M1 by design). Verification
now uses canonical run-owned intents, rejects foreign evidence on
PROPOSED/CANCELLED intents, and sync_intents enforces legal lifecycle
transitions (CANCELLED/CONFLICT never become COMMITTED; late commits are
integrity conflicts). Secret-marker values are redacted standalone.
Adversarial tests A-L in tests/unit/test_m1_round3.py (9 red before the fix,
including strengthened round-2 probes for N1/G/E/N2 and a real-uvicorn HTTP
concurrency test); full suite 120 tests, ruff/mypy clean, offline only.
Status: ready for a separate adversarial acceptance audit (not accepted).
