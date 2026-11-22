"""RTSP input with TCP transport and bounded reconnect backoff."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

LOGGER = logging.getLogger(__name__)

# Must be set before OpenCV opens a capture.  TCP avoids UDP packet loss on
# Wi-Fi, which is exactly what ruins a pose stream.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|stimeout;5000000",
)

BACKOFF_SECONDS = (1, 2, 5, 10, 30)


def redact_rtsp_url(url: str) -> str:
    """Hide credentials before a URL reaches the logs."""
    return re.sub(r"(rtsp://)[^/@\s]+@", r"\1***@", url or "")


class RtspSource:
    """Thin wrapper around ``cv2.VideoCapture`` for one RTSP stream."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._capture = None

    def open(self, open_timeout_ms: int = 10_000) -> bool:
        import cv2

        self.close()
        capture = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        try:
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_timeout_ms)
            capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5_000)
        except Exception:  # pragma: no cover - property support varies
            LOGGER.debug("some OpenCV capture properties are unsupported")
        if not capture.isOpened():
            capture.release()
            return False
        self._capture = capture
        LOGGER.info("RTSP connected: %s", redact_rtsp_url(self.url))
        return True

    def read(self) -> Any | None:
        if self._capture is None:
            return None
        ok, frame = self._capture.read()
        if not ok or frame is None:
            return None
        return frame

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def is_open(self) -> bool:
        return self._capture is not None


def backoff_delay(attempt: int) -> int:
    """1, 2, 5, 10, 30, 30, ... seconds."""
    if attempt < 0:
        attempt = 0
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
