from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import Settings
from .cost import cost_report
from .db import StateStore
from .models import AppMode, parse_utc_datetime, utc_now


LOGGER = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, store: StateStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        handler = self._handler()
        self.server = ThreadingHTTPServer(
            (settings.health_host, settings.health_port),
            handler,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="health-server",
            daemon=True,
        )
        self._started = False

    def start(self) -> None:
        self.thread.start()
        self._started = True
        LOGGER.info(
            "health_server_started",
            extra={"host": self.settings.health_host, "port": self.settings.health_port},
        )

    def stop(self) -> None:
        if self._started:
            self.server.shutdown()
        self.server.server_close()
        if self._started and self.thread.is_alive():
            self.thread.join(timeout=5)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        store = self.store
        settings = self.settings

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path == "/health/live":
                    self._json(200, {"live": True})
                    return
                if self.path in {"/health", "/health/ready"}:
                    report = _readiness_report(store, settings)
                    self._json(200 if report["ready"] else 503, report)
                    return
                if self.path == "/metrics":
                    self._json(200, cost_report(store, settings))
                    return
                self._json(404, {"error": "not_found"})

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _json(self, status: int, payload: Any) -> None:
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        return Handler


def _readiness_report(store: StateStore, settings: Settings) -> dict[str, Any]:
    report = store.health_report()
    latest = report.get("latest_x_success_at")
    if settings.app_mode is AppMode.LIVE:
        if latest is None:
            report["ready"] = False
            report["flags"].append(
                {
                    "key": "x_never_succeeded",
                    "severity": "critical",
                    "message": "No X polling cycle has completed successfully",
                }
            )
        else:
            maximum_age = timedelta(seconds=settings.x_poll_interval_seconds * 3 + 30)
            if utc_now() - parse_utc_datetime(latest) > maximum_age:
                report["ready"] = False
                report["flags"].append(
                    {
                        "key": "x_poll_stale",
                        "severity": "critical",
                        "message": "The most recent successful X poll is stale",
                    }
                )
    return report
