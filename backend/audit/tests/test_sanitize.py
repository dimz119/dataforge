"""Tests for the defensive secret-stripper (INV-AUD-3)."""

from __future__ import annotations

from audit.infra.sanitize import scrub


def test_top_level_secret_keys_redacted() -> None:
    out = scrub({"password": "x", "token": "y", "secret": "z", "prefix": "ok"})
    assert out == {
        "password": "[redacted]",
        "token": "[redacted]",
        "secret": "[redacted]",
        "prefix": "ok",
    }


def test_secret_substrings_match_case_insensitively() -> None:
    out = scrub(
        {
            "Authorization": "Bearer x",
            "key_hash": "h",
            "REFRESH_TOKEN": "t",
            "ApiKey": "k",
            "last4": "UxKz",  # allowed reference
        }
    )
    assert out["Authorization"] == "[redacted]"
    assert out["key_hash"] == "[redacted]"
    assert out["REFRESH_TOKEN"] == "[redacted]"
    assert out["ApiKey"] == "[redacted]"
    assert out["last4"] == "UxKz"


def test_nested_dicts_and_lists_are_scrubbed() -> None:
    out = scrub(
        {
            "outer": {"password": "p", "name": "ada"},
            "items": [{"secret": "s"}, {"label": "ok"}],
        }
    )
    assert out["outer"] == {"password": "[redacted]", "name": "ada"}
    assert out["items"] == [{"secret": "[redacted]"}, {"label": "ok"}]


def test_non_container_values_pass_through() -> None:
    assert scrub("plain") == "plain"
    assert scrub(42) == 42
    assert scrub(None) is None


def test_allowed_references_survive() -> None:
    # prefix / last4 / scopes / role are explicit non-secret references.
    out = scrub({"prefix": "3f8a", "last4": "UxKz", "scopes": ["events:read"], "role": "admin"})
    assert out == {"prefix": "3f8a", "last4": "UxKz", "scopes": ["events:read"], "role": "admin"}
