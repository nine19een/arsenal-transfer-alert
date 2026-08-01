from __future__ import annotations

from datetime import timedelta, timezone

from .models import ClubProfile, Classification, NotificationPayload, Post, Source


def build_notification(
    post: Post,
    source: Source,
    classification: Classification,
    *,
    club: ClubProfile,
    group: str,
    level: str,
    sound: str,
) -> NotificationPayload:
    if not classification.eligible or not classification.notification_text:
        raise ValueError("only an eligible classification can become a notification")
    local_timezone = timezone(
        timedelta(minutes=club.timezone_utc_offset_minutes),
        name=club.timezone_label,
    )
    published = post.created_at.astimezone(local_timezone).strftime("%Y-%m-%d %H:%M:%S")
    url = f"https://x.com/i/web/status/{post.id}"
    separator = (
        "："
        if any(
            "\u3400" <= character <= "\u9fff"
            for character in club.source_label + club.time_label
        )
        else ": "
    )
    body = (
        f"{classification.notification_text}\n\n"
        f"{club.source_label}{separator}{source.name}\n"
        f"{club.time_label}{separator}{club.timezone_label} {published}\n"
        f"{club.open_post_text}"
    )
    title_core = f"[Tier {source.tier}] {source.name}"
    title = (
        f"{club.notification_title_prefix} {title_core}"
        if club.notification_title_prefix
        else title_core
    )
    return NotificationPayload(
        post_id=post.id,
        bark_id=f"{club.notification_id_prefix}-{post.id}",
        title=title,
        body=body,
        url=url,
        group=group or club.notification_group,
        level=level,
        sound=sound,
    )
