#!/usr/bin/env python3
"""Download the MediaPipe Pose Landmarker model used by the service."""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

DEFAULT_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_full/float16/1/pose_landmarker_full.task"
)
MIN_BYTES = 1_000_000


def download(url: str, output: Path, *, force: bool = False) -> int:
    if output.exists() and output.stat().st_size >= MIN_BYTES and not force:
        print(f"model already present: {output} ({output.stat().st_size} bytes)")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "posture-reminder/0.1"})
    try:
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            temporary.open("wb") as handle,
        ):
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        print(f"download failed: {exc}", file=sys.stderr)
        return 1
    size = temporary.stat().st_size
    if size < MIN_BYTES:
        temporary.unlink(missing_ok=True)
        print(f"downloaded file is too small ({size} bytes)", file=sys.stderr)
        return 1
    os.replace(temporary, output)
    print(f"downloaded {url} -> {output} ({size} bytes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="models/pose_landmarker_full.task",
        help="destination path",
    )
    parser.add_argument("--url", default=os.environ.get("POSTURE_MODEL_URL", DEFAULT_URL))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    return download(args.url, Path(args.output), force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
