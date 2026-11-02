"""Export the existing M5.4 deck and accepted static evidence as a narrated MP4.

No app execution, Docker, model API, synthetic UI actions or publishing.
Requires Playwright Chromium and FFmpeg. Existing output directories are refused.
"""

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import wave
from pathlib import Path

from inspect_video import verify

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ffmpeg_path(explicit):
    if explicit:
        return str(Path(explicit).resolve())
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise SystemExit(
            "Provide --ffmpeg, add FFmpeg to PATH, or install imageio-ffmpeg==0.6.0"
        ) from exc


def inputs(timeline):
    presentation = (HERE / timeline["presentation"]).resolve()
    screenshots = (HERE / timeline["screenshot_root"]).resolve()
    published = json.loads((screenshots.parent / "manifest.json").read_text(encoding="utf-8"))
    assert published["git_sha"] == timeline["accepted_sha"]
    assert sum(s["end"] - s["start"] for s in timeline["shots"]) == 120
    assert timeline["shots"][0]["start"] == 0 and timeline["shots"][-1]["end"] == 120
    for left, right in zip(timeline["shots"], timeline["shots"][1:], strict=False):
        assert left["end"] == right["start"]
    sources = []
    for path in sorted(screenshots.glob("*.png")):
        digest = sha(path)
        assert digest == published["artifact_hashes"]["screenshots/" + path.name]
        sources.append({"path": path.relative_to(PACKAGE).as_posix(), "sha256": digest})
    for shot in timeline["shots"]:
        if shot["view"] == "detail":
            path = screenshots / shot["source"]
            width, height = struct.unpack(">II", path.read_bytes()[16:24])
            x, y, w, h = shot["rect"]
            assert x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= width and y + h <= height
    return presentation, screenshots, sources


def render(timeline, out, presentation, screenshots):
    from playwright.sync_api import sync_playwright

    errors = []
    blocked = []
    record = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        version = browser.version
        page = browser.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
        page.on("pageerror", lambda e: errors.append(str(e)))

        def route(request):
            if request.request.url.startswith(("file:", "data:", "about:")):
                request.continue_()
            else:
                blocked.append(request.request.url.split(":", 1)[0])
                request.abort()

        page.route("**/*", route)
        for number, shot in enumerate(timeline["shots"], 1):
            page.goto(presentation.as_uri(), wait_until="load")
            page.evaluate("i => manual(i)", shot["scene"] - 1)
            page.add_style_tag(
                content="""
                .controls{display:none!important}.viewport{inset:0 0 128px!important}
                .deck{transform:scale(1.025)!important}.mode{font-size:17px}
                .zoom-area{height:565px;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#edf2f6}
                .zoom-window{position:relative;overflow:hidden;flex-shrink:0}
                .zoom-window img{max-width:none!important;object-fit:fill!important}
                .zoom-caption{height:34px!important;font-size:18px!important;line-height:1.2!important;padding:8px!important}
            """
            )
            page.locator(".slide.active .mode").evaluate(
                "e=>e.textContent='STATIC CAPTURES · M5.2 · FAKE · SYNTHETIC'"
            )
            if shot["view"] == "detail":
                image_path = screenshots / shot["source"]
                iw, ih = struct.unpack(">II", image_path.read_bytes()[16:24])
                x, y, w, h = shot["rect"]
                scale = min(1064 / w, 561 / h)
                crop = {
                    "uri": image_path.as_uri(),
                    "iw": iw * scale,
                    "ih": ih * scale,
                    "width": w * scale,
                    "height": h * scale,
                    "left": -x * scale,
                    "top": -y * scale,
                    "label": "EDITORIAL ZOOM / " + shot["label"],
                }
                page.locator(".slide.active .images").evaluate(
                    """(container,c)=>{
                    container.replaceChildren();
                    const figure=document.createElement('figure');figure.className='capture';
                    const area=document.createElement('div');area.className='zoom-area';
                    const win=document.createElement('div');win.className='zoom-window';
                    win.style.width=c.width+'px';win.style.height=c.height+'px';
                    const img=document.createElement('img');img.src=c.uri;img.alt=c.label;
                    const dims={width:c.iw,height:c.ih,left:c.left,top:c.top};
                    for(const [key,value] of Object.entries(dims)){
                        img.style.setProperty(key,value+'px','important');
                    }
                    win.append(img);area.append(win);figure.append(area);
                    const caption=document.createElement('figcaption');
                    caption.className='zoom-caption';caption.textContent=c.label;
                    figure.append(caption);container.append(figure);
                }""",
                    crop,
                )
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.slide.active img'))"
                ".every(i=>i.complete&&i.naturalWidth>0)"
            )
            frame = out / f"shot-{number:02}.png"
            page.screenshot(path=str(frame))
            record.append({**shot, "frame": frame.name, "frame_sha256": sha(frame)})
            print(
                f"Rendered shot {number}/{len(timeline['shots'])}: {shot['start']}-{shot['end']}s",
                flush=True,
            )
        browser.close()
    assert not errors and not blocked, {"page_errors": errors, "external_requests": blocked}
    return record, {"chromium": version, "page_errors": 0, "external_requests": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, required=True, help="New output directory; never overwritten"
    )
    parser.add_argument("--ffmpeg")
    parser.add_argument(
        "--assets", type=Path, default=HERE, help="Matching narration/captions/audio-timing bundle"
    )
    args = parser.parse_args()
    encoder = ffmpeg_path(args.ffmpeg)
    out = args.out.resolve()
    if out.exists():
        raise SystemExit("Output must not exist; prior exports and user files are preserved")
    out.mkdir(parents=True)
    try:
        timeline = json.loads((HERE / "timeline.json").read_text(encoding="utf-8"))
        audio_timing = json.loads((args.assets / "audio-timing.json").read_text(encoding="utf-8"))
        assert sha(args.assets / "narration.wav") == audio_timing["narration_sha256"]
        with wave.open(str(args.assets / "narration.wav"), "rb") as audio:
            assert audio.getnframes() / audio.getframerate() == 120
        presentation, screenshots, sources = inputs(timeline)
        frames, render_info = render(timeline, out, presentation, screenshots)
        for name in ("captions.srt", "captions.vtt", "captions.ass"):
            shutil.copyfile(args.assets / name, out / name)
        shutil.copyfile(args.assets / "narration.wav", out / "narration.wav")
        concat = ["ffconcat version 1.0"]
        for shot in frames:
            concat.extend([f"file '{shot['frame']}'", f"duration {shot['end'] - shot['start']}"])
        concat.append(f"file '{frames[-1]['frame']}'")
        (out / "frames.ffconcat").write_text(
            "\n".join(concat) + "\n", encoding="utf-8", newline="\n"
        )
        target = out / "trustflow-demo.mp4"
        command = [
            encoder,
            "-hide_banner",
            "-nostdin",
            "-n",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            "frames.ffconcat",
            "-i",
            "narration.wav",
            "-i",
            "captions.srt",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:s:0",
            "-vf",
            "fps=24,ass=captions.ass",
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-t",
            "120",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-threads",
            "4",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=eng",
            "-metadata:s:s:0",
            "title=English captions",
            "-map_metadata",
            "-1",
            "-metadata",
            "title=TrustFlow: accepted M5.2 static evidence demonstration",
            "-metadata",
            "comment=Static Chromium screenshots with editorial zooms; "
            "synthetic funds; fake inference; no live UI footage.",
            "-movflags",
            "+faststart",
            target.name,
        ]
        print(
            "Encoding 120-second H.264/AAC MP4 with burned and selectable English captions",
            flush=True,
        )
        encoded = subprocess.run(command, cwd=out, capture_output=True, text=True)
        (out / "encode.log").write_text(encoded.stderr, encoding="utf-8", newline="\n")
        if encoded.returncode:
            raise RuntimeError(f"FFmpeg failed: exit {encoded.returncode}; inspect encode.log")
        checked = verify(target, encoder)
        assert checked["embedded_caption_cues"] == len(audio_timing["cues"])
        summary = {
            "status": "VERIFIED_LOCAL_MP4",
            "media_kind": timeline["media_kind"],
            "accepted_sha": timeline["accepted_sha"],
            "presentation_sha256": sha(presentation),
            "timeline_sha256": sha(HERE / "timeline.json"),
            "video_sha256": sha(target),
            "narration_sha256": sha(out / "narration.wav"),
            "render": render_info,
            "video": checked,
            "sources": sources,
            "shots": frames,
            "encoder": subprocess.check_output([encoder, "-version"], text=True).splitlines()[0],
            "command": ["<local-ffmpeg>", *command[1:]],
            "paid_model_calls": 0,
            "publishing": False,
        }
        (out / "export-manifest.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(
            json.dumps(
                {
                    "status": summary["status"],
                    "video": str(target),
                    "sha256": summary["video_sha256"],
                    "probe": checked,
                }
            ),
            flush=True,
        )
    except Exception as exc:
        (out / "FAILED_EXPORT.json").write_text(
            json.dumps({"status": "FAILED", "error": type(exc).__name__, "detail": str(exc)})
            + "\n",
            encoding="utf-8",
        )
        raise


if __name__ == "__main__":
    main()
