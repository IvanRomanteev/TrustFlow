# M5.4 video and submission finish validation · 3 November 2026

The starting candidate is `cc39725e77ce12e9bba971709bc065c35d27fe42` on
`m5.4/hackathon-submission`. The final candidate is one focused local commit on
that SHA, reported in the delivery message and obtained with `git rev-parse HEAD`.
Accepted runtime base remains `38cc3942dbd715681d59bfd893e47643210294a9`.

This is **targeted packaging and media validation**, not independent M5.4
acceptance or a new execution of M5.2. No application test, Docker lifecycle,
cleanup, live model/paid media call, merge, push, tag, deployment or submission
was executed.

## Completed deliverables and checks

| Check | Confirmed result |
|---|---|
| Existing presentation | Original nine-slide HTML/PDF, architecture and accepted evidence remain unchanged. The video renders the existing deck with temporary DOM-only crops/labels. |
| Evidence integrity | 158 provenance-listed copies and 107 canonical manifest artifact hashes match; all 28 original PNGs remain in the accepted bundles. |
| MP4 existence and hash | [trustflow-demo.mp4](video/trustflow-demo.mp4), 4,971,334 bytes; SHA-256 `e7c6330aa0483e7758dcfca8c60f60c08788b6afca593a0db216f997b12985f4` |
| Container / picture | 120.000 seconds; 1920×1080; H.264 (`avc1`); 24 fps; 2,880 frames; faststart metadata before media. |
| Audio / captions | AAC audio; local Microsoft David Desktop narrator; 30 burned/selectable English caption cues; SRT, WebVTT and transcript. AAC track has normal codec padding; container/browser/video duration is 120 seconds. |
| Real export verification | Two complete local exports produced the same MP4 SHA-256 in this environment. Both decoded all audio/video frames and yielded 30 extracted subtitle cues. This is not a promise of identical bytes across other encoders/fonts/OS versions. |
| Readability | Encoded detail frames for full hash, ledger checks and SIGKILL recovery were visually inspected; original evidence pixels are not retouched. The static-capture label stays visible. |
| Audio signal check | Decoded PCM peak −1.14 dBFS, RMS −18.65 dBFS, zero saturated samples. No human listening review is claimed. |
| Actual browser playback | Chromium 145.0.7632.6 loaded 1920×1080 / 120 seconds, played the MP4 and sought to the plan-hash scene. The WebVTT track loaded all 30 cues and exposed the matching active cue. |
| Local preview | Byte-range response and invalid-range refusal checked; browser seeking works. Parent-document links resolve through the submission-root preview. |
| Accessibility | English language/labels, native keyboard-focusable video controls, no autoplay, transcript and caption sidecars; no horizontal overflow at 375/1280/1920 px. |
| Links and text | Authored relative Markdown/HTML targets exist; local player navigation returns HTTP 200. Summary = 365 characters; full description = 2,246 characters. |
| Export refusal | An existing output directory with a sentinel user file is rejected; the file and directory contents remain unchanged. |
| Source and user data | Application/frontend/tests/scripts/Docker/dependencies and the M5.3 placeholder are unchanged. Other worktree file hashes and accepted tags match the session baseline. |
| New Python helpers | AST parsing and targeted Ruff check pass; only documentation/media tooling is added. |

The [media manifest](video/media-manifest.json),
[export manifest](video/export-manifest.json) and
[machine-readable QA report](validation-finish.json) retain the corresponding
hashes, stream metadata and packaging results.

## Commands and environment

Python 3.12.10 and existing Playwright Chromium 145.0.7632.6 were used.
FFmpeg 7.1 was extracted outside Git from the pinned
`imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl`; its wheel SHA-256 matched the PyPI
metadata: `02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a`.
No production dependency or existing Python environment was changed.
[Package source](https://pypi.org/project/imageio-ffmpeg/0.6.0/).

The local synthesis and assembly used the included
`video/synthesize_narration.ps1`, `video/prepare_audio.py` and
`video/export_video.py`. Actual temporary exports and diagnostics are retained
outside Git at
`C:/Users/vanya/Projects/TrustFlow-context/m54-hackathon-submission/finish-20261103-ff679ad5/`.

The final targeted command is reproducible with installed video prerequisites:

```powershell
python docs/submission/validate_finish.py --ffmpeg "C:\path\to\ffmpeg.exe" --out artifacts/m5.4-video/qa-1
python -m ruff check docs/submission/validate_finish.py docs/submission/video
git diff --check
```

The session used the existing interpreter at
`C:/Users/vanya/Projects/TrustFlow-context/m52-reproducible-demo/worktree/.venv/Scripts/python.exe`.
See [video/README.md](video/README.md) for the complete install, export and preview
commands. Initial lower-rate narration attempts did not fit the time budget;
the final rate-1 cues fit without truncation. Initial browser QA exposed lack of
byte-range support in the standard file server; the included loopback preview
fixed seeking and parent-document paths, then the checks passed.

## Remaining gates

Public organizer information was reviewed again on 2026-11-03; complete rules and
authenticated field/attachment limits are still unverified. The preparation
targets are explicitly owner-specified. The public repository and accepted M5.2
source are confirmed; the remote M5.4 branch and a public video URL are not
claimed. [Requirement check](official-requirements.md).

Final human listening, independent M5.4 review, the clean-machine README replay,
authorized publication/link verification, authenticated form review and manual
submission remain open. M5.3 live results remain unfilled pending actual evidence
and an independent verdict; no M5.3 merge was performed. See
[integration.md](integration.md).
