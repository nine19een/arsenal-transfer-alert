from __future__ import annotations

import unittest
import uuid
from dataclasses import replace
from datetime import date
from pathlib import Path

from arsenal_alert.config import SourceCatalog
from arsenal_alert.db import StateStore
from arsenal_alert.identity import SourceIdentityMonitor

from tests.helpers import ROOT, catalog, settings_for


class LookupClient:
    def __init__(self, users):
        self.users = users
        self.calls = 0

    def lookup_sources(self, *, usernames=None, user_ids=None):
        self.calls += 1
        return self.users


class IdentityMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = ROOT / "data" / f"test-{uuid.uuid4().hex}.sqlite3"
        self.addCleanup(self._remove_database)
        self.store = StateStore(self.database)
        self.addCleanup(self.store.close)
        today = date.today().isoformat()
        sources = tuple(
            replace(
                source,
                user_id=str(6000 + index),
                identity_status="verified",
                verified_at=today,
                confirmed=True,
            )
            for index, source in enumerate(catalog().sources)
        )
        self.catalog = SourceCatalog(
            path=Path("test-sources.toml"),
            topic_query="Arsenal",
            sources=sources,
        )
        self.settings = settings_for(self.database)

    def _remove_database(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database}{suffix}").unlink(missing_ok=True)

    def test_successful_numeric_id_recheck_is_cached(self) -> None:
        users = [
            {
                "id": source.user_id,
                "username": source.username,
                "parody": False,
            }
            for source in self.catalog.sources
        ]
        client = LookupClient(users)
        monitor = SourceIdentityMonitor(
            self.settings, self.catalog, self.store, client
        )
        self.assertTrue(monitor.ready())
        self.assertTrue(monitor.ready())
        self.assertEqual(1, client.calls)

    def test_username_mismatch_blocks_and_does_not_loop_calls(self) -> None:
        users = [
            {
                "id": source.user_id,
                "username": (
                    "renamed_account" if index == 0 else source.username
                ),
                "parody": False,
            }
            for index, source in enumerate(self.catalog.sources)
        ]
        client = LookupClient(users)
        monitor = SourceIdentityMonitor(
            self.settings, self.catalog, self.store, client
        )
        self.assertFalse(monitor.ready())
        self.assertFalse(monitor.ready())
        self.assertEqual(1, client.calls)
        report = self.store.health_report()
        self.assertFalse(report["ready"])


if __name__ == "__main__":
    unittest.main()

