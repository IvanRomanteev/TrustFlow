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

Final execution results and exact candidate binding will be recorded after the
quality gates complete. No new live/paid API requests are part of these gates.

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
