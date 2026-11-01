# M5.2 reproducible local browser demo

TrustFlow demonstrates a planner, deterministic policy, human approval, synthetic
execution and independent verification. It uses fake model output, synthetic USD
balances and a local SQLite ledger. There are no real funds, banking integrations,
live Meta calls or public deployment. Fake-provider results demonstrate the local
workflow contracts; they do not measure live-model performance.

The implementation base is the independently accepted P2-05 commit
`d497043810073d53a6ebaa94ba550a4c2c6b9b08` on
`m5/p205-codex-remediation`. M5.2 has a separate developer gate and independent
acceptance scope. Historical BLOCKED reports in `docs/m5.md` and
`TrustFlow-context` remain evidence for their historical candidates.

## Install prerequisites once

Use Python 3.12 or later, Git, Docker Desktop (Linux containers) or a compatible
Docker Engine, and Docker Compose v2.24.4 or later with support for the YAML
`!override` tag. The daemon must be running and able to build Python 3.12 and
Node 22 images. The host browser is Playwright Chromium; Docker runs the backend
and frontend. Node/npm on the host is only needed for the separate frontend build
quality gate, not for replaying the Docker demo.

From the checkout root, Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,demo]"
python -m playwright install chromium
docker info
docker compose version
```

On Linux/macOS, activate the environment with `source .venv/bin/activate`; on a
Linux host that lacks browser system libraries, use
`python -m playwright install --with-deps chromium` as part of prerequisite
installation. The demo dependency is isolated in the `demo` optional dependency
group; production images do not install Playwright.

Initial dependency installation and Docker builds can fetch packages/base
images. Workflow execution uses `FakeLLMProvider` only. Do not add real API keys
or approval credentials to the command or evidence directory.

## One canonical replay command

After prerequisites are installed, execute this command from the checkout root:

```powershell
python scripts/run_m52_demo.py --out artifacts/m5.2/latest
```

No manual browser actions are required. The runner checks prerequisites, creates
a unique invocation below the supplied output directory, prepares an owned seed,
starts actual FastAPI and Next.js services, drives headless Chromium, checks API
and independent SQLite evidence, exercises recovery and cleans up owned
resources. It exits zero only after every mandatory scenario and evidence
publication succeeds. A missing browser or daemon is a FAIL, never a successful
skip. The terminal output identifies the actual invocation/evidence path.

The `latest` argument is a parent directory, not a mutable successful-run alias.
Every attempt gets its own child. A later failure cannot overwrite an earlier
successful invocation. `COMPLETE.json` appears only after the mandatory outcomes,
artifacts and cleanup have succeeded; a failed invocation retains
`FAILED_ATTEMPT.json` and the evidence collected before the failure.

To perform the second independent replay and compare it with a prior successful
invocation, use its printed directory:

```powershell
python scripts/run_m52_demo.py --out artifacts/m5.2/latest --compare artifacts/m5.2/latest/<first-successful-invocation>
```

Replace the placeholder with the actual path from the first run. The two
invocations use separate run identities, Compose projects, ports and persistence.
A comparison mismatch fails the second run and identifies differing normalized
evidence. One successful replay alone does not close the two-run reproducibility
gate.

## Seed and reset boundary

Each invocation exclusively creates a private workspace with an ownership marker
and recorded filesystem identities. Its backend mounts only that workspace's
`data` directory at `/data`. The seed uses the existing SQLite persistence schema
and a new `trustflow.db`, with these synthetic starting balances:

| Account | Initial USD |
|---|---:|
| `acc_alice` | 25000 |
| `acc_bob` | 1200 |
| `acc_ops` | 10000 |
| Total | 36200 |

The initial transaction table, workflow history and audit history are empty.
The seed's account supply differs intentionally from the application's ordinary
default seed so several independent HIGH-risk demos can execute in one replay.
The dataset identity is `m52-synthetic-v1`; it and the resulting balances are
recorded in the invocation evidence.
Transfers conserve the total 36200; no scenario mints synthetic funds.

Reset means creating another fresh invocation. Existing application/user SQLite
files and named Docker volumes are never reset or adopted. Seeding fails if the
target is not owned by the current run. No SQL status edits or ledger edits are
used to manufacture successful recovery outcomes.

## Six mandatory scenarios

| ID | Real interactions and independent expected evidence |
|---|---|
| S1 | Create a balance-check goal in the UI. Backend policy reports LOW risk; the run completes without approval, deterministic verification passes, and independent SQLite balances and transfer count stay unchanged. |
| S2 | Create a transfer of 5000 in the UI. Inspect HIGH risk and the exact 64-character `plan_hash`; reject missing/incorrect tokens and a wrong hash via the API, then approve the exact plan through the UI. The saved approval hash, approved snapshot, audit and single ledger transaction agree. A replay returns a conflict without another payment. A separate UI-created transfer of 1500 receives concurrent valid API approval attempts; exactly one succeeds and the loser conflicts, with only one ledger effect. |
| S3 | Create a separate HIGH-risk transfer of 3000 and reject it through the UI. SQLite persists `REJECTED`, audit records rejection, balances and transactions do not change, and a later approval cannot revive the rejected plan. Reload and display the authoritative rejected state. |
| S4 | Create the explicitly fault-injected LOW-risk transfer of 25. Hold its genuine mutating worker across the configured 0.15-second timeout, producing `EXECUTION_UNCERTAIN`; reconcile while held to demonstrate fail-closed behavior, then release it at an observable checkpoint and reconcile the persisted ledger identity. One semantic-review failure then produces `VERIFICATION_PENDING`; retry verification through the UI. Recovery observes the committed transaction; it does not call the mutating tool again or change its `tx_id`. |
| S5 | Check the UI's mandatory-evidence checkbox and submit the unmatched retrieval title `zzqqxxyy wwkknn`, description `zzqq wwkknn`. The real M2 retrieval/evidence contract identifies missing mandatory policy evidence and fails before execution. API/audit evidence explains the missing evidence, and SQLite proves zero unauthorized ledger mutation. This exercises the actual pre-execution state machine rather than forcing a chosen status. |
| S6 | Create a fresh HIGH-risk transfer and inspect its hash. A test-only hook pauses immediately after the real durable ledger commit and signals a deterministic checkpoint. The runner sends `SIGKILL` to its positively owned backend container, then starts that same container. Startup recovery reads SQLite; verification-only retry resolves the interrupted run without re-executing its succeeded operation. Its original `tx_id`, approved hash and snapshot survive; UI reload shows the persisted result. |

S4 and S6 are intentionally fault-injected experiments. Hooks live in the demo
package, not the production application, and call the real worker, ledger and
recovery implementation. They do not fabricate a crash by editing a status row.
S6 uses an actual forced process interruption inside the owned container; it is
not described as a graceful restart. Checkpoint observation, rather than a guessed
delay, determines when to interrupt it.

The browser uses accessible labels and roles for task creation, run selection,
approval inspection, token entry, approve/reject/recovery buttons, verification
and audit display. It reloads the document to check durable backend state. HTTP
requests add negative/concurrency assertions and independent confirmations; they
do not replace the required browser interactions.

## Evidence and reproducibility

The invocation directory contains `manifest.json`, `scenario-results.json`,
`ledger-before.json`, `ledger-after.json`, `audit-summary.json`, browser
screenshots, sanitized browser/runtime logs, exact commands with exit codes,
`environment.json`, `normalization-rules.json`, `normalized-results.json`, the
normalized digest in the manifest/completion marker and a replay `README.md`.
The manifest identifies the exact Git SHA and dirty state, fake provider mode,
seed, run/project identities, per-scenario assertions, artifact hashes and cleanup
outcome. Failed attempts retain actual failures and never receive a completion
marker.

Review screenshots in the invocation's `screenshots` directory:

| Scenario | Expected screenshot files |
|---|---|
| S1 | `S1-low-risk-verified.png` |
| S2 | `S2-awaiting-exact-hash.png`, `S2-approved-verified.png`, `S2-authoritative-refresh.png`, `S2-concurrent-one-winner.png` |
| S3 | `S3-rejected-authoritative.png` |
| S4 | `S4-uncertain-fault-injected.png`, `S4-verification-pending.png`, `S4-recovered-no-duplicate.png` |
| S5 | `S5-missing-mandatory-evidence.png` |
| S6 | `S6-before-crash-approval.png`, `S6-after-sigkill-recovered-pending.png`, `S6-durable-ui-reload.png`, `S6-after-graceful-restart.png` |

The approval password field is cleared before every
capture. Request headers, raw environment values, Playwright traces and HAR files
are not published. Browser logs retain page errors and failed network requests;
network failure during the explicitly declared backend interruption is labelled
as expected, while unrelated errors fail validation.

SQLite assertions use a separate connection and a consistent database read
transaction. They verify account totals, transaction counts, unique `tx_id` and
idempotency keys, workflow-to-transaction payload/identity bindings, approval
snapshots and append-only audit evidence. The runner does not copy a live DB
without its WAL and infer a verdict from an inconsistent file copy.

Reproducibility uses the versioned projection in `normalized-results.json`:
scenario verdicts and assertion names/outcomes; final workflow statuses, approval
counts and semantic-attempt counts; transfer payloads in transaction order and
the original deterministic `tx_id` values; balances; audit action counts per
workflow; verifier check names/outcomes; browser assertion names; screenshot
filenames; and resource-preservation/cleanup verdicts. Dynamic intent suffixes
in verifier check names map to `:<intent>` while their multiplicity is retained.
Concurrent request winner identity and audit event order are excluded, while the
required approval response multiset remains `[200, 409]`.

Random workflow/plan/operation UUIDs, invocation/project/container/image identities,
timestamps, dynamic ports and absolute paths are excluded. Raw plan hashes,
audit payload hashes and evidence snapshot identities are validated exactly
within each invocation, then excluded from cross-invocation comparison because
they derive from volatile identities. The recorded rules explain these choices;
raw per-invocation artifacts remain available. Screenshots and runtime timing are
evidence, not byte-identical cross-run outputs.

## Resource preservation and cleanup

The runner reuses the accepted P2-05 lifecycle in
`tests/integration/test_m5_docker_e2e.py`. It creates a fresh Compose override,
run/project ownership labels and private SQLite bind directory. It verifies
effective Compose configuration and actual container port bindings: backend and
frontend publish on `127.0.0.1` only, with matching frontend build-time
`NEXT_PUBLIC_API_URL` and backend `TRUSTFLOW_CORS_ORIGINS`.

Cleanup targets only recorded, positively verified container/image identities
and owned network resources. It never uses global prune, project-wide destructive
`compose down`, forced removal, mutable image-tag deletion or named-volume
removal. Host storage deletion uses the accepted identity checks and fixed
allowlist of regular SQLite/marker files, without recursive directory deletion.
Unknown ownership, unexpected files or inconclusive Docker metadata preserve the
resource and fail the attempt. Before/after Docker inventory and user SQLite
hashes provide independent preservation evidence.

If cleanup fails, inspect the recorded resource identity and cause in the failed
attempt. Do not run a global prune or delete an entire output tree to clear the
failure. A running or attached container prevents deletion of its SQLite storage.

## Maintained tests and quality gates

The default suite remains offline and does not require Docker or Playwright:

```powershell
python -m pytest -q
python -m ruff check src tests scripts
python -m mypy src
```

The expensive M5.2 gate is explicit and uses the complete canonical runner:

```powershell
$env:TRUSTFLOW_M52_DEMO_TESTS="1"
python -m pytest tests/integration/test_m52_demo.py -v
```

P2-05 keeps its original opt-in variable and skip semantics:

```powershell
$env:TRUSTFLOW_DOCKER_TESTS="1"
python -m pytest tests/integration/test_m5_docker_e2e.py -v
```

A skipped integration test is not an executed PASS. The final developer handoff
must also include two complete M5.2 executions, full default pytest, the accepted
P2-05 integration suite, Ruff, mypy, the frontend production build, Compose config,
CORS tests and the M4 offline evaluation with `--repeat 2 --ablations`. Consult the
exact candidate report for executed results; installing the runner alone does
not close these gates.

## Troubleshooting and limitations

- Docker unavailable: start Docker Desktop, wait for its Linux engine, then
  verify `docker info` and `docker compose version`. No existing user stack needs
  to be stopped. Missing Compose `!override` support requires updating Compose
  before replay.
- Browser unavailable: run `python -m playwright install chromium` using the
  same Python environment as the runner. Linux browser dependencies can be
  installed during prerequisite setup with `--with-deps`.
- Port collision: the attempt fails with the occupied binding instead of killing
  another process. Start a new invocation so fresh loopback ports are selected.
  A port selected before build cannot be atomically reserved across Docker start;
  the runner verifies actual resulting bindings and fails on startup conflicts.
- First build cannot fetch dependencies: check Docker registry/network access or
  pre-populate the package/base-image cache. Registry traffic during setup is
  distinct from prohibited live-model calls.
- An incomplete attempt is useful failure evidence. Inspect
  `FAILED_ATTEMPT.json`, command exit codes and sanitized runtime/browser logs;
  do not present it as a successful replay.

This is a trusted local, single-instance synthetic demo with a shared demo token
and SQLite persistence. It does not provide production AuthN/RBAC, real banking
APIs, multi-host guarantees or resistance to a malicious same-user process/Docker
administrator. Accepted ownership checks retain their documented final
check/use-window limitation; this demo does not claim general race-freedom.
The recovery tests cover the controlled checkpoints described above, not every
possible operating-system or storage crash. No live Meta quality claim or
independent milestone acceptance is implied by a developer PASS.

## English presenter walkthrough (about two minutes)

“An agent can propose a financial action, but a plausible answer is not proof
that the action was authorized or happened exactly once. TrustFlow makes those
questions observable. This is a local sandbox: every account and payment is
synthetic, and the model provider is deterministic fake mode.

“First, I create a balance check. The planner proposes the steps, but a separate,
deterministic policy engine decides risk. This read-only task finishes without a
human decision, and the verifier confirms that the ledger has not changed.

“Now I request a synthetic transfer of five thousand. Policy classifies it HIGH
risk and execution stops. The UI displays the exact plan hash and operation. My
approval authorizes this precise persisted snapshot. Missing credentials, a
different hash and a replay cannot authorize another payment.

“After approval, we see one committed transaction with a stable transaction ID.
The evidence report checks SQLite directly, rather than trusting a completed
label. The account total is conserved and the approval, audit and ledger agree.

“A separate rejected request stays rejected and produces no transfer. Concurrent
approval attempts have one winning decision. The recovery experiments hold a
real worker across a timeout and kill the owned backend at a controlled
post-commit checkpoint. On recovery, TrustFlow reconciles existing ledger
evidence and retries verification only. It keeps the original transaction ID;
it does not send another payment.

“Finally, mandatory evidence that cannot be retrieved fails closed. Every step
leaves audit and verification artifacts, with screenshots and a reproducibility
digest. One command repeats the full browser demo in isolated storage. These
are local workflow-safety results, not live-model performance or production
banking claims.”
