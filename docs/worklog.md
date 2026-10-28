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

## M1 remediation round 5 (after independent audit of 89c1812)
Independent offline audit BLOCKED 89c1812 with F1–F5 (12 red probes of
38; repo suite 133 green). Audit probes preserved outside the repo at
`TrustFlow-context/probes-m1-audit-89c1812/` (the original temp catalogue
was inaccessible from the sandbox account; copied with elevation, plus
both JUnit XML files). New maintained regressions in
`tests/unit/test_m1_round5.py`: 19 failed / 2 passed on the baseline,
21 passed after the fix. F1: provider validates envelope/output/usage
before field access; malformed data becomes `LLMResponseError` with
unknown-or-available usage and flows into bounded verification-only
recovery (no VERIFYING deadlock, no second transfer). F2: one usage
record per HTTP attempt (retryable, timeout, terminal) created before
retry/raise; logical `attempt` vs `transport_attempt` distinguished;
tracker expands `prior_attempts` flat with no double count. F3:
`check_bounds` enforced for every revision before authorization or
execution. F4: `SemanticReviewInput` requires `issues`/`summary` like
`REVIEW_SCHEMA`; `PLAN_SCHEMA.limit` is integer 1–100 matching local
validation; per-tool constraints the flat schema cannot express are
documented in docs/m1.md with strict local validation retained. F5:
sanitized `semantic_review_failed` audit event (attempt, recovery
state) written before the exception branch returns; recovery still
clears the active error. Existing tests updated only where they encoded
the pre-F4 incomplete-review contract: review fixtures in
test_m1.py, test_m1_stabilization.py, test_m1_remediation.py,
test_m1_round2.py, test_m1_round3.py gained the required `summary`
(and FAIL fixtures likewise, preserving their intended path).
Checks on the fixed source: pytest 154 passed; ruff check src tests
clean; mypy src clean. Independent probes re-run against the fixed
source (import path verified): 36 passed, 2 failed — both probe-side
(superseded no-minimum schema assertion; a debug print dereferencing
`semantic=None`), with the asserted behaviour verified directly.
Offline only; no push, merge, deploy or paid API calls. Remote
`d2690f9…` was not fetched or verified. Status: fixed by the developer,
awaiting independent re-review (M1 not accepted); live acceptance,
restart/multiprocess persistence and AuthN/RBAC remain open/out of scope.

## M1 remediation round 4 (this candidate, after 0bbee55)
Round-3 audit BLOCKED on one P1: verifier accepted arbitrary
`run.cancellations` dicts matching only `intent_id` as cancellation
evidence for ever-authorized intents. Reproduced RED first (13 probes in
`tests/unit/test_m1_round4.py`, 7 failing on 0bbee55, including forged
bare-ID and full actor/policy/fingerprint records returning
`passed=True`). Fix is verifier-only plus model/docs comments: nonempty
`run.cancellations` is unsupported evidence in M1, and a CANCELLED
ever-authorized intent always fails with its obligation unresolved, so
COMPLETED is unreachable. Task-3 audit found no API/application path
that replaces IntentRecords or resets `ever_authorized`; replacement
needs trusted in-process memory access (documented boundary, M3 owns
persistence). Full suite 133 tests, ruff/mypy clean, offline only.
Status: candidate for a separate adversarial audit (not accepted).
