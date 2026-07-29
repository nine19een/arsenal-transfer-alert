from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass

from .bark import BarkClient
from .config import Settings, SourceCatalog
from .db import StateStore
from .deepseek import DeepSeekClassifier
from .health import HealthServer
from .identity import SourceIdentityMonitor
from .mock import MockBarkClient, MockClassifier, MockXClient
from .models import AppMode
from .pipeline import Pipeline
from .x_api import XApiClient


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    catalog: SourceCatalog
    store: StateStore
    pipeline: Pipeline
    health: HealthServer

    def close(self) -> None:
        self.health.stop()
        self.store.close()


def build_runtime(settings: Settings) -> Runtime:
    catalog = SourceCatalog.load(settings.source_config_path)
    if settings.app_mode is AppMode.LIVE:
        # This happens before any external call. Pending IDs or an ambiguous media
        # account therefore cannot accidentally enter production.
        # Numeric IDs/status/confirmation must exist. Freshness is then enforced
        # through the official API monitor before the first Post poll and weekly.
        catalog.assert_live_ready(enforce_age=False)
    store = StateStore(settings.db_path)
    try:
        if settings.app_mode is AppMode.LIVE:
            x_client = XApiClient(settings, store)
            classifier = DeepSeekClassifier(settings, store)
            notifier = BarkClient(settings, store)
            identity_monitor = SourceIdentityMonitor(settings, catalog, store, x_client)
            classifier.verify_model()
        else:
            x_client = MockXClient(
                settings.mock_feed_path,
                catalog,
            )
            classifier = MockClassifier(settings.mock_classifications_path)
            notifier = MockBarkClient()
            identity_monitor = None
        pipeline = Pipeline(
            settings,
            catalog,
            store,
            x_client,
            classifier,
            notifier,
            identity_monitor,
        )
        health = HealthServer(store, settings)
    except Exception:
        store.close()
        raise
    return Runtime(settings, catalog, store, pipeline, health)


def run_service(runtime: Runtime, *, once: bool = False) -> None:
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            try:
                signal.signal(getattr(signal, signal_name), request_stop)
            except (ValueError, OSError):
                pass

    consecutive_failures = 0
    try:
        runtime.pipeline.recover()
        runtime.health.start()
        while not stop_event.is_set():
            cycle_started = time.monotonic()
            try:
                runtime.pipeline.run_cycle()
                consecutive_failures = 0
            except Exception as error:
                consecutive_failures += 1
                error_code = type(error).__name__
                runtime.store.set_health_flag(
                    "unexpected_cycle_failure",
                    "critical",
                    f"Unexpected service-cycle failure: {error_code}",
                )
                LOGGER.exception(
                    "unexpected_cycle_failure",
                    extra={
                        "error_code": error_code,
                        "consecutive_failures": consecutive_failures,
                    },
                )
                if once or consecutive_failures >= 5:
                    raise
            else:
                runtime.store.clear_health_flag("unexpected_cycle_failure")
            if once:
                break
            elapsed = time.monotonic() - cycle_started
            wait_seconds = max(runtime.settings.x_poll_interval_seconds - elapsed, 1)
            stop_event.wait(wait_seconds)
    finally:
        runtime.close()
