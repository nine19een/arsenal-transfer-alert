from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Protocol

from .config import Settings, SourceCatalog
from .cost import XBudgetExceeded, XRequestRateExceeded
from .db import StateStore
from .models import parse_utc_datetime, utc_now
from .x_api import XApiError


LOGGER = logging.getLogger(__name__)


class UserLookupClient(Protocol):
    def lookup_sources(
        self,
        *,
        usernames: list[str] | None = None,
        user_ids: list[str] | None = None,
    ) -> list[dict]: ...


class SourceIdentityMonitor:
    def __init__(
        self,
        settings: Settings,
        catalog: SourceCatalog,
        store: StateStore,
        client: UserLookupClient,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.store = store
        self.client = client
        identity_material = [
            (source.key, source.user_id, source.username)
            for source in catalog.enabled_sources
        ]
        self.catalog_fingerprint = hashlib.sha256(
            json.dumps(identity_material, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def ready(self) -> bool:
        blocked_fingerprint = self.store.get_meta("x_identity_blocked_fingerprint")
        if blocked_fingerprint == self.catalog_fingerprint:
            return False
        now = utc_now()
        last_success_raw = self.store.get_meta("x_identity_last_success_at")
        last_success_matches = (
            self.store.get_meta("x_identity_last_success_fingerprint")
            == self.catalog_fingerprint
        )
        if last_success_raw and last_success_matches:
            last_success = parse_utc_datetime(last_success_raw)
            if now - last_success < timedelta(
                hours=self.settings.x_identity_recheck_hours
            ):
                return True
        last_attempt_raw = self.store.get_meta("x_identity_last_attempt_at")
        last_attempt_matches = (
            self.store.get_meta("x_identity_last_attempt_fingerprint")
            == self.catalog_fingerprint
        )
        if last_attempt_raw and last_attempt_matches:
            last_attempt = parse_utc_datetime(last_attempt_raw)
            if now - last_attempt < timedelta(
                minutes=self.settings.x_identity_retry_minutes
            ):
                return bool(last_success_raw and last_success_matches)
        self.store.set_meta("x_identity_last_attempt_at", now.isoformat())
        self.store.set_meta(
            "x_identity_last_attempt_fingerprint", self.catalog_fingerprint
        )
        try:
            users = self.client.lookup_sources(
                user_ids=[source.user_id for source in self.catalog.enabled_sources]
            )
        except (XApiError, XBudgetExceeded, XRequestRateExceeded) as error:
            error_code = type(error).__name__
            self.store.set_health_flag(
                "x_identity_check",
                "warning" if last_success_raw and last_success_matches else "critical",
                f"Official X identity recheck failed: {error_code}",
            )
            LOGGER.warning(
                "x_identity_check_failed",
                extra={"error_code": error_code},
            )
            return bool(last_success_raw and last_success_matches)
        by_id = {str(user.get("id")): user for user in users}
        mismatches: list[str] = []
        for source in self.catalog.enabled_sources:
            user = by_id.get(source.user_id)
            if user is None:
                mismatches.append(f"{source.key}: ID not returned")
                continue
            api_username = user.get("username")
            if not isinstance(api_username, str) or (
                api_username.lower() != source.username.lower()
            ):
                mismatches.append(f"{source.key}: username changed")
            if user.get("parody") is True:
                mismatches.append(f"{source.key}: API marks account as parody")
        if mismatches:
            self.store.set_meta(
                "x_identity_blocked_fingerprint", self.catalog_fingerprint
            )
            self.store.set_health_flag(
                "x_identity_check",
                "critical",
                "Source identity mismatch; polling is blocked: " + "; ".join(mismatches),
            )
            LOGGER.error(
                "x_identity_mismatch",
                extra={"mismatch_count": len(mismatches)},
            )
            return False
        self.store.set_meta("x_identity_last_success_at", now.isoformat())
        self.store.set_meta(
            "x_identity_last_success_fingerprint", self.catalog_fingerprint
        )
        self.store.set_meta("x_identity_blocked_fingerprint", "")
        self.store.clear_health_flag("x_identity_check")
        LOGGER.info("x_identity_check_complete", extra={"source_count": len(users)})
        return True
