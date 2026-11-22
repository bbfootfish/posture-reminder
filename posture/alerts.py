"""Optional, local-only alert snapshots.

The service default is **no images**.  When the operator explicitly enables
``POSTURE_ALERT_IMAGES=1``, only the frame that triggers a reminder is
downscaled, JPEG-encoded and written under ``data/alerts``; files older than
the configured retention window (25 h by default) are removed.  Nothing is
uploaded and no continuous video is kept.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


def save_alert_image(
    frame: Any,
    path: Path,
    *,
    max_width: int = 960,
    quality: int = 80,
) -> bool:
    """Write one downscaled JPEG; return False on any failure.

    OpenCV is imported lazily so serving the API and unit tests do not require
    the camera stack.
    """
    if frame is None:
        return False
    try:
        import cv2
    except ImportError:  # pragma: no cover - Docker always has mediapipe/cv2
        LOGGER.warning("OpenCV is not installed; alert image not saved")
        return False
    try:
        height, width = frame.shape[:2]
        if height <= 0 or width <= 0:
            return False
        target_width = max(1, int(max_width))
        if width > target_width:
            scale = target_width / float(width)
            frame = cv2.resize(
                frame,
                (target_width, max(1, int(round(height * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        quality = max(1, min(100, int(quality)))
        ok, buffer = cv2.imencode(
            ".jpg",
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), quality],
        )
        if not ok:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(buffer.tobytes())
        return True
    except Exception:
        LOGGER.exception("could not save alert image %s", path)
        return False


def prune_alert_images(
    directory: Path,
    *,
    retention_hours: float,
    now: datetime | None = None,
) -> int:
    """Delete JPEGs older than the retention window; return the count."""
    if retention_hours <= 0:
        return 0
    cutoff = ((now or datetime.now()) - timedelta(hours=retention_hours)).timestamp()
    removed = 0
    try:
        candidates = list(Path(directory).glob("*.jpg"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            LOGGER.debug("could not prune alert image %s", path, exc_info=True)
    return removed
