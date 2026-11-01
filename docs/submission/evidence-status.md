# Evidence status: accepted fake mode; live results pending

## Authority and provenance

The source of truth is **M5.2 FINAL INDEPENDENT ACCEPTANCE: PASS**, dated
2026-11-01 (Asia/Qyzylorda), preserved byte-for-byte as
[REVIEW.original.txt](evidence/m52/REVIEW.original.txt).
SHA-256: `1fa3f4ad1a6195ebdc53b7010d9154bb3921c44272387301bfbfa25fff01152f`.

| Identity | Accepted value |
|---|---|
| Candidate SHA | `38cc3942dbd715681d59bfd893e47643210294a9` |
| Tree | `e89064c2b5ae703db7663cb490bf7bc2372359ca` |
| Reviewed range | `d497043810073d53a6ebaa94ba550a4c2c6b9b08..38cc3942dbd715681d59bfd893e47643210294a9` |
| State during acceptance | Clean detached checkout |
| Inference | `FakeLLMProvider`; no live Meta calls |
| Environment | Windows 11; Python 3.12.10; Docker Engine 29.9.2; Compose 5.6.1; Playwright 1.58.0; Chromium 145.0.7632.6 |
| Scope | Local synthetic browser/ledger workflow and preserved P2-05 behavior |

The report closes all mandatory M5.2 gates with no demonstrated blocking finding
in that scope. It does not accept M5.3 or M5.4. Developer narratives are not used
to upgrade a claim. Packaging checks validate documentation and copies, not a
new implementation acceptance.

## M5.2 acceptance gates

Every row below is an **independently accepted fake-mode result**, not live-model
evaluation. [Machine-readable gates](evidence/m52/logs/acceptance-gates.json).

| Gate | Accepted observed behavior |
|---|---|
| S1 — read-only | LOW; COMPLETED; no approval or transfer; initial balances unchanged. |
| S2 — approval/race | HIGH pauses with full hash; missing/wrong token 401/403; wrong hash and duplicate 409; one 5,000 transfer. Separate concurrent 1,500 approval returns `[200,409]`, one decision/grant/transaction. |
| S3 — rejection | 3,000 request stays REJECTED after reload; no transfer; late approval 409. |
| S4 — late commit | Real worker held over 0.15 s timeout; EXECUTION_UNCERTAIN; late COMMIT `tx_0003`; explicitly injected fake semantic FAIL; verification-only retry completes with original operation/transaction. |
| S5 — evidence missing | Local retrieval returns zero hits for unmatched query; FAILED/retrieval_unavailable before plan/tools; no mutation. |
| S6 — SIGKILL | Genuine `tx_0004` COMMIT before saved success result; signal 9/exit 137; same container starts; VERIFICATION_PENDING → verification-only retry → COMPLETED; approved hash/snapshot and transaction retained. |

Inspect [scenario assertions](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/scenario-results.json),
[S4 held-worker evidence](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s4-held.json),
[S4 late COMMIT](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s4-late-commit.json),
[S6 checkpoint](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s6-at-crash-checkpoint.json)
and [fault journal](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/fault-journal.json).
S4/S6 use a test-only fault backend over the genuine executor/ledger/recovery
path; statuses are not fabricated by editing rows. S5 tests local retrieval
failure, not a bank or external policy service.

## Runs and independent ledger evidence

| Execution | Invocation ID | Accepted outcomes |
|---|---|---|
| Canonical #1 | `3cf94d5897b843e0bf751242513e2b1f` | 6/6 scenarios; 38 scenario assertions; 60 browser assertions; 14 PNG |
| Canonical #2 | `075897be048d47229697cab98e1d7de2` | Same counts; `--compare` equivalent=true |
| Opt-in first | `fa2265c3d5f243489307f1695a310dad` | Same counts, reported by independent acceptance |
| Opt-in second | `5bc6c982106e4127bd95ed741f68a68b` | Same counts, reported by independent acceptance |

The [first manifest](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/manifest.json),
[second manifest](evidence/m52/runs/replay-2/075897be048d47229697cab98e1d7de2/manifest.json)
and [comparison](evidence/m52/runs/replay-2/075897be048d47229697cab98e1d7de2/comparison.json)
are included. The opt-in bundles remain in the full source archive. All four
successful runs share normalized digest
`a17849b150ae8207b5efa629ef4384176ce334666590bfcd3165e59de8e3a370`.
The digest is not an authorization proof; raw hashes, snapshots and evidence
spans were checked separately within each run.

The independent reviewer used separate read-only SQLite connections and backups,
not only API statuses. Included SQL observations:
[replay #1](evidence/m52/independent-sqlite/replay-1/ledger-after/sqlite-readonly.json),
[replay #2](evidence/m52/independent-sqlite/replay-2/ledger-after/sqlite-readonly.json).
Database files are excluded from Git; the original archive retains the backups.

| Transaction per canonical replay | Synthetic USD | Purpose |
|---|---:|---|
| `tx_0001` | 5,000 | S2 exact-hash approval |
| `tx_0002` | 1,500 | S2 concurrent approvals |
| `tx_0003` | 25 | S4 late worker |
| `tx_0004` | 5,000 | S6 post-COMMIT crash |

Each transfers `acc_alice` → `acc_bob` with a unique operation key. Initial
Alice/Bob/Ops balances were 25,000/1,200/10,000; final balances were
13,475/12,725/10,000. Total: 36,200 before and after. S1/S3/S5 have no transfer.

## Other historical independent gates

| Check | Result in the M5.2 report |
|---|---|
| Default pytest | 502 passed, 19 skipped, zero failures/errors |
| M5.2 targeted | 60 passed |
| P2-05 Docker integration | 18 passed, zero skipped |
| M5.2 opt-in integration | 1 passed, zero skipped; two full replay subprocesses |
| Ruff / mypy / frontend build / Compose / CORS | Passed in the reviewed environment |
| M4 offline regression, two repeats + ablations | 33/33; critical 20/20; zero safety violations; reproducible=true |

See [test counts](evidence/m52/logs/test-counts.json),
[quality commands](evidence/m52/logs/quality-results.json),
[Docker results](evidence/m52/logs/docker-gate-results.json),
[M4 report](evidence/m52/m4-eval/evaluation-report.md) and
[repeats](evidence/m52/m4-eval/repeats.json). None measures live-model performance.

## Open gates and limits

| Gate | Status | Needed to close |
|---|---|---|
| M5.2 mandatory gates | CLOSED for exact accepted SHA/scope | Existing independent report |
| M5.3 offline readiness | Not independently accepted here | Exact candidate; offline fail-closed/schema/usage/concurrency evidence; independent verdict |
| Live Meta integration/evaluation | PENDING; NOT RUN in M5.4 | Explicit authorization; actual model/config; sanitized results; failed/successful attempt usage per workflow; ledger evidence; independent verdict |
| M5.4 README clean-machine setup | PENDING | Fresh environment execution of documented setup/replay with logs; inherited results do not replace this check |
| Independent M5.4 acceptance | PENDING | Review of final package commit and claims |
| Final repository/video/portal materials | PENDING | Authorized published URLs, narrated recording and current portal requirements |

Controlled late-worker/post-COMMIT faults were checked in one
Windows/Docker/Chromium environment. Container recreation, every crash schedule,
multi-host execution, production RBAC, real banking and malicious same-user or
Docker-administrator isolation are outside that evidence. The report retains
two unsuccessful preliminary observer attempts and explains their exclusion;
the four-run count includes only completed accepted executions.
[Original report](evidence/m52/REVIEW.original.txt).
