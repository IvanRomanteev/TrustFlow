# Storyboard · 120 seconds

All screenshot links below point to **unedited real Chromium PNGs** from the
independent canonical replay `3cf94d5897b843e0bf751242513e2b1f`, accepted commit
`38cc3942dbd715681d59bfd893e47643210294a9`. No image is generated or retouched.
The [source manifest](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/manifest.json)
contains their original SHA-256 hashes; [provenance](evidence/provenance.json)
binds copies to the acceptance archive.

Narration: [demo-script.md](demo-script.md). Slides: [HTML](presentation.html),
[PDF](presentation.pdf). The screenshot supports the visible state; numerical
safety claims also require raw ledger/scenario evidence, not pixels alone.

| Scene / time | Visual and staging | Caption / focus | Evidence beyond the screenshot |
|---|---|---|---|
| 1 · 00:00–00:12 | Title beside [S1 verified](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/screenshots/S1-low-risk-verified.png). Keep sandbox banner visible. | “Was it authorized? Did it happen once?” | [S1 ledger](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s1-after.json): zero transfers. |
| 2 · 00:12–00:25 | [Architecture SVG](architecture.svg). Trace Planner → Policy → Approval → Executor → Ledger → Verifier. | “Model proposes. Backend authorizes. Ledger proves.” Live-provider path visibly pending. | [Source map](architecture.md); no claim of live invocation. |
| 3 · 00:25–00:43 | [S2 awaiting exact hash](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/screenshots/S2-awaiting-exact-hash.png). Hold on version, full hash and 5,000 payload. | “Approve this exact snapshot.” | [S2 invalid attempts](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s2-invalid.json); HTTP checks in [scenarios](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/scenario-results.json). |
| 4 · 00:43–00:55 | [S2 approved](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/screenshots/S2-approved-verified.png). Mention separate [concurrent result](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/screenshots/S2-concurrent-one-winner.png). | “One decision. One matching transfer.” Concurrency claim is a separate 1,500 request. | [Concurrent ledger](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s2-concurrent.json), response multiset `[200,409]`. |
| 5 · 00:55–01:06 | [S3 rejected](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/screenshots/S3-rejected-authoritative.png). | “Rejection survives reload.” | [Late approval ledger](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s3-late-approve.json): no S3 transfer. |
| 6 · 01:06–01:22 | Pair [S4 uncertain](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/screenshots/S4-uncertain-fault-injected.png) and [S4 recovered](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/screenshots/S4-recovered-no-duplicate.png). Use original-image links for readable detail. | “Controlled fault: late worker + injected fake review failure.” Highlight original `tx_0003`. | [Held worker](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s4-held.json), [late commit](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s4-late-commit.json), [after](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s4-after.json). |
| 7 · 01:22–01:41 | Pair [S6 after SIGKILL](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/screenshots/S6-after-sigkill-recovered-pending.png) and [S6 durable reload](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/screenshots/S6-durable-ui-reload.png). | “Controlled post-COMMIT SIGKILL. Same container. Verify only.” Follow `tx_0004`, exact hash and snapshot. | [Crash checkpoint](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s6-at-crash-checkpoint.json), [journal](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/fault-journal.json), [independent lifecycle checks](evidence/m52/logs/lifecycle-and-visual-checks.json). |
| 8 · 01:41–01:51 | [S5 missing evidence](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/screenshots/S5-missing-mandatory-evidence.png). | “Required evidence absent → no execution.” | [Before](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s5-before.json), [after](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/ledger-s5-after.json): unchanged ledger. |
| 9 · 01:51–02:00 | Command card and evidence summary; accepted/pending labels side by side. | `python scripts/run_m52_demo.py --out artifacts/m5.2/latest` · “Live Muse results: PENDING.” | [Acceptance report](evidence/m52/REVIEW.original.txt), [COMPLETE](evidence/m52/runs/replay-1/3cf94d5897b843e0bf751242513e2b1f/COMPLETE.json), [M5.3 template](m53-live-results.md). |

The full 14-screenshot inventory for each canonical replay is retained. Scenes
select from it; omitted images remain available as supporting evidence. No
screen claims that recovery reverses a committed transfer or that replaying a
request creates a new authorization.
