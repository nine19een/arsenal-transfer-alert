from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import SourceCatalog
from .deepseek import ClassifierInvalidResponse
from .models import (
    Classification,
    ClassifierResult,
    FetchResult,
    NotificationPayload,
    Post,
    QueryCursor,
    QuerySpec,
)


LOGGER = logging.getLogger(__name__)


class MockXClient:
    def __init__(
        self,
        feed_path: Path,
        catalog: SourceCatalog,
    ) -> None:
        self.catalog = catalog
        try:
            raw = json.loads(feed_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid mock X feed: {feed_path}") from error
        if not isinstance(raw, list):
            raise ValueError("mock X feed must be a JSON array")
        posts: list[Post] = []
        known_keys = catalog.by_key()
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"mock X feed item #{index + 1} must be an object")
            source_key = item.get("source_key")
            if source_key not in known_keys:
                raise ValueError(f"mock X feed item #{index + 1} has an unknown source")
            payload = {key: value for key, value in item.items() if key != "source_key"}
            payload["author_id"] = payload.get("author_id") or f"mock:{source_key}"
            payload["_mock_source_key"] = source_key
            posts.append(Post.from_x(payload))
        self.posts = tuple(posts)

    def fetch(self, spec: QuerySpec, cursor: QueryCursor, on_page: Any) -> FetchResult:
        matching = [
            post
            for post in self.posts
            if post.raw.get("_mock_source_key") in spec.source_keys
            and (cursor.since_id is None or int(post.id) > int(cursor.since_id))
        ]
        matching.sort(key=lambda post: int(post.id), reverse=True)
        on_page(matching)
        newest = max((post.id for post in matching), key=int, default=None)
        return FetchResult(newest_id=newest, page_count=1, post_count=len(matching))


class MockClassifier:
    def __init__(self, decisions_path: Path) -> None:
        try:
            raw = json.loads(decisions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid mock classifications: {decisions_path}") from error
        if not isinstance(raw, dict):
            raise ValueError("mock classifications must be a JSON object keyed by Post ID")
        self.decisions = raw
        self.calls: list[str] = []

    def verify_model(self) -> list[str]:
        return ["mock-classifier"]

    def classify(self, post: Post, _source: Any) -> ClassifierResult:
        self.calls.append(post.id)
        if post.id not in self.decisions:
            raise ClassifierInvalidResponse("mock decision is missing")
        value = self.decisions[post.id]
        if value == "__INVALID__":
            raise ClassifierInvalidResponse("mock returned invalid content")
        try:
            classification = Classification.from_mapping(value)
        except ValueError as error:
            raise ClassifierInvalidResponse("mock decision failed validation") from error
        return ClassifierResult(classification=classification)


class MockBarkClient:
    def __init__(self) -> None:
        self.deliveries: list[NotificationPayload] = []

    def send(self, payload: NotificationPayload) -> None:
        self.deliveries.append(payload)
        LOGGER.info(
            "mock_bark_delivery",
            extra={
                "post_id": payload.post_id,
                "title": payload.title,
                "body": payload.body,
                "url": payload.url,
            },
        )
