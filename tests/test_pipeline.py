from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from arsenal_alert.db import StateStore
from arsenal_alert.models import NotificationState, PostState
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


class PipelineAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = (
            Path(__file__).resolve().parents[1]
            / "data"
            / f"test-{uuid.uuid4().hex}.sqlite3"
        )
        self.addCleanup(self._remove_database)
        self.catalog = catalog()

    def _remove_database(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database}{suffix}").unlink(missing_ok=True)

    def pipeline(
        self,
        posts,
        decisions,
        *,
        notifier=None,
        ignore_cursor=False,
        dry_run=False,
        classification_max_attempts=1,
    ):
        settings = settings_for(
            self.database,
            dry_run=dry_run,
            classification_max_attempts=classification_max_attempts,
        )
        store = StateStore(self.database)
        self.addCleanup(store.close)
        notifier = notifier or PlannedNotifier()
        classifier = DecisionClassifier(decisions)
        pipeline = Pipeline(
            settings,
            self.catalog,
            store,
            StaticXClient(posts, ignore_cursor=ignore_cursor),
            classifier,
            notifier,
        )
        return pipeline, store, classifier, notifier

    def test_tier1_original_transfer_pushes_once(self) -> None:
        item = post("2001", "david_ornstein")
        pipeline, store, _classifier, notifier = self.pipeline(
            [item], {"2001": eligible()}
        )
        pipeline.run_cycle()
        self.assertEqual(1, len(notifier.deliveries))
        self.assertEqual("🔴⚪ [Tier 1] David Ornstein", notifier.deliveries[0].title)
        self.assertEqual(NotificationState.SENT, store.notification_status("2001"))

    def test_tier1_ordinary_team_news_is_not_pushed(self) -> None:
        item = post("2002", "david_ornstein", "Arsenal trained today.")
        pipeline, store, _classifier, notifier = self.pipeline(
            [item], {"2002": ineligible()}
        )
        pipeline.run_cycle()
        self.assertEqual([], notifier.deliveries)
        self.assertEqual(PostState.FILTERED, store.post_state("2002"))

    def test_pure_repost_is_rejected_before_classifier(self) -> None:
        item = post("2003", "fabrizio_romano", reference_type="retweeted")
        pipeline, store, classifier, notifier = self.pipeline([item], {})
        pipeline.run_cycle()
        self.assertEqual([], classifier.calls)
        self.assertEqual([], notifier.deliveries)
        self.assertEqual(PostState.PURE_REPOST, store.post_state("2003"))

    def test_substantive_quote_post_is_pushed(self) -> None:
        item = post(
            "2004",
            "charles_watts",
            "Important addition: talks concern a permanent deal, not a loan.",
            reference_type="quoted",
        )
        decision = ClassificationForQuote()
        pipeline, store, _classifier, notifier = self.pipeline(
            [item], {"2004": decision}
        )
        pipeline.run_cycle()
        self.assertEqual(1, len(notifier.deliveries))
        self.assertEqual(NotificationState.SENT, store.notification_status("2004"))

    def test_same_post_fetched_repeatedly_pushes_once(self) -> None:
        item = post("2005", "david_ornstein")
        pipeline, store, classifier, notifier = self.pipeline(
            [item], {"2005": eligible()}, ignore_cursor=True
        )
        pipeline.run_cycle()
        pipeline.run_cycle()
        self.assertEqual(["2005"], classifier.calls)
        self.assertEqual(1, len(notifier.deliveries))
        self.assertEqual(1, store.notification_count())

    def test_two_reporters_same_event_are_both_pushed(self) -> None:
        ornstein = post("2006", "david_ornstein")
        romano = post("2007", "fabrizio_romano")
        pipeline, _store, _classifier, notifier = self.pipeline(
            [ornstein, romano],
            {"2006": eligible(), "2007": eligible()},
        )
        pipeline.run_cycle()
        self.assertEqual({"2006", "2007"}, {item.post_id for item in notifier.deliveries})

    def test_restart_deduplicates_old_post_and_backfills_new_post(self) -> None:
        first = post("2010", "david_ornstein")
        settings = settings_for(self.database)
        first_store = StateStore(self.database)
        first_notifier = PlannedNotifier()
        first_pipeline = Pipeline(
            settings,
            self.catalog,
            first_store,
            StaticXClient([first]),
            DecisionClassifier({"2010": eligible()}),
            first_notifier,
        )
        first_pipeline.run_cycle()
        first_store.close()

        during_outage = post("2011", "fabrizio_romano")
        second_store = StateStore(self.database)
        self.addCleanup(second_store.close)
        second_notifier = PlannedNotifier()
        second_pipeline = Pipeline(
            settings,
            self.catalog,
            second_store,
            StaticXClient([first, during_outage]),
            DecisionClassifier({"2010": eligible(), "2011": eligible()}),
            second_notifier,
        )
        second_pipeline.recover()
        second_pipeline.run_cycle()
        self.assertEqual(["2010"], [item.post_id for item in first_notifier.deliveries])
        self.assertEqual(["2011"], [item.post_id for item in second_notifier.deliveries])
        self.assertEqual(2, second_store.notification_count())

    def test_invalid_deepseek_content_fails_closed(self) -> None:
        item = post("2012", "sami_mokbel")
        pipeline, store, classifier, notifier = self.pipeline(
            [item], {"2012": "__INVALID__"}, classification_max_attempts=1
        )
        pipeline.run_cycle()
        self.assertEqual(["2012"], classifier.calls)
        self.assertEqual([], notifier.deliveries)
        self.assertEqual(PostState.CLASSIFICATION_ERROR, store.post_state("2012"))
        self.assertEqual(0, store.notification_count())

    def test_invalid_deepseek_content_is_not_retried_without_new_feedback(self) -> None:
        item = post("2015", "sami_mokbel")
        pipeline, store, classifier, notifier = self.pipeline(
            [item],
            {"2015": "__INVALID__"},
            ignore_cursor=True,
            classification_max_attempts=3,
        )

        pipeline.run_cycle()
        pipeline.run_cycle()

        self.assertEqual(["2015"], classifier.calls)
        self.assertEqual([], notifier.deliveries)
        self.assertEqual(PostState.CLASSIFICATION_ERROR, store.post_state("2015"))

    def test_successful_reclassification_clears_previous_warning(self) -> None:
        item = post("2016", "charles_watts", "A comment unrelated to transfers.")
        pipeline, store, classifier, notifier = self.pipeline(
            [item],
            {"2016": "__INVALID__"},
            ignore_cursor=True,
            classification_max_attempts=1,
        )
        pipeline.run_cycle()
        self.assertTrue(
            any(
                flag["key"] == "classification_2016"
                for flag in store.health_report()["flags"]
            )
        )

        classifier.decisions["2016"] = ineligible(
            "commentary_only",
            news_origin="commentary_only",
        )
        store.retry_failed_classification("2016")
        pipeline.run_cycle()

        self.assertEqual(PostState.FILTERED, store.post_state("2016"))
        self.assertEqual([], notifier.deliveries)
        self.assertFalse(
            any(
                flag["key"] == "classification_2016"
                for flag in store.health_report()["flags"]
            )
        )

    def test_temporary_bark_failure_retries_without_duplicate_delivery(self) -> None:
        item = post("2013", "david_ornstein")
        notifier = PlannedNotifier(["retry", "success"])
        pipeline, store, _classifier, _notifier = self.pipeline(
            [item], {"2013": eligible()}, notifier=notifier
        )
        pipeline.run_cycle()
        self.assertEqual(2, len(notifier.calls))
        self.assertEqual(1, len(notifier.deliveries))
        self.assertEqual(NotificationState.SENT, store.notification_status("2013"))
        self.assertEqual(
            notifier.calls[0].bark_id,
            notifier.calls[1].bark_id,
            "a stable Bark id is retained across safe retries",
        )

    def test_uncertain_bark_outcome_is_not_automatically_retried(self) -> None:
        item = post("2014", "david_ornstein")
        notifier = PlannedNotifier(["uncertain", "success"])
        pipeline, store, _classifier, _notifier = self.pipeline(
            [item], {"2014": eligible()}, notifier=notifier
        )
        pipeline.run_cycle()
        pipeline.run_cycle()
        self.assertEqual(1, len(notifier.calls))
        self.assertEqual([], notifier.deliveries)
        self.assertEqual(
            NotificationState.UNCERTAIN, store.notification_status("2014")
        )
        store.resolve_uncertain_notification("2014", "assume-delivered")
        self.assertEqual(NotificationState.SENT, store.notification_status("2014"))

    def test_dry_run_builds_payload_but_never_calls_bark(self) -> None:
        item = post("2015", "david_ornstein")
        pipeline, store, _classifier, notifier = self.pipeline(
            [item], {"2015": eligible()}, dry_run=True
        )
        pipeline.run_cycle()
        self.assertEqual([], notifier.calls)
        self.assertEqual(NotificationState.DRY_RUN, store.notification_status("2015"))


def ClassificationForQuote():
    from arsenal_alert.models import Classification

    return Classification.from_mapping(
        {
            "eligible": True,
            "arsenal_scope_eligible": True,
            "arsenal_participation": "buyer/recruiting_club",
            "news_origin": "substantive_new_detail",
            "translation_zh": "补充信息：谈判针对永久转会，而不是租借。",
            "reason_code": "substantive_reply_or_quote",
            "has_substantive_new_information": True,
        }
    )


if __name__ == "__main__":
    unittest.main()
