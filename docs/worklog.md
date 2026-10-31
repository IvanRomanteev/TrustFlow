# TrustFlow Worklog

## M3 remediation round 4 (independent re-review of cbca437: N2-H + N2-G)
Independent re-review VERIFIED FIXED N2-F and all backend gates,
with two residual UI ordering items (review preserved at
TrustFlow-context/review-m3-cbca437-acceptance-a8f67c20/). One
coherent frontend freshness policy closes both: POST responses are
acknowledgements only and visible run/evidence/audit state is
applied exclusively from guarded authoritative GETs, so a stale
POST after a polling-confirmed COMPLETED can no longer roll the UI
back (N2-H); success and error outcomes share one relevance check
(captured run id, selection epoch, action sequence, last-outcome
watermark), so a held Approve(A) HTTP 403 no longer replaces the
current Reject(B) notice (N2-G), while current errors still show.
RED reproduced first in headless Chrome with real FastAPI
responses; after the fix H, the original N2-F A→B→A scenario and G
are all GREEN (backend COMPLETED, attempts=3, one transfer).
Backend untouched. Checks: pytest 289 passed, Ruff clean, mypy
clean, frontend build green, isolated Docker Compose build/run
smoke passed on this candidate (project tf-m3-n2hg-final, removed
afterwards with its volume). No existing test changed;
conversation files preserved; no push, merge, deploy or paid
calls. Status: READY FOR INDEPENDENT CODEX RE-REVIEW; M3 not
accepted.

## M3 remediation round 3 (independent re-review of cb5688c: N2-F only)
Independent re-review VERIFIED FIXED N1 and kept every F1–F7 gate,
with one residual P2: after A→B→A a stale first verify-retry
response (PENDING/attempts=2) overwrote the newer COMPLETED state
(semantic PASS/attempts=3) because the round-2 guard compared only
run identity (review preserved at
TrustFlow-context/review-m3-cb5688c-independent-095398b5/). Frontend
only fix: monotonic selectionEpoch bumped on every explicit
selection plus per-action sequence with a last-applied watermark;
act() captures run id, epoch and sequence before awaiting and
ignores stale responses with a notice, polling fresh backend state
instead. Reproduced RED first in headless Chrome with real FastAPI
responses (held first retry, A→B→A, second retry COMPLETED, stale
delivery with the next GET held: status rolled back to
VERIFICATION_PENDING); GREEN after the fix (stays COMPLETED,
semantic PASS, attempts=3, one transfer), and the delayed-approve
A→B control still passes. Backend untouched. Checks: pytest 289
passed, Ruff clean, mypy clean, frontend build green; Docker
daemon unavailable in this session (runtime NOT VERIFIED here;
independently verified for cb5688c). No existing test changed;
conversation files preserved; no push, merge, deploy or paid
calls. Status: READY FOR INDEPENDENT CODEX RE-REVIEW; M3 not
accepted.

## M3 remediation round 2 (independent re-review of 8143dbc: N1 + N2)
Independent re-review VERIFIED FIXED F1–F4, original F6 and F7, and
kept the gate BLOCKED on two items (review preserved at
TrustFlow-context/review-m3-8143dbc-independent-1a01d72b/). N1 (P1):
reconcile of a definitively dead worker (no live task, no ledger
commit) entered the ordinary revision loop and executed a
replacement transfer (ledger 0→1 with plan_revised). Fix is routing
only: WorkflowEngine.reconcile now verifies with
semantic_only_retry=True, so dead/no-commit ends in bounded FAILED
with zero planner and zero mutating tool calls, committed outcomes
still reconcile verification-only from the ledger, and live workers
stay EXECUTION_UNCERTAIN until they resolve; normal planning and
revisions are untouched. N2 (P2): a late action response
re-selected its own run over a newer user selection; act() now
captures the selected run before awaiting and, when selection moved
on, only refreshes the list and the still-selected run with a
notice. Maintained regressions in
tests/unit/test_m3_remediation2.py (real os._exit(81) child with a
barrier-held worker; live-worker control). RED on 8143dbc: dead
worker test failed as reproduced; GREEN after the fix. Headless
Chrome: delayed approve of A while B selected keeps B in
localStorage and all panels, backend A COMPLETED. Checks: pytest
289 passed (287 previous + 2 new), Ruff clean, mypy clean,
frontend build green; Docker daemon unavailable in this session
(compose config valid, exit 0; Docker runtime for the new candidate
is NOT VERIFIED here). No existing project test changed; both
conversation files preserved; no push, merge, deploy or paid
calls. Status: READY FOR INDEPENDENT CODEX RE-REVIEW; M3 not
accepted.

## M3 remediation round 1 (independent review of 695bfd8: F1–F7 BLOCKED)
Independent offline review BLOCKED 695bfd8 with two P1 and five P2
findings; its probes are preserved unchanged at
TrustFlow-context/review-m3-695bfd8-independent-6237b106/. All seven
are fixed by the developer and await independent re-review (details
in docs/m3.md). F1: PLANNED/EXECUTING durable checkpoints before any
ledger side effect, approval claim CAS-saved before execution, and
ledger-key reconciliation at startup (paid runs cannot be rejected
after restart). F2: per-record persistence_version CAS replaces the
shared repository _versions map; concurrent approve/approve,
approve/reject and reject/approve yield exactly one 200 decision,
one audit event and one transfer. F3: UsageTracker rehydrates from
persisted llm_calls without double counting. F4: semantic attempt
increment is durable before reviewer invocation. F5: deterministic
FAIL in verification-only recovery terminates FAILED; untracked
uncertain steps with no ledger commit resolve as not committed.
F6/F7: generation-guarded polling and deterministic/semantic/overall
verdict separation, both verified in headless Chrome (held stale
AWAITING response after completed approve; injected semantic FAIL).
Maintained regressions in tests/unit/test_m3_remediation.py include
real os._exit child-process crashes after ledger commit and after
completed semantic review. Checks: pytest 287 passed (279 previous
+ 8 new), Ruff clean, mypy clean, frontend build green; the review's
own test_independent.py (11) and test_more.py (5) also pass against
the fixed source from byte-identical copies. No existing project
test changed; both conversation files preserved; no push, merge,
deploy or paid calls. Status: READY FOR INDEPENDENT CODEX
RE-REVIEW; M3 not accepted.

## M3 candidate — persistence + demo UI (from accepted M2 fefd3fd)
Implemented on branch m3/persistence-ui, based exactly on the
independently accepted M2 SHA fefd3fdcebcb784170d9097eb9f2bc428da9f524
(M2 OFFLINE GATE: PASS). The Engineering Playbook was not found
locally and its contents were not invented; M3 scope is the kickoff
text plus accepted M1/M2 contracts (docs/m3.md). Phase 1: SQLite
(stdlib sqlite3, TRUSTFLOW_DB_PATH) behind the existing contracts —
SQLiteRunRepository with optimistic versioning (cross-process
single-use approvals), PersistentLedger with one BEGIN IMMEDIATE
transaction per transfer and UNIQUE(idempotency_key), append-only
SQLiteAuditLogger, idempotent migrations, verification-only restart
recovery (EXECUTING/VERIFYING -> VERIFICATION_PENDING, approvals and
uncertain runs untouched); unset DB path keeps exact M1/M2 in-memory
behaviour. Phase 2: X-Approval-Token authorization (401/403) on
approve/reject/verify-retry/reconcile when configured, GET /workflows,
CORS, and a Next.js three-panel demo UI (task input, run timeline,
approval & evidence with exact plan hash) with a permanent
SANDBOX / NO REAL FUNDS banner and 2s polling. Phase 3:
docker-compose.yml (config validated; Docker daemon was not running
in this environment, so images were not built here) plus
deterministic fake-mode API E2E for ordinary success, HIGH-risk
approval, semantic-failure verification-only recovery and mandatory
evidence blocking before mutation. Checks: pytest 279 passed
(261 pre-existing + 18 new), Ruff clean, mypy clean (37 files),
frontend production build green, real-process restart keeps
COMPLETED run/approval/results, headless Chrome rendered all three
panels against the durable backend. No existing test changed; both
untracked conversation files preserved. Status: M3 candidate ready
for independent review; not self-accepted. No push, merge, deploy
or paid calls.

## M2 round-5 targeted remediation (independent review of c9956ee: C4 only)
Independent review confirmed C1/C2 VERIFIED FIXED and previous C3
cases GREEN, and kept the gate BLOCKED on one regression: C4, the
provider re-derived the executed read from the current last-N
window, promoting 35 later transactions into mandatory evidence
after unrelated history growth (36 required rows became 71, the
8115-char payload exceeded the limit and healthy recovery was
blocked). Review materials are preserved unchanged at
TrustFlow-context/review-m2-c9956ee-independent-c0195e2d/.
Maintained regressions in tests/unit/test_m2_remediation5.py:
3 failed on baseline c9956ee, all green after the fix. Fix: the
reviewer marks its obligation set complete via
mandatory_tx_ids_complete, carried as an attribute of the internal
SemanticPayload (never serialized, transmitted or counted in the
reservation shape); a complete authoritative hint is used exactly
(verified executed-read IDs union committed-operation IDs union
cited IDs) with the C3 required-subset-of-available check intact,
while an unmarked/incomplete hint keeps the previous C3 union
protection so empty/partial direct metadata still cannot drop
requested rows and missing committed evidence is rejected before
HTTP. Checks: pytest 261 passed, Ruff clean, mypy clean (31
files), eval 33/33, adapted independent aggregate 238/238,
unchanged round-3 probes 109/111 (only the two known out-of-scope
foreign-read exploratory failures), round-4 independent suite
green on all six former C4 reproductions with only the two
previously documented direct-provider exploratory failures
remaining. No existing project test changed, no push, merge,
deploy or paid calls. Status: fixed by the developer, READY FOR
INDEPENDENT CODEX RE-REVIEW; M2 not accepted, M3 not started.

## M2 round-4 targeted remediation (independent review of bde4dae: C1–C3)
Independent review kept the gate BLOCKED on three P2 items (all
prior gates retained): C1 canonical snapshot uniqueness was only
checked where ledger spans exist, so a read-only workflow completed
on a duplicate canonical identity; C2 mandatory retrieval context
could still be shrunk at transport retry when conflicting
mandatory+shrinkable flags were both set; C3 mandatory ledger
evidence could vanish behind the recent-50 history window (aged
operation after later transfers), executed reads were recomputed
from current state, and an empty/partial mandatory_tx_ids hint
could silently drop required rows at the provider. Review
materials are preserved unchanged at
TrustFlow-context/review-m2-bde4dae-independent-8a1bc9c1/.
Maintained regressions in tests/unit/test_m2_remediation4.py:
8 failed on baseline bde4dae, all green after the fix. Fixes:
snapshot_validation_errors now runs at one acceptance boundary
independent of ledger spans (semantic validation on any supplied
snapshot, fresh and stored engine snapshot pairing); the Planner
forwards context_shrinkable = shrinkable and not mandatory, so
mandatory context is never shrunk at retry; snapshots and semantic
evidence are built from the full ledger history for the plan's
accounts, executed list_transactions rows keep their exact
identities verified row-by-row against the authoritative ledger,
the reviewer enforces required_mandatory_ids subset of available
canonical IDs before any provider call, and at the provider a hint
can only add obligations (requested reads unioned in, missing
required rows are a typed pre-HTTP failure). Checks: pytest 258
passed, Ruff clean, mypy clean (31 files), eval 33/33, previously
adjudicated adapted probes 238/238, current round-3 independent
suite 109 passed with only the 2 exploratory foreign-read probes
failing (classified by the review itself as a non-blocker
limitation; no foreign-read protocol added). No existing project
test changed, no push, merge, deploy or paid calls. Status: fixed
by the developer, READY FOR INDEPENDENT CODEX RE-REVIEW; M2 not
accepted, M3 not started.

## M2 round-3 targeted remediation (independent review of 70e20f6: N1, F1–F4)
Independent review kept the gate BLOCKED on five findings (R6 and
R1/R4/R7/R8 VERIFIED FIXED and untouched): N1 malformed policy key
crashed coverage-key construction (HTTP 500, stalled VERIFYING),
F1 conflicting duplicate canonical tx_id could be cited while
another row was provenance-checked, F2 fresh snapshot ID was not
rechecked against its hash, F3 optional context still changed M1
outcomes at transport retry and budget 1515, F4 pruning dropped
mandatory ledger rows (current transfer, requested reads). Review
materials are preserved unchanged at
TrustFlow-context/review-m2-70e20f6-independent-d622c217/.
Maintained regressions in tests/unit/test_m2_remediation3.py:
9 failed / 1 passed on baseline 70e20f6, all green after the fix.
Fixes: coverage keys are built only from validated EvidenceSpan
objects after typed validation raises; canonical tx_id/operation
identity is unique at every acceptance boundary and spans are
located inside the exact canonical row of their backend operation;
snapshot_validation_errors plus fresh-path model revalidation apply
one ID/hash/canonical contract to fresh and stored snapshots;
provider transport retry shrinks only caller-marked shrinkable
advisory context, with the new reservation derived from actually
removed characters and never below the context-free estimate, and
the context-free semantic fallback omits the optional keys entirely
(M1 budget-1515 outcome restored, R6 contract preserved); pruning
keeps mandatory rows by exact transaction identity via the
reviewer-supplied mandatory_tx_ids hint (committed operations plus
requested list_transactions rows, never transmitted, identical
historical payloads not promoted), with a documented size-reduced
projection and a typed pre-HTTP error if mandatory evidence cannot
fit. Independent probes with verified workspace import path:
unmodified originals 230 passed / 8 failed, all 8 being the pre-F3
empty-placeholder payload-shape expectations superseded by the F3
contract; adapted copies of those two probe files (shape-only
changes, adaptation.diff preserved in the review catalogue) pass
with all other probes, 238 passed. Checks: pytest 250 passed, Ruff
clean, mypy clean (31 files), eval 33/33. No existing project test
changed, no push, merge, deploy or paid calls. Status: fixed by the
developer, awaiting independent re-review (READY FOR INDEPENDENT
CODEX RE-REVIEW); M2 not accepted, M3 not started.

## M2 residual remediation (re-review of ffe1dc7: R2/R3/R5/R6 residual, N1/N2/F1–F4)
Independent re-review VERIFIED FIXED R1, R4, R7, R8 and the offsets
gate, and kept the M2 offline gate BLOCKED on residual R2/R3/R5/R6:
F1 missing mandatory policy spans completed, F2 duplicate-index
laundering of a foreign canonical snapshot, F3 optional planner
context still changed budget outcomes, F4 provider silently sliced
mandatory semantic JSON at 6000 chars, plus new regressions N1
(malformed snapshot amount escaped to HTTP 500 / stalled VERIFYING)
and N2 (legacy reservation for untransmitted context). Review
materials are preserved unchanged at
TrustFlow-context/review-m2-ffe1dc7/. Maintained regressions in
tests/unit/test_m2_remediation2.py: 9 failed on baseline ffe1dc7,
all green after the fix (plus a provider boundary test added with
the fix). Fixes: mandatory policy obligation is checked per
verification attempt independently of ledger coverage/truthiness;
snapshot canonical text is the single authoritative representation
(hash/id, strict numeric schema, duplicate views validated as
exactly equivalent, provenance read from parsed canonical
transactions bound by operation keys); malformed prepared evidence
is a typed EvidenceValidationError/LLMResponseError in bounded
verification-only recovery with report/usage/audit preserved, one
transfer and healthy retry after the cause is removed, with no
general catch of programming errors; engine-advisory planner
context is shrunk before reservation only when the engine marks it
shrinkable, mandatory context is never shrunk, and capability plus
the actual transmitted arguments are determined before any
reservation (legacy reserves nothing untransmitted); the semantic
provider sends one whole structured JSON document under an explicit
6000-char limit, dropping only uncited evidence transactions and
raising a controlled error before any HTTP call if mandatory
evidence cannot fit. Independent probes, unmodified and run from
byte-identical copies with the workspace import path verified:
original 41/41 and additional 88/88 pass (129 total). Checks: pytest
240 passed, Ruff clean, mypy clean (31 files), eval 33/33. No
existing project test was changed, no push, merge, deploy or paid
calls. Status: fixed by the developer, awaiting independent
re-review; M2 not accepted, M3 not started. No claim->citation
grounding and no live prompt-injection robustness are claimed.

## M2 remediation R1–R8 (after independent BLOCKED review of f0a1536)
Independent offline review BLOCKED f0a1536 with R1–R8 (plus a P3
offset-typing clarification); its catalogue is preserved unchanged
outside the repo at TrustFlow-context/review-m2-f0a1536/ (REPORT,
independent probes, results). Maintained regressions in
tests/unit/test_m2_remediation.py reproduced the defects on the
baseline (17 failed / 2 passed) before the fix. R1: mandatory policy
evidence is validated (hash/source/version/chunk/relevance) before
first execution and again before resume/revision execution;
corruption while AWAITING_APPROVAL fails with zero transfers. R2:
the fail-open snapshot/span preparation swallow is removed;
malformed or unbuildable mandatory evidence raises typed
EvidenceValidationError/LLMResponseError into bounded
verification-only recovery (one transfer, report/usage/audit
preserved, no COMPLETED without evidence). R3: ledger citations are
bound to backend-owned operation/intent keys with payload, snapshot
provenance, per-operation coverage and real relevance; foreign
self-consistent snapshots and arbitrary slices are rejected. R4:
snapshot+spans are stored and reused as a consistent pair while
coverage holds, atomically rotated with audit otherwise; outage A,
completion B, healthy retry A completes with one transfer each. R5:
advisory semantic context is dropped and re-reserved when it cannot
fit, restoring the M1 budget-1800 outcome; mandatory evidence is
never dropped and incurred usage is kept. R6: planner reservation
includes the identical bounded context actually transmitted
(initial, repair, retry). R7: chunk_id resolves through the
authoritative corpus and the span must lie inside its chunk. R8:
retrieval-context capability is signature-checked against the actual
propose_plan callable; legacy providers keep M1 behaviour with no
TypeError retry. P3: strict integer offsets (bool/string/fraction
rejected, integer-valued JSON numbers accepted). One pre-existing M2
test was corrected to the R1 contract (corruption while awaiting
approval now blocks payment instead of completing a transfer);
post-commit behaviour is covered by the R2 regressions. The review's
original probes were not modified; they pass 41/41 on the fixed
source with the workspace import path verified (baseline 23/18).
Checks: pytest 230 passed, Ruff clean, mypy clean (31 files), eval
33/33. No push, merge, deploy or paid calls. Status: fixed by the
developer, awaiting independent re-review; M2 not accepted, M3 not
started.

## M2 candidate — policy retrieval and evidence spans (offline)
Implemented on branch m2/retrieval-evidence-spans from the documented
M1 acceptance state (162cc70), contracts first in docs/m2.md
(d047311). Agreed scope is a synthetic in-repository corpus and span
annotations, explicitly not a reproduction of the unprovided
Playbook. Retrieval covers versioned synthetic policies
(synthetic-policies-v1, deterministic lexical retrieval) and
deterministic ledger-evidence snapshots (canonical JSON, SHA-256,
snapshot id/hash immutable after later commits). Text spans use
start inclusive / end exclusive in Unicode code points with UTF-8
SHA-256 hashes; integrity, relevance and authority are checked
separately and retrieval stays advisory — PolicyEngine and ledger
verification remain decisive, retrieved text cannot change approval,
limits or tools. Mandatory citations exist only via the explicit
WorkflowGoal.require_evidence opt-in: missing mandatory
pre-execution evidence fails before any mutation; span errors found
after a commit preserve the transfer/report/usage/audit and use
bounded verification-only recovery with no second transfer. 11 new
eval scenarios (33 total with the preserved 22) plus unit contracts
cover prompt injection in a retrieved document, intact-but-irrelevant
spans, substituted version/source, corrupted hashes, snapshot
stability and both fail-closed modes. P2/P3 live harness fixes remain
mandatory before any future paid run and did not block this offline
work; no paid calls, push, merge or deploy. Status: M2 candidate for
independent Codex review, not self-accepted.

## M1 technical acceptance (independent Codex confirmation)
Independent Codex review confirmed offline PASS and functional live
VERIFIED PASS for source 75f1d9e5d6f09e724d187ae95c26018996561e74.
M1 is therefore technically accepted in the verified synthetic /
single-process scope. Successful live configuration:
TRUSTFLOW_LLM_MODE=live, TRUSTFLOW_MAX_OUTPUT_TOKENS 1200 -> 4000
(configuration only, no source change), muse-wonder-1.0 for planning
and semantic review, synthetic ledger only, default transport
retries; small transfer, HIGH approval, injected semantic outage with
verification-only retry, and FX schema all PASS. Procedure remarks
are recorded separately and the bounded procedure is not recorded as
fully complied with: the intermediate $0.50 cap was exceeded by
$0.016220 before the $1.00 total cap was subsequently agreed;
confirmation of the specific 20-request limit is absent (17 real Meta
requests were recorded in total); $0.813836 is an estimate from known
reported usage, not confirmed provider billing. Registered
follow-ups, mandatory before any further paid runs but not requiring
repetition of the passed functional M1 acceptance: P2 harness
reservation, P3 request counters. Current HEAD at recording time:
16ffd0db27fee81909534ce5ffd10f21070c4d9f on local main; it differs
from the reviewed source only in README.md, docs/m1.md and
docs/worklog.md. Published branch m1/offline-pass-75f1d9e remains at
75f1d9e. The untracked user file conversation-2026-10-28-171132.txt
was preserved and not committed. No push, merge, deploy or paid API
calls in this step; M2 code not started, plan presented for scope
agreement.

## M1 offline PASS publication and bounded live acceptance
Independent re-review confirmed offline PASS for
75f1d9e5d6f09e724d187ae95c26018996561e74 (N1 VERIFIED FIXED). The
candidate was published unmodified as branch m1/offline-pass-75f1d9e
at the exact reviewed SHA; main was not pushed or merged (origin/main
has an unrelated one-line README commit pending separate
integration). Bounded live acceptance was then executed by the
developer on the same SHA against the real Meta API, synthetic
ledger only, under user-approved caps (final total cap $1.00):
small transfer, HIGH approval (1500 USD, hash-bound), injected
semantic outage -> verification-only retry with one transfer, and
FX schema (USD->EUR 0.92) all PASS. Totals across all launch
attempts: 17 real Meta requests, $0.813836 estimated from reported
usage. Two configuration-only adjustments were needed and are
recorded in the evidence: TRUSTFLOW_MAX_OUTPUT_TOKENS 1200 -> 4000
(at 1200 Wonder consumed the output budget on reasoning and
returned no text, correctly rejected as malformed) and the default
transport retries restored after a transient Meta HTTP 500; an
intermediate $0.50 cap was exceeded by $0.016 before the raised cap
was approved. Source code was never changed; the published branch
still points at the reviewed SHA. Sanitized evidence (request IDs,
per-attempt usage, approvals, ledger transactions, verification,
audit; no keys) is outside the repo at
TrustFlow-context/live-acceptance/. Status: offline PASS published;
live acceptance passed by the developer; full M1 acceptance awaits
user/independent confirmation of the live evidence. No deploy, no
M2 work.

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
