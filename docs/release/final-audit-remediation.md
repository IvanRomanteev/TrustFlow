# Final audit remediation — 5 November 2026

Base: `56f9a4e4bacbee7e688f08223b6a8d9929fd4354` (submitted public main).
Branch: `remediation/final-audit-20261105`. This is a separate local candidate;
publication, merge, tags and changes to the submitted version require owner approval.

## Scope

- **F1:** Next.js `15.3.4` → `15.5.29`; React/React DOM `19.1.0` → `19.1.10`.
  Next-scoped `postcss=8.5.32` and `sharp=0.35.5` replace vulnerable transitive
  versions. Next 15 is retained to avoid an unrelated framework major migration.
  The generated `next-env.d.ts` route-type reference accompanies Next's update.
- **F3:** [sanitized M5.5 evidence](../submission/evidence/m55/README.md), offline
  extractor/verifier and corruption tests. No raw SQLite, credentials, request
  bodies, reasoning or account-owner data is added. Original evidence is retained.
- **F4:** runtime handling of FastAPI string/array details and HTTP fallback;
  empty/whitespace Title rejected before network activity. Node regression tests
  and a separate opt-in production Chromium test cover real 422 multi-field errors.
- **F5:** PowerShell uses the virtual environment's `python.exe` directly and
  documents `npm.cmd`; no ExecutionPolicy relaxation. Linux/macOS instructions
  retain shell activation and `python`/`npm` conventions.
- **F2 remains open:** synchronous provider architecture is unchanged. See the
  [separate design and offline test plan](f2-async-provider-plan.md).

No backend application source, existing tests, Docker lifecycle code, historical
evidence, media or deployment settings are modified.

## Security evidence

The original independent audit recorded 1 critical and 2 high vulnerable
dependencies at the base. Updating direct dependencies alone left PostCSS and
sharp advisories (and the inherited Next dependency finding). The explicit
overrides replace their vulnerable implementations; no advisory ignore list,
audit threshold reduction, `--force` upgrade or disabled security gate is used.

Resolved transitive advisories observed during this remediation:

- PostCSS: GHSA-qx2v-qp2m-jg93, GHSA-6g55-p6wh-862q,
  GHSA-fxqj-rqcc-2cmp, GHSA-r28c-9q8g-f849.
- sharp: GHSA-f88m-g3jw-g9cj, GHSA-rgj7-g3m4-5g8c,
  GHSA-wq5f-xc86-pv6w.

Upstream references: [Next October security release](https://nextjs.org/blog/nextjs-security-update-october-14-2026),
[Next 15.5.29](https://github.com/vercel/next.js/releases/tag/v15.5.29),
[React RSC advisory](https://github.com/react/react/security/advisories/GHSA-xqc2-xf4c-x3h3).
An audit with zero findings covers known registry advisories at execution time,
not universal security or production readiness. The native sharp override is a
minor-version change; validate Linux container builds as well as Windows build.

## Validation commands

Run from the repository root with a fresh venv and installed Chromium. Docker
gates use unique projects/ports and ownership-checked cleanup; run them serially
so their preservation inventories do not overlap with another test's resources.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check --no-respect-gitignore src tests scripts
.\.venv\Scripts\python.exe -m mypy src
.\.venv\Scripts\python.exe -m trustflow.evaluation --out artifacts/remediation-m4 --repeat 2 --ablations
.\.venv\Scripts\python.exe scripts/m55_public_evidence.py
$env:TRUSTFLOW_DOCKER_TESTS="1"
.\.venv\Scripts\python.exe -m pytest tests/integration/test_m5_docker_e2e.py -q
$env:TRUSTFLOW_M52_DEMO_TESTS="1"
.\.venv\Scripts\python.exe -m pytest tests/integration/test_m52_demo.py -q
$env:TRUSTFLOW_FRONTEND_TESTS="1"
.\.venv\Scripts\python.exe -m pytest tests/integration/test_frontend_validation.py -q
```

From `frontend`: `npm.cmd ci`, `npm.cmd test`, `npm.cmd run build`,
`node node_modules/typescript/bin/tsc --noEmit --incremental false`,
`npm.cmd audit --json`. Linux/macOS use `.venv/bin/python` and `npm` equivalents.

## Executed results

Clean implementation candidate: `a12fef5e3b91c3f81b75aa053bd0c93039a539aa`.
The subsequent report commit changes only this document; application, dependencies,
verifier and tests retain the tested Git blobs. The final SHA and patch hash are
recorded in the separately retained `FINAL_REPORT.md` / `release-manifest.json`
to avoid a self-referential commit hash.

| Gate | Observed result |
|---|---|
| Full pytest | **665 passed, 20 skipped, 33 warnings**; opt-in skips exercised separately below |
| Maintained Docker tests | **18 passed**, 7 expected/deprecation warnings; executed on the same runtime/test files before the implementation commit |
| Maintained Chromium reproducibility test | **1 passed**, encompassing two complete **S1–S6 6/6** replays; equal normalized digest, cleanup and preservation PASS |
| New F4 production Chromium test | **1 passed**: empty/whitespace Title sends no POST; real multi-field HTTP 422 displays field/message; valid creation and reload work |
| Fresh-clone PowerShell quickstart | **PASS** under explicit `ExecutionPolicy Restricted`: new venv, pip install, Chromium install, Docker prerequisites, **S1–S6 6/6** replay |
| Frontend Node regression tests | **15 passed**, including empty Title validation details, arrays, malformed/non-JSON responses and unchanged success |
| Ruff / mypy | PASS; mypy: 53 source files |
| Production build / TypeScript | PASS after clean `npm.cmd ci`; Linux Node 22 builds also exercised in Docker/browser gates |
| npm audit | **0 vulnerabilities**, full audit including development dependencies; no remaining reported advisories |
| M4 | **33/33**, two repeats; critical **20/20**, safety violations **0**; ablations generated |
| M5.3 offline transport matrix | PASS, 27 MockTransport attempts; **not live calls** |
| Independent audit failure probes | **15/15 + 4/4** copied probes passed; only temporary root/source path adaptation where needed |
| M5.5 public evidence | 11 calls, 11,042/11,299 tokens, $0.073201; exact retained-source projection and separate audit-verifier crosscheck PASS |
| M5.5 corruption tests | **11 passed** within full pytest; optimized `python -O` verifier explicitly rejected |
| Public-only fresh checkout | Manifest/verifier PASS with `core.autocrlf=true`; verifier LF bytes fixed by `.gitattributes` |
| Native dependency smoke check | Windows `sharp=0.35.5`, libvips `8.18.7`: PNG generation PASS |
| Baseline preservation | **55,966 protected files unchanged** across original checkout, audit and historical context; original Git state and Docker/host/container SQLite snapshots identical |

Chromium: `145.0.7632.6`, Playwright `1.58.0`; host Python `3.12.10`, Node
`24.22.1`, Docker Engine `29.9.2`, Compose `5.6.1`. Full dependency versions and
actual argv/cwd/exit codes are retained with logs outside the repository at
`TrustFlow-remediation-20261105/logs/`. Historical audit files are not overwritten.

The three replay invocation IDs are `4d9263c40c6e4bc7a15e3ec632ceae4c`,
`372df4232a8841c7bf699c79f5753fd6`, and `32737442714d464abd7610b8ccf59154`.
The first two normalized results match:
`a17849b150ae8207b5efa629ef4384176ce334666590bfcd3165e59de8e3a370`.
The public M5.5 projection was also regenerated into a separate directory and
compared byte-for-byte: SHA-256
`62870bf2f2221fb238536b4cd61cc0a4ce488e2fee3828b1aed5f9967d44ca75`.
Each includes independent SQLite observations, real late-worker timeout and
post-COMMIT SIGKILL, concurrency/duplicate decisions and verification-only recovery.
The separate failure probes also cover stale/corrupt approved snapshots, operation
collisions, attempt limits, failed-provider usage and budget exhaustion. No backend
application source changed; no regression was observed within these boundaries.

Intermediate failures are retained, not counted as final passes: direct dependency
upgrade still reported vulnerable transitives; new Python files initially failed
Ruff formatting; direct local clone hit Git ownership protection (replaced by a
local bundle clone without changing global Git configuration); external diagnostic
wrappers encountered Windows encoding/quoting issues and were corrected. These
were followed by the successful checks above. No security bypass was applied.

## Limits

No new paid/provider requests, exploit attempts, production deployment or remote
publication occurred. Retained live records are not fresh model observations or
provider-signed receipts. Native Linux/macOS quickstart was not separately run;
Linux container build/runtime was exercised, and Windows Restricted quickstart
was executed in a new local clone/venv. The full suite still emits known lifecycle
and dependency deprecation warnings. A green suite does not establish exhaustive
concurrency correctness or independently accept the complete M5.5 milestone.

The preservation inventory excludes tool/dependency caches, virtual environments
and `.git` internals. Git worktree registration/new local commits necessarily
change shared Git metadata; the original checkout's HEAD, branch, tracked files
and two untracked conversation files remain unchanged. Existing Docker inventory
(2 containers, 31 images, 4 networks, 1 volume), three host SQLite files and three
container SQLite files match the pre-test snapshot. Historical audit/source/media
files are covered by the protected-file hash comparison.

## Release boundary

F3's public delivery gate closes only when the owner approves publication and the
sanitized package is actually accessible from the public branch/PR. Full live
Wonder planning, the mixed-model matrix and full independent M5.5 acceptance remain
open. Account-level usage figures remain developer-supplied and unverified.

The [official event page](https://dev.meta.ai/events/global-hackathon), checked for
this remediation, states November 4, 21:00 ET: **November 5, 07:00 Asia/Qyzylorda**.
Organizer changes and the authenticated portal's rules for editing an already
submitted entry must be checked by the owner. This local patch does not alter that
entry or establish permission to replace it after a deadline.
