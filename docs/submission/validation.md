# Initial M5.4 packaging validation · 1 November 2026

This historical check applies to initial assembly commit
`cc39725e77ce12e9bba971709bc065c35d27fe42`, before the video follow-up.
Current video/export validation is recorded separately in
[validation-finish.md](validation-finish.md). Initial presentation/PDF/evidence
assets are preserved; older statements below describe that initial check.

Executed on 2026-11-01 (Asia/Qyzylorda). Scope: **documentation, archival
consistency and presentation rendering only**. No application test, Docker
workload/cleanup, model inference or paid API call was run during M5.4 assembly.
Historical M5.2 test outcomes are attributed separately in the
[evidence status](evidence-status.md).

## Identity and change boundary

Base/start SHA: `38cc3942dbd715681d59bfd893e47643210294a9`.
Branch: `m5.4/hackathon-submission`.
Worktree: `C:/Users/vanya/Projects/TrustFlow-context/m54-hackathon-submission/worktree`.
The initial package SHA is `cc39725e77ce12e9bba971709bc065c35d27fe42`;
its parent is the base above. Later documentation/video commits do not alter
that initial identity or extend its acceptance scope.

The patch is confined to root `README.md` and new `docs/submission/` artifacts.
All pre-existing files except the README retain their accepted Git blobs. The
original README checkout bytes are preserved in the evidence directory.
`src/`, `frontend/`, `tests/`, `scripts/`, Docker/Compose configuration and
dependency files have no changes against the base.

## Executed checks

| Check | Observed packaging result |
|---|---|
| Copied evidence provenance | 158 files / 9,456,875 bytes match source SHA-256 and lengths |
| Canonical completion/manifest chain | Both `COMPLETE.json` manifest hashes match; 107 manifest-listed artifact hashes match |
| Original screenshot inventory | 28 valid PNGs, 14 per canonical replay; original manifest hashes match |
| Normalized outcomes | Both digests independently recomputed as `a17849b150ae8207b5efa629ef4384176ce334666590bfcd3165e59de8e3a370` |
| Archived SQL consistency | Independent SQL JSON rows match copied ledger transactions and saved runs; four unique authorized operations per replay, exact payloads, balances and total agree |
| Approval binding in archived data | Plan hashes recomputed without application helpers; approved snapshot/hash matches; committed operation payloads match authorized intents |
| Documentation links | New authored local Markdown/HTML targets exist; verbatim historical text is excluded from this navigation check |
| Diagram | SVG parses as XML and renders in Chromium; Mermaid is supplied as editable source, without a separate Mermaid renderer check |
| Presentation | All nine scenes rendered and visually inspected; no overflow into footer; images loaded; keyboard navigation and all timed scene boundaries checked |
| Timing | Nine scenes total 120 seconds; narration count 250 words. Narration has not been recorded or timed with a speaker |
| PDF | Exported locally with Chromium; nine pages, valid PDF header |
| Browser | Chromium 145.0.7632.6; zero page errors or external requests while rendering the local presentation |
| Publication hygiene | No database/WAL/SHM or credentials files in new package; high-confidence key/private-key pattern scan found zero matches (a bounded pattern scan, not proof against every possible secret format) |
| Preservation | Original checkout HEAD/branch/status, two conversation-file hashes and all existing tags match the session baseline; copied source evidence hashes still match |
| Git whitespace / scope | Authored-document whitespace checks pass; archival evidence is exempt via `.gitattributes` to preserve its original bytes/line endings; implementation paths unchanged |

The diagnostic validator is outside the Git worktree:

```powershell
C:\Users\vanya\Projects\TrustFlow-context\m52-reproducible-demo\worktree\.venv\Scripts\python.exe C:\Users\vanya\Projects\TrustFlow-context\m54-hackathon-submission\validate_package.py
```

It uses Python 3.12.10 and the existing Playwright installation. Its final
machine-readable results and scene renders are retained under
`C:/Users/vanya/Projects/TrustFlow-context/m54-hackathon-submission/validation-output/`.
Initial rendering probes exposed slide overflow and Unicode label conversion;
the presentation assets were corrected and the full packaging check passed.
No application source or existing test was edited to obtain that result.

Additional Git checks run during finalization:

```powershell
git diff --check
git diff --cached --check
git diff --exit-code 38cc3942dbd715681d59bfd893e47643210294a9 -- src frontend tests scripts Dockerfile docker-compose.yml .dockerignore pyproject.toml
```

## Limits and pending acceptance

This check rereads archived evidence; it is not a new independent runtime review.
The full application suite, Docker replays, clean-machine setup and live Meta
inference were not rerun. The timed slide boundaries were exercised by advancing
the presentation clock, not by making an unreported two-minute recording.
Original report links point into the larger source archive; the curated evidence
guide explains omissions, including SQLite backups and opt-in run bundles.

M5.2 acceptance stays tied to its exact SHA and local fake-mode scope. Live
results, independent M5.3/M5.4 acceptance, a clean-machine M5.4 README run, the
finished video and externally accessible submission links remain open as listed
in the [checklist](checklist.md). The user-authorized M5.4 action is a local
documentation commit; no push, merge, deploy or accepted-tag change is included.
