from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from arsenal_alert.config import ConfigurationError, SourceCatalog
from arsenal_alert.logging_utils import JsonFormatter
from arsenal_alert.models import AppMode

from tests.helpers import ROOT, settings_for


_SECRET_RULES = (
    ("deepseek_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "authorization_bearer",
        re.compile(r"\bBearer\s+[A-Za-z0-9_-]{24,}\b", re.IGNORECASE),
    ),
    (
        "bark_device_key_url",
        re.compile(r"api\.day\.app/[A-Za-z0-9]{16,}/push"),
    ),
)

_GENERATED_OR_LOCAL_SUFFIXES = {
    ".db",
    ".log",
    ".pyc",
    ".sqlite",
    ".sqlite3",
}


def _git_paths(*arguments: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )


def _tracked_text_files() -> tuple[tuple[str, Path], ...]:
    tracked = _git_paths()
    ignored = set(_git_paths("-ci", "--exclude-standard"))
    selected: list[tuple[str, Path]] = []
    for relative in tracked:
        path = ROOT / relative
        if relative in ignored:
            continue
        if path.name == ".env" or (
            path.name.startswith(".env.") and path.name != ".env.example"
        ):
            continue
        if path.suffix.lower() in _GENERATED_OR_LOCAL_SUFFIXES:
            continue
        if any(part in {"__pycache__", "data"} for part in path.parts):
            continue
        selected.append((relative, path))
    return tuple(selected)


def _find_secret_locations(
    path_label: str, content: str
) -> tuple[tuple[str, int, str], ...]:
    findings: list[tuple[str, int, str]] = []
    for line_number, line in enumerate(content.splitlines(), 1):
        for rule_name, pattern in _SECRET_RULES:
            if pattern.search(line) is not None:
                findings.append((path_label, line_number, rule_name))
    return tuple(findings)


def _format_secret_findings(findings: tuple[tuple[str, int, str], ...]) -> str:
    locations = "\n".join(
        f"{path}:{line_number}: {rule_name}"
        for path, line_number, rule_name in findings
    )
    return "possible secrets in Git-tracked files:\n" + locations


def _assert_no_secret_findings(
    findings: tuple[tuple[str, int, str], ...]
) -> None:
    if findings:
        raise AssertionError(_format_secret_findings(findings))


class SecurityAndConfigTests(unittest.TestCase):
    def test_env_example_contains_no_credentials(self) -> None:
        values = {}
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value
        self.assertEqual("", values["X_BEARER_TOKEN"])
        self.assertEqual("", values["DEEPSEEK_API_KEY"])
        self.assertEqual("", values["BARK_DEVICE_KEY"])

    def test_repository_has_no_obvious_committed_secret(self) -> None:
        findings: list[tuple[str, int, str]] = []
        for relative, path in _tracked_text_files():
            if path.is_symlink():
                content = os.readlink(path)
            elif path.is_file():
                try:
                    content = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
            else:
                continue
            findings.extend(_find_secret_locations(relative, content))
        _assert_no_secret_findings(tuple(findings))
        fake_secret = "sk-" + ("synthetic" * 4)
        synthetic_findings = _find_secret_locations(
            "synthetic-fixture.env",
            f"DEEPSEEK_API_KEY={fake_secret}",
        )
        self.assertEqual(
            (("synthetic-fixture.env", 1, "deepseek_api_key"),),
            synthetic_findings,
        )
        with self.assertRaises(AssertionError) as caught:
            _assert_no_secret_findings(synthetic_findings)
        rendered = str(caught.exception)
        self.assertNotIn(fake_secret, rendered)
        self.assertEqual(
            "possible secrets in Git-tracked files:\n"
            "synthetic-fixture.env:1: deepseek_api_key",
            rendered,
        )

    def test_json_logs_redact_secret_fields(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("security-test")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
        secret = "never-appear-in-test-output"
        logger.info("api_key: %s", secret)
        rendered = stream.getvalue()
        self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_catalog_contains_only_tier_zero_to_two(self) -> None:
        catalog = SourceCatalog.load(ROOT / "config" / "sources.toml")
        self.assertEqual({0, 1, 2}, {source.tier for source in catalog.sources})
        self.assertEqual(13, len(catalog.sources))
        self.assertEqual(12, len(catalog.enabled_sources))

    def test_all_enabled_sources_are_identity_ready(self) -> None:
        catalog = SourceCatalog.load(ROOT / "config" / "sources.toml")
        catalog.assert_live_ready()
        pending = [
            source.key
            for source in catalog.enabled_sources
            if not source.user_id
            or source.identity_status != "verified"
            or (source.confirmation_required and not source.confirmed)
        ]
        self.assertEqual([], pending)
        sami = next(source for source in catalog.sources if source.key == "sami_mokbel")
        self.assertEqual("193221420", sami.user_id)
        self.assertEqual("SamiMokbel_BBC", sami.username)
        guardian = next(source for source in catalog.sources if source.key == "guardian")
        self.assertFalse(guardian.enabled)
        self.assertEqual("46403451", guardian.user_id)
        athletic = next(
            source for source in catalog.sources if source.key == "the_athletic"
        )
        self.assertTrue(athletic.confirmed)
        self.assertEqual("970939705629069312", athletic.user_id)
        expected_new_ids = {
            "art_de_roche": "779610333145104384",
            "david_hytner": "595406077",
            "jacob_steinberg": "43984593",
        }
        actual_new_ids = {
            source.key: source.user_id
            for source in catalog.sources
            if source.key in expected_new_ids
        }
        self.assertEqual(expected_new_ids, actual_new_ids)

    def test_source_directory_has_no_pending_identity_records(self) -> None:
        catalog = SourceCatalog.load(ROOT / "config" / "sources.toml")
        pending = [
            source.key
            for source in catalog.sources
            if not source.user_id or source.identity_status != "verified"
        ]
        self.assertEqual([], pending)

    def test_long_running_live_mode_rejects_stale_price_verification(self) -> None:
        settings = replace(
            settings_for(ROOT / "data" / "unused-test.sqlite3"),
            app_mode=AppMode.LIVE,
            x_price_verified_at=date(2020, 1, 1),
            deepseek_price_verified_at=date(2020, 1, 1),
        )
        with self.assertRaises(ConfigurationError):
            settings.assert_live_prices_fresh()

    def test_deepseek_thinking_defaults_on_with_reasoning_capacity(self) -> None:
        settings = settings_for(ROOT / "data" / "unused-test.sqlite3")

        self.assertTrue(settings.deepseek_thinking_enabled)
        self.assertEqual(8192, settings.deepseek_max_tokens)

    def test_deepseek_thinking_rejects_a_truncation_prone_token_limit(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "at least 8192"):
            settings_for(
                ROOT / "data" / "unused-test.sqlite3",
                extra={"DEEPSEEK_MAX_TOKENS": "4096"},
            )

    def test_deepseek_nonthinking_mode_allows_a_smaller_token_limit(self) -> None:
        settings = settings_for(
            ROOT / "data" / "unused-test.sqlite3",
            extra={
                "DEEPSEEK_THINKING_ENABLED": "false",
                "DEEPSEEK_MAX_TOKENS": "700",
            },
        )

        self.assertFalse(settings.deepseek_thinking_enabled)
        self.assertEqual(700, settings.deepseek_max_tokens)


if __name__ == "__main__":
    unittest.main()
