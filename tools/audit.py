#!/usr/bin/env python3
"""One-shot camera placement audit.

Usage::

    tools/audit.py --url rtsp://user:pass@192.168.2.133:8554/camera

Prints the report and exits 0 when the placement passes, 1 when it fails and
2 when the stream/model could not be used at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from posture.audit import AuditAccumulator, AuditObservation  # noqa: E402
from posture.capture import RtspSource, redact_rtsp_url  # noqa: E402
from posture.config import Settings  # noqa: E402
from posture.frames import frame_is_usable  # noqa: E402
from posture.metrics import diagnostics_from_landmarks  # noqa: E402
from posture.pose import ModelUnavailable, PoseEstimator  # noqa: E402

LOGGER = logging.getLogger("posture.audit")


def collect(url: str, seconds: float, model: Path, settings: Settings) -> AuditAccumulator:
    accumulator = AuditAccumulator(settings)
    estimator = PoseEstimator(model)
    source = RtspSource(url)
    started = time.monotonic()
    if not source.open():
        estimator.close()
        raise RuntimeError(f"cannot open RTSP stream {redact_rtsp_url(url)}")
    try:
        first_deadline = started + 15.0
        got_frame = False
        while time.monotonic() - started < seconds:
            frame = source.read()
            if frame is None:
                if not got_frame and time.monotonic() < first_deadline:
                    time.sleep(0.05)
                    continue
                break
            got_frame = True
            height, width = frame.shape[:2]
            if not frame_is_usable(frame, settings.min_gray_std):
                continue
            landmarks = estimator.detect(frame, int(time.monotonic() * 1000))
            diagnostics = diagnostics_from_landmarks(landmarks, width, height)
            accumulator.add(AuditObservation.from_diagnostics(datetime.now(), diagnostics))
        if not got_frame:
            raise RuntimeError("RTSP stream produced no frames")
    finally:
        source.close()
        estimator.close()
    return accumulator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("POSTURE_RTSP_URL", ""))
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument(
        "--model",
        type=Path,
        default=Path(os.environ.get("POSTURE_MODEL_PATH", "models/pose_landmarker_full.task")),
    )
    parser.add_argument("--json", action="store_true", help="JSON only")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.url:
        print("audit error: --url or POSTURE_RTSP_URL is required", file=sys.stderr)
        return 2
    settings = Settings.from_env()
    model = args.model
    if not model.is_absolute() and not model.exists():
        model = PROJECT_ROOT / model
    try:
        accumulator = collect(args.url, args.seconds, model, settings)
    except (RuntimeError, ModelUnavailable, OSError) as exc:
        print(f"audit error: {exc}", file=sys.stderr)
        return 2

    report = accumulator.report(min_duration_seconds=args.seconds * 0.8)
    payload = report.as_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"camera audit: {'PASS' if report.passed else 'FAIL'}")
        print(f"  view: {report.view}")
        print(f"  duration: {report.duration_seconds:.1f}s over {report.frames} frames")
        print(
            f"  detection: {report.detection_rate:.0%}  "
            f"shoulders: "
            + ("n/a" if report.shoulder_width_pct is None else f"{report.shoulder_width_pct:.1f}%")
            + "  hip visible: "
            + ("n/a" if report.hip_visible_rate is None else f"{report.hip_visible_rate:.0%}")
        )
        print(
            f"  head_fwd jitter: {report.head_fwd_jitter_deg:.2f} deg  "
            f"5s range: {report.head_fwd_range_5s_deg:.1f} deg"
        )
        for name, passed in report.checks.items():
            print(f"  [{'x' if passed else ' '}] {name}")
        for reason in report.reasons:
            print(f"  - {reason}")
    if report.frames == 0:
        return 2
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
