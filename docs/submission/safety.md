# Safety contracts and recovery

These explanations describe the accepted source and the controlled local results
in the [independent M5.2 report](evidence/m52/REVIEW.original.txt). They do not
extend acceptance to live inference, real financial systems or every crash
schedule. [Results and open gates](evidence-status.md).

## Hash-bound approval

The Planner proposes a versioned `Plan`. The deterministic `PolicyEngine`
classifies risk; a model cannot classify its own payment as safe enough to
bypass backend policy. For HIGH risk, the workflow waits at `AWAITING_APPROVAL`.
An approval request must pass the API token gate and match the current
`plan.plan_hash`. The decision records the plan version and hash; the service
deep-copies the approved plan into `approved_plan_snapshot` and stores
`approved_hash`. It authorizes exactly that snapshot's payment intents and
payloads. Repeated decisions and stale hashes are rejected.

The hash binds content; it is not a signature or an identity system. Before a
mutation, Executor checks the backend-owned intent, its authorized payload
fingerprint and its relationship to the approved plan. A different plan or
payload needs the applicable authorization; a new planning step does not create
permission. The shared demo token remains a local-demo boundary.

Sources: [approval service](../../src/trustflow/agents/approval.py),
[executor](../../src/trustflow/agents/executor.py),
[engine](../../src/trustflow/orchestration/engine.py),
[API](../../src/trustflow/api/app.py).
Accepted S2: wrong hash `409`; missing/wrong token `401/403`; duplicate `409`;
concurrent approvals `[200,409]`, one decision/grant/transaction. S3: rejection
persists and late approval returns `409`.

## Idempotency and payment identity

A logical payment has a backend-owned intent/operation identity. That identity
is the transfer's idempotency key, independent of an LLM's phrasing or a new step
number. The intent records the proposed and authorized payload and authorization
history. Re-planning must preserve that obligation or surface a conflict; it
cannot silently launder a changed payment into the original grant.

`PersistentLedger` uses `BEGIN IMMEDIATE` for idempotency lookup, balance changes
and transaction insertion in one transaction. A unique `idempotency_key` is the
storage backstop. An existing key with the same payload returns its committed
transaction; a different payload conflicts. The executor first reconciles an
existing commit, preserving the original `tx_id`.

Sources: [engine](../../src/trustflow/orchestration/engine.py),
[SQLite ledger](../../src/trustflow/persistence/ledger.py).
M5.2's observed evidence is narrower than all possible re-planning paths: S2
exercises replay/concurrency, and S4/S6 retain their original operation and
transaction through recovery. The report independently maps each of four ledger
rows to its authorized workflow/intent. This does not establish a general
distributed exactly-once guarantee.

## Evidence grounding and deterministic priority

The Verifier independently reads the ledger. It matches operation keys, source
and destination accounts, amount, currency and transaction ID, checks every
required authorized intent has exactly one matching commit, and checks total
balance conservation. A tool's success message or an API `COMPLETED` label alone
does not establish that a transfer was authorized or happened once.

The semantic reviewer receives goal, plan and separately constructed ledger
evidence. Evidence spans bind source text, hashes, snapshot identities and exact
offsets. Retrieved policy text is evidence, not authority to execute a tool.
Mandatory evidence missing at retrieval fails before execution. Deterministic
failure has priority; semantic PASS cannot repair it. The semantic provider in
the accepted replay is fake, including the explicitly injected S4 review failure.

Sources: [deterministic verifier](../../src/trustflow/agents/verifier.py),
[semantic reviewer](../../src/trustflow/agents/semantic.py),
[evidence models](../../src/trustflow/retrieval/models.py).
The independent reviewer also recomputed hashes and evidence offsets and read
SQLite through a separate read-only connection. These checks support the local
evidence contract, not a claim that an LLM cannot hallucinate.

## Timeout, late commit and SIGKILL

Cancelling an await does not stop a worker thread. S4 holds a genuine transfer
worker past the 0.15-second demo timeout. The result becomes
`EXECUTION_UNCERTAIN`; reconciliation while the worker is alive does not launch
another transfer. After release, the original worker commits `tx_0003`.
Reconciliation uses that ledger identity. An injected fake semantic failure
leaves `VERIFICATION_PENDING`; verification-only retry completes the workflow
without another mutating invocation.

S6 pauses after genuine SQLite COMMIT of `tx_0004`, while the persisted run is
still `EXECUTING` and the successful transfer result has not been saved. The
runner sends SIGKILL to its owned backend (signal `9`, exit `137`) and starts
that same container. Startup recovery identifies interrupted runs, moves them to
`VERIFICATION_PENDING` and reconciles committed operations. The UI's
verification-only retry completes using the same approved snapshot, hash,
operation and `tx_id`. A separate graceful restart/reload is also checked.

Sources: [startup recovery](../../src/trustflow/persistence/recovery.py),
[engine recovery paths](../../src/trustflow/orchestration/engine.py),
[test-only fault backend](../../scripts/m52/fault_backend.py).
This is same-container kill/start evidence. Container recreation, lost storage,
all filesystem failures and all interleavings are untested here. Unresolved or
uncommitted work can remain pending or fail closed at the attempt limit; recovery
does not promise eventual successful payment.

## Bounded work, usage and observability

Settings bound steps, re-plans, retries, timeouts and semantic attempts. Current
defaults include `max_steps=20`, `max_retries=2`,
`semantic_max_attempts=3` (including the initial review), a 20,000-token workflow
budget and live transport attempts `1 + llm_max_retries` (default 3).
These are source configuration values, not measured live behavior.

Usage trackers are scoped to workflows; provider error paths retain attempt
records and avoid silent fake fallback. Audit events store hashed payloads;
browser logs, ledger snapshots and manifests expose the decision path. Payload
hashes support consistency checks; they are not an externally anchored audit
signature resistant to a database administrator.

Sources: [settings](../../src/trustflow/core/settings.py),
[usage](../../src/trustflow/llm/usage.py),
[provider](../../src/trustflow/llm/provider.py),
[audit](../../src/trustflow/audit/logger.py).
Live budget behavior, unknown provider usage, latency, concurrency and
failed-attempt accounting remain explicit M5.3 evidence requirements. No live
cost is inferred from zero-cost fake calls.
