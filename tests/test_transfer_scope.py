from __future__ import annotations

import unittest
import uuid
from pathlib import Path

from arsenal_alert.db import StateStore
from arsenal_alert.models import Classification, NotificationState, PostState
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


class TransferScopeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = (
            Path(__file__).resolve().parents[1]
            / "data"
            / f"test-scope-{uuid.uuid4().hex}.sqlite3"
        )
        self.addCleanup(self._remove_database)
        self.next_id = 5000

    def _remove_database(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database}{suffix}").unlink(missing_ok=True)

    def _run(self, text: str, decision: Classification, source_key: str = "the_athletic"):
        self.next_id += 1
        post_id = str(self.next_id)
        item = post(post_id, source_key, text)
        store = StateStore(self.database)
        self.addCleanup(store.close)
        notifier = PlannedNotifier()
        pipeline = Pipeline(
            settings_for(self.database),
            catalog(),
            store,
            StaticXClient([item]),
            DecisionClassifier({post_id: decision}),
            notifier,
        )
        pipeline.run_cycle()
        return post_id, store, notifier

    def test_tomiyasu_between_other_clubs_is_filtered(self) -> None:
        post_id, store, notifier = self._run(
            "Former Arsenal defender Takehiro Tomiyasu is training with Crystal Palace "
            "as he considers a possible move after leaving Ajax.",
            ineligible(
                "former_arsenal_player_unrelated",
                news_origin="first_hand_report",
            ),
        )
        self.assertEqual(PostState.FILTERED, store.post_state(post_id))
        self.assertEqual([], notifier.deliveries)
        self.assertEqual(0, store.notification_count())

    def test_former_arsenal_player_joining_another_club_is_filtered(self) -> None:
        post_id, store, notifier = self._run(
            "Former Arsenal player Alex Example has agreed to join Milan from Roma.",
            ineligible(
                "former_arsenal_player_unrelated",
                news_origin="first_hand_report",
            ),
        )
        self.assertEqual(PostState.FILTERED, store.post_state(post_id))
        self.assertEqual([], notifier.deliveries)

    def test_former_arsenal_player_explicitly_returning_is_eligible(self) -> None:
        post_id, store, notifier = self._run(
            "Arsenal are in talks to bring former midfielder Alex Example back to the club.",
            eligible(
                "阿森纳正在商谈让前中场球员 Alex Example 重返俱乐部。",
                participation="buyer/recruiting_club",
            ),
        )
        self.assertEqual(NotificationState.SENT, store.notification_status(post_id))
        self.assertEqual(1, len(notifier.deliveries))

    def test_current_arsenal_player_possible_departure_is_eligible(self) -> None:
        post_id, store, notifier = self._run(
            "A current Arsenal first-team defender could leave after talks with Milan.",
            eligible(
                "一名现役阿森纳一线队后卫在与米兰接触后可能离队。",
                participation="seller/current_club",
            ),
        )
        self.assertEqual(NotificationState.SENT, store.notification_status(post_id))
        self.assertEqual(1, len(notifier.deliveries))

    def test_arsenal_pursuing_another_clubs_player_is_eligible(self) -> None:
        post_id, store, notifier = self._run(
            "Arsenal have contacted Valencia about a possible deal for their midfielder.",
            eligible(
                "阿森纳已就可能引进一名中场球员与瓦伦西亚取得联系。",
                participation="buyer/recruiting_club",
            ),
        )
        self.assertEqual(NotificationState.SENT, store.notification_status(post_id))
        self.assertEqual(1, len(notifier.deliveries))

    def test_arsenal_owned_loan_player_sale_or_return_is_eligible(self) -> None:
        post_id, store, notifier = self._run(
            "Arsenal's on-loan first-team player will return before talks over a sale.",
            eligible(
                "阿森纳外租的一线队球员将先回归，随后俱乐部会商谈出售事宜。",
                participation="loan_owner",
                reason="loan_update",
            ),
        )
        self.assertEqual(NotificationState.SENT, store.notification_status(post_id))
        self.assertEqual(1, len(notifier.deliveries))

    def test_sell_on_fee_without_transfer_participation_is_filtered(self) -> None:
        post_id, store, notifier = self._run(
            "A former Arsenal player is joining Milan from Roma, with Arsenal due a "
            "sell-on fee.",
            ineligible(
                "former_arsenal_player_unrelated",
                news_origin="first_hand_report",
            ),
        )
        self.assertEqual(PostState.FILTERED, store.post_state(post_id))
        self.assertEqual([], notifier.deliveries)

    def test_womens_youth_and_ordinary_news_remain_filtered(self) -> None:
        scenarios = (
            (
                "Arsenal Women have agreed to sign a new striker.",
                ineligible(
                    "womens_or_youth",
                    "buyer/recruiting_club",
                    news_origin="first_hand_report",
                ),
            ),
            (
                "Arsenal academy have signed a 16-year-old prospect.",
                ineligible(
                    "womens_or_youth",
                    "buyer/recruiting_club",
                    news_origin="first_hand_report",
                ),
            ),
            (
                "Arsenal's men's first team trained this morning.",
                ineligible("ordinary_team_news"),
            ),
        )
        for text, decision in scenarios:
            with self.subTest(text=text):
                post_id, store, notifier = self._run(text, decision)
                self.assertEqual(PostState.FILTERED, store.post_state(post_id))
                self.assertEqual([], notifier.deliveries)

    def test_local_validation_rejects_eligible_result_with_no_participation(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "requires the Arsenal scope gate to pass",
        ):
            Classification.from_mapping(
                {
                    "eligible": True,
                    "arsenal_scope_eligible": False,
                    "arsenal_participation": "none",
                    "news_origin": "first_hand_report",
                    "translation_zh": "前阿森纳球员可能加盟另一家俱乐部。",
                    "reason_code": "transfer_update",
                    "has_substantive_new_information": True,
                }
            )

    def test_former_player_reason_requires_no_participation(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "requires arsenal_participation=none",
        ):
            Classification.from_mapping(
                {
                    "eligible": False,
                    "arsenal_scope_eligible": False,
                    "arsenal_participation": "seller/current_club",
                    "news_origin": "first_hand_report",
                    "translation_zh": None,
                    "reason_code": "former_arsenal_player_unrelated",
                    "has_substantive_new_information": False,
                }
            )


if __name__ == "__main__":
    unittest.main()
