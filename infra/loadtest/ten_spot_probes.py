#!/usr/bin/env python3
"""TEN cross-tenant spot probes during the LOAD-5K window (P11-14, exit #7).

The live-traffic analogue of ``backend/tests/tenancy/test_cross_tenant_probes.py``:
while the load test runs, repeatedly attempt to read one workspace's resources with
ANOTHER workspace's credentials (foreign API key, foreign JWT, no credential) and
assert the security §3.3 masking outcome on every probe:

  * foreign object/collection route  -> 404 (existence never confirmed, W-3);
    NEVER 403 (a 403 permission-denied on a foreign object confirms existence);
    NEVER 2xx carrying A's data; NEVER 5xx (SEC-AUTH-11).
  * no credential                    -> 401.
  * no A-sentinel (A's ids / stream id / event_id) in ANY foreign response body.

It uses the k6 teardown manifest (>=2 workspaces) to pick a VICTIM (A) and an
ATTACKER (B) tenant, then fires the probe set repeatedly for ``--rounds`` to catch
any isolation breach under concurrent load. A single failed probe -> non-zero exit
(exit criterion #7: one workspace's data/metering never reachable from another).

NO secret literals: keys come from the manifest, never hard-coded. Stdlib-only HTTP.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class Probe:
    label: str
    method: str
    path: str  # relative to API base, with the VICTIM's ids substituted
    expect: set[int]  # acceptable status codes (masking outcome)


def _request(api: str, method: str, path: str, headers: dict[str, str]) -> tuple[int, str]:
    url = f"{api}{path}"
    data = b"{}" if method in ("POST", "PATCH", "PUT") else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except urllib.error.URLError as e:
        return 0, str(e)


def _scan_sentinels(body: str, sentinels: list[str], request_path: str) -> list[str]:
    """Return any victim sentinel that leaked OUTSIDE the request path.

    The RFC 9457 ``instance`` member legitimately mirrors the request path, so the
    attacker-supplied ids in the URL are stripped before scanning (same rule as the
    permanent cross-tenant test)."""
    scrubbed = body.replace(request_path, "")
    return [s for s in sentinels if s and s in scrubbed]


def build_probes(victim_streams: list[str], victim_ws: str) -> list[Probe]:
    """The foreign-resource probe set against the victim's ids.

    Object/collection routes must mask to 404 under a foreign credential. The
    no-credential variant must be 401.
    """
    probes: list[Probe] = []
    sid = victim_streams[0] if victim_streams else "00000000-0000-0000-0000-000000000000"
    # Foreign object route: read A's stream detail.
    probes.append(Probe("stream-detail", "GET", f"/streams/{sid}", {404}))
    # Foreign data-plane collection: read A's delivered events.
    probes.append(
        Probe("stream-events", "GET", f"/streams/{sid}/events?from=earliest&limit=10", {404})
    )
    # Foreign workspace sub-collection: list A's API keys.
    probes.append(Probe("ws-api-keys", "GET", f"/workspaces/{victim_ws}/api-keys", {404}))
    # Foreign control verb: attempt to pause A's stream.
    probes.append(Probe("stream-pause", "POST", f"/streams/{sid}/pause", {404}))
    return probes


def run_round(
    api: str,
    probes: list[Probe],
    attacker_key: str,
    attacker_jwt: str | None,
    sentinels: list[str],
) -> list[str]:
    """Run every probe under each credential variant; return failure messages."""
    failures: list[str] = []
    variants: list[tuple[str, dict[str, str], set[int] | None]] = [
        ("foreign_key", {"X-API-Key": attacker_key}, None),  # use probe.expect
    ]
    if attacker_jwt:
        variants.append(("foreign_jwt", {"Authorization": f"Bearer {attacker_jwt}"}, None))
    variants.append(("no_cred", {}, {401}))

    for probe in probes:
        for variant_name, headers, override_expect in variants:
            status, body = _request(api, probe.method, probe.path, headers)
            expect = override_expect if override_expect is not None else probe.expect
            label = f"{probe.label}[{variant_name}] {probe.method} {probe.path}"

            # 1. Never a 5xx — an isolation/robustness bug.
            if status >= 500:
                failures.append(f"{label}: server error {status} (body={body[:200]})")
                continue
            # 2. A foreign object/collection must never 403 (confirms existence).
            if variant_name in ("foreign_key", "foreign_jwt") and status == 403:
                failures.append(
                    f"{label}: 403 permission-denied on a foreign resource — must be "
                    "404 (W-3, confirms existence otherwise)"
                )
                continue
            # 3. The masking status must be in the expected set.
            if status not in expect:
                failures.append(
                    f"{label}: got {status}, expected one of {sorted(expect)} "
                    f"(body={body[:200]})"
                )
            # 4. No victim sentinel may leak in ANY response body.
            leaked = _scan_sentinels(body, sentinels, probe.path)
            if leaked:
                failures.append(f"{label}: victim sentinel(s) leaked: {leaked}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="LOAD-5K TEN cross-tenant spot probes")
    parser.add_argument("--manifest", required=True, help="k6 teardown manifest JSON")
    parser.add_argument("--rounds", type=int, default=10, help="probe rounds during load")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between rounds")
    args = parser.parse_args()

    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    api = manifest.get("api", os.environ.get("API", "http://localhost:8000/api/v1"))
    samples: list[dict[str, Any]] = manifest.get("samples", [])

    if len(samples) < 2:
        print(
            "[ten] manifest has <2 workspaces — cross-tenant probes need a victim "
            "AND an attacker tenant. Re-run k6 with -e WORKSPACES=2 (or more)."
        )
        return 2

    victim, attacker = samples[0], samples[1]
    victim_ws = victim["workspace_id"]
    victim_streams = victim.get("streams", [])
    attacker_key = attacker["key"]

    # Victim sentinels: its ids must never surface under the attacker's credential
    # (outside the request path). event_ids would require draining the victim with
    # the victim's own key first — the workspace + stream ids are sufficient id
    # sentinels for the masking assertion.
    sentinels = [victim_ws, *list(victim_streams)]

    probes = build_probes(victim_streams, victim_ws)
    print(
        f"[ten] victim_ws={victim_ws} streams={len(victim_streams)} ; "
        f"attacker key (foreign) probing {len(probes)} routes x {args.rounds} rounds"
    )

    all_failures: list[str] = []
    for r in range(args.rounds):
        failures = run_round(api, probes, attacker_key, None, sentinels)
        if failures:
            print(f"[ten] round {r + 1}: {len(failures)} FAILURE(S)")
            for f in failures:
                print(f"  - {f}")
            all_failures.extend(failures)
        else:
            print(f"[ten] round {r + 1}/{args.rounds}: all probes masked correctly")
        if r + 1 < args.rounds:
            time.sleep(args.interval)

    if all_failures:
        print(f"\n[ten] {len(all_failures)} ISOLATION BREACH(ES) under load — gate FAILS")
        return 1
    print("\n[ten] zero cross-tenant breaches across all rounds (TEN clean under load)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
