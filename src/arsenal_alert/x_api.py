from __future__ import annotations

import json
import logging
import random
import time
import urllib.parse
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from .config import Settings
from .cost import XBudgetGuard
from .db import StateStore
from .http_transport import HttpResponse, HttpTransport, TransportError, UrllibTransport
from .models import FetchResult, Post, QueryCursor, QuerySpec, isoformat_z, utc_now


LOGGER = logging.getLogger(__name__)


class XApiError(RuntimeError):
    pass


class XAuthenticationError(XApiError):
    pass


class XResponseError(XApiError):
    pass


class XPageLimitError(XApiError):
    pass


PageHandler = Callable[[list[Post]], None]


class XApiClient:
    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        transport: HttpTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.store = store
        self.transport = transport or UrllibTransport()
        self.sleeper = sleeper
        self.budget = XBudgetGuard(store, settings)

    def fetch(
        self,
        spec: QuerySpec,
        cursor: QueryCursor,
        on_page: PageHandler,
    ) -> FetchResult:
        params: dict[str, str] = {
            "query": spec.query,
            "max_results": str(self.settings.x_max_results),
            "sort_order": "recency",
            "tweet.fields": (
                "author_id,created_at,edit_history_tweet_ids,referenced_tweets,"
                "in_reply_to_user_id,conversation_id,lang,note_tweet,entities"
            ),
        }
        if cursor.since_id:
            params["since_id"] = cursor.since_id
        else:
            now = utc_now()
            start = (
                cursor.last_success_at - timedelta(seconds=self.settings.x_overlap_seconds)
                if cursor.last_success_at
                else now - timedelta(minutes=self.settings.x_initial_lookback_minutes)
            )
            earliest = now - timedelta(days=6, hours=23)
            params["start_time"] = isoformat_z(max(start, earliest))

        next_token: str | None = None
        page_count = 0
        post_count = 0
        first_page_newest: str | None = None
        while True:
            page_count += 1
            if page_count > self.settings.x_max_pages_per_query:
                raise XPageLimitError("X pagination exceeded the configured safety limit")
            request_params = dict(params)
            if next_token:
                request_params["next_token"] = next_token
            payload = self._recent_search(request_params)
            raw_data = payload.get("data", [])
            if raw_data is None:
                raw_data = []
            if not isinstance(raw_data, list):
                raise XResponseError("X data field is not an array")
            posts: list[Post] = []
            for item in raw_data:
                if not isinstance(item, dict):
                    raise XResponseError("X data item is not an object")
                try:
                    posts.append(Post.from_x(item))
                except ValueError as error:
                    raise XResponseError("X returned an invalid Post object") from error
            self.store.record_x_response(
                (post.id for post in posts),
                self.settings.x_post_read_unit_usd,
            )
            on_page(posts)
            post_count += len(posts)
            partial_errors = payload.get("errors")
            if partial_errors:
                if not isinstance(partial_errors, list):
                    raise XResponseError("X errors field is not an array")
                raise XResponseError(
                    "X returned a partial response; persisted Posts will be retried before advancing the cursor"
                )
            meta = payload.get("meta", {})
            if not isinstance(meta, dict):
                raise XResponseError("X meta field is not an object")
            newest = meta.get("newest_id")
            if page_count == 1 and newest is not None:
                if not isinstance(newest, str) or not newest.isdigit():
                    raise XResponseError("X newest_id is invalid")
                first_page_newest = newest
            raw_next_token = meta.get("next_token")
            if raw_next_token is None:
                break
            if not isinstance(raw_next_token, str) or not raw_next_token:
                raise XResponseError("X next_token is invalid")
            next_token = raw_next_token
        return FetchResult(
            newest_id=first_page_newest,
            page_count=page_count,
            post_count=post_count,
        )

    def lookup_sources(
        self,
        *,
        usernames: list[str] | None = None,
        user_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if bool(usernames) == bool(user_ids):
            raise ValueError("provide exactly one of usernames or user_ids")
        identifiers = usernames if usernames else user_ids
        assert identifiers is not None
        self.budget.authorize_user_lookup(len(identifiers))
        if usernames:
            path = "/2/users/by"
            params = {"usernames": ",".join(usernames)}
        else:
            path = "/2/users"
            params = {"ids": ",".join(user_ids or [])}
        params["user.fields"] = (
            "id,name,username,verified,verified_type,affiliation,description,url,"
            "protected,parody"
        )
        payload = self._get_json(path, params, operation="user_lookup", budget_post_page=False)
        raw_data = payload.get("data", [])
        if not isinstance(raw_data, list):
            raise XResponseError("X user lookup data field is not an array")
        users: list[dict[str, Any]] = []
        for item in raw_data:
            if not isinstance(item, dict):
                raise XResponseError("X user lookup item is not an object")
            user_id = item.get("id")
            username = item.get("username")
            if not isinstance(user_id, str) or not user_id.isdigit():
                raise XResponseError("X user id is invalid")
            if not isinstance(username, str) or not username:
                raise XResponseError("X username is invalid")
            users.append(item)
        self.store.record_x_user_response(
            (user["id"] for user in users),
            self.settings.x_user_read_unit_usd,
        )
        return users

    def _recent_search(self, params: dict[str, str]) -> dict[str, Any]:
        return self._get_json(
            "/2/tweets/search/recent",
            params,
            operation="recent_search",
            budget_post_page=True,
        )

    def _get_json(
        self,
        path: str,
        params: dict[str, str],
        *,
        operation: str,
        budget_post_page: bool,
    ) -> dict[str, Any]:
        url = f"{self.settings.x_api_base_url}{path}?{urllib.parse.urlencode(params)}"
        last_error: Exception | None = None
        for attempt in range(1, self.settings.x_max_attempts + 1):
            if budget_post_page:
                self.budget.authorize_post_page()
            try:
                response = self.transport.request(
                    "GET",
                    url,
                    headers={
                        "Authorization": f"Bearer {self.settings.x_bearer_token}",
                        "Accept": "application/json",
                        "User-Agent": "arsenal-transfer-alert/0.1",
                    },
                    timeout=self.settings.x_http_timeout_seconds,
                )
            except TransportError as error:
                self.store.record_api_error("x", operation, "network_error")
                last_error = error
                if attempt == self.settings.x_max_attempts:
                    break
                self._sleep(attempt, None)
                continue
            if response.status in {401, 403}:
                self.store.record_api_error("x", operation, f"http_{response.status}")
                raise XAuthenticationError(f"X rejected authentication with HTTP {response.status}")
            if response.status == 429 or response.status >= 500:
                self.store.record_api_error("x", operation, f"http_{response.status}")
                last_error = XApiError(f"temporary X HTTP {response.status}")
                if attempt == self.settings.x_max_attempts:
                    break
                self._sleep(attempt, response)
                continue
            if not 200 <= response.status < 300:
                self.store.record_api_error("x", operation, f"http_{response.status}")
                raise XApiError(f"X returned non-retryable HTTP {response.status}")
            try:
                payload = response.json()
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                self.store.record_api_error("x", operation, "invalid_json")
                raise XResponseError("X returned invalid JSON") from error
            if not isinstance(payload, dict):
                self.store.record_api_error("x", operation, "invalid_shape")
                raise XResponseError("X response is not a JSON object")
            return payload
        raise XApiError("X request retries exhausted") from last_error

    def _sleep(self, attempt: int, response: HttpResponse | None) -> None:
        delay = min(2 ** (attempt - 1), 30) + random.random()
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    delay = min(max(float(retry_after), 0), 60)
                except ValueError:
                    pass
            elif response.headers.get("x-rate-limit-reset"):
                try:
                    reset_at = float(response.headers["x-rate-limit-reset"])
                    delay = min(max(reset_at - time.time() + 1, 0), 60)
                except ValueError:
                    pass
        LOGGER.warning(
            "x_request_retry",
            extra={"attempt": attempt, "delay_seconds": round(delay, 2)},
        )
        self.sleeper(delay)
