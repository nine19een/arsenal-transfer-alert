from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from arsenal_alert.bark import BarkDeliveryUncertain, BarkRetryableError
from arsenal_alert.config import Settings, SourceCatalog
from arsenal_alert.deepseek import ClassifierInvalidResponse
from arsenal_alert.models import (
    Classification,
    ClassifierResult,
    FetchResult,
    NotificationPayload,
    Post,
    QueryCursor,
    QuerySpec,
    utc_now,
)


ROOT = Path(__file__).resolve().parents[1]


def settings_for(
    database: Path,
    *,
    dry_run: bool = False,
    classification_max_attempts: int = 1,
    extra: dict[str, str] | None = None,
) -> Settings:
    env = {
        "APP_ENV": "development",
        "APP_MODE": "mock",
        "DRY_RUN": "true" if dry_run else "false",
        "PAID_API_CALLS_ENABLED": "false",
        "BARK_SEND_ENABLED": "false",
        "SOURCE_CONFIG_PATH": str(ROOT / "config" / "sources.toml"),
        "DB_PATH": str(database),
        "MOCK_FEED_PATH": str(ROOT / "fixtures" / "mock_posts.json"),
        "MOCK_CLASSIFICATIONS_PATH": str(
            ROOT / "fixtures" / "mock_classifications.json"
        ),
        "X_API_BASE_URL": "https://api.x.invalid",
        "X_BEARER_TOKEN": "unit-test-token",
        "X_POST_READ_UNIT_USD": "0.005",
        "X_USER_READ_UNIT_USD": "0.010",
        "X_MAX_RESULTS": "25",
        "X_MAX_ATTEMPTS": "1",
        "DEEPSEEK_BASE_URL": "https://deepseek.invalid",
        "DEEPSEEK_MODEL": "deepseek-v4-flash",
        "DEEPSEEK_API_KEY": "unit-test-key",
        "DEEPSEEK_INPUT_CACHE_HIT_USD_PER_M": "0.0028",
        "DEEPSEEK_INPUT_CACHE_MISS_USD_PER_M": "0.14",
        "DEEPSEEK_OUTPUT_USD_PER_M": "0.28",
        "BARK_BASE_URL": "https://bark.invalid",
        "BARK_DEVICE_KEY": "unit-test-device",
        "BARK_RETRY_BASE_SECONDS": "0",
        "CLASSIFICATION_MAX_ATTEMPTS": str(classification_max_attempts),
        "CLASSIFICATION_RETRY_BASE_SECONDS": "0",
    }
    if extra:
        env.update(extra)
    return Settings.from_env(env)


def catalog() -> SourceCatalog:
    return SourceCatalog.load(ROOT / "config" / "sources.toml")


def post(
    post_id: str,
    source_key: str,
    text: str = "Arsenal are in talks over a possible transfer.",
    reference_type: str | None = None,
    referenced_post_id: str = "9000",
    article_url: str | None = None,
) -> Post:
    references = (reference_type,) if reference_type else ()
    raw: dict[str, Any] = {"_mock_source_key": source_key}
    if reference_type:
        raw["referenced_tweets"] = [
            {"type": reference_type, "id": referenced_post_id}
        ]
    if article_url:
        raw["entities"] = {
            "urls": [
                {
                    "url": "https://t.co/example",
                    "expanded_url": article_url,
                }
            ]
        }
    return Post(
        id=post_id,
        author_id=f"mock:{source_key}",
        text=text,
        created_at=utc_now() - timedelta(minutes=2),
        referenced_types=references,
        raw=raw,
    )


def eligible(
    translation: str = "阿森纳正在就一笔可能的转会进行谈判。",
    participation: str = "buyer/recruiting_club",
    reason: str = "transfer_update",
    news_origin: str = "first_hand_report",
) -> Classification:
    return Classification.from_mapping(
        {
            "eligible": True,
            "arsenal_scope_eligible": True,
            "arsenal_participation": participation,
            "news_origin": news_origin,
            "translation_zh": translation,
            "reason_code": reason,
            "has_substantive_new_information": True,
        }
    )


def ineligible(
    reason: str = "ordinary_team_news",
    participation: str = "none",
    *,
    scope_eligible: bool = False,
    news_origin: str = "unclear_origin",
) -> Classification:
    return Classification.from_mapping(
        {
            "eligible": False,
            "arsenal_scope_eligible": scope_eligible,
            "arsenal_participation": participation,
            "news_origin": news_origin,
            "translation_zh": None,
            "reason_code": reason,
            "has_substantive_new_information": False,
        }
    )


class StaticXClient:
    def __init__(self, posts: list[Post], *, ignore_cursor: bool = False) -> None:
        self.posts = list(posts)
        self.ignore_cursor = ignore_cursor
        self.calls: list[tuple[str, str | None]] = []

    def fetch(self, spec: QuerySpec, cursor: QueryCursor, on_page: Any) -> FetchResult:
        self.calls.append((spec.key, cursor.since_id))
        matching = [
            item
            for item in self.posts
            if item.raw["_mock_source_key"] in spec.source_keys
            and (
                self.ignore_cursor
                or cursor.since_id is None
                or int(item.id) > int(cursor.since_id)
            )
        ]
        on_page(matching)
        newest = max((item.id for item in matching), key=int, default=None)
        return FetchResult(newest_id=newest, page_count=1, post_count=len(matching))


class DecisionClassifier:
    def __init__(self, decisions: dict[str, Classification | str]) -> None:
        self.decisions = decisions
        self.calls: list[str] = []

    def classify(self, item: Post, _source: Any) -> ClassifierResult:
        self.calls.append(item.id)
        decision = self.decisions[item.id]
        if decision == "__INVALID__":
            raise ClassifierInvalidResponse("invalid mock response")
        assert isinstance(decision, Classification)
        return ClassifierResult(classification=decision)


class PlannedNotifier:
    def __init__(self, outcomes: list[str] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[NotificationPayload] = []
        self.deliveries: list[NotificationPayload] = []

    def send(self, payload: NotificationPayload) -> None:
        self.calls.append(payload)
        outcome = self.outcomes.pop(0) if self.outcomes else "success"
        if outcome == "retry":
            raise BarkRetryableError("temporary")
        if outcome == "uncertain":
            raise BarkDeliveryUncertain("uncertain")
        self.deliveries.append(payload)
