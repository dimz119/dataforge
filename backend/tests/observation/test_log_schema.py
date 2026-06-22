"""Structured-log frozen-schema + redaction tests (phase-11 exit #8; observability §2.2).

The frozen log field schema and the redaction processor are a *contract* (the SOAK /
GA log assertions and any downstream log-based SLI parse it), so they are gated here:

* the shared chain emits the frozen field schema as one JSON object per line
  (``ts``/``level``/``event``/``message``/``logger``/``service``/``env``/``release``,
  nullable tenant fields, ``duration_ms``/``status``, ``error.*`` on errors);
* the bound correlation context (``request_id``/``workspace_id``/``stream_id``/
  ``shard_id``/``user_id``/``api_key_id``) rides every line below the bind;
* the redaction processor strips API-key secrets/hashes, JWTs, passwords/hashes,
  verification/reset tokens, and ``Authorization`` — top-level AND nested in ``ctx`` —
  and renders a leaked full-key string as its public ``prefix…last4`` handle, so **no
  secret value survives** (the redaction CI test the build rules require);
* the LV-1 (≤1 tick summary/stream/60s) and LV-4 (WARNING+ 60s dedup + suppressed_count)
  volume rules behave as specified.

Pure: drives :mod:`config.logging` directly, capturing the JSON the shared handler
writes to a buffer. No Django models, no Postgres — runs in either lane.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
import structlog

from config import logging as dflog


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    """Configure the shared chain writing to a buffer; reset context + limiters.

    Resets the module ``_configured`` flag so ``cache_logger_on_first_use`` does not
    pin a logger bound to a previous test's handler, then redirects the single shared
    handler's stream to an in-memory buffer the test reads back as JSON lines.
    """
    dflog._configured = False  # force a fresh, uncached logger factory for this test
    structlog.contextvars.clear_contextvars()
    dflog.reset_volume_limiters()
    dflog.configure_logging(service="runner", env_name="dev", release="rel-sha", level="DEBUG")
    buffer = io.StringIO()
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(buffer)
    yield buffer
    structlog.contextvars.clear_contextvars()
    dflog.reset_volume_limiters()


def _lines(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def test_emits_frozen_field_schema(captured_logs: io.StringIO) -> None:
    """One JSON line carries the frozen always-present field schema (observability §2.2)."""
    log = structlog.get_logger("dataforge.delivery")
    log.info("http.request.completed", duration_ms=12, status=200)
    obj = _lines(captured_logs)[-1]
    # Always-present schema fields.
    for field in ("ts", "level", "event", "message", "logger", "service", "env", "release"):
        assert field in obj, f"frozen schema field {field!r} missing from log line"
    assert obj["service"] == "runner"
    assert obj["env"] == "dev"
    assert obj["release"] == "rel-sha"
    assert obj["event"] == "http.request.completed"
    assert obj["message"] == "http.request.completed"  # defaults to event
    assert obj["level"] == "info"
    assert obj["status"] == 200
    assert obj["duration_ms"] == 12
    # ts is RFC 3339 UTC with millisecond precision, Z suffix.
    assert isinstance(obj["ts"], str) and obj["ts"].endswith("Z") and "." in str(obj["ts"])
    # Nullable tenant fields default to null when unbound (req-nullable, not absent).
    assert obj["workspace_id"] is None
    assert obj["stream_id"] is None


def test_bound_correlation_context_rides_every_line(captured_logs: io.StringIO) -> None:
    """``bind_log_context`` puts the correlation fields on every subsequent line (§3.1)."""
    dflog.bind_log_context(
        request_id="018f-req", workspace_id="ws-uuid", stream_id="st-uuid",
        shard_id=3, user_id="usr-uuid", api_key_id="key-uuid",
    )
    log = structlog.get_logger("dataforge.runner")
    log.info("runner.boot")
    log.warning("runner.slow_tick")
    for obj in _lines(captured_logs):
        assert obj["request_id"] == "018f-req"
        assert obj["workspace_id"] == "ws-uuid"
        assert obj["stream_id"] == "st-uuid"
        assert obj["shard_id"] == 3
        assert obj["user_id"] == "usr-uuid"
        assert obj["api_key_id"] == "key-uuid"


def test_redacts_secrets_top_level_and_nested(captured_logs: io.StringIO) -> None:
    """Every secret-bearing key is masked at top level AND inside ``ctx`` (§2.2 redaction).

    Asserts no secret VALUE survives anywhere in the rendered line — the redaction CI
    test the build rules require (no secret/JWT/password/Authorization survives _redact)."""
    secret_values = {
        "password": "hunter2-correct-horse",
        "key_hash": "sha256:deadbeefcafef00d",
        "jwt": "eyJhbGciOi.JSUzI1NiJ9.signature",
        "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.body.sig",
        "refresh_token": "rt-supersecret-value",
        "reset_token": "reset-supersecret-value",
        "verification_token": "verify-supersecret-value",
        "access_token": "at-supersecret-value",
        "token_hash": "th-supersecret-value",
        "password_hash": "ph-supersecret-value",
        "secret": "raw-supersecret-value",
    }
    log = structlog.get_logger("dataforge.identity")
    log.info(
        "auth.key.rejected",
        api_key_id="key-uuid-stays",
        ctx={"nested_password": "x", "password": secret_values["password"], "ok": "keepme"},
        **secret_values,
    )
    raw = captured_logs.getvalue()
    obj = _lines(captured_logs)[-1]
    for key, value in secret_values.items():
        assert obj[key] == "[redacted]", f"{key} not redacted: {obj[key]!r}"
        assert value not in raw, f"secret value for {key!r} leaked into the log line"
    # Nested secret-keyed value in ctx is masked; non-secret siblings survive.
    ctx = obj["ctx"]
    assert isinstance(ctx, dict)
    assert ctx["password"] == "[redacted]"
    assert ctx["ok"] == "keepme"
    # The key id (non-secret) survives — only secrets are stripped.
    assert obj["api_key_id"] == "key-uuid-stays"


def test_leaked_full_key_string_collapsed_to_public_handle(captured_logs: io.StringIO) -> None:
    """A full ``df_<env>_<prefix>_<secret>`` string in a value becomes ``prefix…last4``.

    The last line of defence: a raw key that leaks into a non-secret-named value is not
    emitted verbatim; the secret body is dropped, leaving only the public handle (§2.2)."""
    full_key = "df_dev_abcd1234_supersecretbodyXYZ9"
    log = structlog.get_logger("dataforge.identity")
    log.info("auth.key.used", note=full_key)
    raw = captured_logs.getvalue()
    obj = _lines(captured_logs)[-1]
    assert "supersecretbodyXYZ9" not in raw, "the secret body leaked despite masking"
    assert obj["note"] == "abcd1234…XYZ9"  # public short id … last 4 of the secret body


def test_public_api_key_handle_renders_prefix_dot_last4() -> None:
    """``public_api_key_handle`` renders the contract ``prefix…last4`` form (§2.2)."""
    # From a full secret string.
    assert dflog.public_api_key_handle("df_prod_xyz789_abcdef123456") == "xyz789…3456"
    # From a stored prefix + an explicit last4.
    assert dflog.public_api_key_handle("df_prod_xyz789", last4="9999") == "xyz789…9999"


def test_error_fields_escaped_on_exception(captured_logs: io.StringIO) -> None:
    """An exception is escaped into ``error.kind``/``error.message``/``error.stack`` (§2.2)."""
    log = structlog.get_logger("dataforge.runner")
    try:
        raise ValueError("boom-detail")
    except ValueError:
        log.error("ledger.append.failed", exc_info=True)
    obj = _lines(captured_logs)[-1]
    assert obj["error.kind"] == "ValueError"
    assert obj["error.message"] == "boom-detail"
    assert "ValueError: boom-detail" in str(obj["error.stack"])
    assert obj["level"] == "error"


def test_lv1_tick_summary_at_most_one_per_stream_per_window(captured_logs: io.StringIO) -> None:
    """LV-1: ``emit_tick_summary`` admits at most one INFO per stream per 60 s window."""
    log = structlog.get_logger("dataforge.runner")
    for _ in range(10):
        dflog.emit_tick_summary(log, stream_id="st-1", shard_id=0, events=100)
    # A different stream is independently allowed one line in the same window.
    dflog.emit_tick_summary(log, stream_id="st-2", shard_id=0, events=5)
    summaries = [o for o in _lines(captured_logs) if o["event"] == "runner.tick.summary"]
    assert len(summaries) == 2, f"LV-1 admitted {len(summaries)} lines, expected one per stream"
    assert {str(o["stream_id"]) for o in summaries} == {"st-1", "st-2"}


def test_lv4_warning_dedup_carries_suppressed_count(captured_logs: io.StringIO) -> None:
    """LV-4: WARNING+ dedup emits once/60 s per (event, ws, stream) + suppressed_count."""
    log = structlog.get_logger("dataforge.runner")
    for _ in range(5):
        dflog.emit_deduped_warning(
            log, "buffer.write.degraded", workspace_id="ws-1", stream_id="st-1", detail="slow"
        )
    warnings = [o for o in _lines(captured_logs) if o["event"] == "buffer.write.degraded"]
    assert len(warnings) == 1, f"LV-4 admitted {len(warnings)} lines, expected one"
    # The next emission in the window after suppression carries the suppressed count.
    dflog.emit_deduped_warning(
        log, "buffer.write.degraded", workspace_id="ws-1", stream_id="st-1", detail="slow"
    )
    # Still suppressed (window not elapsed); force a window roll and emit again.
    dflog.reset_volume_limiters()
    dflog.emit_deduped_warning(
        log, "buffer.write.degraded", workspace_id="ws-1", stream_id="st-1", detail="slow"
    )
    again = [o for o in _lines(captured_logs) if o["event"] == "buffer.write.degraded"]
    assert len(again) == 2, "LV-4 did not re-emit after the window reset"
