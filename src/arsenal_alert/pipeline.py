from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from .bark import (
    BarkDeliveryUncertain,
    BarkPermanentError,
    BarkRetryableError,
)
from .config import ConfigurationError, Settings, SourceCatalog
from .cost import XBudgetExceeded, XRequestRateExceeded, billing_cycle_start
from .db import StateStore
from .deepseek import ClassifierError
from .models import (
    AppMode,
    ClassifierResult,
    FetchResult,
    NotificationPayload,
    Post,
    PostState,
    QueryCursor,
    QuerySpec,
    Source,
    utc_now,
)
from .notification import build_notification
from .origin import original_report_fingerprint
from .x_api import XApiError


LOGGER = logging.getLogger(__name__)


class XClient(Protocol):
    def fetch(
        self,
        spec: QuerySpec,
        cursor: QueryCursor,
        on_page: Callable[[list[Post]], None],
    ) -> FetchResult: ...


class Classifier(Protocol):
    def classify(self, post: Post, source: Source) -> ClassifierResult: ...


class Notifier(Protocol):
    def send(self, payload: NotificationPayload) -> None: ...


class IdentityMonitor(Protocol):
    def ready(self) -> bool: ...


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        catalog: SourceCatalog,
        store: StateStore,
        x_client: XClient,
        classifier: Classifier,
        notifier: Notifier,
        identity_monitor: IdentityMonitor | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.store = store
        self.x_client = x_client
        self.classifier = classifier
        self.notifier = notifier
        self.identity_monitor = identity_monitor
        self.queries = catalog.build_queries()
        self.sources_by_key = catalog.by_key()
        self.sources_by_id = catalog.by_user_id()

    def recover(self) -> None:
        uncertain = self.store.recover_inflight_notifications()
        if uncertain:
            self.store.set_health_flag(
                "delivery_recovery_uncertain",
                "critical",
                f"{uncertain} Bark delivery attempt(s) were interrupted; automatic retry was suppressed",
            )
            LOGGER.error(
                "delivery_recovery_uncertain",
                extra={"notification_count": uncertain},
            )

    def run_cycle(self) -> None:
        if self.settings.app_mode is AppMode.LIVE:
            try:
                self.settings.assert_live_prices_fresh()
            except ConfigurationError as error:
                self.store.set_health_flag(
                    "price_verification_stale",
                    "critical",
                    str(error),
                )
                LOGGER.error("polling_blocked_by_stale_price_verification")
                return
            self.store.clear_health_flag("price_verification_stale")
        if self.identity_monitor is not None and not self.identity_monitor.ready():
            LOGGER.error("x_polling_blocked_by_source_identity")
            self._update_cost_health()
            return
        for spec in self.queries:
            self._poll_query(spec)
        self._process_pending_posts()
        if not self.settings.dry_run:
            self._deliver_notifications()
        self._update_cost_health()
        self.store.run_maintenance(self.settings.usage_retention_days)

    def _poll_query(self, spec: QuerySpec) -> None:
        cursor = self.store.get_cursor(spec.key, spec.fingerprint)
        if cursor.last_success_at and utc_now() - cursor.last_success_at > timedelta(days=7):
            self.store.set_health_flag(
                f"x_gap_{spec.key}",
                "critical",
                "X outage exceeded Recent Search's seven-day recovery window; an unrecoverable gap may exist",
            )
        try:
            result = self.x_client.fetch(
                spec,
                cursor,
                lambda posts: self._store_page(spec, posts),
            )
        except XBudgetExceeded as error:
            self.store.mark_cursor_error(spec.key, spec.fingerprint, "budget_guard")
            self.store.set_health_flag("x_budget_guard", "critical", str(error))
            LOGGER.error("x_budget_guard_blocked", extra={"query_key": spec.key})
            return
        except XRequestRateExceeded as error:
            self.store.mark_cursor_error(spec.key, spec.fingerprint, "request_rate_guard")
            self.store.set_health_flag("x_request_rate_guard", "warning", str(error))
            LOGGER.warning("x_request_rate_guard_blocked", extra={"query_key": spec.key})
            return
        except XApiError as error:
            error_code = type(error).__name__
            self.store.mark_cursor_error(spec.key, spec.fingerprint, error_code)
            self.store.set_health_flag(
                f"x_query_{spec.key}",
                "warning",
                f"X query failed: {error_code}",
            )
            LOGGER.warning(
                "x_query_failed",
                extra={"query_key": spec.key, "error_code": error_code},
            )
            return
        self.store.commit_cursor(spec.key, spec.fingerprint, result.newest_id, utc_now())
        self.store.clear_health_flag(f"x_query_{spec.key}")
        self.store.clear_health_flag("x_request_rate_guard")
        self.store.clear_health_flag("x_budget_guard")
        LOGGER.info(
            "x_query_complete",
            extra={
                "query_key": spec.key,
                "pages": result.page_count,
                "posts": result.post_count,
            },
        )

    def _store_page(self, spec: QuerySpec, posts: list[Post]) -> None:
        records: list[tuple[Post, str]] = []
        allowed = set(spec.source_keys)
        for post in posts:
            source_key = ""
            source = self.sources_by_id.get(post.author_id)
            if source and source.key in allowed:
                source_key = source.key
            elif self.settings.app_mode is AppMode.MOCK:
                hint = post.raw.get("_mock_source_key")
                if isinstance(hint, str) and hint in allowed:
                    source_key = hint
            records.append((post, source_key))
        inserted = self.store.store_posts(records)
        if inserted:
            LOGGER.info("posts_persisted", extra={"count": inserted, "query_key": spec.key})

    def _process_pending_posts(self) -> None:
        while True:
            pending = self.store.pending_posts(limit=100)
            if not pending:
                return
            for stored in pending:
                post = stored.post
                source = self.sources_by_key.get(stored.source_key)
                if source is None or not self._author_matches(post, source):
                    self.store.mark_post_terminal(
                        post.id,
                        PostState.REJECTED_SOURCE,
                        "author_id_not_in_verified_whitelist",
                    )
                    self.store.set_health_flag(
                        f"source_mismatch_{post.id}",
                        "critical",
                        "A fetched Post did not match a configured numeric source ID",
                    )
                    LOGGER.error(
                        "post_rejected_source",
                        extra={"post_id": post.id, "author_id": post.author_id},
                    )
                    continue
                if post.is_pure_repost:
                    self.store.mark_post_terminal(post.id, PostState.PURE_REPOST)
                    LOGGER.info("post_skipped_pure_repost", extra={"post_id": post.id})
                    continue
                origin = original_report_fingerprint(post)
                origin_fingerprint = origin.value if origin else None
                attempt = self.store.increment_classification_attempt(post.id)
                try:
                    result = self.classifier.classify(post, source)
                except ClassifierError as error:
                    self._classification_failed(post.id, attempt, error)
                    continue
                self.store.clear_health_flag(f"classification_{post.id}")
                if not result.classification.eligible:
                    self.store.save_classification(
                        post.id,
                        result.classification,
                        origin_fingerprint,
                    )
                    LOGGER.info(
                        "post_filtered",
                        extra={
                            "post_id": post.id,
                            "source_key": source.key,
                            "reason_code": result.classification.reason_code,
                        },
                    )
                    continue
                payload = build_notification(
                    post,
                    source,
                    result.classification,
                    group=self.settings.bark_group,
                    level=self.settings.bark_level,
                    sound=self.settings.bark_sound,
                )
                created = self.store.save_classification_and_enqueue(
                    post.id,
                    result.classification,
                    payload,
                    self.settings.dry_run,
                    origin_fingerprint,
                )
                if not created and self.store.post_state(post.id) is PostState.FILTERED:
                    LOGGER.info(
                        "duplicate_original_report_suppressed",
                        extra={
                            "post_id": post.id,
                            "source_key": source.key,
                            "origin_kind": origin.kind if origin else None,
                        },
                    )
                if self.settings.dry_run and created:
                    LOGGER.info(
                        "dry_run_notification",
                        extra={
                            "post_id": post.id,
                            "title": payload.title,
                            "body": payload.body,
                            "url": payload.url,
                        },
                    )

    def _author_matches(self, post: Post, source: Source) -> bool:
        if self.settings.app_mode is AppMode.MOCK:
            return post.raw.get("_mock_source_key") == source.key
        return bool(source.user_id) and post.author_id == source.user_id

    def _classification_failed(
        self,
        post_id: str,
        attempt: int,
        error: ClassifierError,
    ) -> None:
        error_code = type(error).__name__
        if attempt < self.settings.classification_max_attempts:
            delay = self.settings.classification_retry_base_seconds * (2 ** (attempt - 1))
            self.store.schedule_classification_retry(
                post_id,
                utc_now() + timedelta(seconds=delay),
                error_code,
            )
            LOGGER.warning(
                "classification_retry_scheduled",
                extra={"post_id": post_id, "attempt": attempt, "error_code": error_code},
            )
            return
        self.store.mark_post_terminal(
            post_id,
            PostState.CLASSIFICATION_ERROR,
            error_code,
        )
        self.store.set_health_flag(
            f"classification_{post_id}",
            "warning",
            f"Post classification failed closed after {attempt} attempt(s): {error_code}",
        )
        LOGGER.error(
            "classification_failed_closed",
            extra={"post_id": post_id, "attempt": attempt, "error_code": error_code},
        )

    def _deliver_notifications(self) -> None:
        while True:
            due = self.store.claim_due_notifications(limit=50)
            if not due:
                return
            for stored in due:
                try:
                    self.notifier.send(stored.payload())
                except BarkRetryableError as error:
                    if stored.attempt_count < self.settings.bark_max_attempts:
                        delay = self.settings.bark_retry_base_seconds * (
                            2 ** (stored.attempt_count - 1)
                        )
                        self.store.schedule_notification_retry(
                            stored.post_id,
                            utc_now() + timedelta(seconds=delay),
                            type(error).__name__,
                        )
                        LOGGER.warning(
                            "bark_retry_scheduled",
                            extra={
                                "post_id": stored.post_id,
                                "attempt": stored.attempt_count,
                                "delay_seconds": delay,
                            },
                        )
                    else:
                        self.store.mark_notification_failed(
                            stored.post_id, "retry_attempts_exhausted"
                        )
                        self.store.set_health_flag(
                            f"bark_{stored.post_id}",
                            "critical",
                            "Bark retry attempts were exhausted",
                        )
                    continue
                except BarkDeliveryUncertain:
                    self.store.mark_notification_uncertain(
                        stored.post_id, "delivery_outcome_uncertain"
                    )
                    self.store.set_health_flag(
                        f"bark_{stored.post_id}",
                        "critical",
                        "Bark delivery may have succeeded; automatic retry was suppressed to avoid duplicates",
                    )
                    continue
                except BarkPermanentError as error:
                    self.store.mark_notification_failed(
                        stored.post_id, type(error).__name__
                    )
                    self.store.set_health_flag(
                        f"bark_{stored.post_id}",
                        "critical",
                        "Bark permanently rejected a notification",
                    )
                    continue
                self.store.mark_notification_sent(stored.post_id)
                self.store.clear_health_flag(f"bark_{stored.post_id}")
                LOGGER.info(
                    "bark_notification_sent",
                    extra={"post_id": stored.post_id, "attempt": stored.attempt_count},
                )

    def _update_cost_health(self) -> None:
        now = utc_now()
        cycle_start = billing_cycle_start(now, self.settings.x_billing_cycle_day)
        spend = self.store.x_estimated_spend_since(cycle_start)
        if spend >= self.settings.x_alert_threshold_usd:
            self.store.set_health_flag(
                "x_cost_alert",
                "warning",
                (
                    f"Estimated X spend is ${spend}; threshold is "
                    f"${self.settings.x_alert_threshold_usd}"
                ),
            )
            LOGGER.warning("x_cost_alert", extra={"estimated_spend_usd": str(spend)})
        else:
            self.store.clear_health_flag("x_cost_alert")
        first, last = self.store.usage_window("x", cycle_start)
        if first is not None and last is not None:
            elapsed_seconds = Decimal(str(max((last - first).total_seconds(), 60)))
            if elapsed_seconds >= Decimal(24 * 60 * 60):
                projected = (
                    spend
                    / elapsed_seconds
                    * Decimal("30.44")
                    * Decimal(24 * 60 * 60)
                )
                if projected >= self.settings.x_alert_threshold_usd:
                    self.store.set_health_flag(
                        "x_cost_projection_alert",
                        "warning",
                        (
                            f"Observed X run rate projects ${projected:.2f} per 30.44 days; "
                            f"threshold is ${self.settings.x_alert_threshold_usd}"
                        ),
                    )
                    LOGGER.warning(
                        "x_cost_projection_alert",
                        extra={"projected_30_44_day_usd": f"{projected:.2f}"},
                    )
                else:
                    self.store.clear_health_flag("x_cost_projection_alert")
            else:
                self.store.clear_health_flag("x_cost_projection_alert")
        else:
            self.store.clear_health_flag("x_cost_projection_alert")
        if spend >= self.settings.active_x_budget_usd:
            self.store.set_health_flag(
                "x_budget_guard",
                "critical",
                "Estimated X spend reached the active application budget",
            )
