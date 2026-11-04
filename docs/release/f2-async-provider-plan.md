# F2: separate provider responsiveness design and test plan

Status: proposed follow-up, **not implemented or accepted in this remediation**.
Audit baseline: `56f9a4e4bacbee7e688f08223b6a8d9929fd4354`.

## Confirmed problem

`WorkflowEngine.create_workflow` calls synchronous `Planner.create_plan` from an
async request. Semantic verification and re-planning also reach synchronous
`MetaResponsesProvider` HTTP and retry sleep. The independent audit's delayed
offline planner held up `/health` for approximately one second. This is evidence
of event-loop blocking, not evidence of duplicate ledger transfers.

## Proposed boundary

Prefer an explicit async provider contract with `httpx.AsyncClient`, awaited
transport and nonblocking backoff. Migrate planning, re-planning and semantic
review together; retain typed outputs, schemas, model allowlists and sanitization.
The deterministic fake provider needs the same async contract. A bounded executor
bridge is an alternative to evaluate, but `asyncio.to_thread` alone is insufficient:
cancelling an await does not stop a thread, a provider may complete late, and
shared trackers/run objects must not be mutated from background workers.

Before yielding, persist a workflow-specific attempt claim with a unique attempt
identity, persistence version and input/plan/evidence hashes. Reserve attempts and
estimated budget atomically. Define how initial PLANNING claims and crash recovery
are persisted; do not assume the current semantic claim covers initial planning.
Prepare immutable inputs; keep database transactions and locks out of network
awaits. Apply a result only to the matching current claim using compare-and-set.
Late or superseded results must not overwrite run state, authorize intents, replan
an already approved payment, or initiate execution. They still require accounting.

Track each physical HTTP attempt exactly once, including timeout, cancellation,
invalid JSON/schema, provider error and unknown usage. Persist reservations across
restart; unknown consumption must remain conservative. Isolate trackers per
workflow; configure bounded concurrency and bounded queueing separately from
per-workflow attempt limits. Advisory budgets remain estimates, not billing caps.

Client disconnect must have an explicit contract: either the claimed operation
finishes in a managed task, or cancellation is durably recorded with uncertain
provider usage. Do not automatically repeat an uncertain request after restart.
No transport change may replay a ledger mutation. Verification recovery remains
verification-only and deterministic failure retains priority over semantic PASS.
Approval snapshot/hash and backend-owned operation identities remain authoritative.

## Offline acceptance matrix

Use barriers/events and controlled HTTP transport to establish ordering; time
assertions are supplementary. No paid requests are required.

| Injection | Required observation |
|---|---|
| Hold plan/re-plan/review for 2 seconds | Same-worker `/health`, workflow polling and unrelated requests complete before release; latency target <250 ms on controlled local harness, with a recorded baseline |
| Concurrent workflows with distinct usage | No cross-workflow token/cost records, IDs or limits; failed attempts charged once |
| Concurrent same-run verification retries | One durable claim and one admitted inference; bounded attempts survive restart |
| Disconnect/cancel before send, after send, after response | Explicit durable state and conservative accounting; no orphan overwrite or unbounded retry |
| Timeout then late valid response | Superseded claim rejected by CAS; paid-attempt accounting retained; no execution from stale plan |
| Restart after claim, before result persistence | Recovery observes claimed attempt; no silent reset of reservation or retry budget |
| Restart after ledger COMMIT | Original operation reconciled; verification-only recovery; exactly one synthetic transaction |
| Concurrent approve/reject/replan and stale hash | At most one decision; exact approved snapshot executes; intent IDs preserved |
| 429/503, invalid schema/model, missing evidence | Same fail-closed behavior, sanitized errors, bounded backoff; health remains responsive |
| Queue saturation/shutdown | Bounded admission, explicit failure/recovery state; unrelated health remains available |
| Semantic PASS with deterministic FAIL | Final failure; no mutation retry |

Re-run current full pytest, Ruff/mypy, M4, maintained Docker, Chromium S1–S6 and
independent ledger/failure probes. Add repository tests for the above concurrency
boundaries, inspect durable claims and read ledger independently. Require a new
exact-SHA independent review before accepting F2; passing responsiveness alone
does not prove preservation of payment safety. Live validation, if later needed,
requires a separately authorized bounded plan.
