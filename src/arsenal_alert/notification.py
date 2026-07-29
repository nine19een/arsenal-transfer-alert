from __future__ import annotations

from datetime import timedelta, timezone

from .models import Classification, NotificationPayload, Post, Source


BEIJING_TIME = timezone(timedelta(hours=8), name="Asia/Shanghai")


def build_notification(
    post: Post,
    source: Source,
    classification: Classification,
    *,
    group: str,
    level: str,
    sound: str,
) -> NotificationPayload:
    if not classification.eligible or not classification.translation_zh:
        raise ValueError("only an eligible classification can become a notification")
    published = post.created_at.astimezone(BEIJING_TIME).strftime("%Y-%m-%d %H:%M:%S")
    url = f"https://x.com/i/web/status/{post.id}"
    body = (
        f"{classification.translation_zh}\n\n"
        f"来源：{source.name}\n"
        f"时间：北京时间 {published}\n"
        "点击通知打开 X 原帖。"
    )
    return NotificationPayload(
        post_id=post.id,
        bark_id=f"arsenal-transfer-{post.id}",
        title=source.title,
        body=body,
        url=url,
        group=group,
        level=level,
        sound=sound,
    )

