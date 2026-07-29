from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .config import Settings
from .db import StateStore
from .models import CostSnapshot, utc_now


def billing_cycle_start(now: datetime, day: int) -> datetime:
    current = now.astimezone(timezone.utc)
    if current.day >= day:
        year, month = current.year, current.month
    elif current.month == 1:
        year, month = current.year - 1, 12
    else:
        year, month = current.year, current.month - 1
    valid_day = min(day, monthrange(year, month)[1])
    return datetime(year, month, valid_day, tzinfo=timezone.utc)


class XBudgetExceeded(RuntimeError):
    pass


class XRequestRateExceeded(RuntimeError):
    pass


class XBudgetGuard:
    def __init__(self, store: StateStore, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def authorize_post_page(self) -> None:
        self._authorize(
            Decimal(self.settings.x_max_results) * self.settings.x_post_read_unit_usd
        )

    def authorize_user_lookup(self, maximum_users: int) -> None:
        self._authorize(
            Decimal(maximum_users) * self.settings.x_user_read_unit_usd
        )

    def _authorize(self, worst_case_cost: Decimal) -> None:
        now = utc_now()
        cycle_start = billing_cycle_start(now, self.settings.x_billing_cycle_day)
        spent = self.store.x_estimated_spend_since(cycle_start)
        if spent + worst_case_cost > self.settings.active_x_budget_usd:
            raise XBudgetExceeded(
                "application X budget guard denied the request because its worst-case "
                "response could exceed the active budget"
            )
        requests = self.store.request_count_since("x", now - timedelta(hours=1))
        if requests >= self.settings.x_max_requests_per_hour:
            raise XRequestRateExceeded("application X request-per-hour guard denied the request")


def cost_report(store: StateStore, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    current = now or utc_now()
    cycle_start = billing_cycle_start(current, settings.x_billing_cycle_day)
    snapshot: CostSnapshot = store.cost_snapshot(cycle_start)
    first, last = store.usage_window("x", cycle_start)
    observed_hours = Decimal("0")
    projected_x_usd: Decimal | None = None
    confidence = "no_data"
    if first is not None and last is not None:
        elapsed_seconds = max((last - first).total_seconds(), 60)
        observed_hours = Decimal(str(elapsed_seconds)) / Decimal(3600)
        if snapshot.x_post_units:
            projected_x_usd = (
                snapshot.x_estimated_usd
                / observed_hours
                * Decimal(24)
                * Decimal("30.44")
            )
        else:
            projected_x_usd = Decimal("0")
        confidence = "low" if observed_hours < 24 else "medium" if observed_hours < 168 else "higher"
    return {
        "cycle_start_utc": cycle_start.isoformat(),
        "x": {
            "requests": snapshot.x_requests,
            "post_resources_estimated_billed": snapshot.x_post_units,
            "estimated_spend_usd": _money(snapshot.x_estimated_usd),
            "active_application_budget_usd": _money(settings.active_x_budget_usd),
            "production_console_limit_target_usd": _money(settings.x_monthly_budget_usd),
            "alert_threshold_usd": _money(settings.x_alert_threshold_usd),
            "sample_hours": str(observed_hours.quantize(Decimal("0.01"))),
            "projected_30_44_day_usd": (
                _money(projected_x_usd) if projected_x_usd is not None else None
            ),
            "projection_confidence": confidence,
            "unit_price_configured_usd": str(settings.x_post_read_unit_usd),
            "price_verified_at": (
                settings.x_price_verified_at.isoformat()
                if settings.x_price_verified_at
                else None
            ),
        },
        "deepseek": {
            "requests": snapshot.deepseek_requests,
            "estimated_spend_usd": _money(snapshot.deepseek_estimated_usd),
        },
        "bark": {
            "requests": snapshot.bark_requests,
        },
    }


def _money(value: Decimal | None) -> str:
    if value is None:
        return "0.00"
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
