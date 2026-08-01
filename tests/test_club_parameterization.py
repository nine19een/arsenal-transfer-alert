from __future__ import annotations

import json
import unittest
import uuid
from datetime import datetime, timezone

from arsenal_alert.config import ConfigurationError, SourceCatalog
from arsenal_alert.deepseek import build_system_prompt, _validate_notification_language
from arsenal_alert.models import Classification, Post
from arsenal_alert.notification import build_notification

from tests.helpers import ROOT, eligible


class ClubParameterizationTests(unittest.TestCase):
    def test_checked_in_generic_example_is_minimal_and_structurally_valid(self) -> None:
        catalog = SourceCatalog.load(ROOT / "config" / "sources.example.toml")
        club = catalog.club
        assert club is not None
        self.assertEqual("Example FC", club.name)
        self.assertEqual("English", club.output_language)
        self.assertEqual(2, len(catalog.sources))
        self.assertEqual("pending", catalog.sources[0].identity_status)
        with self.assertRaisesRegex(ConfigurationError, "not live-ready"):
            catalog.assert_live_ready()

    def test_repository_default_remains_the_arsenal_edition(self) -> None:
        catalog = SourceCatalog.load(ROOT / "config" / "sources.toml")
        self.assertIsNotNone(catalog.club)
        club = catalog.club
        assert club is not None
        self.assertEqual("arsenal", club.key)
        self.assertEqual("Arsenal", club.name)
        self.assertEqual("Arsenal OR #AFC OR Gunners", catalog.topic_query)
        self.assertEqual("Simplified Chinese", club.output_language)

        source = catalog.by_key()["david_ornstein"]
        item = Post(
            id="2083480403348684978",
            author_id=source.user_id,
            text="Arsenal are in talks.",
            created_at=datetime(2026, 8, 1, 9, 10, 12, tzinfo=timezone.utc),
        )
        payload = build_notification(
            item,
            source,
            eligible("阿森纳已经开启谈判。"),
            club=club,
            group="",
            level="active",
            sound="",
        )
        self.assertEqual("🔴⚪ [Tier 1] David Ornstein", payload.title)
        self.assertEqual("arsenal-transfer-2083480403348684978", payload.bark_id)
        self.assertEqual("Arsenal Transfer Alert", payload.group)
        self.assertEqual(
            "阿森纳已经开启谈判。\n\n"
            "来源：David Ornstein\n"
            "时间：北京时间 2026-08-01 17:10:12\n"
            "点击通知打开 X 原帖。",
            payload.body,
        )

        with self.assertRaisesRegex(ValueError, "Chinese output language"):
            _validate_notification_language(
                eligible("Arsenal have opened talks for the player."), club
            )

    def test_minimal_second_club_config_drives_every_runtime_surface(self) -> None:
        catalog = self._load_temp_catalog(
            """catalog_version = 2

[club]
key = "liverpool"
name = "Liverpool"
query_terms = ["Liverpool FC", "#LFC"]

[[sources]]
key = "liverpool_official"
name = "Liverpool Official"
tier = 0
username = "LFC"
query_mode = "all"

[[sources]]
key = "trusted_reporter"
name = "Trusted Reporter"
tier = 1
username = "TrustedReporter"
query_mode = "topic"
"""
        )
        club = catalog.club
        assert club is not None
        self.assertEqual('"Liverpool FC" OR #LFC', catalog.topic_query)
        self.assertEqual("English", club.output_language)
        self.assertEqual("Liverpool Transfer Alert", club.notification_group)
        topic_queries = [
            spec.query for spec in catalog.build_queries() if "TrustedReporter" in spec.query
        ]
        self.assertEqual(1, len(topic_queries))
        self.assertIn('("Liverpool FC" OR #LFC)', topic_queries[0])
        self.assertNotIn("Arsenal", topic_queries[0])

        prompt = build_system_prompt(club)
        self.assertIn('"name":"Liverpool"', prompt)
        self.assertIn('"output_language":"English"', prompt)
        self.assertNotIn("Arsenal", prompt)

        classification = Classification.from_mapping(
            {
                "eligible": True,
                "club_scope_eligible": True,
                "club_participation": "buyer/recruiting_club",
                "news_origin": "first_hand_report",
                "notification_text": "Liverpool have opened talks for the player.",
                "reason_code": "transfer_update",
                "has_substantive_new_information": True,
            }
        )
        serialized = json.loads(classification.to_json())
        self.assertIn("club_scope_eligible", serialized)
        self.assertNotIn("arsenal_scope_eligible", serialized)

        source = catalog.by_key()["trusted_reporter"]
        payload = build_notification(
            Post(
                id="3001",
                author_id="mock:trusted_reporter",
                text="Liverpool have opened talks.",
                created_at=datetime(2026, 8, 1, 9, 10, 12, tzinfo=timezone.utc),
            ),
            source,
            classification,
            club=club,
            group="",
            level="active",
            sound="",
        )
        self.assertEqual("⚽ [Tier 1] Trusted Reporter", payload.title)
        self.assertEqual("liverpool-transfer-3001", payload.bark_id)
        self.assertEqual("Liverpool Transfer Alert", payload.group)
        self.assertIn("Source: Trusted Reporter", payload.body)
        self.assertIn("Time: UTC 2026-08-01 09:10:12", payload.body)
        self.assertNotIn("Arsenal", payload.body)

    def test_catalog_version_one_and_legacy_classification_still_load(self) -> None:
        catalog = self._load_temp_catalog(
            """catalog_version = 1
topic_query = "Arsenal OR #AFC"

[[sources]]
key = "arsenal"
name = "Arsenal Official"
tier = 0
username = "Arsenal"
query_mode = "all"
"""
        )
        club = catalog.club
        assert club is not None
        self.assertEqual("Arsenal", club.name)
        self.assertEqual("Arsenal OR #AFC", catalog.topic_query)
        legacy = Classification.from_mapping(
            {
                "eligible": False,
                "arsenal_scope_eligible": False,
                "arsenal_participation": "none",
                "news_origin": "first_hand_report",
                "translation_zh": None,
                "reason_code": "former_arsenal_player_unrelated",
                "has_substantive_new_information": False,
            }
        )
        self.assertEqual("former_target_club_player_unrelated", legacy.reason_code)
        self.assertFalse(legacy.club_scope_eligible)

    def test_query_terms_cannot_inject_x_operators(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "X query operator"):
            self._load_temp_catalog(
                """catalog_version = 2

[club]
key = "unsafe"
name = "Unsafe FC"
query_terms = ["from:someone"]

[[sources]]
key = "official"
name = "Official"
tier = 0
username = "Official"
query_mode = "all"
"""
            )

        with self.assertRaisesRegex(ConfigurationError, "valid X username"):
            self._load_temp_catalog(
                """catalog_version = 2

[club]
key = "unsafe"
name = "Unsafe FC"
query_terms = ["Unsafe FC"]

[[sources]]
key = "official"
name = "Official"
tier = 0
username = "Official) OR from:attacker"
query_mode = "all"
"""
            )

    def _load_temp_catalog(self, content: str) -> SourceCatalog:
        path = ROOT / "data" / f"test-club-{uuid.uuid4().hex}.toml"
        try:
            path.write_text(content, encoding="utf-8")
            return SourceCatalog.load(path)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
