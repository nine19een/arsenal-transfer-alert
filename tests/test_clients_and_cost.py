from __future__ import annotations

import json
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from arsenal_alert.bark import BarkClient
from arsenal_alert.cost import XBudgetExceeded, XBudgetGuard
from arsenal_alert.db import StateStore
from arsenal_alert.deepseek import DeepSeekClassifier
from arsenal_alert.http_transport import HttpResponse, UrllibTransport
from arsenal_alert.mock import MockXClient
from arsenal_alert.models import NotificationPayload, QueryCursor, QuerySpec, utc_now
from arsenal_alert.x_api import XApiClient, XResponseError

from tests.helpers import catalog, post, settings_for


class QueueTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(
        self,
        method,
        url,
        *,
        headers=None,
        json_body=None,
        timeout,
    ):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json_body": json_body,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


class FakeUrlResponse:
    status = 200
    headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return b'{"code":200}'


def response(payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={},
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


class ClientAndCostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = (
            Path(__file__).resolve().parents[1]
            / "data"
            / f"test-{uuid.uuid4().hex}.sqlite3"
        )
        self.addCleanup(self._remove_database)
        self.settings = settings_for(self.database)
        self.store = StateStore(self.database)
        self.addCleanup(self.store.close)

    def _remove_database(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database}{suffix}").unlink(missing_ok=True)

    def test_x_recent_search_paginates_and_keeps_first_newest_id(self) -> None:
        first_post = {
            "id": "3010",
            "author_id": "123",
            "text": "Arsenal talks.",
            "created_at": "2026-07-29T00:10:00Z",
        }
        second_post = {
            "id": "3009",
            "author_id": "123",
            "text": "Arsenal contact.",
            "created_at": "2026-07-29T00:09:00Z",
        }
        transport = QueueTransport(
            [
                response(
                    {
                        "data": [first_post],
                        "meta": {"newest_id": "3010", "next_token": "NEXT"},
                    }
                ),
                response({"data": [second_post], "meta": {"newest_id": "3009"}}),
            ]
        )
        client = XApiClient(
            self.settings,
            self.store,
            transport=transport,
            sleeper=lambda _delay: None,
        )
        pages = []
        spec = QuerySpec("test", "from:example -is:retweet", ("david_ornstein",))
        cursor = QueryCursor("test", spec.fingerprint, "3000", utc_now())
        result = client.fetch(spec, cursor, pages.append)
        self.assertEqual("3010", result.newest_id)
        self.assertEqual(2, result.page_count)
        self.assertEqual(2, result.post_count)
        self.assertIn("since_id=3000", transport.requests[0]["url"])
        self.assertIn("next_token=NEXT", transport.requests[1]["url"])
        self.assertEqual(2, self.store.cost_snapshot(utc_now() - timedelta(days=1)).x_post_units)

    def test_x_partial_response_is_persisted_but_not_accepted_as_complete(self) -> None:
        returned_post = {
            "id": "3011",
            "author_id": "123",
            "text": "Arsenal talks.",
            "created_at": "2026-07-29T00:11:00Z",
        }
        transport = QueueTransport(
            [
                response(
                    {
                        "data": [returned_post],
                        "errors": [
                            {
                                "title": "PartialError",
                                "detail": "A requested expansion was unavailable.",
                            }
                        ],
                        "meta": {"newest_id": "3011"},
                    }
                )
            ]
        )
        client = XApiClient(
            self.settings,
            self.store,
            transport=transport,
            sleeper=lambda _delay: None,
        )
        pages = []
        spec = QuerySpec("test", "from:example -is:retweet", ("david_ornstein",))
        cursor = QueryCursor("test", spec.fingerprint, "3000", utc_now())

        with self.assertRaises(XResponseError):
            client.fetch(spec, cursor, pages.append)

        self.assertEqual(["3011"], [item.id for page in pages for item in page])
        self.assertEqual(
            1,
            self.store.cost_snapshot(utc_now() - timedelta(days=1)).x_post_units,
        )

    def test_x_budget_guard_reserves_worst_case_page(self) -> None:
        self.store.record_x_response(
            (str(4000 + index) for index in range(390)),
            Decimal("0.005"),
        )
        guard = XBudgetGuard(self.store, self.settings)
        with self.assertRaises(XBudgetExceeded):
            guard.authorize_post_page()

    def test_mock_x_feed_does_not_pollute_paid_usage_or_cost(self) -> None:
        mock_client = MockXClient(
            self.settings.mock_feed_path,
            catalog(),
        )
        spec = next(iter(catalog().build_queries()))
        cursor = QueryCursor(spec.key, spec.fingerprint, None, utc_now())
        pages = []

        result = mock_client.fetch(spec, cursor, pages.append)

        self.assertGreater(result.post_count, 0)
        snapshot = self.store.cost_snapshot(utc_now() - timedelta(days=1))
        self.assertEqual(0, snapshot.x_requests)
        self.assertEqual(0, snapshot.x_post_units)
        self.assertEqual(Decimal("0"), snapshot.x_estimated_usd)

    def test_query_change_cannot_reuse_old_cursor_after_first_error(self) -> None:
        successful_at = utc_now() - timedelta(hours=1)
        self.store.commit_cursor("query", "old-fingerprint", "2999", successful_at)

        self.store.mark_cursor_error("query", "new-fingerprint", "temporary_error")

        cursor = self.store.get_cursor("query", "new-fingerprint")
        self.assertIsNone(cursor.since_id)
        self.assertIsNone(cursor.last_success_at)

    def test_bark_json_round_trip_preserves_utf8_without_terminal_codepage(self) -> None:
        title = "\U0001f534\u26aa Arsenal Transfer Alert"
        body = (
            "\u963f\u68ee\u7eb3\u8f6c\u4f1a\u63a8\u9001\u3002\n"
            "Chinese, English, Emoji, and newline remain unchanged."
        )
        payload = NotificationPayload(
            post_id="utf8-test",
            bark_id="arsenal-transfer-utf8-test",
            title=title,
            body=body,
            url="",
            group="Arsenal Transfer Alert",
            level="active",
            sound="",
        )

        with patch(
            "arsenal_alert.http_transport.urllib.request.urlopen",
            return_value=FakeUrlResponse(),
        ) as urlopen:
            BarkClient(
                self.settings,
                self.store,
                transport=UrllibTransport(),
            ).send(payload)

        self.assertEqual(1, urlopen.call_count)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            "application/json; charset=utf-8",
            request.get_header("Content-type"),
        )
        self.assertIsInstance(request.data, bytes)
        decoded = json.loads(request.data.decode("utf-8"))
        self.assertEqual(title, decoded["title"])
        self.assertEqual(body, decoded["body"])
        self.assertNotIn("?", decoded["title"])
        self.assertNotIn("?", decoded["body"])
        self.assertIn(title.encode("utf-8"), request.data)
        self.assertIn(
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
            request.data,
        )

    def test_deepseek_request_disables_thinking_and_validates_json(self) -> None:
        payload = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "eligible": True,
                                "arsenal_scope_eligible": True,
                                "arsenal_participation": "buyer/recruiting_club",
                                "news_origin": "first_hand_report",
                                "translation_zh": "阿森纳可能考虑这笔转会。",
                                "reason_code": "transfer_update",
                                "has_substantive_new_information": True,
                            },
                            ensure_ascii=False,
                        )
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 100,
                "completion_tokens": 20,
            },
        }
        transport = QueueTransport([response(payload)])
        classifier = DeepSeekClassifier(
            self.settings,
            self.store,
            transport=transport,
            sleeper=lambda _delay: None,
        )
        result = classifier.classify(
            post("3020", "david_ornstein", "Arsenal may consider a move."),
            catalog().by_key()["david_ornstein"],
        )
        request_body = transport.requests[0]["json_body"]
        self.assertEqual({"type": "disabled"}, request_body["thinking"])
        self.assertEqual({"type": "json_object"}, request_body["response_format"])
        self.assertEqual("deepseek-v4-flash", request_body["model"])
        self.assertIn(
            "Mandatory Arsenal participation gate",
            request_body["messages"][0]["content"],
        )
        self.assertIn(
            "Mandatory news-origin gate",
            request_body["messages"][0]["content"],
        )
        self.assertIn(
            'Merely writing "Exclusive"',
            request_body["messages"][0]["content"],
        )
        user_message = json.loads(
            request_body["messages"][1]["content"].split("\n", 1)[1]
        )
        self.assertEqual(
            {
                "referenced_post_id": None,
                "normalized_article_urls": [],
                "previously_notified_same_origin": False,
                "prior_original_report_text": None,
            },
            user_message["origin_metadata"],
        )
        self.assertIn("可能", result.classification.translation_zh)
        snapshot = self.store.cost_snapshot(utc_now() - timedelta(days=1))
        self.assertEqual(1, snapshot.deepseek_requests)
        self.assertGreater(snapshot.deepseek_estimated_usd, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
