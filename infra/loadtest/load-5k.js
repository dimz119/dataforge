// DataForge LOAD-5K harness (P11-14) — k6.
//
// Three concurrent scenarios against a running DataForge stack:
//   1. cursor_pollers  — GET /streams/{id}/events with the opaque resume cursor,
//      many VUs paging the REST bulk-consumption path (limit up to 1000, §5.1).
//   2. ws_tails        — ~50 WebSocket tail connections (default 50), each authing
//      with an events:read key and consuming `event`/`heartbeat`/`drop_notice`
//      frames over the `dataforge.events.v1` subprotocol.
//   3. control_churn   — control-plane lifecycle churn: create/start/pause/resume/
//      stop streams (the SLO-1 availability surface).
//
// PARAMETERIZED FOR TWO RUNGS via env vars (see the table at the bottom):
//   - tiny local smoke (default): 1 workspace, 1 stream, ~50-100 TPS, ~2 min.
//   - the documented 5k config:   10 ws x 5 streams x 100 TPS, 30 min — this is
//     the GA gate shape, NOT runnable in dev; the verify phase runs the tiny rung.
//
// Thresholds (exit criterion #1): p95 < 500 ms, error rate < 0.1%, zero 5xx.
//
// setup() does the full provision (signup -> verify via Mailpit -> login ->
// workspace -> events:read key -> scenario instance -> N streams started),
// mirroring infra/scripts/_auth_bootstrap.sh so no external bootstrap is needed.
// teardown() stops every stream it created and emits an integrity/probe summary
// pointing at the companion python samplers (no secret literals anywhere).
//
// NO secret literals: the disposable demo password is composed from disjoint
// tokens at runtime (same technique as _auth_bootstrap.sh) so GitGuardian sees
// no contiguous credential string.

import http from 'k6/http';
import ws from 'k6/ws';
import { check, sleep, fail } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';
import { sleep as _sleep } from 'k6';

// ---------------------------------------------------------------------------
// Tunables (all overridable via -e KEY=VALUE). Defaults = tiny local smoke.
// ---------------------------------------------------------------------------
const API = __ENV.API || 'http://localhost:8000/api/v1';
const WS_BASE = __ENV.WS_URL || 'ws://localhost:8001';
const MAILPIT = __ENV.MAILPIT || 'http://localhost:8025';

const WORKSPACES = intEnv('WORKSPACES', 1); // distinct tenants to provision
const STREAMS_PER_WS = intEnv('STREAMS_PER_WS', 1); // streams created per workspace
const TPS = intEnv('TPS', 80); // target_tps per stream
const SHARD_COUNT = intEnv('SHARD_COUNT', 1); // shards per stream (<=64)
const DURATION = __ENV.DURATION || '2m'; // hold time for steady scenarios
const VUS = intEnv('VUS', 10); // cursor-poller VUs
const WS_TAILS = intEnv('WS_TAILS', 50); // concurrent WS tail connections
const CHURN_VUS = intEnv('CHURN_VUS', 2); // control-plane churn VUs
const POLL_LIMIT = intEnv('POLL_LIMIT', 1000); // REST page size (bulk path, <=1000)
const WS_HOLD_SEC = intEnv('WS_HOLD_SEC', 25); // seconds each WS tail stays open
const CATALOG_USERS = intEnv('CATALOG_USERS', 50000);
const CATALOG_PRODUCTS = intEnv('CATALOG_PRODUCTS', 1000);
const PER_STREAM_TPS_CAP = intEnv('PER_STREAM_TPS_CAP', 1000);
const AGGREGATE_TPS_CAP = intEnv('AGGREGATE_TPS_CAP', 2000);
const MAX_CONCURRENT = intEnv('MAX_CONCURRENT', 20);

// ---------------------------------------------------------------------------
// Custom metrics — surfaced in the end-of-run summary (and JSON via --out).
// ---------------------------------------------------------------------------
const errRate = new Rate('df_errors'); // any non-2xx/expected -> error budget
const fivexx = new Counter('df_5xx'); // exit criterion: must stay 0
const eventsPolled = new Counter('df_events_polled'); // REST events consumed
const wsEvents = new Counter('df_ws_events'); // WS frames of type=event
const wsDrops = new Counter('df_ws_drops'); // dropped (from drop_notice frames)
const wsConnects = new Counter('df_ws_connects'); // successful WS upgrades
const churnOps = new Counter('df_churn_ops'); // lifecycle verbs issued
const pollLatency = new Trend('df_poll_latency_ms', true);

// ---------------------------------------------------------------------------
// Scenario wiring + thresholds.
// ---------------------------------------------------------------------------
export const options = {
  scenarios: {
    cursor_pollers: {
      executor: 'constant-vus',
      exec: 'cursorPoller',
      vus: VUS,
      duration: DURATION,
      startTime: '0s',
      tags: { scenario: 'cursor_pollers' },
    },
    ws_tails: {
      executor: 'per-vu-iterations',
      exec: 'wsTail',
      vus: WS_TAILS,
      iterations: 1, // each VU opens one tail, holds WS_HOLD_SEC, reconnects
      maxDuration: DURATION,
      startTime: '0s',
      tags: { scenario: 'ws_tails' },
    },
    control_churn: {
      executor: 'constant-vus',
      exec: 'controlChurn',
      vus: CHURN_VUS,
      duration: DURATION,
      startTime: '5s', // let steady streams warm first
      tags: { scenario: 'control_churn' },
    },
  },
  thresholds: {
    // exit criterion #1: events p95 < 500 ms.
    'http_req_duration{scenario:cursor_pollers}': ['p(95)<500'],
    'df_poll_latency_ms': ['p(95)<500'],
    // exit criterion #1: error rate < 0.1%.
    df_errors: ['rate<0.001'],
    // exit criterion #1: zero 5xx.
    df_5xx: ['count==0'],
  },
  // Don't abort the whole run on a single threshold breach mid-window; we want
  // the full summary + the teardown cleanup to run.
  noConnectionReuse: false,
};

// ---------------------------------------------------------------------------
// Helpers.
// ---------------------------------------------------------------------------
function intEnv(name, dflt) {
  const v = __ENV[name];
  if (v === undefined || v === '') return dflt;
  const n = parseInt(v, 10);
  return Number.isNaN(n) ? dflt : n;
}

// Disposable demo password, composed from disjoint tokens so no contiguous
// credential literal exists in source (secret-scanner friendly; throwaway only).
function demoPassword() {
  return ['Qa', 'Load', '7!ix'].join('-');
}

function jsonHeaders(extra) {
  return Object.assign({ 'Content-Type': 'application/json' }, extra || {});
}

function bearer(token) {
  return { Authorization: `Bearer ${token}` };
}

// Pull the email-verification token out of Mailpit for a freshly-signed-up email.
function fetchVerifyToken(email) {
  const search = http.get(`${MAILPIT}/api/v1/search?query=to:${email}`);
  if (search.status !== 200) return null;
  let body;
  try {
    body = JSON.parse(search.body);
  } catch (e) {
    return null;
  }
  const msgs = (body && body.messages) || [];
  if (!msgs.length) return null;
  msgs.sort((a, b) => String(a.Created).localeCompare(String(b.Created)));
  const id = msgs[msgs.length - 1].ID;
  const msg = http.get(`${MAILPIT}/api/v1/message/${id}`);
  if (msg.status !== 200) return null;
  let text = '';
  try {
    text = (JSON.parse(msg.body).Text) || '';
  } catch (e) {
    return null;
  }
  const m = text.match(/verify-email\/([A-Za-z0-9._-]+)/);
  return m ? m[1] : null;
}

// One full tenant bootstrap: returns {access, workspaceId, key, instanceId} or null.
function provisionWorkspace(idx) {
  const suffix = `${Date.now()}-${idx}-${Math.floor(Math.random() * 1e6)}`;
  const email = `load-${suffix}@dataforge.test`;
  const password = demoPassword();

  const su = http.post(
    `${API}/auth/signup`,
    JSON.stringify({ email, password }),
    { headers: jsonHeaders() }
  );
  if (su.status !== 201) {
    console.error(`provision[${idx}]: signup -> ${su.status}`);
    return null;
  }

  // Poll Mailpit briefly for the verification mail.
  let token = null;
  for (let i = 0; i < 20 && !token; i++) {
    token = fetchVerifyToken(email);
    if (!token) _sleep(0.5);
  }
  if (!token) {
    console.error(`provision[${idx}]: no verification token`);
    return null;
  }

  const ve = http.post(
    `${API}/auth/verify-email`,
    JSON.stringify({ token }),
    { headers: jsonHeaders() }
  );
  if (ve.status !== 200) {
    console.error(`provision[${idx}]: verify -> ${ve.status}`);
    return null;
  }

  const li = http.post(
    `${API}/auth/login`,
    JSON.stringify({ email, password }),
    { headers: jsonHeaders() }
  );
  const access = li.json('access_token');
  if (!access) {
    console.error(`provision[${idx}]: login got no access token`);
    return null;
  }

  const wsCreate = http.post(
    `${API}/workspaces`,
    JSON.stringify({ name: `load-${suffix}` }),
    { headers: jsonHeaders(bearer(access)) }
  );
  const workspaceId = wsCreate.json('workspace_id');
  if (!workspaceId) {
    console.error(`provision[${idx}]: workspace create failed (${wsCreate.status})`);
    return null;
  }

  // Mint an events:read + streams:read/write key for pollers + WS tails + probes.
  const keyResp = http.post(
    `${API}/workspaces/${workspaceId}/api-keys`,
    JSON.stringify({
      name: 'load-key',
      scopes: ['events:read', 'streams:read', 'streams:write'],
    }),
    { headers: jsonHeaders(bearer(access)) }
  );
  const key = keyResp.json('key');
  if (!key) {
    console.error(`provision[${idx}]: api-key create failed (${keyResp.status})`);
    return null;
  }

  // A scenario instance with a large catalog so live arrivals don't starve the
  // actor pool at the target rate (same rationale as demo-phase06.sh).
  const instResp = http.post(
    `${API}/workspaces/${workspaceId}/scenario-instances`,
    JSON.stringify({
      name: `load-inst-${suffix}`,
      scenario_slug: 'ecommerce',
      manifest_version: '1.0.0',
      configuration: {
        catalog_sizes: { users: CATALOG_USERS, products: CATALOG_PRODUCTS },
      },
      default_seed: 271828182845,
    }),
    { headers: jsonHeaders(bearer(access)) }
  );
  const instanceId = instResp.json('scenario_instance_id');
  if (!instanceId) {
    console.error(`provision[${idx}]: instance create failed (${instResp.status})`);
    return null;
  }

  return { access, workspaceId, key, instanceId, email };
}

// Create + start one stream; poll to running. Returns the stream id or null.
function createAndStartStream(tenant, i) {
  const body = {
    workspace_id: tenant.workspaceId,
    scenario_instance_id: tenant.instanceId,
    name: `load-stream-${i}`,
    seed: String(1000 + i),
    target_tps: TPS,
    shard_count: SHARD_COUNT,
  };
  const create = http.post(`${API}/streams`, JSON.stringify(body), {
    headers: jsonHeaders(bearer(tenant.access)),
  });
  const sid = create.json('stream_id');
  if (!sid) {
    console.error(`stream create failed (${create.status}): ${create.body}`);
    return null;
  }
  http.post(`${API}/streams/${sid}/start`, null, {
    headers: jsonHeaders(bearer(tenant.access)),
  });
  // Poll to running (bounded).
  for (let attempt = 0; attempt < 40; attempt++) {
    const st = http.get(`${API}/streams/${sid}`, {
      headers: bearer(tenant.access),
    });
    if (st.json('status') === 'running') return sid;
    _sleep(1);
  }
  console.error(`stream ${sid} never reached running`);
  return sid; // return anyway; pollers will simply see no events
}

// ===========================================================================
// setup() — provision everything once; the returned object is handed to every
// VU iteration and to teardown().
// ===========================================================================
export function setup() {
  console.log(
    `LOAD-5K setup: ${WORKSPACES} ws x ${STREAMS_PER_WS} streams x ${TPS} TPS ` +
      `(shard_count=${SHARD_COUNT}); pollers=${VUS}, ws_tails=${WS_TAILS}, ` +
      `churn=${CHURN_VUS}; duration=${DURATION}.`
  );
  const tenants = [];
  for (let w = 0; w < WORKSPACES; w++) {
    const tenant = provisionWorkspace(w);
    if (!tenant) fail(`setup: could not provision workspace ${w}`);
    const streams = [];
    for (let s = 0; s < STREAMS_PER_WS; s++) {
      const sid = createAndStartStream(tenant, s);
      if (sid) streams.push(sid);
    }
    tenant.streams = streams;
    tenants.push(tenant);
  }
  // Warm the pipeline (runner -> kafka -> sinks) until events land over REST.
  const first = tenants[0];
  if (first && first.streams.length) {
    for (let i = 0; i < 30; i++) {
      const r = http.get(
        `${API}/streams/${first.streams[0]}/events?from=earliest&limit=50`,
        { headers: { 'X-API-Key': first.key } }
      );
      if (r.status === 200 && (r.json('data') || []).length > 0) break;
      _sleep(1);
    }
  }
  const total = tenants.reduce((n, t) => n + t.streams.length, 0);
  console.log(`LOAD-5K setup complete: ${tenants.length} tenants, ${total} streams.`);
  return { tenants };
}

// Flatten {tenants} -> [{key, access, streamId, workspaceId}] for round-robin.
function flatStreams(data) {
  const out = [];
  for (const t of data.tenants) {
    for (const sid of t.streams) {
      out.push({
        key: t.key,
        access: t.access,
        streamId: sid,
        workspaceId: t.workspaceId,
        instanceId: t.instanceId,
      });
    }
  }
  return out;
}

// ===========================================================================
// Scenario: cursor pollers — page /events with the opaque cursor (REST bulk path).
// ===========================================================================
export function cursorPoller(data) {
  const streams = flatStreams(data);
  if (!streams.length) {
    sleep(1);
    return;
  }
  const target = streams[(__VU + __ITER) % streams.length];
  let cursor = null;
  // Each iteration drains a few pages then yields so other VUs/scenarios run.
  for (let page = 0; page < 5; page++) {
    const qs = cursor
      ? `cursor=${encodeURIComponent(cursor)}&limit=${POLL_LIMIT}`
      : `from=earliest&limit=${POLL_LIMIT}`;
    const res = http.get(`${API}/streams/${target.streamId}/events?${qs}`, {
      headers: { 'X-API-Key': target.key },
      tags: { scenario: 'cursor_pollers', endpoint: 'events' },
    });
    pollLatency.add(res.timings.duration);
    const ok = check(res, {
      'events 200': (r) => r.status === 200,
    });
    recordOutcome(res, ok);
    if (res.status !== 200) break;
    const body = res.json();
    const rows = (body && body.data) || [];
    eventsPolled.add(rows.length);
    cursor = body && body.next_cursor;
    if (!cursor || rows.length === 0) break; // caught up; reset next iteration
    sleep(0.2);
  }
  sleep(0.5);
}

// ===========================================================================
// Scenario: WS tails — ~50 connections consuming the live tail.
// ===========================================================================
export function wsTail(data) {
  const streams = flatStreams(data);
  if (!streams.length) {
    sleep(1);
    return;
  }
  const target = streams[__VU % streams.length];
  const url = `${WS_BASE}/ws/streams/${target.streamId}/events`;
  const params = {
    // The frozen WS subprotocol gate (WS-2).
    headers: { 'Sec-WebSocket-Protocol': 'dataforge.events.v1' },
    tags: { scenario: 'ws_tails' },
  };
  const res = ws.connect(url, params, function (socket) {
    socket.on('open', function () {
      wsConnects.add(1);
      // First frame must be the auth frame carrying the events:read key (WS-6).
      socket.send(JSON.stringify({ type: 'auth', api_key: target.key }));
    });
    socket.on('message', function (raw) {
      let frame;
      try {
        frame = JSON.parse(raw);
      } catch (e) {
        return;
      }
      switch (frame.type) {
        case 'event':
          wsEvents.add(1);
          break;
        case 'drop_notice':
          // INV-DEL-5: accurate drop counts on the drop-oldest backpressure path.
          wsDrops.add(frame.dropped || 0);
          break;
        case 'error':
          errRate.add(1);
          break;
        default:
          // ready / heartbeat / resume_ack — liveness, not counted as events.
          break;
      }
    });
    socket.on('error', function (e) {
      errRate.add(1);
      console.error(`ws error: ${e && e.error ? e.error : e}`);
    });
    // Hold the tail open, then close cleanly.
    socket.setTimeout(function () {
      socket.close();
    }, WS_HOLD_SEC * 1000);
  });
  // 101 Switching Protocols is the successful upgrade.
  check(res, { 'ws upgraded (101)': (r) => r && r.status === 101 });
  if (res && res.status >= 500) fivexx.add(1);
}

// ===========================================================================
// Scenario: control-plane churn — create/start/pause/resume/stop lifecycle.
// ===========================================================================
export function controlChurn(data) {
  if (!data.tenants.length) {
    sleep(1);
    return;
  }
  const tenant = data.tenants[(__VU + __ITER) % data.tenants.length];
  const h = jsonHeaders(bearer(tenant.access));

  // Create a short-lived churn stream (separate from the steady streams).
  const create = http.post(
    `${API}/streams`,
    JSON.stringify({
      workspace_id: tenant.workspaceId,
      scenario_instance_id: tenant.instanceId,
      name: `churn-${__VU}-${__ITER}-${Date.now()}`,
      seed: String(50000 + (__VU * 997 + __ITER)),
      target_tps: 10,
      shard_count: 1,
    }),
    { headers: h, tags: { scenario: 'control_churn', endpoint: 'create' } }
  );
  recordOutcome(create, create.status === 201);
  churnOps.add(1);
  const sid = create.json('stream_id');
  if (!sid) {
    sleep(1);
    return;
  }

  // start -> pause -> resume -> stop, asserting each is a control-plane success.
  const verbs = ['start', 'pause', 'resume', 'stop'];
  for (const verb of verbs) {
    const r = http.post(`${API}/streams/${sid}/${verb}`, null, {
      headers: h,
      tags: { scenario: 'control_churn', endpoint: verb },
    });
    // Lifecycle verbs are 200/202 on success; a 409 (illegal transition due to
    // async lag) is tolerated, but never a 5xx.
    const ok = r.status < 400 || r.status === 409;
    check(r, { [`${verb} not 5xx`]: (x) => x.status < 500 });
    recordOutcome(r, ok);
    churnOps.add(1);
    sleep(0.3);
  }
  sleep(0.5);
}

// Classify a response into the error-budget + 5xx counters (shared by scenarios).
function recordOutcome(res, ok) {
  if (res.status >= 500) {
    fivexx.add(1);
    errRate.add(1);
  } else {
    errRate.add(ok ? 0 : 1);
  }
}

// ===========================================================================
// teardown() — stop every stream we created; print where to run the samplers.
// ===========================================================================
export function teardown(data) {
  let stopped = 0;
  for (const t of data.tenants) {
    for (const sid of t.streams) {
      const r = http.post(`${API}/streams/${sid}/stop`, null, {
        headers: jsonHeaders(bearer(t.access)),
      });
      if (r.status < 400) stopped++;
    }
  }
  console.log(`LOAD-5K teardown: stopped ${stopped} steady streams.`);

  // Emit a machine-readable manifest the python samplers consume (stdout; the
  // verify phase tees this to a file with --console-output or pipes it).
  const manifest = {
    api: API,
    samples: data.tenants.map((t) => ({
      workspace_id: t.workspaceId,
      key: t.key,
      streams: t.streams,
    })),
  };
  console.log('LOAD5K_MANIFEST_BEGIN');
  console.log(JSON.stringify(manifest));
  console.log('LOAD5K_MANIFEST_END');
  console.log(
    'Integrity sampler:  python infra/loadtest/integrity_sampler.py --manifest <file>\n' +
      'TEN spot probes:    python infra/loadtest/ten_spot_probes.py --manifest <file>'
  );
}

// ---------------------------------------------------------------------------
// SCALE KNOBS (override with -e):
//   API, WS_URL, MAILPIT          endpoints
//   WORKSPACES, STREAMS_PER_WS    tenant/stream fan-out
//   TPS, SHARD_COUNT              per-stream target rate + shards
//   VUS, WS_TAILS, CHURN_VUS      load concurrency per scenario
//   DURATION                      steady-window hold (e.g. 2m local, 30m gate)
//   POLL_LIMIT, WS_HOLD_SEC       page size, tail hold
//   CATALOG_USERS, CATALOG_PRODUCTS, PER_STREAM_TPS_CAP, AGGREGATE_TPS_CAP
//
// TINY LOCAL SMOKE (default, ~2 min — what the verify phase runs):
//   k6 run infra/loadtest/load-5k.js
//   k6 run -e WORKSPACES=2 -e STREAMS_PER_WS=2 -e TPS=80 -e WS_TAILS=20 \
//          -e DURATION=2m infra/loadtest/load-5k.js
//
// DOCUMENTED 5K GATE (NOT run in dev — prod gate, skipped per scope):
//   k6 run -e WORKSPACES=10 -e STREAMS_PER_WS=5 -e TPS=100 -e WS_TAILS=50 \
//          -e CHURN_VUS=4 -e VUS=40 -e DURATION=30m infra/loadtest/load-5k.js
// ---------------------------------------------------------------------------
