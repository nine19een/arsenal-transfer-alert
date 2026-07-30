from __future__ import annotations

import hashlib
import posixpath
import urllib.parse
from dataclasses import dataclass

from .models import Post


TRACKING_QUERY_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_src",
        "s",
        "source",
    }
)

NON_ARTICLE_HOSTS = frozenset(
    {
        "t.co",
        "twitter.com",
        "www.twitter.com",
        "x.com",
        "www.x.com",
    }
)


@dataclass(frozen=True, slots=True)
class OriginalReportFingerprint:
    value: str
    kind: str
    referenced_post_id: str | None = None
    normalized_url: str | None = None


def original_report_fingerprint(post: Post) -> OriginalReportFingerprint | None:
    referenced_post_id = _referenced_post_id(post)
    if referenced_post_id:
        return OriginalReportFingerprint(
            value=f"post:{referenced_post_id}",
            kind="referenced_post",
            referenced_post_id=referenced_post_id,
        )
    urls = normalized_article_urls(post)
    if not urls:
        return None
    normalized = urls[0]
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return OriginalReportFingerprint(
        value=f"url:{digest}",
        kind="article_url",
        normalized_url=normalized,
    )


def previous_edit_post_ids(post: Post) -> tuple[str, ...]:
    """Return earlier X Post IDs from the same edit history.

    X assigns a new Post ID to each edited version. The history also contains
    the current ID, so only distinct predecessor IDs are returned.
    """

    history = post.raw.get("edit_history_tweet_ids")
    if not isinstance(history, list):
        return ()
    previous: list[str] = []
    for item in history:
        if (
            isinstance(item, str)
            and item.isdigit()
            and item != post.id
            and item not in previous
        ):
            previous.append(item)
    return tuple(previous)


def normalized_article_urls(post: Post) -> tuple[str, ...]:
    entities = post.raw.get("entities")
    if not isinstance(entities, dict):
        return ()
    raw_urls = entities.get("urls")
    if not isinstance(raw_urls, list):
        return ()
    normalized: list[str] = []
    for item in raw_urls:
        if not isinstance(item, dict):
            continue
        candidate = next(
            (
                item.get(field)
                for field in ("unwound_url", "expanded_url", "url")
                if isinstance(item.get(field), str) and item[field].strip()
            ),
            None,
        )
        if candidate is None:
            continue
        result = normalize_article_url(candidate)
        if result and result not in normalized:
            normalized.append(result)
    return tuple(normalized)


def normalize_article_url(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not hostname:
        return None
    if hostname in NON_ARTICLE_HOSTS:
        return None
    if hostname.startswith("www."):
        hostname = hostname[4:]
    try:
        port = parsed.port
    except ValueError:
        return None
    if port and not (scheme == "http" and port == 80) and not (
        scheme == "https" and port == 443
    ):
        hostname = f"{hostname}:{port}"
    path = parsed.path or "/"
    normalized_path = posixpath.normpath(path)
    if path.endswith("/") and normalized_path != "/":
        normalized_path = f"{normalized_path}/"
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    retained = [
        (key, item)
        for key, item in query_pairs
        if not key.lower().startswith("utm_")
        and key.lower() not in TRACKING_QUERY_KEYS
    ]
    retained.sort()
    query = urllib.parse.urlencode(retained, doseq=True)
    return urllib.parse.urlunsplit(("https", hostname, normalized_path, query, ""))


def _referenced_post_id(post: Post) -> str | None:
    references = post.raw.get("referenced_tweets")
    if not isinstance(references, list):
        return None
    for preferred_type in ("quoted", "replied_to"):
        for item in references:
            if not isinstance(item, dict) or item.get("type") != preferred_type:
                continue
            post_id = item.get("id")
            if isinstance(post_id, str) and post_id.isdigit():
                return post_id
    return None
