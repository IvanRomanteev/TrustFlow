"""Read-only package/media checks, plus an owned loopback preview for playback QA."""

import argparse
import array
import ast
import functools
import hashlib
import importlib.util
import json
import math
import re
import subprocess
import threading
import wave
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

PACKAGE = Path(__file__).resolve().parent
WS = PACKAGE.parent.parent
VIDEO = PACKAGE / "video"
START = "cc39725e77ce12e9bba971709bc065c35d27fe42"
RUNTIME = "38cc3942dbd715681d59bfd893e47643210294a9"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def git(*args, cwd=WS):
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("src", "href", "poster") and value:
                self.targets.append(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit("QA output directory must be new")
    args.out.mkdir(parents=True)
    report = {"checked_on": "2026-11-03", "start_sha": START, "runtime_base": RUNTIME}
    head = git("rev-parse", "HEAD")
    assert head == START or git("rev-parse", "HEAD^") == START
    assert git("branch", "--show-current") == "m5.4/hackathon-submission"
    protected = [
        "src",
        "frontend",
        "tests",
        "scripts",
        "Dockerfile",
        "docker-compose.yml",
        "pyproject.toml",
        "docs/submission/evidence",
        "docs/submission/architecture.md",
        "docs/submission/architecture.svg",
        "docs/submission/architecture.mmd",
        "docs/submission/presentation.html",
        "docs/submission/presentation.pdf",
        "docs/submission/m53-live-results.md",
    ]
    subprocess.run(
        ["git", "-C", str(WS), "diff", "--exit-code", START, "--", *protected],
        check=True,
        capture_output=True,
    )
    report["protected_code_presentation_evidence_m53_unchanged"] = True
    provenance = read(PACKAGE / "evidence/provenance.json")
    for item in provenance["files"]:
        assert sha(PACKAGE / item["path"]) == item["sha256"]
    count = 0
    for relative in [
        "replay-1/3cf94d5897b843e0bf751242513e2b1f",
        "replay-2/075897be048d47229697cab98e1d7de2",
    ]:
        root = PACKAGE / "evidence/m52/runs" / relative
        manifest = read(root / "manifest.json")
        assert sha(root / "manifest.json") == read(root / "COMPLETE.json")["manifest_sha256"]
        for name, digest in manifest["artifact_hashes"].items():
            assert sha(root / name) == digest
            count += 1
    report.update(evidence_files=len(provenance["files"]), accepted_manifest_hashes=count)
    fields = read(PACKAGE / "submission-fields.json")
    assert len(fields["summary"]) <= 500 and 1500 <= len(fields["full_description"]) <= 2500
    text = (PACKAGE / "submission-text.md").read_text(encoding="utf-8")
    assert fields["summary"] in text and fields["full_description"] in text
    assert fields["demo_video_url"].startswith("PENDING")
    assert fields["verified_live_m53_results"].startswith("PENDING")
    report.update(
        summary_characters=len(fields["summary"]),
        full_description_characters=len(fields["full_description"]),
    )
    local_links = 0
    authored = [WS / "README.md", *PACKAGE.glob("*.md"), *VIDEO.glob("*.md")]
    for path in authored:
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            parts = urlsplit(target.strip("<>"))
            if parts.scheme or not parts.path:
                continue
            assert (path.parent / unquote(parts.path)).exists(), (path.name, target)
            local_links += 1
    for path in [PACKAGE / "presentation.html", VIDEO / "index.html"]:
        links = Links()
        links.feed(path.read_text(encoding="utf-8"))
        for target in links.targets:
            parts = urlsplit(target)
            assert not parts.scheme, target
            assert (path.parent / unquote(parts.path)).exists(), target
            local_links += 1
    report["authored_local_links"] = local_links
    timeline = read(VIDEO / "timeline.json")
    timing = read(VIDEO / "audio-timing.json")
    assert len(timeline["scenes"]) == 9 and len(timeline["shots"]) == 19
    assert sum(s["end"] - s["start"] for s in timeline["shots"]) == 120
    assert [c["text"] for c in timing["cues"]] == [
        text for s in timeline["scenes"] for text in s["narration"]
    ]
    prior = 0
    for cue in timing["cues"]:
        assert prior <= cue["start"] < cue["end"] <= 120
        prior = cue["end"]
    assert (VIDEO / "captions.srt").read_text(encoding="utf-8").count(" --> ") == 30
    assert (VIDEO / "captions.vtt").read_text(encoding="utf-8").count(" --> ") == 30
    assert sha(VIDEO / "narration.wav") == timing["narration_sha256"]
    with wave.open(str(VIDEO / "narration.wav"), "rb") as audio:
        assert audio.getnframes() / audio.getframerate() == 120
    for path in [*VIDEO.glob("*.py"), PACKAGE / "validate_finish.py"]:
        ast.parse(path.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("video_inspector", VIDEO / "inspect_video.py")
    inspector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inspector)
    report["video"] = inspector.verify(VIDEO / "trustflow-demo.mp4", args.ffmpeg)
    assert sha(VIDEO / "trustflow-demo.mp4") == read(VIDEO / "export-manifest.json")["video_sha256"]
    report["video_sha256"] = sha(VIDEO / "trustflow-demo.mp4")
    assets = read(VIDEO / "media-manifest.json")
    for item in assets["files"]:
        assert sha(VIDEO / item["path"]) == item["sha256"], item["path"]
    report["media_assets_hashed"] = len(assets["files"])
    pcm = subprocess.run(
        [
            args.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(VIDEO / "trustflow-demo.mp4"),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "22050",
            "-f",
            "s16le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    samples = array.array("h", pcm)
    peak = max(abs(s) for s in samples)
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    assert peak < 32767 and rms > 300
    report["audio"] = {
        "peak_dbfs": round(20 * math.log10(peak / 32768), 2),
        "rms_dbfs": round(20 * math.log10(rms / 32768), 2),
        "clipped_samples": 0,
    }
    # An existing export directory must survive a refused invocation unchanged.
    refusal = args.out / "existing-output"
    refusal.mkdir()
    sentinel = refusal / "user-file.txt"
    sentinel.write_bytes(b"preserve this file")
    before = sha(sentinel)
    result = subprocess.run(
        [
            __import__("sys").executable,
            str(VIDEO / "export_video.py"),
            "--out",
            str(refusal),
            "--ffmpeg",
            args.ffmpeg,
        ],
        capture_output=True,
    )
    assert result.returncode != 0 and sha(sentinel) == before
    assert list(refusal.iterdir()) == [sentinel]
    report["existing_output_refused_and_preserved"] = True
    from playwright.sync_api import sync_playwright

    preview_spec = importlib.util.spec_from_file_location(
        "video_preview", VIDEO / "preview_video.py"
    )
    preview = importlib.util.module_from_spec(preview_spec)
    preview_spec.loader.exec_module(preview)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(preview.RangeFiles, directory=str(PACKAGE))
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    errors = []
    external = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            report["chromium"] = browser.version
            page = browser.new_page(viewport={"width": 1280, "height": 1000})
            page.on("pageerror", lambda e: errors.append(str(e)))
            origin = f"http://127.0.0.1:{server.server_port}"

            def route(request):
                if request.request.url.startswith(origin + "/"):
                    request.continue_()
                else:
                    external.append(request.request.url.split(":", 1)[0])
                    request.abort()

            page.route("**/*", route)
            page.goto(origin + "/video/", wait_until="load")
            page.wait_for_function("document.querySelector('video').readyState>=2")
            assert page.locator("html").get_attribute("lang") == "en"
            assert page.locator("video").get_attribute("aria-label")
            assert page.locator("video").get_attribute("autoplay") is None
            partial = page.request.get(
                origin + "/video/trustflow-demo.mp4", headers={"Range": "bytes=32-95"}
            )
            assert partial.status == 206
            assert partial.body() == (VIDEO / "trustflow-demo.mp4").read_bytes()[32:96]
            invalid = page.request.get(
                origin + "/video/trustflow-demo.mp4", headers={"Range": "bytes=999999999-"}
            )
            assert invalid.status == 416
            for target in page.locator("nav a").evaluate_all("a=>a.map(e=>e.getAttribute('href'))"):
                assert page.request.head(urljoin(page.url, target)).status == 200
            media = page.locator("video").evaluate(
                "v=>({duration:v.duration,width:v.videoWidth,height:v.videoHeight})"
            )
            assert media == {"duration": 120, "width": 1920, "height": 1080}
            page.locator("video").focus()
            assert page.evaluate("document.activeElement.tagName") == "VIDEO"
            page.evaluate("document.querySelector('track').track.mode='hidden'")
            page.wait_for_function("document.querySelector('track').track.cues?.length===30")
            page.locator("video").evaluate("async v=>{v.muted=true;await v.play()}")
            page.wait_for_timeout(400)
            assert page.locator("video").evaluate("v=>v.currentTime") > 0
            page.locator("video").evaluate("v=>{v.pause();v.currentTime=30;}")
            page.wait_for_function("!document.querySelector('video').seeking")
            page.wait_for_function("document.querySelector('track').track.activeCues?.length>0")
            assert "plan version" in page.evaluate(
                "document.querySelector('track').track.activeCues[0].text"
            )
            page.screenshot(path=str(args.out / "player-desktop.png"))
            for width in [375, 1280, 1920]:
                page.set_viewport_size({"width": width, "height": 1000})
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.set_viewport_size({"width": 375, "height": 900})
            page.screenshot(path=str(args.out / "player-mobile.png"), full_page=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
    assert not errors and not external
    report["player"] = {
        "duration_seconds": 120,
        "actual_playback": True,
        "seeking": True,
        "http_byte_ranges": True,
        "webvtt_cues": 30,
        "keyboard_focus": True,
        "responsive_widths": [375, 1280, 1920],
        "page_errors": 0,
        "external_requests": 0,
    }
    if args.baseline:
        baseline = read(args.baseline)
        changes = []
        for checkout in baseline["worktrees"]:
            directory = Path(checkout["path"])
            if directory == WS:
                for name, digest in checkout["files"].items():
                    if name.startswith("docs/submission/evidence/") or name in {
                        "docs/submission/presentation.html",
                        "docs/submission/presentation.pdf",
                        "docs/submission/architecture.md",
                        "docs/submission/architecture.svg",
                        "docs/submission/architecture.mmd",
                        "docs/submission/m53-live-results.md",
                    }:
                        assert sha(directory / name) == digest, name
                continue
            for name, digest in {**checkout["files"], **checkout["untracked"]}.items():
                if not (directory / name).is_file() or sha(directory / name) != digest:
                    changes.append(str(directory / name))
        assert not changes, (
            "Changes observed outside M5.4; inspect baseline before attributing them"
        )
        assert git("show-ref", "--tags") == baseline["tags"]
        report["other_worktree_user_files_and_tags_preserved"] = True
    report["status"] = "PASS"
    (args.out / "checks.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
