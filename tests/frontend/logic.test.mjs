import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ApiClient,
  ApiError,
  AnalyticalQueue,
  StaleRequest,
  queryString,
  verifyPage,
} from '../../src/outage_explorer/frontend/static/outage_explorer/api.js';
import {
  isoDate,
  addDays,
  dayCount,
  dateLabel,
  numberLabel,
  scalarLabel,
  columnTitle,
} from '../../src/outage_explorer/frontend/static/outage_explorer/dom.js';
import {
  csvCell,
  csvText,
} from '../../src/outage_explorer/frontend/static/outage_explorer/table.js';
import {
  chartPoints,
  contiguousSegments,
} from '../../src/outage_explorer/frontend/static/outage_explorer/chart.js';
import {
  cleanHistory,
  exampleSql,
  quoteIdentifier,
  retireHistoryScopes,
  HISTORY_PREFIX,
} from '../../src/outage_explorer/frontend/static/outage_explorer/sql.js';
import { Workspace } from '../../src/outage_explorer/frontend/static/outage_explorer/app.js';
import { RefreshController } from '../../src/outage_explorer/frontend/static/outage_explorer/refresh.js';

const model = 'a'.repeat(32);
const page = {
  dataset_id: 'fleet_daily',
  model_id: model,
  columns: [
    { name: 'period', type: 'DATE' },
    { name: 'offline_pct', type: 'DECIMAL(24,9)' },
  ],
  rows: [['2026-09-01', '0.000000000']],
};

test('query encoding preserves identity and zero while omitting absent filters', () => {
  const value = new URLSearchParams(
    queryString({ facility_id: '001', limit: 0, start: '', end: null }).slice(1),
  );
  assert.equal(value.get('facility_id'), '001');
  assert.equal(value.get('limit'), '0');
  assert.equal(value.size, 2);
});

test('result identity, shape and finite scalars fail closed', () => {
  assert.equal(verifyPage(page, 'fleet_daily', model), page);
  for (const modified of [
    { ...page, model_id: 'b'.repeat(32) },
    { ...page, dataset_id: 'generator_daily' },
    { ...page, rows: [['2026-09-01']] },
    { ...page, rows: [['2026-09-01', Infinity]] },
    { ...page, rows: [['2026-09-01', { html: 'bad' }]] },
  ]) {
    assert.throws(() => verifyPage(modified, 'fleet_daily', model), ApiError);
  }
});

test('calendar arithmetic stays in UTC across DST/leap boundaries and rejects invalid dates', () => {
  assert.equal(addDays('2026-11-01', 1), '2026-11-02');
  assert.equal(addDays('2024-02-28', 1), '2024-02-29');
  assert.equal(dayCount('2026-03-07', '2026-03-09'), 3);
  assert.equal(isoDate('2026-02-30'), null);
  assert.equal(isoDate('20260901'), null);
  assert.throws(() => dayCount('2026-09-03', '2026-09-01'));
  assert.equal(dateLabel('2026-09-01'), 'Sep 1, 2026');
});

test('zero, missing values, exact decimals and text identifiers remain distinct', () => {
  assert.equal(numberLabel(null), '—');
  assert.equal(numberLabel(0), '0.0');
  assert.equal(scalarLabel('001', 'VARCHAR'), '001');
  assert.equal(scalarLabel('9007199254740993', 'BIGINT'), '9,007,199,254,740,993');
  assert.equal(scalarLabel('1234.123456789012', 'DECIMAL(24,12)'), '1,234.123456789012');
  assert.equal(scalarLabel('100.250000000', 'DECIMAL(24,9)'), '100.25');
  assert.equal(scalarLabel(null, 'DECIMAL(24,9)'), '—');
});

test('user SQL aliases do not resolve inherited JavaScript object properties', () => {
  assert.equal(columnTitle('constructor'), 'constructor');
  assert.equal(columnTitle('toString'), 'toString');
  assert.equal(typeof columnTitle('__proto__'), 'string');
  assert.equal(columnTitle('capacity_mw'), 'Capacity (MW)');
});

test('spreadsheet-safe CSV protects formulas, leading whitespace and leading-zero text IDs', () => {
  for (const value of ['=1+1', '+cmd', '-cmd', '@SUM(A1)', ' \t=1+1', '\ttext']) {
    assert.ok(csvCell(value, 'VARCHAR').startsWith('"\''), value);
  }
  assert.equal(csvCell('001', 'VARCHAR'), '"\'001"');
  assert.equal(csvCell('-1.230000000', 'DECIMAL(24,9)'), '"-1.230000000"');
  assert.equal(csvCell('a,"b"\nc', 'VARCHAR'), '"a,""b""\nc"');
  assert.equal(csvCell(null), '""');
});

test('CSV exports exactly the supplied page and preserves exact numeric strings', () => {
  const result = csvText({
    columns: [{ name: '=alias', type: 'BIGINT' }],
    rows: [['9007199254740993']],
  });
  assert.equal(result, '\ufeff"\'=alias"\r\n"9007199254740993"\r\n');
});

test('chart preserves true zero and outliers, and breaks at both nulls and missing dates', () => {
  const data = {
    ...page,
    rows: [
      ['2026-09-05', '110.000000000'],
      ['2026-09-01', '0.000000000'],
      ['2026-09-03', '20.000000000'],
      ['2026-09-04', null],
    ],
  };
  const points = chartPoints(data);
  assert.equal(points[0].value, 0);
  assert.equal(points.at(-1).value, 110);
  assert.deepEqual(
    contiguousSegments(points).map((group) => group.map((point) => point.period)),
    [['2026-09-01'], ['2026-09-03'], ['2026-09-05']],
  );
});

test('history is bounded and filters entries by the current authorized catalog', () => {
  const entry = {
    dataset_id: 'fleet_daily',
    sql: '<img src=x onerror=bad>',
    status: 'success',
    created_at: '2026-11-01T00:00:00Z',
    ignored: 'not retained',
  };
  const result = cleanHistory(
    [
      entry,
      { ...entry, dataset_id: 'generator_daily' },
      { ...entry, sql: 'x'.repeat(17000) },
      ...Array(15).fill(entry),
    ],
    ['fleet_daily'],
  );
  assert.equal(result.length, 10);
  assert.equal(result[0].sql, entry.sql);
  assert.equal(result[0].ignored, undefined);
  assert.deepEqual(cleanHistory(null, []), []);
});

test('SQL examples use only the chosen schema and deterministic date ordering', () => {
  const dataset = {
    id: 'generator_daily',
    schema: [
      ['period', 'DATE'],
      ['facility_id', 'VARCHAR'],
      ['generator_id', 'VARCHAR'],
    ],
    primary_key: ['period', 'facility_id', 'generator_id'],
  };
  const sql = exampleSql(dataset);
  assert.ok(sql.includes('FROM "generator_daily"'));
  assert.ok(sql.includes('ORDER BY "period" DESC, "facility_id", "generator_id"'));
  assert.equal(quoteIdentifier('a"b'), '"a""b"');
});

test('analytic queue serializes work, skips obsolete queued requests and discards stale in-flight results', async () => {
  const queue = new AnalyticalQueue();
  let release;
  let current = true;
  const events = [];
  const first = queue.run(
    async () => {
      events.push('first');
      await new Promise((resolve) => {
        release = resolve;
      });
      return 1;
    },
    () => current,
  );
  await new Promise((resolve) => setImmediate(resolve));
  const obsolete = queue.run(async () => {
    events.push('obsolete');
  });
  const last = queue.run(async () => {
    events.push('last');
    return 3;
  });
  current = false;
  // Attach rejection handlers before releasing to avoid unhandled rejections.
  const firstResult = assert.rejects(first, StaleRequest);
  release();
  await firstResult;
  await obsolete;
  assert.equal(await last, 3);
  assert.deepEqual(events, ['first', 'obsolete', 'last']);
  let ran = false;
  await assert.rejects(
    queue.run(
      async () => {
        ran = true;
      },
      () => false,
    ),
    StaleRequest,
  );
  assert.equal(ran, false);
});

test('client rotates CSRF after rejection and never sends credentials in URLs', async (t) => {
  const original = globalThis.fetch,
    calls = [];
  t.after(() => {
    globalThis.fetch = original;
  });
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1)
      return new Response(JSON.stringify({ code: 'csrf_failed', detail: 'CSRF' }), {
        status: 403,
        headers: { 'Content-Type': 'application/problem+json' },
      });
    if (url.endsWith('/auth/csrf'))
      return new Response(JSON.stringify({ csrf_token: 'rotated' }), {
        headers: { 'Content-Type': 'application/json' },
      });
    return new Response(JSON.stringify({ ok: true }), {
      headers: { 'Content-Type': 'application/json' },
    });
  };
  const client = new ApiClient();
  client.csrf = 'old';
  assert.deepEqual(await client.request('/query', { method: 'POST', body: { sql: 'SELECT 1' } }), {
    ok: true,
  });
  assert.equal(calls.length, 3);
  assert.equal(calls[2].options.headers['X-CSRFToken'], 'rotated');
  assert.equal(calls[2].options.credentials, 'same-origin');
  assert.equal(calls[2].url, '/api/v1/query');
  assert.ok(!calls[2].url.includes('SELECT'));
});

test('client signals session loss but does not mistake network failure for logout', async (t) => {
  const original = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = original;
  });
  let lost = 0;
  const client = new ApiClient({ onSessionLost: () => lost++ });
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ code: 'session_revoked', detail: 'Sign in again.' }), {
      status: 401,
      headers: { 'Content-Type': 'application/problem+json' },
    });
  await assert.rejects(client.request('/catalog'), ApiError);
  assert.equal(lost, 1);
  globalThis.fetch = async () => {
    throw new TypeError('offline');
  };
  await assert.rejects(client.request('/catalog'), (error) => error.code === 'network_error');
  assert.equal(lost, 1);
});

test('focus escalates an in-flight background session check and keeps data hidden on network failure', async (t) => {
  const original = globalThis.document;
  t.after(() => {
    if (original === undefined) delete globalThis.document;
    else globalThis.document = original;
  });
  const root = { hidden: false };
  globalThis.document = { getElementById: (id) => (id === 'app-root' ? root : null) };
  let rejectCheck,
    shields = 0;
  const workspace = {
    ended: false,
    catalog: { model_id: model },
    scope: { username: 'viewer', persona: 'viewer', session_scope: 'new-scope' },
    client: {
      request: () =>
        new Promise((resolve, reject) => {
          rejectCheck = reject;
        }),
    },
    shield: () => {
      shields++;
      root.hidden = true;
    },
    checkLatest: () => {},
  };
  const pending = Workspace.prototype.checkSession.call(workspace, false);
  await Workspace.prototype.checkSession.call(workspace, true);
  assert.equal(root.hidden, true);
  rejectCheck(new ApiError('offline'));
  await pending;
  assert.equal(root.hidden, true);
  assert.equal(shields, 1);
  assert.equal(workspace.sessionCheck, false);
  workspace.client.request = async () => ({
    user: { username: 'viewer', persona: 'viewer' },
    session_scope: 'new-scope',
  });
  await Workspace.prototype.checkSession.call(workspace, false);
  assert.equal(root.hidden, false);
  assert.equal(workspace.protectSessionCheck, false);
});

test('new session scopes retire old account/role histories but reload preserves the current scope', () => {
  const data = new Map([
    [HISTORY_PREFIX + 'old-analyst', 'private'],
    [HISTORY_PREFIX + 'current-viewer', 'own'],
    ['unrelated-setting', 'keep'],
  ]);
  const storage = {
    get length() {
      return data.size;
    },
    key: (index) => [...data.keys()][index],
    removeItem: (key) => data.delete(key),
  };
  retireHistoryScopes(storage, HISTORY_PREFIX + 'current-viewer');
  assert.equal(data.has(HISTORY_PREFIX + 'old-analyst'), false);
  assert.equal(data.get(HISTORY_PREFIX + 'current-viewer'), 'own');
  assert.equal(data.get('unrelated-setting'), 'keep');
  retireHistoryScopes(storage);
  assert.deepEqual([...data], [['unrelated-setting', 'keep']]);
});

test('a replacement session for the same account/role cannot reuse the prior workspace', async (t) => {
  const original = globalThis.document;
  t.after(() => {
    if (original === undefined) delete globalThis.document;
    else globalThis.document = original;
  });
  const root = { hidden: false };
  globalThis.document = { getElementById: () => root };
  let expired = false;
  const workspace = {
    ended: false,
    catalog: {},
    scope: { username: 'viewer', persona: 'viewer', session_scope: 'old' },
    client: {
      request: async () => ({
        user: { username: 'viewer', persona: 'viewer' },
        session_scope: 'new',
      }),
    },
    expire: () => {
      expired = true;
      root.hidden = true;
    },
  };
  await Workspace.prototype.checkSession.call(workspace, true);
  assert.equal(expired, true);
  assert.equal(root.hidden, true);
});

test('delayed initial refresh history cannot replace a newly queued job', async (t) => {
  const original = globalThis.document;
  t.after(() => {
    if (original === undefined) delete globalThis.document;
    else globalThis.document = original;
  });
  const nodes = new Map();
  globalThis.document = {
    getElementById: (id) => {
      if (!nodes.has(id)) nodes.set(id, {});
      return nodes.get(id);
    },
    querySelector: () => ({ value: 'recent' }),
  };
  let resolveOld,
    requests = 0;
  const queued = { id: 'new-job', mode: 'recent', status: 'queued' };
  const controller = Object.assign(Object.create(RefreshController.prototype), {
    sequence: 0,
    loaded: false,
    job: null,
    busy: false,
    idempotency: null,
    context: {
      alive: () => true,
      client: {
        request: async (path, options) => {
          if (options?.method === 'POST') return queued;
          if (++requests === 1)
            return new Promise((resolve) => {
              resolveOld = resolve;
            });
          return { jobs: [queued] };
        },
      },
    },
    renderJobs: () => {},
    render: () => {},
    schedule: () => {},
  });
  const initial = controller.load();
  await controller.start();
  assert.equal(controller.job.id, 'new-job');
  resolveOld({ jobs: [{ id: 'old-job', status: 'succeeded' }] });
  await initial;
  assert.equal(controller.job.id, 'new-job');
  assert.equal(controller.job.status, 'queued');
});
