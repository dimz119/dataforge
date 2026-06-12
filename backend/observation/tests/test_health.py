"""Smoke tests for /healthz and /readyz (observability §6) — no live services."""

import json
from collections.abc import Callable, Iterator

import pytest
from django.test import Client

from observation.application import readiness


@pytest.fixture(autouse=True)
def _fresh_probe_cache() -> Iterator[None]:
    readiness.reset_cache()
    yield
    readiness.reset_cache()


@pytest.fixture
def probe_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Callable[[], None]]:
    """Replace every dependency probe with a stub that succeeds."""
    stubs: dict[str, Callable[[], None]] = {}
    for component in list(readiness._PROBES):
        stub: Callable[[], None] = lambda: None  # noqa: E731
        stubs[component] = stub
        monkeypatch.setitem(readiness._PROBES, component, stub)
    return stubs


def test_healthz_is_alive_without_dependencies(client: Client) -> None:
    """Liveness must not run dependency checks (observability §6.1)."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert json.loads(response.content)["status"] == "ok"


def test_healthz_echoes_request_id(client: Client) -> None:
    response = client.get("/healthz")
    assert response.headers["X-Request-Id"]


def test_readyz_ok_reports_component_map(
    client: Client, probe_stubs: dict[str, Callable[[], None]]
) -> None:
    """200 with the per-component JSON map and the web gating set (§6.1-6.2)."""
    response = client.get("/readyz")
    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["status"] == "ready"
    assert body["components"] == {
        "postgres": "ok",
        "redis": "ok",
        "migrations": "ok",
        "kafka": "ok",
    }
    assert body["gating"] == ["postgres", "redis", "migrations"]
    assert "release" in body


def test_readyz_gating_failure_returns_503(
    client: Client,
    probe_stubs: dict[str, Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_postgres() -> None:
        raise ConnectionError("connection refused")

    monkeypatch.setitem(readiness._PROBES, "postgres", broken_postgres)
    response = client.get("/readyz")
    assert response.status_code == 503
    body = json.loads(response.content)
    assert body["status"] == "unready"
    assert body["components"]["postgres"] == "error: ConnectionError"


def test_readyz_nongating_kafka_failure_keeps_api_ready(
    client: Client,
    probe_stubs: dict[str, Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Kafka outage must not take the control-plane API out of rotation (§6.1)."""

    def broken_kafka() -> None:
        raise ConnectionError("broker down")

    monkeypatch.setitem(readiness._PROBES, "kafka", broken_kafka)
    response = client.get("/readyz")
    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["components"]["kafka"] == "error: ConnectionError"


def test_readyz_caches_probe_results_for_five_seconds(
    client: Client,
    probe_stubs: dict[str, Callable[[], None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    def counting_probe() -> None:
        calls["count"] += 1

    monkeypatch.setitem(readiness._PROBES, "redis", counting_probe)
    assert client.get("/readyz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert calls["count"] == 1  # second call served from the 5 s cache (§6.1)
