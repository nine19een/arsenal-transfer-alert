from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from .models import AppMode, ClubProfile, QueryMode, QuerySpec, Source


class ConfigurationError(ValueError):
    pass


def load_env_file(path: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    target = dict(os.environ if environ is None else environ)
    if not path.exists():
        return target
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"{path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or any(character.isspace() for character in key):
            raise ConfigurationError(f"{path}:{line_number}: invalid environment variable name")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        target.setdefault(key, value)
    return target


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{key} must be true or false")


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{key} must be an integer") from error


def _decimal(env: Mapping[str, str], key: str, default: str = "0") -> Decimal:
    raw = env.get(key, default)
    try:
        result = Decimal(raw)
    except InvalidOperation as error:
        raise ConfigurationError(f"{key} must be a decimal number") from error
    if not result.is_finite():
        raise ConfigurationError(f"{key} must be finite")
    return result


def _optional_date(env: Mapping[str, str], key: str) -> date | None:
    raw = env.get(key, "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise ConfigurationError(f"{key} must use YYYY-MM-DD") from error


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    app_mode: AppMode
    dry_run: bool
    paid_api_calls_enabled: bool
    bark_send_enabled: bool
    source_config_path: Path
    db_path: Path
    mock_feed_path: Path
    mock_classifications_path: Path
    log_level: str
    health_host: str
    health_port: int
    usage_retention_days: int
    x_api_base_url: str
    x_bearer_token: str
    x_poll_interval_seconds: int
    x_initial_lookback_minutes: int
    x_overlap_seconds: int
    x_max_results: int
    x_max_pages_per_query: int
    x_max_requests_per_hour: int
    x_identity_recheck_hours: int
    x_identity_retry_minutes: int
    x_http_timeout_seconds: int
    x_max_attempts: int
    x_post_read_unit_usd: Decimal
    x_user_read_unit_usd: Decimal
    x_price_verified_at: date | None
    x_price_max_age_days: int
    x_monthly_budget_usd: Decimal
    x_development_budget_usd: Decimal
    x_alert_threshold_usd: Decimal
    x_billing_cycle_day: int
    deepseek_base_url: str
    deepseek_model: str
    deepseek_api_key: str
    deepseek_http_timeout_seconds: int
    deepseek_max_attempts: int
    deepseek_thinking_enabled: bool
    deepseek_max_tokens: int
    deepseek_price_verified_at: date | None
    deepseek_price_max_age_days: int
    deepseek_input_cache_hit_usd_per_m: Decimal
    deepseek_input_cache_miss_usd_per_m: Decimal
    deepseek_output_usd_per_m: Decimal
    bark_base_url: str
    bark_device_key: str
    bark_group: str
    bark_level: str
    bark_sound: str
    bark_http_timeout_seconds: int
    bark_max_attempts: int
    bark_retry_base_seconds: int
    classification_max_attempts: int
    classification_retry_base_seconds: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        mode_raw = source.get("APP_MODE", "mock").strip().lower()
        try:
            app_mode = AppMode(mode_raw)
        except ValueError as error:
            raise ConfigurationError("APP_MODE must be mock or live") from error
        settings = cls(
            app_env=source.get("APP_ENV", "development").strip().lower(),
            app_mode=app_mode,
            dry_run=_bool(source, "DRY_RUN", True),
            paid_api_calls_enabled=_bool(source, "PAID_API_CALLS_ENABLED", False),
            bark_send_enabled=_bool(source, "BARK_SEND_ENABLED", False),
            source_config_path=Path(source.get("SOURCE_CONFIG_PATH", "config/sources.toml")),
            db_path=Path(source.get("DB_PATH", "data/arsenal-alert.sqlite3")),
            mock_feed_path=Path(source.get("MOCK_FEED_PATH", "fixtures/mock_posts.json")),
            mock_classifications_path=Path(
                source.get("MOCK_CLASSIFICATIONS_PATH", "fixtures/mock_classifications.json")
            ),
            log_level=source.get("LOG_LEVEL", "INFO").strip().upper(),
            health_host=source.get("HEALTH_HOST", "127.0.0.1").strip(),
            health_port=_int(source, "HEALTH_PORT", 8080),
            usage_retention_days=_int(source, "USAGE_RETENTION_DAYS", 45),
            x_api_base_url=source.get("X_API_BASE_URL", "").strip().rstrip("/"),
            x_bearer_token=source.get("X_BEARER_TOKEN", "").strip(),
            x_poll_interval_seconds=_int(source, "X_POLL_INTERVAL_SECONDS", 60),
            x_initial_lookback_minutes=_int(source, "X_INITIAL_LOOKBACK_MINUTES", 120),
            x_overlap_seconds=_int(source, "X_OVERLAP_SECONDS", 90),
            x_max_results=_int(source, "X_MAX_RESULTS", 25),
            x_max_pages_per_query=_int(source, "X_MAX_PAGES_PER_QUERY", 5),
            x_max_requests_per_hour=_int(source, "X_MAX_REQUESTS_PER_HOUR", 130),
            x_identity_recheck_hours=_int(source, "X_IDENTITY_RECHECK_HOURS", 168),
            x_identity_retry_minutes=_int(source, "X_IDENTITY_RETRY_MINUTES", 60),
            x_http_timeout_seconds=_int(source, "X_HTTP_TIMEOUT_SECONDS", 20),
            x_max_attempts=_int(source, "X_MAX_ATTEMPTS", 4),
            x_post_read_unit_usd=_decimal(source, "X_POST_READ_UNIT_USD"),
            x_user_read_unit_usd=_decimal(source, "X_USER_READ_UNIT_USD"),
            x_price_verified_at=_optional_date(source, "X_PRICE_VERIFIED_AT"),
            x_price_max_age_days=_int(source, "X_PRICE_MAX_AGE_DAYS", 7),
            x_monthly_budget_usd=_decimal(source, "X_MONTHLY_BUDGET_USD", "10"),
            x_development_budget_usd=_decimal(source, "X_DEVELOPMENT_BUDGET_USD", "2"),
            x_alert_threshold_usd=_decimal(source, "X_ALERT_THRESHOLD_USD", "8"),
            x_billing_cycle_day=_int(source, "X_BILLING_CYCLE_DAY", 1),
            deepseek_base_url=source.get("DEEPSEEK_BASE_URL", "").strip().rstrip("/"),
            deepseek_model=source.get("DEEPSEEK_MODEL", "").strip(),
            deepseek_api_key=source.get("DEEPSEEK_API_KEY", "").strip(),
            deepseek_http_timeout_seconds=_int(source, "DEEPSEEK_HTTP_TIMEOUT_SECONDS", 30),
            deepseek_max_attempts=_int(source, "DEEPSEEK_MAX_ATTEMPTS", 3),
            deepseek_thinking_enabled=_bool(
                source, "DEEPSEEK_THINKING_ENABLED", True
            ),
            deepseek_max_tokens=_int(source, "DEEPSEEK_MAX_TOKENS", 8192),
            deepseek_price_verified_at=_optional_date(source, "DEEPSEEK_PRICE_VERIFIED_AT"),
            deepseek_price_max_age_days=_int(source, "DEEPSEEK_PRICE_MAX_AGE_DAYS", 30),
            deepseek_input_cache_hit_usd_per_m=_decimal(
                source, "DEEPSEEK_INPUT_CACHE_HIT_USD_PER_M"
            ),
            deepseek_input_cache_miss_usd_per_m=_decimal(
                source, "DEEPSEEK_INPUT_CACHE_MISS_USD_PER_M"
            ),
            deepseek_output_usd_per_m=_decimal(source, "DEEPSEEK_OUTPUT_USD_PER_M"),
            bark_base_url=source.get("BARK_BASE_URL", "").strip().rstrip("/"),
            bark_device_key=source.get("BARK_DEVICE_KEY", "").strip(),
            bark_group=source.get("BARK_GROUP", "").strip(),
            bark_level=source.get("BARK_LEVEL", "active").strip(),
            bark_sound=source.get("BARK_SOUND", "").strip(),
            bark_http_timeout_seconds=_int(source, "BARK_HTTP_TIMEOUT_SECONDS", 15),
            bark_max_attempts=_int(source, "BARK_MAX_ATTEMPTS", 4),
            bark_retry_base_seconds=_int(source, "BARK_RETRY_BASE_SECONDS", 5),
            classification_max_attempts=_int(source, "CLASSIFICATION_MAX_ATTEMPTS", 3),
            classification_retry_base_seconds=_int(
                source, "CLASSIFICATION_RETRY_BASE_SECONDS", 30
            ),
        )
        settings.validate()
        return settings

    @property
    def active_x_budget_usd(self) -> Decimal:
        if self.app_env == "production":
            return self.x_monthly_budget_usd
        return min(self.x_monthly_budget_usd, self.x_development_budget_usd)

    def validate(self) -> None:
        if self.app_env not in {"development", "production"}:
            raise ConfigurationError("APP_ENV must be development or production")
        if self.x_poll_interval_seconds < 30:
            raise ConfigurationError("X_POLL_INTERVAL_SECONDS cannot be below 30")
        if not 10 <= self.x_max_results <= 100:
            raise ConfigurationError("X_MAX_RESULTS must be between 10 and 100")
        if not 1 <= self.x_max_pages_per_query <= 20:
            raise ConfigurationError("X_MAX_PAGES_PER_QUERY must be between 1 and 20")
        if self.x_max_requests_per_hour < 1:
            raise ConfigurationError("X_MAX_REQUESTS_PER_HOUR must be positive")
        if not 1 <= self.x_billing_cycle_day <= 28:
            raise ConfigurationError("X_BILLING_CYCLE_DAY must be between 1 and 28")
        if self.active_x_budget_usd <= 0:
            raise ConfigurationError("the active X budget must be positive")
        if not Decimal("0") < self.x_alert_threshold_usd < self.x_monthly_budget_usd:
            raise ConfigurationError("X alert threshold must be between zero and the monthly budget")
        if self.bark_level not in {"active", "critical", "timeSensitive", "passive"}:
            raise ConfigurationError("BARK_LEVEL is invalid")
        if not 1 <= self.health_port <= 65535:
            raise ConfigurationError("HEALTH_PORT is invalid")
        if self.usage_retention_days < 35:
            raise ConfigurationError("USAGE_RETENTION_DAYS must be at least 35")
        positive_values = {
            "X_INITIAL_LOOKBACK_MINUTES": self.x_initial_lookback_minutes,
            "X_HTTP_TIMEOUT_SECONDS": self.x_http_timeout_seconds,
            "X_MAX_ATTEMPTS": self.x_max_attempts,
            "X_IDENTITY_RECHECK_HOURS": self.x_identity_recheck_hours,
            "X_IDENTITY_RETRY_MINUTES": self.x_identity_retry_minutes,
            "DEEPSEEK_HTTP_TIMEOUT_SECONDS": self.deepseek_http_timeout_seconds,
            "DEEPSEEK_MAX_ATTEMPTS": self.deepseek_max_attempts,
            "DEEPSEEK_MAX_TOKENS": self.deepseek_max_tokens,
            "BARK_HTTP_TIMEOUT_SECONDS": self.bark_http_timeout_seconds,
            "BARK_MAX_ATTEMPTS": self.bark_max_attempts,
            "CLASSIFICATION_MAX_ATTEMPTS": self.classification_max_attempts,
        }
        invalid = [name for name, value in positive_values.items() if value < 1]
        if invalid:
            raise ConfigurationError(f"these settings must be positive: {', '.join(invalid)}")
        if self.deepseek_thinking_enabled and self.deepseek_max_tokens < 8192:
            raise ConfigurationError(
                "DEEPSEEK_MAX_TOKENS must be at least 8192 when DeepSeek thinking is enabled"
            )
        if self.app_mode is AppMode.LIVE:
            self._validate_live()

    def _validate_live(self) -> None:
        if not self.paid_api_calls_enabled:
            raise ConfigurationError(
                "live mode is locked: set PAID_API_CALLS_ENABLED=true only after approving paid calls"
            )
        required = {
            "X_API_BASE_URL": self.x_api_base_url,
            "X_BEARER_TOKEN": self.x_bearer_token,
            "DEEPSEEK_BASE_URL": self.deepseek_base_url,
            "DEEPSEEK_MODEL": self.deepseek_model,
            "DEEPSEEK_API_KEY": self.deepseek_api_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError(f"live mode is missing: {', '.join(missing)}")
        self.assert_live_prices_fresh()
        if self.x_post_read_unit_usd <= 0 or self.x_user_read_unit_usd <= 0:
            raise ConfigurationError("X resource prices must be current and positive")
        deepseek_prices = (
            self.deepseek_input_cache_hit_usd_per_m,
            self.deepseek_input_cache_miss_usd_per_m,
            self.deepseek_output_usd_per_m,
        )
        if any(price <= 0 for price in deepseek_prices):
            raise ConfigurationError("DeepSeek price settings must be current and positive")
        if not self.dry_run:
            if not self.bark_send_enabled:
                raise ConfigurationError(
                    "real Bark delivery is locked: set BARK_SEND_ENABLED=true explicitly"
                )
            if not self.bark_base_url or not self.bark_device_key:
                raise ConfigurationError("real Bark delivery requires base URL and device key")

    def assert_live_prices_fresh(self) -> None:
        self._require_recent_price_check(
            "X_PRICE_VERIFIED_AT", self.x_price_verified_at, self.x_price_max_age_days
        )
        self._require_recent_price_check(
            "DEEPSEEK_PRICE_VERIFIED_AT",
            self.deepseek_price_verified_at,
            self.deepseek_price_max_age_days,
        )

    @staticmethod
    def _require_recent_price_check(name: str, checked: date | None, max_age_days: int) -> None:
        if checked is None:
            raise ConfigurationError(f"{name} is required in live mode")
        today = datetime.now(timezone.utc).date()
        if checked > today or today - checked > timedelta(days=max_age_days):
            raise ConfigurationError(f"{name} is stale; re-check the official pricing page")


_CLUB_KEY_PATTERN = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*\Z")
_X_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
_QUERY_OPERATOR_TERMS = frozenset({"and", "or", "not"})
_QUERY_OPERATOR_PREFIXES = (
    "from:",
    "to:",
    "is:",
    "-is:",
    "lang:",
    "url:",
    "has:",
)


def _legacy_arsenal_profile(topic_query: str) -> ClubProfile:
    return ClubProfile(
        key="arsenal",
        name="Arsenal",
        query_terms=("Arsenal", "#AFC", "Gunners"),
        topic_query=topic_query,
        notification_title_prefix="🔴⚪",
        notification_group="Arsenal Transfer Alert",
        notification_id_prefix="arsenal-transfer",
        output_language="Simplified Chinese",
        timezone_utc_offset_minutes=480,
        timezone_label="北京时间",
        source_label="来源",
        time_label="时间",
        open_post_text="点击通知打开 X 原帖。",
    )


def _parse_club_profile(raw: object) -> ClubProfile:
    if not isinstance(raw, dict):
        raise ConfigurationError("catalog version 2 requires a [club] table")
    required = {"key", "name", "query_terms"}
    optional = {
        "notification_title_prefix",
        "notification_group",
        "notification_id_prefix",
        "output_language",
        "timezone_utc_offset_minutes",
        "timezone_label",
        "source_label",
        "time_label",
        "open_post_text",
    }
    missing = required - set(raw)
    extra = set(raw) - required - optional
    if missing or extra:
        raise ConfigurationError(
            "club fields mismatch; "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )

    key = raw["key"]
    name = raw["name"]
    query_terms_raw = raw["query_terms"]
    if not isinstance(key, str) or not _CLUB_KEY_PATTERN.fullmatch(key.strip()):
        raise ConfigurationError(
            "club.key must be a lowercase slug using letters, numbers, '-' or '_'"
        )
    key = key.strip()
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
        raise ConfigurationError("club.name must be a non-empty string of at most 100 characters")
    name = name.strip()
    if not isinstance(query_terms_raw, list) or not query_terms_raw:
        raise ConfigurationError("club.query_terms must be a non-empty string array")
    if len(query_terms_raw) > 20:
        raise ConfigurationError("club.query_terms cannot contain more than 20 terms")
    query_terms: list[str] = []
    for index, term_raw in enumerate(query_terms_raw, 1):
        if not isinstance(term_raw, str):
            raise ConfigurationError(f"club.query_terms item #{index} must be a string")
        term = term_raw.strip()
        lowered = term.lower()
        if not term or len(term) > 80:
            raise ConfigurationError(
                f"club.query_terms item #{index} must contain 1 to 80 characters"
            )
        if any(ord(character) < 32 for character in term) or any(
            character in term for character in ('"', "(", ")")
        ):
            raise ConfigurationError(
                f"club.query_terms item #{index} contains unsafe query characters"
            )
        if lowered in _QUERY_OPERATOR_TERMS or lowered.startswith(
            _QUERY_OPERATOR_PREFIXES
        ):
            raise ConfigurationError(
                f"club.query_terms item #{index} cannot be an X query operator"
            )
        if term not in query_terms:
            query_terms.append(term)
    topic_query = " OR ".join(
        f'"{term}"' if any(character.isspace() for character in term) else term
        for term in query_terms
    )

    defaults: dict[str, object] = {
        "notification_title_prefix": "⚽",
        "notification_group": f"{name} Transfer Alert",
        "notification_id_prefix": f"{key}-transfer",
        "output_language": "English",
        "timezone_utc_offset_minutes": 0,
        "timezone_label": "UTC",
        "source_label": "Source",
        "time_label": "Time",
        "open_post_text": "Open the original post on X.",
    }
    values = {field: raw.get(field, default) for field, default in defaults.items()}
    for field in defaults:
        value = values[field]
        if field == "notification_title_prefix":
            if not isinstance(value, str) or len(value.strip()) > 20:
                raise ConfigurationError(
                    "club.notification_title_prefix must be a string of at most 20 characters"
                )
            values[field] = value.strip()
        elif field != "timezone_utc_offset_minutes":
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 150:
                raise ConfigurationError(f"club.{field} must be a non-empty string")
            values[field] = value.strip()
    offset = values["timezone_utc_offset_minutes"]
    if type(offset) is not int or not -720 <= offset <= 840:
        raise ConfigurationError(
            "club.timezone_utc_offset_minutes must be between -720 and 840"
        )
    notification_id_prefix = values["notification_id_prefix"]
    assert isinstance(notification_id_prefix, str)
    if not _CLUB_KEY_PATTERN.fullmatch(notification_id_prefix):
        raise ConfigurationError(
            "club.notification_id_prefix must be a lowercase slug"
        )
    return ClubProfile(
        key=key,
        name=name,
        query_terms=tuple(query_terms),
        topic_query=topic_query,
        notification_title_prefix=str(values["notification_title_prefix"]),
        notification_group=str(values["notification_group"]),
        notification_id_prefix=notification_id_prefix,
        output_language=str(values["output_language"]),
        timezone_utc_offset_minutes=offset,
        timezone_label=str(values["timezone_label"]),
        source_label=str(values["source_label"]),
        time_label=str(values["time_label"]),
        open_post_text=str(values["open_post_text"]),
    )


@dataclass(frozen=True, slots=True)
class SourceCatalog:
    path: Path
    topic_query: str
    sources: tuple[Source, ...]
    club: ClubProfile | None = None

    def __post_init__(self) -> None:
        if self.club is None:
            object.__setattr__(self, "club", _legacy_arsenal_profile(self.topic_query))

    @classmethod
    def load(cls, path: Path) -> "SourceCatalog":
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ConfigurationError(f"source catalog does not exist: {path}") from error
        except tomllib.TOMLDecodeError as error:
            raise ConfigurationError(f"invalid source catalog: {error}") from error
        version = data.get("catalog_version")
        if version not in {1, 2}:
            raise ConfigurationError("unsupported source catalog version")
        if version == 1:
            topic_query = data.get("topic_query")
            if not isinstance(topic_query, str) or not topic_query.strip():
                raise ConfigurationError("topic_query must be a non-empty string")
            topic_query = topic_query.strip()
            club = _legacy_arsenal_profile(topic_query)
        else:
            root_fields = set(data)
            expected_root_fields = {"catalog_version", "club", "sources"}
            if root_fields != expected_root_fields:
                raise ConfigurationError(
                    "catalog version 2 root fields mismatch; "
                    f"missing={sorted(expected_root_fields - root_fields)}, "
                    f"extra={sorted(root_fields - expected_root_fields)}"
                )
            club = _parse_club_profile(data.get("club"))
            topic_query = club.topic_query
        raw_sources = data.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ConfigurationError("source catalog must contain sources")
        required_fields = {
            "key",
            "name",
            "tier",
            "username",
            "query_mode",
        }
        optional_fields = {
            "user_id",
            "enabled",
            "identity_status",
            "verified_at",
            "confirmation_required",
            "confirmed",
            "identity_evidence_url",
            "notes",
        }
        sources: list[Source] = []
        for index, item in enumerate(raw_sources):
            if not isinstance(item, dict):
                raise ConfigurationError(f"source #{index + 1} must be a table")
            missing = required_fields - set(item)
            extra = set(item) - required_fields - optional_fields
            if missing or extra:
                raise ConfigurationError(
                    f"source #{index + 1} fields mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
                )
            item = {
                "user_id": "",
                "enabled": True,
                "identity_status": "pending",
                "verified_at": "",
                "confirmation_required": False,
                "confirmed": False,
                "identity_evidence_url": "",
                "notes": "",
                **item,
            }
            try:
                query_mode = QueryMode(item["query_mode"])
            except ValueError as error:
                raise ConfigurationError(f"source #{index + 1} has invalid query_mode") from error
            tier = item["tier"]
            if type(tier) is not int or tier not in {0, 1, 2}:
                raise ConfigurationError("only Tier 0, 1, and 2 sources are allowed")
            string_fields = (
                "key",
                "name",
                "username",
                "user_id",
                "identity_status",
                "verified_at",
                "identity_evidence_url",
                "notes",
            )
            if any(not isinstance(item[field], str) for field in string_fields):
                raise ConfigurationError(f"source #{index + 1} has a non-string field")
            bool_fields = ("enabled", "confirmation_required", "confirmed")
            if any(type(item[field]) is not bool for field in bool_fields):
                raise ConfigurationError(f"source #{index + 1} has a non-boolean field")
            source_key = item["key"].strip()
            source_name = item["name"].strip()
            username = item["username"].strip()
            if username.startswith("@"):
                username = username[1:]
            if not _CLUB_KEY_PATTERN.fullmatch(source_key):
                raise ConfigurationError(
                    f"source #{index + 1} key must be a lowercase slug"
                )
            if not source_name or any(ord(character) < 32 for character in source_name):
                raise ConfigurationError(f"source #{index + 1} has an invalid name")
            if not _X_USERNAME_PATTERN.fullmatch(username):
                raise ConfigurationError(
                    f"source {source_key} username must be a valid X username"
                )
            user_id = item["user_id"].strip()
            if user_id and not user_id.isdigit():
                raise ConfigurationError(f"source {item['key']} user_id must be numeric")
            sources.append(
                Source(
                    key=source_key,
                    name=source_name,
                    tier=tier,
                    username=username,
                    user_id=user_id,
                    enabled=item["enabled"],
                    query_mode=query_mode,
                    identity_status=item["identity_status"].strip(),
                    verified_at=item["verified_at"].strip(),
                    confirmation_required=item["confirmation_required"],
                    confirmed=item["confirmed"],
                    identity_evidence_url=item["identity_evidence_url"].strip(),
                    notes=item["notes"].strip(),
                )
            )
        keys = [source.key for source in sources]
        usernames = [source.username.lower() for source in sources]
        if len(keys) != len(set(keys)):
            raise ConfigurationError("source keys must be unique")
        if len(usernames) != len(set(usernames)):
            raise ConfigurationError("source usernames must be unique")
        if not any(source.tier == 0 for source in sources):
            raise ConfigurationError("the catalog must retain at least one Tier 0 source")
        return cls(
            path=path,
            topic_query=topic_query,
            sources=tuple(sources),
            club=club,
        )

    @property
    def enabled_sources(self) -> tuple[Source, ...]:
        return tuple(source for source in self.sources if source.enabled)

    def by_key(self) -> dict[str, Source]:
        return {source.key: source for source in self.enabled_sources}

    def by_user_id(self) -> dict[str, Source]:
        return {source.user_id: source for source in self.enabled_sources if source.user_id}

    def assert_live_ready(
        self,
        today: date | None = None,
        max_age_days: int = 30,
        *,
        enforce_age: bool = True,
    ) -> None:
        current = today or datetime.now(timezone.utc).date()
        failures: list[str] = []
        for source in self.enabled_sources:
            if not source.user_id:
                failures.append(f"{source.key}: missing numeric X user ID")
            if source.identity_status != "verified":
                failures.append(f"{source.key}: identity status is {source.identity_status!r}")
            if source.confirmation_required and not source.confirmed:
                failures.append(f"{source.key}: user confirmation is required")
            try:
                checked = date.fromisoformat(source.verified_at)
            except ValueError:
                failures.append(f"{source.key}: verified_at is missing or invalid")
            else:
                if checked > current or (
                    enforce_age and current - checked > timedelta(days=max_age_days)
                ):
                    failures.append(f"{source.key}: X identity verification is stale")
        if failures:
            raise ConfigurationError("source catalog is not live-ready: " + "; ".join(failures))

    def build_queries(self, maximum_length: int = 512) -> tuple[QuerySpec, ...]:
        specs: list[QuerySpec] = []
        for mode in (QueryMode.ALL, QueryMode.TOPIC):
            mode_sources = [
                source
                for source in self.enabled_sources
                if source.query_mode is mode
            ]
            if not mode_sources:
                continue
            chunks: list[list[Source]] = []
            current: list[Source] = []
            for source in mode_sources:
                candidate = [*current, source]
                query = self._query_for(mode, candidate)
                if len(query) > maximum_length and current:
                    chunks.append(current)
                    current = [source]
                else:
                    current = candidate
            if current:
                chunks.append(current)
            for index, chunk in enumerate(chunks, 1):
                query = self._query_for(mode, chunk)
                if len(query) > maximum_length:
                    raise ConfigurationError(
                        f"X query for source {chunk[0].key} exceeds {maximum_length} characters"
                    )
                digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
                specs.append(
                    QuerySpec(
                        key=f"{mode.value}-{index}-{digest}",
                        query=query,
                        source_keys=tuple(source.key for source in chunk),
                    )
                )
        return tuple(specs)

    def _query_for(self, mode: QueryMode, sources: list[Source]) -> str:
        authors = " OR ".join(f"from:{source.username}" for source in sources)
        query = f"({authors})"
        if mode is QueryMode.TOPIC:
            query += f" ({self.topic_query})"
        return f"{query} -is:retweet"
