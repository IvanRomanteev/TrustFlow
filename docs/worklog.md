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

## M1 remediation round 7 (after round-6 re-review of 98d3aaa)
Re-review BLOCKED 98d3aaa on a single confirmed new regression N1;
R1/F1 and R2/F2 were independently VERIFIED FIXED, F3/F5 stayed
verified, and all earlier safety gates passed. Review materials
preserved outside the repo at `TrustFlow-context/review-98d3aaa/`
(new probes, compare_numeric.py, logs/JUnit, adaptation diffs,
integrity manifest); the untracked user file
conversation-2026-10-28-171132.txt was preserved and not committed.
N1: `strict_json_number` called float() on huge JSON integers, so a
semantic confidence=+-10**400 after a committed transfer raised
OverflowError outside typed validation (HTTP 500, stuck VERIFYING).
Minimal fix in src/trustflow/tools/ledger.py only: an overflowing
conversion now raises ValueError, flowing into the existing
ValidationError -> LLMResponseError -> bounded verification-only
recovery. No engine catch-all added; bool/string/non-finite rejection
and the confidence range are unchanged, nothing is clamped.
Regressions in tests/unit/test_m1_n1.py: 6 failed / 3 passed on
98d3aaa, 9 passed after the fix (healthy verify-retry to COMPLETED
with one transfer, bounded FAILED at the attempt limit, per-attempt
usage 7/11 without duplicates, sanitized audit history after the
active error clears, shared-validator amount checks, valid-value
guards). Also corrected the round-6 docs phrase about record append
vs cost estimation to match the actual order in UsageTracker.record
(cost per item immediately before its append; safety rests on the
ingestion bound plus non-raising Decimal arithmetic). Checks on the
fixed source: pytest 191 passed; ruff check src tests clean (after a
blind-exception lint fix in the new test); mypy src clean.
Independent probes re-run against the fixed source (import path
confirmed, originals byte-identical by SHA256): new round-6 probes
181 passed (the N1 correctness test is green); adapted 38 passed;
extra-adapted 46 passed; originals 36 passed / 2 failed (the same two
probe-side failures adjudicated earlier); the reviewer's
compare_numeric.py on the fixed tree reports ValidationError for
+-10**400 confidence, create 201 -> VERIFICATION_PENDING -> healthy
retry COMPLETED, one transfer, audit attempt/recovery state saved.
Offline only; no push, merge, deploy, pull, reset or paid API calls.
Status: N1 fixed by the developer, awaiting independent confirmation
(M1 not accepted); live acceptance, persistence and AuthN/RBAC
remain open/out of scope.

## M1 remediation round 6 (after re-review of 71df99c)
Re-review BLOCKED 71df99c with residual R1–R3 only; F3 and F5 were
independently VERIFIED FIXED and their gates (plus all earlier safety
probes) stayed green. Review catalogue preserved outside the repo at
`TrustFlow-context/review-71df99c/` (original, adapted, extra,
adaptation.diff, JUnit results, integrity manifest); original probe
SHA256 values match the re-review's recorded hashes. New regressions
in `tests/unit/test_m1_round6.py`: 15 failed / 13 passed on 71df99c,
28 passed after the fix. R1: token counts are ingested only within
0..10^9, out-of-range/invalid counts become per-field unknown (known
sibling counts preserved, no invented values), `estimate_cost` uses
Decimal and cannot raise OverflowError, records are appended
independently of cost estimation. R2: Planner/SemanticReviewer pass the
per-workflow tracker and reservation estimate per call to
tracker-aware providers; interim attempts are recorded in the
originating workflow and re-reserved before any next HTTP request
(both 2000- and 100-token first attempts block the retry at budget
1500), terminal attempts are provider-recorded without double count,
logical/transport attempt numbering preserved, no shared
current-workflow provider state. R3: strict JSON numeric types for
limit/amount/confidence — bool and numeric strings rejected,
integer-valued JSON numbers accepted, fractions rejected.
No existing project test was changed in this round. Checks on the
fixed source: pytest 182 passed (154 + 28); ruff check src tests
clean; mypy src clean. Independent verification against the fixed
source (import path confirmed): adapted probes 38 passed; extra
deep/accounting probes 45 passed, 1 failed — the failing one is the
diagnostic that asserts the OverflowError itself, preserved unchanged;
it fails by design now that the defect is absent, and round-6 tests
prove the absence. Original probes (unchanged): 36 passed / 2 failed,
the same two probe-side failures adjudicated in the re-review.
Offline only; no push, merge, deploy, pull or paid API calls. Remote
d2690f9… was not fetched or verified. Status: R1–R3 fixed by the
developer, awaiting independent confirmation (M1 not accepted); live
acceptance, restart/multiprocess persistence and AuthN/RBAC remain
open/out of scope.

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
