"""Standard-library HTTP API and the embedded monitoring dashboard.

``/healthz``, ``POST /calibrate`` and ``POST /audit`` are the machine
interfaces.  ``/`` serves a small vanilla-JS dashboard that explains the
current judgement and the reminder actions; ``/api/monitor`` is its JSON
source.  Everything is meant for a trusted LAN: there is no authentication
and no image/video endpoint.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .baseline import BaselineError
from .errors import CalibrationTimeout, ServiceBusy, ServiceUnavailable

LOGGER = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent / "web"
_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "posture-reminder/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOGGER.debug("http: " + fmt, *args)

    @property
    def service(self):
        return self.server.service  # type: ignore[attr-defined]

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        *,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _path(self) -> str:
        path = urlsplit(self.path).path
        return path.rstrip("/") or "/"

    def _static(self, path: str) -> bool:
        asset = _ASSETS.get(path)
        if asset is None:
            return False
        filename, content_type = asset
        try:
            body = (_WEB_DIR / filename).read_bytes()
        except OSError:
            LOGGER.exception("dashboard asset missing: %s", filename)
            self._send_json({"error": "dashboard asset missing"}, status=500)
            return True
        self._send_bytes(body, content_type, cache="no-cache")
        return True

    def _send_alert_image(self, path: str) -> None:
        name = unquote(path.rsplit("/", 1)[-1])
        invalid = (
            not name
            or name != Path(name).name
            or name.startswith(".")
            or not name.lower().endswith(".jpg")
        )
        if invalid or not self.service.settings.capture_alert_images:
            self._send_json({"error": "alert image not found"}, status=404)
            return
        try:
            body = (self.service.settings.alert_image_dir / name).read_bytes()
        except OSError:
            self._send_json({"error": "alert image not found"}, status=404)
            return
        self._send_bytes(body, "image/jpeg", cache="private, no-store")

    def _dashboard_payload(self) -> dict[str, Any]:
        query = parse_qs(urlsplit(self.path).query)
        raw_minutes = (query.get("minutes") or ["15"])[0]
        try:
            minutes = float(raw_minutes)
        except ValueError:
            raise ValueError("minutes must be a number") from None
        minutes = min(180.0, max(1.0, minutes))
        raw_since = (query.get("since") or [""])[0].strip()
        since = None
        if raw_since:
            try:
                since = datetime.fromisoformat(raw_since)
            except ValueError:
                raise ValueError("since must be an ISO-8601 datetime") from None
        return self.service.monitor(minutes=minutes, since=since)

    def _history_payload(self) -> dict[str, Any]:
        query = parse_qs(urlsplit(self.path).query)
        raw_hours = (query.get("hours") or ["24"])[0]
        raw_bucket = (query.get("bucket_seconds") or ["300"])[0]
        try:
            hours = float(raw_hours)
            bucket_seconds = float(raw_bucket)
        except ValueError:
            raise ValueError("hours and bucket_seconds must be numbers") from None
        hours = min(48.0, max(1.0, hours))
        bucket_seconds = min(3600.0, max(60.0, bucket_seconds))
        return self.service.history(hours=hours, bucket_seconds=bucket_seconds)

    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        if path == "/healthz":
            try:
                self._send_json(self.service.status())
            except Exception as exc:  # keep health checkable
                LOGGER.exception("healthz failed")
                self._send_json({"status": "degraded", "error": str(exc)}, status=200)
            return
        if path == "/api/monitor":
            try:
                self._send_json(self._dashboard_payload())
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            except Exception as exc:  # keep the page diagnosable
                LOGGER.exception("monitor payload failed")
                self._send_json({"error": str(exc)}, status=500)
            return
        if path.startswith("/api/alert-image/"):
            self._send_alert_image(path)
            return
        if path == "/api/history":
            try:
                self._send_json(self._history_payload())
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            except Exception as exc:  # keep the page diagnosable
                LOGGER.exception("history payload failed")
                self._send_json({"error": str(exc)}, status=500)
            return
        if path == "/api/tuning":
            try:
                self._send_json(self.service.tuning_suggestion())
            except Exception as exc:
                LOGGER.exception("tuning payload failed")
                self._send_json({"error": str(exc)}, status=500)
            return
        if path == "/api/policy":
            self._send_json(self.service.active_policy())
            return
        if path == "/api/timeline":
            query = parse_qs(urlsplit(self.path).query)
            try:
                start = datetime.fromisoformat((query.get("start") or [""])[0])
                end = datetime.fromisoformat((query.get("end") or [""])[0])
                bucket = float((query.get("bucket_seconds") or ["60"])[0])
            except (ValueError, TypeError):
                self._send_json(
                    {"error": "start/end must be ISO-8601 and bucket_seconds numeric"},
                    status=400,
                )
                return
            bucket = max(1.0, bucket)
            if abs((end - start).total_seconds()) > 26 * 3600:
                self._send_json({"error": "window too large"}, status=400)
                return
            try:
                rows = self.service.timeline(
                    start=start, end=end, bucket_seconds=bucket
                )
            except Exception as exc:
                LOGGER.exception("timeline payload failed")
                self._send_json({"error": str(exc)}, status=500)
                return
            self._send_json(
                {
                    "start": start.isoformat(timespec="seconds"),
                    "end": end.isoformat(timespec="seconds"),
                    "bucket_seconds": bucket,
                    "buckets": rows,
                }
            )
            return
        if path == "/api/missed-events":
            query = parse_qs(urlsplit(self.path).query)
            try:
                hours = float((query.get("hours") or ["24"])[0])
                limit = int((query.get("limit") or ["200"])[0])
            except ValueError:
                self._send_json({"error": "hours and limit must be numbers"}, status=400)
                return
            self._send_json({"events": self.service.list_missed(hours=hours, limit=limit)})
            return
        if self._static(path):
            return
        self._send_json({"error": "not found"}, status=404)

    def _json_body(self) -> dict[str, Any]:
        """Parse a JSON object body; raise ValueError on malformed input."""
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if length <= 0 or length > 1_000_000:
            raise ValueError("empty or oversized request body")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("body must be valid JSON") from None
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return payload

    def do_POST(self) -> None:  # noqa: N802
        path = self._path()
        if path == "/calibrate":
            self._run(self.service.calibrate)
            return
        if path == "/audit":
            self._run(self.service.audit)
            return
        if path == "/api/alert-verdict":
            try:
                body = self._json_body()
                if "id" not in body or "verdict" not in body:
                    self._send_json(
                        {"error": "id and verdict are required"}, status=400
                    )
                    return
                row = self.service.set_verdict(
                    int(body["id"]),
                    body["verdict"],
                    body.get("note"),
                )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            except KeyError as exc:
                self._send_json({"error": str(exc)}, status=404)
                return
            except (TypeError, OverflowError):
                self._send_json({"error": "id must be an integer"}, status=400)
                return
            self._send_json({"ok": True, "reminder": row})
            return
        if path == "/api/threshold-suggestion":
            self._send_json(self.service.tuning_suggestion())
            return
        if path == "/api/apply-thresholds":
            try:
                body = self._json_body()
                result = self.service.apply_thresholds(body.get("values", body))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json(result)
            return
        if path == "/api/missed-events":
            try:
                body = self._json_body()
                action = body.get("action", "add")
                if action == "add":
                    if "at" not in body or "issue" not in body:
                        self._send_json(
                            {"error": "at and issue are required"}, status=400
                        )
                        return
                    try:
                        moment = datetime.fromisoformat(str(body["at"]))
                    except ValueError:
                        self._send_json(
                            {"error": "at must be an ISO-8601 datetime"}, status=400
                        )
                        return
                    row = self.service.add_missed(
                        moment,
                        body["issue"],
                        body.get("note"),
                        snapshot=body.get("snapshot"),
                    )
                    self._send_json({"ok": True, "event": row}, status=201)
                    return
                if action == "update":
                    if "id" not in body:
                        self._send_json({"error": "id is required"}, status=400)
                        return
                    row = self.service.update_missed(
                        int(body["id"]),
                        issue=body.get("issue"),
                        note=body.get("note"),
                    )
                    self._send_json({"ok": True, "event": row})
                    return
                if action == "delete":
                    if "id" not in body:
                        self._send_json({"error": "id is required"}, status=400)
                        return
                    removed = self.service.delete_missed(int(body["id"]))
                    if not removed:
                        self._send_json({"error": "no such event"}, status=404)
                        return
                    self._send_json({"ok": True})
                    return
                self._send_json({"error": "invalid action"}, status=400)
            except (ValueError, TypeError, OverflowError) as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            except KeyError as exc:
                self._send_json({"error": str(exc)}, status=404)
                return
        self._send_json({"error": "not found"}, status=404)

    def _run(self, operation) -> None:
        try:
            result = operation()
        except ServiceBusy as exc:
            self._send_json({"error": str(exc)}, status=409)
        except ServiceUnavailable as exc:
            self._send_json({"error": str(exc)}, status=503)
        except CalibrationTimeout as exc:
            self._send_json({"error": str(exc)}, status=504)
        except BaselineError as exc:
            self._send_json({"error": str(exc)}, status=500)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("API operation failed")
            self._send_json({"error": str(exc)}, status=500)
        else:
            self._send_json(result, status=200)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ApiServer:
    def __init__(self, service, host: str, port: int) -> None:
        self.service = service
        self.host = host
        self.port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> int:
        server = _Server((self.host, self.port), _Handler)
        server.service = self.service  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=server.serve_forever,
            name="posture-http",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        self.port = int(server.server_address[1])
        return self.port

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._server = None
        self._thread = None
