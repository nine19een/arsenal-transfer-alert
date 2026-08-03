from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ConfigurationError, Settings, SourceCatalog, load_env_file
from .cost import cost_report
from .db import StateStore
from .logging_utils import configure_logging
from .service import build_runtime, run_service
from .x_api import XApiClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arsenal-transfer-alert")
    parser.add_argument(
        "--env-file",
        default=".env",
        help="optional KEY=VALUE file; existing process environment wins",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run the polling service")
    run.add_argument("--once", action="store_true", help="run one cycle and exit")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="force Bark delivery off for this invocation",
    )
    run.add_argument("--mock-feed", help="override MOCK_FEED_PATH")
    run.add_argument("--mock-classifications", help="override MOCK_CLASSIFICATIONS_PATH")

    subparsers.add_parser("doctor", help="validate local configuration without external calls")
    subparsers.add_parser("cost-report", help="print usage and projected cost JSON")

    verify = subparsers.add_parser(
        "verify-sources",
        help="resolve configured usernames through the official X API",
    )
    verify.add_argument(
        "--allow-paid-call",
        action="store_true",
        help="required acknowledgement: X User reads may be billed",
    )
    verify.add_argument(
        "--output",
        help="optional JSON output path; does not edit sources.toml",
    )

    subparsers.add_parser(
        "healthcheck",
        help="exit zero only when the local service readiness endpoint is healthy",
    )
    resolve = subparsers.add_parser(
        "resolve-notification",
        help="manually resolve a Bark uncertain/failed delivery",
    )
    resolve.add_argument("post_id")
    resolve.add_argument(
        "--action",
        required=True,
        choices=("assume-delivered", "retry"),
    )
    resolve.add_argument(
        "--acknowledge-duplicate-risk",
        action="store_true",
        help="required for retry because Bark may already have delivered the notification",
    )
    retry_classification = subparsers.add_parser(
        "retry-classification",
        help="requeue one filtered or classification_error Post after changing the classifier",
    )
    retry_classification.add_argument("post_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        env = load_env_file(Path(args.env_file))
        if args.command == "run":
            if args.dry_run:
                env["DRY_RUN"] = "true"
            if args.mock_feed:
                env["MOCK_FEED_PATH"] = args.mock_feed
            if args.mock_classifications:
                env["MOCK_CLASSIFICATIONS_PATH"] = args.mock_classifications
        if args.command == "verify-sources":
            return _verify_sources(args, env)
        settings = Settings.from_env(env)
        configure_logging(settings.log_level)
        if args.command == "run":
            runtime = build_runtime(settings)
            run_service(runtime, once=args.once)
            return 0
        if args.command == "doctor":
            return _doctor(settings)
        if args.command == "cost-report":
            with StateStore(settings.db_path) as store:
                print(json.dumps(cost_report(store, settings), indent=2, ensure_ascii=False))
            return 0
        if args.command == "healthcheck":
            return _healthcheck(settings)
        if args.command == "resolve-notification":
            return _resolve_notification(args, settings)
        if args.command == "retry-classification":
            return _retry_classification(args, settings)
    except (ConfigurationError, ValueError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 1


def _doctor(settings: Settings) -> int:
    catalog = SourceCatalog.load(settings.source_config_path)
    assert catalog.club is not None
    queries = catalog.build_queries()
    live_ready = True
    live_ready_error: str | None = None
    try:
        catalog.assert_live_ready()
    except ConfigurationError as error:
        live_ready = False
        live_ready_error = str(error)
    report = {
        "configuration": "valid",
        "app_env": settings.app_env,
        "app_mode": settings.app_mode.value,
        "dry_run": settings.dry_run,
        "paid_api_calls_enabled": settings.paid_api_calls_enabled,
        "bark_send_enabled": settings.bark_send_enabled,
        "database_path": str(settings.db_path),
        "club": {
            "key": catalog.club.key,
            "name": catalog.club.name,
            "query_terms": list(catalog.club.query_terms),
            "output_language": catalog.club.output_language,
            "notification_group": (
                settings.bark_group or catalog.club.notification_group
            ),
        },
        "topic_query": catalog.topic_query,
        "sources": len(catalog.enabled_sources),
        "tiers": sorted({source.tier for source in catalog.enabled_sources}),
        "query_lengths": {spec.key: len(spec.query) for spec in queries},
        "source_catalog_live_ready": live_ready,
        "source_catalog_blocker": live_ready_error,
        "secrets_present": {
            "x_bearer_token": bool(settings.x_bearer_token),
            "deepseek_api_key": bool(settings.deepseek_api_key),
            "bark_device_key": bool(settings.bark_device_key),
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def _verify_sources(args: argparse.Namespace, env: dict[str, str]) -> int:
    # Parse with mock-mode structural validation so this focused command does not
    # require DeepSeek or Bark credentials. X-specific safety checks remain below.
    validation_env = dict(env)
    validation_env["APP_MODE"] = "mock"
    settings = Settings.from_env(validation_env)
    configure_logging(settings.log_level)
    if not args.allow_paid_call or not settings.paid_api_calls_enabled:
        raise ConfigurationError(
            "source verification is locked; pass --allow-paid-call and set "
            "PAID_API_CALLS_ENABLED=true only after approving the billed X User reads"
        )
    if not settings.x_api_base_url or not settings.x_bearer_token:
        raise ConfigurationError("X_API_BASE_URL and X_BEARER_TOKEN are required")
    if settings.x_user_read_unit_usd <= 0 or settings.x_price_verified_at is None:
        raise ConfigurationError("current X user-read price and verification date are required")
    Settings._require_recent_price_check(
        "X_PRICE_VERIFIED_AT",
        settings.x_price_verified_at,
        settings.x_price_max_age_days,
    )
    catalog = SourceCatalog.load(settings.source_config_path)
    with StateStore(settings.db_path) as store:
        client = XApiClient(settings, store)
        users = client.lookup_sources(
            usernames=[source.username for source in catalog.enabled_sources]
        )
    by_username = {str(user["username"]).lower(): user for user in users}
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    results: list[dict[str, Any]] = []
    all_match = True
    for source in catalog.enabled_sources:
        user = by_username.get(source.username.lower())
        api_id = user.get("id") if user else None
        match = user is not None and (not source.user_id or source.user_id == api_id)
        all_match = all_match and match
        results.append(
            {
                "source_key": source.key,
                "configured_name": source.name,
                "configured_username": source.username,
                "configured_user_id": source.user_id or None,
                "api_user": user,
                "id_matches_config": match,
                "identity_evidence_url": source.identity_evidence_url,
                "confirmation_required": source.confirmation_required,
                "confirmed": source.confirmed,
            }
        )
    output = {
        "checked_at": checked_at,
        "provider": "official X API",
        "all_configured_ids_match": all_match,
        "manual_review_required": True,
        "instructions": (
            "Review employer evidence, parody/protected/affiliation fields, and ambiguous "
            "accounts. Then manually copy numeric IDs into config/sources.toml, set "
            "identity_status='verified', and set verified_at to the UTC check date."
        ),
        "sources": results,
    }
    encoded = json.dumps(output, indent=2, ensure_ascii=False)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded + "\n", encoding="utf-8")
        print(f"wrote redaction-safe source verification report to {destination}")
    else:
        print(encoded)
    return 0 if all_match else 3


def _healthcheck(settings: Settings) -> int:
    url = f"http://127.0.0.1:{settings.health_port}/health/ready"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, TimeoutError, OSError):
        return 1


def _resolve_notification(args: argparse.Namespace, settings: Settings) -> int:
    if args.action == "retry" and not args.acknowledge_duplicate_risk:
        raise ConfigurationError(
            "retry requires --acknowledge-duplicate-risk because the original Bark "
            "request may already have succeeded"
        )
    with StateStore(settings.db_path) as store:
        store.resolve_uncertain_notification(args.post_id, args.action)
        store.clear_health_flag(f"bark_{args.post_id}")
    print(f"notification {args.post_id} resolved with action {args.action}")
    return 0


def _retry_classification(args: argparse.Namespace, settings: Settings) -> int:
    with StateStore(settings.db_path) as store:
        store.requeue_classification(args.post_id)
    print(f"Post {args.post_id} queued for reclassification")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
