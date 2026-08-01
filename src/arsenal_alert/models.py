from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any


class AppMode(StrEnum):
    MOCK = "mock"
    LIVE = "live"


class QueryMode(StrEnum):
    ALL = "all"
    TOPIC = "topic"


class PostState(StrEnum):
    PENDING = "pending"
    CLASSIFICATION_RETRY = "classification_retry"
    CLASSIFICATION_ERROR = "classification_error"
    FILTERED = "filtered"
    REJECTED_SOURCE = "rejected_source"
    PURE_REPOST = "pure_repost"
    NOTIFICATION_PENDING = "notification_pending"
    DRY_RUN = "dry_run"
    NOTIFIED = "notified"
    NOTIFICATION_UNCERTAIN = "notification_uncertain"
    NOTIFICATION_FAILED = "notification_failed"


class NotificationState(StrEnum):
    PENDING = "pending"
    RETRY = "retry"
    SENDING = "sending"
    SENT = "sent"
    DRY_RUN = "dry_run"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


ELIGIBLE_REASON_CODES = frozenset(
    {
        "transfer_update",
        "contract_update",
        "loan_update",
        "deal_failed_or_withdrawn",
        "denial_or_clarification",
        "substantive_reply_or_quote",
    }
)

INELIGIBLE_REASON_CODES = frozenset(
    {
        "attributed_relay",
        "commentary_only",
        "former_target_club_player_unrelated",
        "ordinary_team_news",
        "match_lineup_injury_training",
        "womens_or_youth",
        "tactics_or_opinion",
        "promotion_or_link_only",
        "no_new_facts",
        "unclear_origin",
        "insufficient_own_text",
        "not_target_club_mens_first_team_transfer",
    }
)


class ClubParticipation(StrEnum):
    BUYER_OR_RECRUITING_CLUB = "buyer/recruiting_club"
    SELLER_OR_CURRENT_CLUB = "seller/current_club"
    CONTRACT_PARTY = "contract_party"
    LOAN_OWNER = "loan_owner"
    NONE = "none"


# Public compatibility alias for stored fixtures and callers from catalog version 1.
ArsenalParticipation = ClubParticipation


class NewsOrigin(StrEnum):
    FIRST_HAND_REPORT = "first_hand_report"
    INDEPENDENT_CONFIRMATION = "independent_confirmation"
    SUBSTANTIVE_NEW_DETAIL = "substantive_new_detail"
    ATTRIBUTED_RELAY = "attributed_relay"
    COMMENTARY_ONLY = "commentary_only"
    UNCLEAR_ORIGIN = "unclear_origin"


ALLOWED_NEWS_ORIGINS = frozenset(
    {
        NewsOrigin.FIRST_HAND_REPORT,
        NewsOrigin.INDEPENDENT_CONFIRMATION,
        NewsOrigin.SUBSTANTIVE_NEW_DETAIL,
    }
)

ORIGIN_FILTER_REASON_CODES = frozenset(
    {
        NewsOrigin.ATTRIBUTED_RELAY.value,
        NewsOrigin.COMMENTARY_ONLY.value,
        NewsOrigin.UNCLEAR_ORIGIN.value,
    }
)


@dataclass(frozen=True, slots=True)
class Source:
    key: str
    name: str
    tier: int
    username: str
    user_id: str
    enabled: bool
    query_mode: QueryMode
    identity_status: str
    verified_at: str
    confirmation_required: bool
    confirmed: bool
    identity_evidence_url: str
    notes: str = ""

    @property
    def title(self) -> str:
        return f"[Tier {self.tier}] {self.name}"


@dataclass(frozen=True, slots=True)
class ClubProfile:
    key: str
    name: str
    query_terms: tuple[str, ...]
    topic_query: str
    notification_title_prefix: str
    notification_group: str
    notification_id_prefix: str
    output_language: str
    timezone_utc_offset_minutes: int
    timezone_label: str
    source_label: str
    time_label: str
    open_post_text: str


@dataclass(frozen=True, slots=True)
class QuerySpec:
    key: str
    query: str
    source_keys: tuple[str, ...]

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.query.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Post:
    id: str
    author_id: str
    text: str
    created_at: datetime
    referenced_types: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pure_repost(self) -> bool:
        return "retweeted" in self.referenced_types

    @classmethod
    def from_x(cls, payload: dict[str, Any]) -> "Post":
        post_id = payload.get("id")
        author_id = payload.get("author_id")
        text = payload.get("text")
        note_tweet = payload.get("note_tweet")
        if isinstance(note_tweet, dict) and isinstance(note_tweet.get("text"), str):
            text = note_tweet["text"]
        created_at_raw = payload.get("created_at")
        if not isinstance(post_id, str) or not post_id.isdigit():
            raise ValueError("X Post id must be a numeric string")
        if not isinstance(author_id, str) or not author_id:
            raise ValueError("X author_id must be a non-empty string")
        if not isinstance(text, str):
            raise ValueError("X Post text must be a string")
        if not isinstance(created_at_raw, str):
            raise ValueError("X Post created_at must be an ISO-8601 string")
        created_at = parse_utc_datetime(created_at_raw)
        references = payload.get("referenced_tweets", [])
        reference_types: list[str] = []
        if references is not None:
            if not isinstance(references, list):
                raise ValueError("referenced_tweets must be an array")
            for item in references:
                if not isinstance(item, dict):
                    raise ValueError("referenced_tweets items must be objects")
                reference_type = item.get("type")
                if reference_type not in {"retweeted", "quoted", "replied_to"}:
                    raise ValueError("unknown referenced_tweets type")
                reference_types.append(reference_type)
        return cls(
            id=post_id,
            author_id=author_id,
            text=text,
            created_at=created_at,
            referenced_types=tuple(reference_types),
            raw=dict(payload),
        )

    def to_record_json(self) -> str:
        return json.dumps(self.raw, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Classification:
    eligible: bool
    club_scope_eligible: bool
    club_participation: ClubParticipation
    news_origin: NewsOrigin
    notification_text: str | None
    reason_code: str
    has_substantive_new_information: bool

    @property
    def arsenal_scope_eligible(self) -> bool:
        """Compatibility view for catalog-version-1 callers."""
        return self.club_scope_eligible

    @property
    def arsenal_participation(self) -> ClubParticipation:
        """Compatibility view for catalog-version-1 callers."""
        return self.club_participation

    @property
    def translation_zh(self) -> str | None:
        """Compatibility view for the former Chinese-only output field."""
        return self.notification_text

    @classmethod
    def from_mapping(cls, value: Any) -> "Classification":
        if not isinstance(value, dict):
            raise ValueError("classification must be a JSON object")
        generic_fields = {
            "eligible",
            "club_scope_eligible",
            "club_participation",
            "news_origin",
            "notification_text",
            "reason_code",
            "has_substantive_new_information",
        }
        legacy_fields = {
            "eligible",
            "arsenal_scope_eligible",
            "arsenal_participation",
            "news_origin",
            "translation_zh",
            "reason_code",
            "has_substantive_new_information",
        }
        fields = set(value)
        if fields == generic_fields:
            scope_eligible = value["club_scope_eligible"]
            participation_raw = value["club_participation"]
            notification_text = value["notification_text"]
        elif fields == legacy_fields:
            scope_eligible = value["arsenal_scope_eligible"]
            participation_raw = value["arsenal_participation"]
            notification_text = value["translation_zh"]
        else:
            raise ValueError("classification JSON has missing or extra fields")
        eligible = value["eligible"]
        news_origin_raw = value["news_origin"]
        reason_code = value["reason_code"]
        has_new = value["has_substantive_new_information"]
        if type(eligible) is not bool:
            raise ValueError("eligible must be a JSON boolean")
        if type(scope_eligible) is not bool:
            raise ValueError("club_scope_eligible must be a JSON boolean")
        if type(has_new) is not bool:
            raise ValueError("has_substantive_new_information must be a JSON boolean")
        if not isinstance(reason_code, str):
            raise ValueError("reason_code must be a string")
        reason_code = {
            "former_arsenal_player_unrelated": "former_target_club_player_unrelated",
            "not_arsenal_mens_first_team_transfer": (
                "not_target_club_mens_first_team_transfer"
            ),
        }.get(reason_code, reason_code)
        try:
            participation = ClubParticipation(participation_raw)
        except (TypeError, ValueError) as error:
            raise ValueError("club_participation is invalid") from error
        try:
            news_origin = NewsOrigin(news_origin_raw)
        except (TypeError, ValueError) as error:
            raise ValueError("news_origin is invalid") from error
        if participation is ClubParticipation.NONE and scope_eligible:
            raise ValueError("club_scope_eligible requires the target club to participate")
        if eligible:
            if not scope_eligible or participation is ClubParticipation.NONE:
                raise ValueError("eligible result requires the club scope gate to pass")
            if news_origin not in ALLOWED_NEWS_ORIGINS:
                raise ValueError("eligible result requires an allowed news_origin")
            if reason_code not in ELIGIBLE_REASON_CODES:
                raise ValueError("eligible result has an unsupported reason_code")
            if not has_new:
                raise ValueError("eligible result must contain substantive new information")
            if not isinstance(notification_text, str) or not notification_text.strip():
                raise ValueError("eligible result must contain notification_text")
            if len(notification_text) > 2_500:
                raise ValueError("notification_text is too long for a mobile notification")
        else:
            if reason_code not in INELIGIBLE_REASON_CODES:
                raise ValueError("ineligible result has an unsupported reason_code")
            if notification_text is not None:
                raise ValueError("ineligible result must set notification_text to null")
            if (
                reason_code == "former_target_club_player_unrelated"
                and participation is not ClubParticipation.NONE
            ):
                raise ValueError(
                    "former_target_club_player_unrelated requires club_participation=none"
                )
            if reason_code in ORIGIN_FILTER_REASON_CODES:
                if news_origin.value != reason_code:
                    raise ValueError("origin filter reason must match news_origin")
        return cls(
            eligible=eligible,
            club_scope_eligible=scope_eligible,
            club_participation=participation,
            news_origin=news_origin,
            notification_text=(
                notification_text.strip()
                if isinstance(notification_text, str)
                else None
            ),
            reason_code=reason_code,
            has_substantive_new_information=has_new,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "eligible": self.eligible,
                "club_scope_eligible": self.club_scope_eligible,
                "club_participation": self.club_participation.value,
                "news_origin": self.news_origin.value,
                "notification_text": self.notification_text,
                "reason_code": self.reason_code,
                "has_substantive_new_information": self.has_substantive_new_information,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class ModelUsage:
    prompt_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    completion_tokens: int = 0

    def estimated_cost(
        self,
        cache_hit_per_m: Decimal,
        cache_miss_per_m: Decimal,
        output_per_m: Decimal,
    ) -> Decimal:
        million = Decimal(1_000_000)
        return (
            Decimal(self.prompt_cache_hit_tokens) * cache_hit_per_m
            + Decimal(self.prompt_cache_miss_tokens) * cache_miss_per_m
            + Decimal(self.completion_tokens) * output_per_m
        ) / million


@dataclass(frozen=True, slots=True)
class ClassifierResult:
    classification: Classification
    usage: ModelUsage = ModelUsage()


@dataclass(frozen=True, slots=True)
class NotificationPayload:
    post_id: str
    bark_id: str
    title: str
    body: str
    url: str
    group: str
    level: str
    sound: str = ""

    def as_bark_json(self, device_key: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "device_key": device_key,
            "id": self.bark_id,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "group": self.group,
            "level": self.level,
        }
        if self.sound:
            body["sound"] = self.sound
        return body


@dataclass(frozen=True, slots=True)
class QueryCursor:
    query_key: str
    query_fingerprint: str
    since_id: str | None
    last_success_at: datetime | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    newest_id: str | None
    page_count: int
    post_count: int


@dataclass(frozen=True, slots=True)
class CostSnapshot:
    cycle_start: datetime
    x_post_units: int
    x_estimated_usd: Decimal
    x_requests: int
    deepseek_requests: int
    deepseek_estimated_usd: Decimal
    bark_requests: int


def parse_utc_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
