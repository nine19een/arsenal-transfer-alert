from __future__ import annotations

import sqlite3
import unittest
import uuid
from pathlib import Path

from arsenal_alert.db import StateStore
from arsenal_alert.models import Classification, NotificationState, PostState
from arsenal_alert.origin import (
    normalize_article_url,
    original_report_fingerprint,
)
from arsenal_alert.pipeline import Pipeline

from tests.helpers import (
    DecisionClassifier,
    PlannedNotifier,
    StaticXClient,
    catalog,
    eligible,
    ineligible,
    post,
    settings_for,
)


ARTICLE_URL = (
    "https://www.theathletic.com/football/arsenal-transfer/"
    "?utm_source=x&article_id=42"
)


class NewsOriginGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = (
            Path(__file__).resolve().parents[1]
            / "data"
            / f"test-origin-{uuid.uuid4().hex}.sqlite3"
        )
        self.addCleanup(self._remove_database)

    def _remove_database(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database}{suffix}").unlink(missing_ok=True)

    def _pipeline(self, posts, decisions):
        store = StateStore(self.database)
        self.addCleanup(store.close)
        notifier = PlannedNotifier()
        classifier = DecisionClassifier(decisions)
        pipeline = Pipeline(
            settings_for(self.database),
            catalog(),
            store,
            StaticXClient(posts),
            classifier,
            notifier,
        )
        return pipeline, store, classifier, notifier

    def test_scenarios_a_to_e_apply_origin_gate_without_event_deduplication(self) -> None:
        own_article = post(
            "6001",
            "the_athletic",
            "Exclusive: Arsenal have opened talks to sign Mateo Silva.",
            article_url=ARTICLE_URL,
        )
        pure_repost = post(
            "6002",
            "gunnerblog",
            "RT @TheAthleticFC: Arsenal have opened talks to sign Mateo Silva.",
            reference_type="retweeted",
            referenced_post_id="6001",
        )
        attributed_summary = post(
            "6003",
            "gunnerblog",
            "According to @TheAthleticFC, Arsenal have opened talks for Mateo Silva.",
            article_url=(
                "http://theathletic.com/football/arsenal-transfer"
                "?article_id=42&utm_medium=social"
            ),
        )
        independent_confirmation = post(
            "6004",
            "gunnerblog",
            "I can independently confirm Arsenal have opened talks for Mateo Silva.",
            article_url=ARTICLE_URL,
        )
        substantive_quote = post(
            "6005",
            "gunnerblog",
            "My information is that Arsenal's opening bid is £45m plus £5m in add-ons.",
            reference_type="quoted",
            referenced_post_id="6001",
        )
        decisions = {
            "6001": eligible(
                "The Athletic 独家报道，阿森纳已开启引进 Mateo Silva 的谈判。",
                news_origin="first_hand_report",
            ),
            "6003": ineligible(
                "attributed_relay",
                "buyer/recruiting_club",
                scope_eligible=True,
                news_origin="attributed_relay",
            ),
            "6004": eligible(
                "Gunnerblog 独立确认，阿森纳已开启引进 Mateo Silva 的谈判。",
                news_origin="independent_confirmation",
            ),
            "6005": eligible(
                "新增信息：阿森纳的首份报价为4500万英镑，另加500万英镑浮动。",
                reason="substantive_reply_or_quote",
                news_origin="substantive_new_detail",
            ),
        }
        pipeline, store, classifier, notifier = self._pipeline(
            [
                own_article,
                pure_repost,
                attributed_summary,
                independent_confirmation,
                substantive_quote,
            ],
            decisions,
        )

        pipeline.run_cycle()

        self.assertNotIn("6002", classifier.calls)
        self.assertEqual(PostState.PURE_REPOST, store.post_state("6002"))
        self.assertEqual(PostState.FILTERED, store.post_state("6003"))
        self.assertEqual(
            {"6001", "6004", "6005"},
            {item.post_id for item in notifier.deliveries},
        )
        detail = next(item for item in notifier.deliveries if item.post_id == "6005")
        self.assertIn("4500万英镑", detail.body)
        self.assertNotIn("开启引进", detail.body)

    def test_same_normalized_article_and_same_report_is_suppressed(self) -> None:
        first = post(
            "6101",
            "the_athletic",
            "Arsenal have made an opening bid for Mateo Silva.",
            article_url=ARTICLE_URL,
        )
        repeated_distribution = post(
            "6102",
            "bbc_sport",
            "Arsenal have made an opening bid for Mateo Silva.",
            article_url=(
                "https://THEATHLETIC.com/football/arsenal-transfer/"
                "?article_id=42&utm_campaign=repeat#section"
            ),
        )
        pipeline, store, _classifier, notifier = self._pipeline(
            [first, repeated_distribution],
            {
                "6101": eligible(news_origin="first_hand_report"),
                "6102": eligible(news_origin="first_hand_report"),
            },
        )

        pipeline.run_cycle()

        self.assertEqual(["6101"], [item.post_id for item in notifier.deliveries])
        self.assertEqual(PostState.FILTERED, store.post_state("6102"))
        row = store._connection.execute(
            "SELECT last_error FROM posts WHERE post_id = '6102'"
        ).fetchone()
        self.assertEqual("duplicate_original_report", row["last_error"])

    def test_origin_fingerprint_survives_restart(self) -> None:
        first = post(
            "6201",
            "the_athletic",
            "Arsenal have contacted Valencia about Mateo Silva.",
            article_url=ARTICLE_URL,
        )
        first_store = StateStore(self.database)
        first_notifier = PlannedNotifier()
        first_pipeline = Pipeline(
            settings_for(self.database),
            catalog(),
            first_store,
            StaticXClient([first]),
            DecisionClassifier({"6201": eligible()}),
            first_notifier,
        )
        first_pipeline.run_cycle()
        first_store.close()

        repeated = post(
            "6202",
            "bbc_sport",
            "Arsenal have contacted Valencia about Mateo Silva.",
            article_url=ARTICLE_URL,
        )
        second_store = StateStore(self.database)
        self.addCleanup(second_store.close)
        second_notifier = PlannedNotifier()
        second_pipeline = Pipeline(
            settings_for(self.database),
            catalog(),
            second_store,
            StaticXClient([repeated]),
            DecisionClassifier({"6202": eligible()}),
            second_notifier,
        )

        second_pipeline.run_cycle()

        self.assertEqual(["6201"], [item.post_id for item in first_notifier.deliveries])
        self.assertEqual([], second_notifier.deliveries)
        self.assertEqual(PostState.FILTERED, second_store.post_state("6202"))

    def test_substantive_new_detail_bypasses_same_url_suppression(self) -> None:
        first = post(
            "6301",
            "the_athletic",
            "Arsenal are in talks for Mateo Silva.",
            article_url=ARTICLE_URL,
        )
        detail = post(
            "6302",
            "the_athletic",
            "Update: Arsenal have now submitted a £50m bid.",
            article_url=ARTICLE_URL,
        )
        pipeline, _store, _classifier, notifier = self._pipeline(
            [first, detail],
            {
                "6301": eligible(news_origin="first_hand_report"),
                "6302": eligible(
                    "新增信息：阿森纳现已提交5000万英镑报价。",
                    news_origin="substantive_new_detail",
                ),
            },
        )

        pipeline.run_cycle()

        self.assertEqual(
            {"6301", "6302"},
            {item.post_id for item in notifier.deliveries},
        )

    def test_two_independent_reporters_same_event_are_not_merged(self) -> None:
        ornstein = post(
            "6401",
            "david_ornstein",
            "My sources say Arsenal are in talks for Mateo Silva.",
        )
        romano = post(
            "6402",
            "fabrizio_romano",
            "Arsenal talks for Mateo Silva are under way, as independently confirmed.",
        )
        pipeline, _store, _classifier, notifier = self._pipeline(
            [ornstein, romano],
            {
                "6401": eligible(news_origin="first_hand_report"),
                "6402": eligible(news_origin="independent_confirmation"),
            },
        )

        pipeline.run_cycle()

        self.assertEqual(
            {"6401", "6402"},
            {item.post_id for item in notifier.deliveries},
        )

    def test_unclear_origin_fails_closed_without_notification(self) -> None:
        item = post(
            "6501",
            "gunnerblog",
            "Arsenal and Mateo Silva. Interesting.",
        )
        pipeline, store, _classifier, notifier = self._pipeline(
            [item],
            {
                "6501": ineligible(
                    "unclear_origin",
                    "buyer/recruiting_club",
                    scope_eligible=True,
                    news_origin="unclear_origin",
                )
            },
        )

        pipeline.run_cycle()

        self.assertEqual(PostState.FILTERED, store.post_state("6501"))
        self.assertEqual([], notifier.deliveries)

    def test_url_normalization_and_reference_priority_are_deterministic(self) -> None:
        left = normalize_article_url(
            "http://WWW.Example.com:80/story/?b=2&utm_source=x&a=1#top"
        )
        right = normalize_article_url(
            "https://example.com/story?a=1&b=2&utm_medium=social"
        )
        self.assertEqual("https://example.com/story?a=1&b=2", left)
        self.assertEqual(left, right)

        quoted = post(
            "6601",
            "gunnerblog",
            "New detail.",
            reference_type="quoted",
            referenced_post_id="123456",
            article_url=ARTICLE_URL,
        )
        fingerprint = original_report_fingerprint(quoted)
        self.assertIsNotNone(fingerprint)
        assert fingerprint is not None
        self.assertEqual("post:123456", fingerprint.value)
        self.assertEqual("referenced_post", fingerprint.kind)

    def test_legacy_database_is_migrated_without_losing_posts(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE posts (
                post_id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL DEFAULT '',
                author_id TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                referenced_types_json TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                state TEXT NOT NULL,
                classification_json TEXT,
                classification_attempts INTEGER NOT NULL DEFAULT 0,
                next_classification_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.commit()
        connection.close()

        store = StateStore(self.database)
        self.addCleanup(store.close)
        columns = {
            row["name"]
            for row in store._connection.execute("PRAGMA table_info(posts)").fetchall()
        }
        self.assertIn("origin_fingerprint", columns)
        self.assertIn("news_origin", columns)
        self.assertEqual("2", store.get_meta("schema_version"))

    def test_local_validation_rejects_disallowed_origin_as_eligible(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires an allowed news_origin"):
            Classification.from_mapping(
                {
                    "eligible": True,
                    "arsenal_scope_eligible": True,
                    "arsenal_participation": "buyer/recruiting_club",
                    "news_origin": "attributed_relay",
                    "translation_zh": "据其他记者报道，阿森纳正在谈判。",
                    "reason_code": "transfer_update",
                    "has_substantive_new_information": True,
                }
            )

    def test_out_of_scope_commentary_is_a_valid_safe_rejection(self) -> None:
        result = Classification.from_mapping(
            {
                "eligible": False,
                "arsenal_scope_eligible": False,
                "arsenal_participation": "none",
                "news_origin": "commentary_only",
                "translation_zh": None,
                "reason_code": "commentary_only",
                "has_substantive_new_information": False,
            }
        )

        self.assertFalse(result.eligible)
        self.assertFalse(result.arsenal_scope_eligible)
        self.assertEqual("commentary_only", result.reason_code)


if __name__ == "__main__":
    unittest.main()
