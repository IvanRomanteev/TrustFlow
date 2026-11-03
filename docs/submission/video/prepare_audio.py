"""Fit local SAPI cue audio to the existing nine-scene, 120-second timeline."""

import argparse
import hashlib
import json
import subprocess
import textwrap
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent


def wav_info(path):
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate(), audio.getparams()


def stamp(seconds, separator=","):
    millis = round(seconds * 1000)
    return (
        f"{millis // 3600000:02}:{millis // 60000 % 60:02}:"
        f"{millis // 1000 % 60:02}{separator}{millis % 1000:03}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ffmpeg", required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name in (
        "narration.wav",
        "captions.srt",
        "captions.vtt",
        "captions.ass",
        "audio-timing.json",
        "transcript.md",
    ):
        if (args.out / name).exists():
            raise SystemExit(f"Refusing to overwrite existing asset: {name}")
    timeline = json.loads((HERE / "timeline.json").read_text(encoding="utf-8"))
    sample_rate = 22050
    output = bytearray(round(timeline["duration_seconds"] * sample_rate) * 2)
    cues = []
    stats = []
    for scene in timeline["scenes"]:
        raw = [
            args.raw / f"scene-{scene['id']:02}-cue-{i + 1:02}.wav"
            for i in range(len(scene["narration"]))
        ]
        durations = [wav_info(p)[0] for p in raw]
        # Keep a deliberate 350ms lead-in and 450ms tail in every scene.
        available = scene["end"] - scene["start"] - 0.8
        speed = max(0.85, sum(durations) / available)
        if speed > 1.3:
            raise SystemExit(f"Scene {scene['id']} needs an excessive speaking rate: {speed:.3f}")
        position = round((scene["start"] + 0.35) * sample_rate)
        scene_start = position
        for number, (file, text) in enumerate(zip(raw, scene["narration"], strict=True), 1):
            fitted = args.raw / f"fitted-{scene['id']:02}-{number:02}.wav"
            command = [
                args.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-i",
                str(file),
                "-af",
                f"atempo={speed:.8f}",
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(fitted),
            ]
            subprocess.run(command, check=True, capture_output=True)
            with wave.open(str(fitted), "rb") as audio:
                assert (
                    audio.getframerate() == sample_rate
                    and audio.getsampwidth() == 2
                    and audio.getnchannels() == 1
                )
                data = audio.readframes(audio.getnframes())
            end_position = position + len(data) // 2
            assert end_position <= round(scene["end"] * sample_rate), (
                f"Scene {scene['id']} audio overflow"
            )
            output[position * 2 : end_position * 2] = data
            cues.append(
                {
                    "scene": scene["id"],
                    "start": position / sample_rate,
                    "end": end_position / sample_rate,
                    "text": text,
                }
            )
            position = end_position
        stats.append(
            {
                "scene": scene["id"],
                "raw_seconds": sum(durations),
                "tempo_factor": speed,
                "speech_start": scene_start / sample_rate,
                "speech_end": position / sample_rate,
            }
        )
    with wave.open(str(args.out / "narration.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(output)
    srt = []
    vtt = ["WEBVTT", ""]
    ass = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1920",
        "PlayResY: 1080",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00102026,&H00102026,"
        "0,0,0,0,100,100,0,0,3,2,0,2,70,70,24,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for number, cue in enumerate(cues, 1):
        text = "\n".join(textwrap.wrap(cue["text"], width=76))
        srt.extend([str(number), f"{stamp(cue['start'])} --> {stamp(cue['end'])}", text, ""])
        vtt.extend(
            [str(number), f"{stamp(cue['start'], '.')} --> {stamp(cue['end'], '.')}", text, ""]
        )

        def ass_stamp(value):
            centis = round(value * 100)
            return (
                f"{centis // 360000}:{centis // 6000 % 60:02}:"
                f"{centis // 100 % 60:02}.{centis % 100:02}"
            )

        ass_text = text.replace("\n", "\\N")
        ass.append(
            f"Dialogue: 0,{ass_stamp(cue['start'])},{ass_stamp(cue['end'])},"
            f"Default,,0,0,0,,{ass_text}"
        )
    for name, lines in [("captions.srt", srt), ("captions.vtt", vtt), ("captions.ass", ass)]:
        (args.out / name).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    record = {
        "voice": json.loads((args.raw / "voice.json").read_text(encoding="utf-8-sig")),
        "duration_seconds": 120,
        "sample_rate": sample_rate,
        "cues": cues,
        "scenes": stats,
        "narration_sha256": hashlib.sha256((args.out / "narration.wav").read_bytes()).hexdigest(),
    }
    (args.out / "audio-timing.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    transcript = [
        "# Final video transcript",
        "",
        "English local synthetic narration; montage of accepted static captures, "
        "not recorded UI footage.",
        "",
    ]
    for scene in timeline["scenes"]:
        transcript.extend(
            [
                f"## Scene {scene['id']} / {scene['start']}–{scene['end']} seconds",
                "",
                " ".join(scene["narration"]),
                "",
            ]
        )
    (args.out / "transcript.md").write_text("\n".join(transcript), encoding="utf-8", newline="\n")
    print(json.dumps({"cue_count": len(cues), "duration_seconds": 120, "scene_timing": stats}))


if __name__ == "__main__":
    main()
