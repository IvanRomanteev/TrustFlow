"""Read MP4 track metadata with the standard library; optionally fully decode it."""

import argparse
import json
import struct
import subprocess
from pathlib import Path


def boxes(data):
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        if size == 0:
            size = len(data) - offset
        if size < header or offset + size > len(data):
            raise ValueError("Malformed MP4 box")
        yield kind.decode("ascii"), data[offset + header : offset + size]
        offset += size


def child(data, kind):
    return next(payload for tag, payload in boxes(data) if tag == kind)


def duration(data):
    if data[0] == 0:
        scale, ticks = struct.unpack_from(">II", data, 12)
    else:
        scale, ticks = struct.unpack_from(">IQ", data, 20)
    return ticks / scale


def inspect(path):
    data = path.read_bytes()
    top = list(boxes(data))
    moov = next(p for k, p in top if k == "moov")
    tracks = []
    for kind, track in boxes(moov):
        if kind != "trak":
            continue
        media = child(track, "mdia")
        handler = child(media, "hdlr")[8:12].decode("ascii")
        table = child(child(media, "minf"), "stbl")
        codec = child(table, "stsd")[12:16].decode("ascii")
        count = struct.unpack_from(">I", child(table, "stsz"), 8)[0]
        record = {
            "handler": handler,
            "codec": codec,
            "samples": count,
            "duration_seconds": duration(child(media, "mdhd")),
        }
        if handler == "vide":
            width, height = struct.unpack_from(
                ">II", child(track, "tkhd"), len(child(track, "tkhd")) - 8
            )
            record.update(
                width=width // 65536, height=height // 65536, fps=count / record["duration_seconds"]
            )
        tracks.append(record)
    return {
        "duration_seconds": duration(child(moov, "mvhd")),
        "bytes": len(data),
        "tracks": tracks,
        "faststart": [k for k, _ in top].index("moov") < [k for k, _ in top].index("mdat"),
    }


def verify(path, ffmpeg):
    result = inspect(path)
    assert abs(result["duration_seconds"] - 120) < 0.001
    video = next(t for t in result["tracks"] if t["handler"] == "vide")
    assert (video["width"], video["height"], video["samples"], video["codec"]) == (
        1920,
        1080,
        2880,
        "avc1",
    )
    assert abs(video["fps"] - 24) < 0.001
    assert any(t["codec"] == "mp4a" for t in result["tracks"])
    assert any(t["codec"] == "tx3g" for t in result["tracks"])
    assert result["faststart"]
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-progress",
        "pipe:1",
        "-f",
        "null",
        "-",
    ]
    decoded = subprocess.run(command, capture_output=True, text=True, check=True)
    assert not decoded.stderr.strip(), decoded.stderr
    assert "progress=end" in decoded.stdout
    assert (
        int(
            [
                line.split("=", 1)[1]
                for line in decoded.stdout.splitlines()
                if line.startswith("frame=")
            ][-1]
        )
        == 2880
    )
    captions = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:s:0",
            "-f",
            "srt",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    result.update(
        full_av_decode=True,
        decoded_video_frames=2880,
        embedded_caption_cues=captions.count(" --> "),
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--ffmpeg")
    args = parser.parse_args()
    print(
        json.dumps(
            verify(args.video, args.ffmpeg) if args.ffmpeg else inspect(args.video), indent=2
        )
    )
