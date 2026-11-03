# Final demo video and local export

The local [trustflow-demo.mp4](trustflow-demo.mp4) is a verified **120-second,
1920×1080, 24 fps H.264/AAC MP4**, approximately 5 MB, with 30 English caption
cues. It has burned captions and an embedded selectable English subtitle track.
An [accessible local player](index.html), [WebVTT](captions.vtt), [SRT](captions.srt)
and [complete transcript](transcript.md) are included.

This is a **narrated montage of actual accepted M5.2 static Chromium captures**,
with editorial crops/zooms. It is not recorded live UI footage. The export reuses
the existing nine-slide `presentation.html`; it does not rebuild that deck or
the PDF. The original screenshots, presentation, architecture and PDF remain
unchanged. Source PNG hashes are checked against the accepted replay manifest
before rendering. No screenshot pixels are retouched in the evidence archive.

No MP4/WebM/MOV/MKV source footage was found in the M5.2 implementation, independent
acceptance or existing M5.4 artifact roots during the targeted inventory. The
acceptance report also states that browser video/trace/HAR was not published.
Accordingly, the voice introduction and every scene identify the static media.
Edits do not simulate clicks, a moving cursor, an approval event or a new run.

## Edit and audio

The [timeline](timeline.json) defines nine scenes and 19 shots, including ten
detail crops for the full plan hash, approval, original `tx_0001`, ledger checks,
the late worker, SIGKILL recovery, preserved approval hash and original `tx_0004`.
The continuous original-operation identity is supported by the archived ledger
evidence; the captions do not use screenshots alone to prove it.

English narration was synthesized locally with **Microsoft David Desktop**,
Windows `System.Speech` / SAPI, rate 1. Sentence-level WAV cues were fitted to
the existing scene windows with pitch-preserving FFmpeg tempo adjustment.
No voice clone, external TTS/model API, music-generation service or paid call
was used. It is a local synthetic narrator, not a claimed human voice recording.
The [audio timing](audio-timing.json) records the voice, sample boundaries and
tempo factors. The committed [narration.wav](narration.wav) removes the need for
Windows TTS when re-exporting on another machine.

Final listening and form review before publication are still recommended.
Objective audio checks and full decode are documented in
[finish validation](../validation-finish.md); no unperformed human listening
review is claimed.

## Re-export locally

From the repository root, install video tools in a separate environment once:

```powershell
python -m venv .venv-video
.\.venv-video\Scripts\Activate.ps1
python -m pip install -r docs/submission/video/requirements-export.txt
python -m playwright install chromium
```

On Linux/macOS, activate with `source .venv-video/bin/activate`; install browser
system dependencies if required. Initial package/browser installation can use
the network. Encoding itself uses local assets and blocks external browser
requests. Video tooling is separate from `pyproject.toml` and production images.

One command produces a new verified export from the existing deck and assets:

```powershell
python docs/submission/video/export_video.py --out artifacts/m5.4-video/reexport-1
```

The directory must not already exist. Existing exports/user files are never
overwritten. FFmpeg is resolved from `--ffmpeg`, PATH, or `imageio-ffmpeg`.
To use an installed executable explicitly, append
`--ffmpeg "C:\path\to\ffmpeg.exe"`. Output includes MP4, original frame renders,
caption sidecars, encoder log and `export-manifest.json`. A failure retains
`FAILED_EXPORT.json`; do not call that a successful video.

The exporter parses MP4 tracks, verifies 120 seconds / 2,880 frames, decodes the
entire audio/video stream and extracts the subtitle track. A successful result
is `VERIFIED_LOCAL_MP4`. To verify the committed file separately:

```powershell
python docs/submission/video/inspect_video.py docs/submission/video/trustflow-demo.mp4 --ffmpeg "C:\path\to\ffmpeg.exe"
```

The complete targeted package check, including a temporary owned loopback
player, can be run from the repository root:

```powershell
python docs/submission/validate_finish.py --ffmpeg "C:\path\to\ffmpeg.exe" --out artifacts/m5.4-video/qa-1
```

Use a new output path for each attempt. This validates media and artifacts;
it does not run TrustFlow, Docker or inference.

Playback/export outcomes and timing are reproducible; identical MP4 bytes across
different encoders, font stacks or operating systems are not promised. The
included [export manifest](export-manifest.json) records this machine's encoder,
source/frame/video hashes and probe result. Its frame filenames refer to
regenerable export intermediates, retained in the local export directory rather
than duplicated in Git. [Media hashes](media-manifest.json) cover committed assets.

## Optional local narration regeneration

The shipped WAV and captions are sufficient for normal export. To regenerate
the same script locally on Windows, use new directories:

```powershell
$ffmpegPath = python -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/submission/video/synthesize_narration.ps1 -Out artifacts/m5.4-video/raw-1
python docs/submission/video/prepare_audio.py --raw artifacts/m5.4-video/raw-1 --out artifacts/m5.4-video/audio-1 --ffmpeg "$ffmpegPath"
python docs/submission/video/export_video.py --assets artifacts/m5.4-video/audio-1 --out artifacts/m5.4-video/export-1
```

`-ExecutionPolicy Bypass` applies only to that PowerShell process and the included
local synthesis script; it does not change the user's persistent execution
policy. The selected voice must exist locally. Preparation refuses to replace
existing generated assets. Use matching WAV/caption/timing bundles; replacing
the narration alone can desynchronize captions.

## Preview and publication boundary

For the local player, WebVTT track, parent-document links and MP4 seeking, run
the included byte-range-capable loopback server:

```powershell
python docs/submission/video/preview_video.py --port 8765
```

Open `http://127.0.0.1:8765/video/` and stop the server when finished. It serves
the submission directory so links to evidence and other documents resolve.
This serves local
artifacts only; it does not run TrustFlow or deploy a public site. Captions are
already burned into the picture; the optional WebVTT track need not be enabled
simultaneously. Download the SRT for a video-host caption upload.

The local file exists and is verified. **Public demo video URL: PENDING.**
No video hosting, public deployment, Git push or submission was performed.
The 120-second target comes from the project owner, not a verified portal rule.
Live Meta M5.3 results remain [pending](../m53-live-results.md).
