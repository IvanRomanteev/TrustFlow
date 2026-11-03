# Final integration: provenance, validation and boundaries

Prepared 2026-11-04 for the owner-authorized GitHub release. Publication is only
complete when the PR merge and remote `main` ref are verified. Intended branch:
`release/hackathon-2026-final`; repository: [IvanRomanteev/TrustFlow](https://github.com/IvanRomanteev/TrustFlow).
The PR conversation carries the exact final candidate SHA and actual gate results.

## Source histories

| Source | Exact commit | Scope |
|---|---|---|
| Accepted M5.2 | `38cc3942dbd715681d59bfd893e47643210294a9` | Independently accepted synthetic fake-mode browser/ledger demo |
| M5.3 offline foundation | `53394d48c67827abfdffa53407e524af6af92610` | PASS WITH APPROVED EVIDENCE-PRESERVATION EXCEPTION, `M53-E1-EXCEPTION-01` |
| Integrated M5.3 | `c48c2affd3dda652e00aca9f690404c82182b694` | Foundation plus bounded Spark diagnostics; one successful paid read-only plan is separately evidenced |
| Integrated M5.4 | `b0558e8da93b3b5ce94d722d0de3fca8348ce1e4` | Submission package, archived evidence, slides, narrated MP4 and export tools |
| Fetched public main | `6b3ab06a6712edb95aec3797883f643034441465` | Unique README clarification: synthetic financial data only |

Both source branches descend from accepted M5.2. M5.3 and M5.4 are combined by
a genuine two-parent merge; current public main is a second genuine merge.
Its single README conflict is resolved by keeping the M5.4 landing-page
structure, strengthening the synthetic-only boundary and correcting setup and
live authentication. No accepted commit is amended, tag moved or force update used.
The final branch preserves all three histories.

## Historical acceptance is exact-SHA evidence

The [original M5.2 report](../submission/evidence/m52/REVIEW.original.txt),
archived manifests, captures and read-only ledger observations retain their
original bytes. Its 502 passed / 19 skipped suite and four browser replays are
historical results, not automatic acceptance of this merge.

The M5.3 review initially blocked on preservation of three historical XML
originals. Those originals remain unrecovered. The owner approved exactly
`M53-E1-EXCEPTION-01` for the offline foundation commit/tree; the disposition is
**PASS WITH APPROVED EVIDENCE-PRESERVATION EXCEPTION**, not unconditional PASS.

- [Original independent review](evidence/M53-REVIEW.original.txt).
- [Original owner decision](evidence/M53-OWNER-DECISION.original.txt).
- [Original acceptance addendum](evidence/M53-ACCEPTANCE-ADDENDUM.original.txt).
- [Copy hashes and provenance](evidence/provenance.json).

These are byte-exact archival text copies, including historical relative link
text; the source evidence directories are preserved locally. The later
diagnostic commit and this integrated release do not inherit that exact-SHA
disposition. New local gates validate their own candidate; this integration
assignment is not a separate independent milestone acceptance review.

## Runtime and safety preservation

The integrated application source, tests, frontend, Docker definitions and demo
scripts remain byte-equivalent in Git to M5.3 `c48c2af`. M5.4 changes the README
and adds submission documents/media. Release additions update public copy and
record provenance; no synthetic ledger, policy, approval, executor, recovery or
tool semantics are changed during final integration.

M5.3 retains strict schemas, typed/sanitized response failures, F1 nested JSON
recursion handling, H1 HTTP-date `Retry-After`, failed-call usage, bounded HTTP
attempts/cost, fake default and one-request diagnostics. The successful Spark
case is planning-only: [live results](../submission/m53-live-results.md),
[public report projection](evidence/spark-planning.json).

## Mandatory release gates

Run against the committed final candidate before any push/PR merge. The local
release evidence directory retains actual argv, exit codes, stdout/stderr,
warnings, skipped counts, candidate/tree IDs and hashes. The PR body records
observed results; this list is a requirement, not a claim of execution.

| Gate | Required check |
|---|---|
| Python suite | Full `python -m pytest -q`; report skips and warnings |
| M5.3 | Four focused MockTransport/diagnostic test files, including F1/H1 regressions; offline all-profile harness |
| P2-05 | Opt-in `tests/integration/test_m5_docker_e2e.py` using owned resources |
| Chromium | Actual M5.2 runner with archived normalized comparison, real timeout/SIGKILL and ledger assertions |
| M4 | 33 scenarios, `--repeat 2 --ablations` |
| Static/frontend | Ruff, mypy, `npm ci`, Next.js production build |
| Compose | Fake/live config rendered with a placeholder credential and null env file; loopback publications only |
| Documentation | Root/submission local links, provenance hashes, accepted replay manifests, fields and media |
| MP4 | File/hash, 120 seconds, 1920×1080, 24 fps H.264/AAC, subtitle track and complete A/V decode |
| Security | Credential/secret and private-file scan without printing values; dependency advisory audit |
| Git/preservation | Final diff, exact ancestry, accepted tags, original worktrees/user files, historical reports and foreign Docker/SQLite preservation |

Original M5.4 validators intentionally pin their historical branch and runtime
base. They remain archived unchanged. Release verification checks their
underlying evidence/media contracts on the integrated tree without pretending
that their M5.4-only branch assertions apply to a new merge.

## Media and submission status

The [MP4](../submission/video/trustflow-demo.mp4) is the existing accepted-screenshot
montage, not live Muse footage. The presentation, narration, subtitles, transcript,
export scripts and original manifests remain unchanged. The video is a normal
Git blob, not a Git LFS pointer. [Media manifest](../submission/video/media-manifest.json).

The current [submission fields](../submission/submission-fields.json) and
[copy-ready text](../submission/submission-text.md) distinguish the real single
Spark plan from fake-mode evidence. Their GitHub targets become published only
after remote verification. Old packaging documents describe their dated local
snapshot; they do not override the owner authorization for this release.

The owner still needs to verify authenticated organizer rules/form, eligibility,
track/team, actual field/video-host requirements, final submission and receipt.
No organizer submission or third-party video-host upload is performed here.

## Existing dependency risk

`frontend/package.json` pins Next.js **15.3.4**, React/React DOM **19.1.0**.
These versions predate multiple published React Server Components/Next.js
security fixes, including the critical RSC remote-code-execution advisory
**CVE-2025-55182 / CVE-2025-66478**, subsequent denial of service and source-code
exposure advisories. Applicability depends on the packaged Next.js server and
enabled routes; this release does not claim exploit testing or remediation.
The current npm advisory audit is retained with the release checks.

Primary references checked 2026-11-04:
[Next.js RCE advisory](https://nextjs.org/blog/CVE-2025-66478),
[Next.js subsequent security update](https://nextjs.org/blog/security-update-2025-12-11),
[React follow-up advisories](https://react.dev/blog/2025/12/11/denial-of-service-and-source-code-exposure-in-react-server-components).

Keep the prototype local on loopback; do not deploy it publicly. A dependency
upgrade needs a separately validated patch and is outside this integration.
No paid Meta request, public service deployment, broad upgrade, Docker prune,
foreign resource deletion, accepted-tag change or original-worktree reset is
authorized or required by this release.
