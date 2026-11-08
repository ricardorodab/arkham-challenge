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
import {
  ExploreController,
  compareDecimals,
  normalizeConditions,
  filterOperators,
  retainCursors,
  loadedSearchIndex,
  filterLoadedPage,
} from '../../src/outage_explorer/frontend/static/outage_explorer/explore.js';
import {
  RefreshController,
  refreshProgress,
  refreshTimes,
} from '../../src/outage_explorer/frontend/static/outage_explorer/refresh.js';

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
  assert.deepEqual(
    await client.request('/query', {
      method: 'POST',
      body: { sql: 'SELECT 1' },
    }),
    {
      ok: true,
    },
  );
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

for (const catalog of [null, { model_id: model }]) {
  test(`focus protects ${catalog ? 'published' : 'empty'} workspaces during an in-flight session failure`, async (t) => {
    const original = globalThis.document;
    t.after(() => {
      if (original === undefined) delete globalThis.document;
      else globalThis.document = original;
    });
    const root = { hidden: false };
    globalThis.document = {
      getElementById: (id) => (id === 'app-root' ? root : null),
    };
    let rejectCheck,
      shields = 0;
    const workspace = {
      ended: false,
      catalog,
      scope: {
        username: 'viewer',
        persona: 'viewer',
        session_scope: 'new-scope',
      },
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
}

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

for (const catalog of [null, {}]) {
  test(`a replacement session cannot reuse the ${catalog ? 'published' : 'empty'} workspace`, async (t) => {
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
      catalog,
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
}

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

test('a first publication is discovered without adoption, then applied across the workspace', async (t) => {
  const original = globalThis.document;
  t.after(() => {
    if (original === undefined) delete globalThis.document;
    else globalThis.document = original;
  });
  const nodes = new Map();
  const node = () => ({
    hidden: false,
    attributes: {},
    children: [],
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
    replaceChildren(...children) {
      this.children = children;
    },
  });
  globalThis.document = {
    getElementById: (id) => {
      if (!nodes.has(id)) nodes.set(id, node());
      return nodes.get(id);
    },
    createElement: () => node(),
  };
  const priorError = document.getElementById('global-notice');
  priorError.attributes.role = 'alert';
  priorError.children = [{ textContent: 'No analytical model is published.' }];
  const published = { model_id: model, datasets: [] };
  const configured = [];
  let requests = 0,
    routed = 0;
  const workspace = Object.assign(Object.create(Workspace.prototype), {
    ended: false,
    catalog: null,
    candidate: null,
    client: {
      request: async (path) => {
        assert.equal(path, '/catalog');
        requests++;
        return published;
      },
    },
    overview: {
      configure: (catalog) => configured.push(['overview', catalog]),
    },
    explore: { configure: (catalog) => configured.push(['explore', catalog]) },
    sql: { configure: (catalog) => configured.push(['sql', catalog]) },
    refresh: { render: () => {} },
    route: () => routed++,
  });
  await workspace.checkLatest();
  assert.equal(requests, 1);
  assert.equal(workspace.catalog, null);
  assert.equal(workspace.candidate, published);
  assert.deepEqual(configured, []);
  assert.equal(document.getElementById('snapshot-notice').hidden, false);
  assert.equal(priorError.attributes.role, 'alert');

  await workspace.applyLatest();
  assert.equal(workspace.catalog, published);
  assert.equal(workspace.candidate, null);
  assert.deepEqual(
    configured,
    ['overview', 'explore', 'sql'].map((name) => [name, published]),
  );
  assert.equal(document.getElementById('snapshot-notice').hidden, true);
  assert.equal(priorError.attributes.role, 'status');
  assert.match(priorError.children[0].textContent, /latest verified publication/);
  assert.equal(routed, 1);
});

test('refresh history loads without a model while analytical controllers stay idle', (t) => {
  const originalDocument = globalThis.document,
    originalLocation = globalThis.location;
  t.after(() => {
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
    if (originalLocation === undefined) delete globalThis.location;
    else globalThis.location = originalLocation;
  });
  globalThis.document = {
    getElementById: () => ({}),
    querySelectorAll: () => [],
  };
  globalThis.location = { hash: '#refresh' };
  let history = 0,
    analytics = 0;
  const workspace = Object.assign(Object.create(Workspace.prototype), {
    ended: false,
    catalog: null,
    scope: { persona: 'admin' },
    toggleNav: () => {},
    refresh: { load: () => history++ },
    overview: { load: () => analytics++ },
    explore: { activate: () => analytics++ },
  });
  workspace.route(false);
  assert.equal(workspace.view, 'refresh');
  assert.equal(history, 1);
  location.hash = '#overview';
  workspace.route(false);
  assert.equal(analytics, 0);
});

test('refresh counters and stages are typed, allowlisted and absent for queued or legacy jobs', () => {
  const detail = refreshProgress({
    status: 'running',
    progress: {
      stage: 'extracting',
      route: 'facility',
      rows_received: 1234,
      pages_fetched: 3,
      routes: {
        national: { rows_received: 4, pages_fetched: 1 },
        facility: { rows_received: 1230, pages_fetched: 2 },
        unknown: { rows_received: 99 },
      },
    },
  });
  assert.match(detail.stage, /Downloading observations.*Facility/);
  assert.equal(detail.counts, '1,234 rows received · 3 pages fetched');
  assert.deepEqual(detail.routes, [
    'National: 4 rows received · 1 page fetched',
    'Facility: 1,230 rows received · 2 pages fetched',
  ]);
  for (const progress of [
    undefined,
    {},
    [],
    { stage: 'constructor', rows_received: 3 },
    { stage: ['extracting'], rows_received: 3 },
    { stage: '<img src=x onerror=bad>', rows_received: 3 },
  ]) {
    const legacy = refreshProgress({ status: 'running', progress });
    assert.equal(legacy.stage, '');
    assert.equal(legacy.counts, '');
    assert.equal(legacy.exportLabel, '');
  }
  assert.equal(
    refreshProgress({
      status: 'running',
      progress: {
        stage: 'extracting',
        route: '<img>',
        rows_received: '10',
        pages_fetched: -1,
      },
    }).counts,
    '',
  );
  assert.equal(
    refreshProgress({
      status: 'queued',
      progress: { stage: 'extracting', rows_received: 10 },
    }).stage,
    '',
  );
});

test('only a valid model-export denominator produces a subtask percentage', () => {
  const detail = (progress) => refreshProgress({ status: 'running', progress });
  assert.equal(
    detail({ stage: 'exporting_model', completed: 3, total: 10 }).exportLabel,
    'Model export 30%',
  );
  assert.equal(
    detail({ stage: 'exporting_model', completed: 0, total: 10 }).exportLabel,
    'Model export 0%',
  );
  for (const progress of [
    { stage: 'extracting', completed: 3, total: 10 },
    { stage: 'exporting_model', completed: 3, total: 0 },
    { stage: 'exporting_model', completed: 11, total: 10 },
    { stage: 'exporting_model', completed: true, total: 10 },
    { stage: 'exporting_model', completed: 3, total: 1000000 },
    { stage: 'exporting_model', completed: 0.5, total: 10 },
  ])
    assert.equal(detail(progress).exportLabel, '');
});

test('refresh timing measures actual events and freezes elapsed time when a job ends', () => {
  const updated_at = '2026-11-07T10:01:00Z';
  const job = {
    status: 'running',
    started_at: '2026-11-07T10:00:00Z',
    progress: { stage: 'extracting', updated_at },
  };
  const now = Date.parse('2026-11-07T10:02:05Z');
  assert.deepEqual(refreshTimes(job, now), {
    elapsed: '2m 5s',
    updated: '1m 5s ago',
    updatedAt: updated_at,
  });
  const finished = {
    ...job,
    status: 'failed',
    finished_at: '2026-11-07T10:02:00Z',
  };
  assert.equal(refreshTimes(finished, now).elapsed, '2m 0s');
  assert.deepEqual(refreshTimes(finished, now), refreshTimes(finished, now + 600000));
  assert.deepEqual(refreshTimes({ ...job, status: 'queued' }, now), {
    elapsed: '',
    updated: '',
    updatedAt: null,
  });
  assert.equal(refreshTimes({ status: 'interrupted', progress: {} }, now).elapsed, '');
});

test('count polls repaint progress without live-announcement chatter and timers stop on completion', (t) => {
  const original = globalThis.document;
  t.after(() => {
    if (original === undefined) delete globalThis.document;
    else globalThis.document = original;
  });
  const nodes = new Map();
  const node = () => ({
    hidden: false,
    dataset: {},
    attributes: {},
    writes: 0,
    text: '',
    children: [],
    get textContent() {
      return this.text;
    },
    set textContent(value) {
      this.text = value;
      this.writes++;
    },
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
    removeAttribute(name) {
      delete this.attributes[name];
    },
    replaceChildren(...children) {
      this.children = children;
    },
  });
  globalThis.document = {
    getElementById: (id) => {
      if (!nodes.has(id)) nodes.set(id, node());
      return nodes.get(id);
    },
    createElement: () => node(),
  };
  let tick,
    cleared = 0,
    now = Date.parse('2026-11-07T10:02:00Z');
  t.mock.method(globalThis, 'setInterval', (callback) => {
    tick = callback;
    return 1;
  });
  t.mock.method(globalThis, 'clearInterval', () => {
    cleared++;
  });
  t.mock.method(Date, 'now', () => now);
  const controller = Object.assign(Object.create(RefreshController.prototype), {
    context: { hasUpdate: () => false, alive: () => true },
    job: {
      id: 'job',
      status: 'running',
      started_at: '2026-11-07T10:00:00Z',
      progress: {
        stage: 'extracting',
        rows_received: 10,
        pages_fetched: 1,
        updated_at: '2026-11-07T10:01:00Z',
      },
    },
  });
  controller.render();
  const announcement = document.getElementById('refresh-announcement');
  assert.equal(announcement.writes, 1);
  controller.job.progress.rows_received = 20;
  controller.job.progress.pages_fetched = 2;
  controller.job.progress.route = 'generator';
  controller.render();
  assert.match(document.getElementById('refresh-counts').textContent, /20 rows.*2 pages/);
  assert.equal(announcement.writes, 1);
  now += 1000;
  tick();
  assert.equal(document.getElementById('refresh-updated').textContent, '1m 1s ago');
  assert.equal(announcement.writes, 1);
  controller.job.progress = {
    ...controller.job.progress,
    stage: 'exporting_model',
    completed: 3,
    total: 10,
  };
  controller.render();
  assert.equal(announcement.writes, 2);
  assert.equal(document.getElementById('refresh-progress-label').textContent, 'Model export 30%');
  controller.job.status = 'failed';
  controller.job.message = 'Refresh failed safely.';
  controller.job.finished_at = '2026-11-07T10:02:00Z';
  controller.render();
  assert.equal(document.getElementById('refresh-step-label').textContent, 'Last reported step');
  assert.match(document.getElementById('refresh-counts').textContent, /20 rows/);
  assert.equal(document.getElementById('refresh-elapsed').textContent, '2m 0s');
  assert.ok(cleared > 0);
  controller.dispose();
  assert.equal(document.getElementById('refresh-status').hidden, true);
});

test('polling merges running progress into retained history without an extra history request', async (t) => {
  const original = globalThis.document;
  t.after(() => {
    if (original === undefined) delete globalThis.document;
    else globalThis.document = original;
  });
  const nodes = new Map();
  const node = () => ({
    dataset: {},
    children: [],
    setAttribute() {},
    removeAttribute() {},
    replaceChildren(...children) {
      this.children = children;
    },
    append(...children) {
      this.children.push(...children);
    },
  });
  globalThis.document = {
    getElementById: (id) => {
      if (!nodes.has(id)) nodes.set(id, node());
      return nodes.get(id);
    },
    createElement: () => node(),
  };
  t.mock.method(globalThis, 'setInterval', () => 1);
  t.mock.method(globalThis, 'clearInterval', () => {});
  const queued = {
    id: 'job',
    mode: 'recent',
    status: 'queued',
    created_at: '2026-11-07T10:00:00Z',
  };
  const running = {
    ...queued,
    status: 'running',
    progress: { stage: 'extracting', rows_received: 7, pages_fetched: 1 },
  };
  const requests = [];
  const controller = Object.assign(Object.create(RefreshController.prototype), {
    context: {
      hasUpdate: () => false,
      alive: () => true,
      client: {
        request: async (path) => {
          requests.push(path);
          return running;
        },
      },
    },
    job: queued,
    sequence: 0,
    schedule: () => {},
  });
  controller.renderJobs([queued]);
  assert.equal(
    document.getElementById('refresh-jobs').children[0].children[1].textContent,
    'queued',
  );
  await controller.poll();
  const row = document.getElementById('refresh-jobs').children[0];
  assert.equal(row.children[1].textContent, 'running');
  assert.match(row.children[0].children[2].textContent, /7 rows received/);
  assert.deepEqual(requests, ['/refresh/job']);
  controller.dispose();
});

test('typed browse conditions preserve exact values and validate ranges without Number rounding', () => {
  const columns = [
    ['period', 'DATE'],
    ['amount', 'DECIMAL(24,9)'],
    ['identifier', 'BIGINT'],
    ['name', 'VARCHAR'],
    ['active', 'BOOLEAN'],
  ];
  const conditions = normalizeConditions(columns, [
    { column: 'amount', op: 'gte', value: '0.500000000' },
    { column: 'identifier', op: 'eq', value: '9007199254740993' },
    { column: 'name', op: 'eq', value: '' },
    {
      column: 'period',
      op: 'between',
      value: '2026-09-01',
      upper: '2026-09-03',
    },
    { column: 'active', op: 'eq', value: 'false' },
    { column: 'amount', op: 'is_null', value: 'ignored', upper: 'ignored' },
  ]);
  assert.equal(conditions[1].value, '9007199254740993');
  assert.equal(conditions[2].value, '');
  assert.equal(conditions[4].value, 'false');
  assert.deepEqual(conditions[5], { column: 'amount', op: 'is_null' });
  assert.ok(compareDecimals('9007199254740993', '9007199254740992') > 0);
  assert.ok(compareDecimals('-0.001', '-0.0001') < 0);
  assert.equal(compareDecimals('-0.000', '+0'), 0);
  for (const draft of [
    { column: 'amount', op: 'eq', value: '0.0000000001' },
    { column: 'amount', op: 'eq', value: '1e3' },
    { column: 'period', op: 'eq', value: '2026-02-30' },
    {
      column: 'identifier',
      op: 'between',
      value: '9007199254740993',
      upper: '9007199254740992',
    },
    { column: 'active', op: 'contains', value: 'true' },
    { column: 'name', op: 'eq', value: 'x\0y' },
  ])
    assert.throws(() => normalizeConditions(columns, [draft]));
  assert.throws(() =>
    normalizeConditions(columns, Array(9).fill({ column: 'name', op: 'eq', value: '' })),
  );
  assert.ok(filterOperators('BOOLEAN').includes('not_null'));
  assert.ok(!filterOperators('DATE').includes('contains'));
});

test('browse cursor history remains bounded and discards stale forward continuations', () => {
  let cursors = new Map([[0, null]]);
  for (let page = 0; page <= 1000; page++)
    cursors = retainCursors(cursors, page, `cursor-${page + 1}`);
  assert.equal(cursors.size, 52);
  assert.equal(Math.min(...cursors.keys()), 950);
  assert.equal(cursors.get(1001), 'cursor-1001');
  cursors = retainCursors(cursors, 980, 'new-981');
  assert.equal(cursors.get(981), 'new-981');
  assert.ok(!cursors.has(982));
});

test('header sorting preserves the applied query and pending draft while resetting keyset history', (t) => {
  const original = globalThis.document;
  t.after(() => {
    if (original === undefined) delete globalThis.document;
    else globalThis.document = original;
  });
  const controls = new Map();
  globalThis.document = {
    getElementById: (id) => {
      if (!controls.has(id)) controls.set(id, {});
      return controls.get(id);
    },
  };
  let sent;
  const applied = {
    dataset_id: 'generator_daily',
    search: '01999',
    sort: 'outage_mw',
    direction: 'asc',
    filters: [{ column: 'capacity_mw', op: 'gte', value: '0.5' }],
    model_id: model,
  };
  const controller = Object.assign(Object.create(ExploreController.prototype), {
    applied,
    dirty: true,
    working: false,
    page: 8,
    cursors: new Map([[8, 'old']]),
    markDirty: () => {
      controller.dirty = true;
    },
    send: (page) => {
      sent = page;
    },
  });
  controller.sortBy('outage_mw');
  assert.equal(controller.applied.search, '01999');
  assert.equal(controller.applied.filters, applied.filters);
  assert.equal(controller.applied.direction, 'desc');
  assert.equal(controller.dirty, true);
  assert.equal(sent, 0);
  assert.deepEqual([...controller.cursors], [[0, null]]);
});

test('Explore sends structured reads in POST bodies and retains captured applied context', async (t) => {
  const original = globalThis.document;
  t.after(() => {
    if (original === undefined) delete globalThis.document;
    else globalThis.document = original;
  });
  const nodes = new Map();
  const node = () => ({
    children: [],
    attributes: {},
    append(...items) {
      this.children.push(...items);
    },
    replaceChildren(...items) {
      this.children = items;
    },
    setAttribute(k, v) {
      this.attributes[k] = v;
    },
    querySelectorAll() {
      return [];
    },
  });
  globalThis.document = {
    getElementById: (id) => {
      if (!nodes.has(id)) nodes.set(id, node());
      return nodes.get(id);
    },
    createElement: () => node(),
  };
  const requests = [];
  const applied = {
    dataset_id: 'generator_daily',
    model_id: model,
    start: '',
    end: '',
    facility_id: '',
    generator_id: '',
    search: '01999',
    sort: 'outage_mw',
    direction: 'desc',
    filters: [{ column: 'capacity_mw', op: 'gte', value: '0.5' }],
    limit: 25,
  };
  const controller = Object.assign(Object.create(ExploreController.prototype), {
    applied,
    catalog: { model_id: model },
    sequence: 0,
    page: 0,
    cursors: new Map([[0, null]]),
    dirty: false,
    context: {
      alive: () => true,
      queue: { run: async (task) => task() },
      client: {
        request: async (path, options) => {
          requests.push({ path, options });
          controller.dirty = true;
          return {
            dataset_id: 'generator_daily',
            model_id: model,
            columns: [],
            rows: [],
            returned_rows: 0,
            next_cursor: null,
          };
        },
      },
    },
  });
  await controller.send(0);
  assert.equal(requests[0].path, '/data/generator_daily');
  assert.equal(requests[0].options.method, 'POST');
  assert.deepEqual(requests[0].options.body.filters, applied.filters);
  assert.equal(requests[0].options.body.search, '01999');
  assert.ok(!Object.hasOwn(requests[0].options.body, 'start'));
  assert.equal(controller.dirty, true);
  assert.match(document.getElementById('explore-applied-filters').textContent, /01999.*descending/);
});

test('local filtering uses literal canonical cells, excludes null, and preserves exact source values', () => {
  const packet = {
    rows: [
      ['001', '2026-09-01', '1000.000000001', true, null, 'O’Brien %_ Ω'],
      ['002', '2026-09-03', '1000.000000002', false, '', 'A.* literal'],
      ['003', '2026-09-04', '0.000000000', null, 'null', '<img src=x>'],
    ],
    returned_rows: 3,
    next_cursor: 'raw-signed-cursor',
  };
  const original = JSON.stringify(packet),
    index = loadedSearchIndex(packet);
  for (const [needle, positions] of [
    ['001', [0]],
    ['2026-09-03', [1]],
    ['1000.000000002', [1]],
    ['1,000', []],
    ['true', [0]],
    ['false', [1]],
    ['null', [2]],
    ['%_ ω', [0]],
    ['.*', [1]],
    ['<IMG', [2]],
    ['0012026', []],
    [' 2026 ', []],
  ]) {
    const result = filterLoadedPage(packet, needle, index);
    assert.deepEqual(result.positions, positions, needle);
    assert.equal(result.packet.next_cursor, 'raw-signed-cursor');
  }
  assert.equal(filterLoadedPage(packet, '', index).packet.rows.length, 3);
  assert.equal(JSON.stringify(packet), original);
});

function exploreHarness(t) {
  const previous = globalThis.document;
  t.after(() => {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  });
  const controls = new Map();
  const rawRows = [{ hidden: false }, { hidden: false }];
  const node = (id = '') => ({
    id,
    value: '',
    hidden: false,
    children: [],
    attributes: {},
    dataset: {},
    listeners: new Map(),
    addEventListener(name, fn) {
      this.listeners.set(name, fn);
    },
    emit(name, event = {}) {
      this.listeners.get(name)?.(event);
    },
    append(...items) {
      this.children.push(...items);
    },
    replaceChildren(...items) {
      this.children = items;
    },
    setAttribute(name, value) {
      this.attributes[name] = value;
    },
    querySelectorAll(selector) {
      return id === 'explore-table' && selector === 'tbody tr' ? rawRows : [];
    },
    focus() {
      document.activeElement = this;
    },
  });
  globalThis.document = {
    body: node('body'),
    activeElement: null,
    getElementById(id) {
      if (!controls.has(id)) controls.set(id, node(id));
      return controls.get(id);
    },
    createElement: () => node(),
  };
  const get = (id) => document.getElementById(id);
  get('explore-sort').value = 'period';
  get('explore-direction').value = 'asc';
  get('explore-page-size').value = '2';
  const calls = [];
  const dataset = {
    id: 'fleet_daily',
    primary_key: ['period'],
    schema: [
      ['period', 'DATE'],
      ['label', 'VARCHAR'],
      ['amount', 'DECIMAL(24,9)'],
    ],
  };
  const packet = {
    dataset_id: dataset.id,
    model_id: model,
    next_cursor: 'next-signed',
    columns: dataset.schema.map(([name, type]) => ({ name, type })),
    rows: [
      ['2026-09-01', 'Alpha', '0.100000001'],
      ['2026-09-03', 'Beta', '0.200000002'],
    ],
    returned_rows: 2,
  };
  const context = {
    alive: () => true,
    isView: () => true,
    queue: { run: async (fn) => fn() },
    client: {
      request: async (path, options) => {
        calls.push({ path, options });
        return packet;
      },
    },
  };
  const controller = new ExploreController(context);
  Object.assign(controller, {
    selected: dataset.id,
    catalog: { model_id: model, datasets: [dataset] },
    packet,
    visiblePacket: packet,
    searchIndex: loadedSearchIndex(packet),
    loaded: true,
    applied: {
      dataset_id: dataset.id,
      model_id: model,
      search: '',
      filters: [],
      sort: 'period',
      direction: 'asc',
      limit: 2,
    },
    cursors: new Map([
      [0, null],
      [1, 'next-signed'],
    ]),
  });
  return { controller, get, calls, packet, rawRows, context };
}

test('typing, paste, cut and plain Enter only filter loaded rows; local zero keeps raw Next and CSV scope', (t) => {
  const { controller, get, calls, packet, rawRows } = exploreHarness(t);
  get('explore-search').value = 'BETA';
  get('explore-search').emit('input', { inputType: 'insertFromPaste' });
  assert.equal(controller.packet, packet);
  assert.deepEqual(
    rawRows.map((row) => row.hidden),
    [true, false],
  );
  assert.equal(get('explore-result-meta').textContent, '1 of 2 loaded rows shown');
  assert.equal(controller.visiblePacket.rows[0][1], 'Beta');
  assert.match(csvText(controller.visiblePacket), /0\.200000002/);
  assert.doesNotMatch(csvText(controller.visiblePacket), /Alpha/);
  let prevented = false;
  get('explore-search').emit('keydown', {
    key: 'Enter',
    preventDefault: () => {
      prevented = true;
    },
  });
  assert.ok(prevented);
  get('explore-search').value = 'No loaded match';
  get('explore-search').emit('input', { inputType: 'insertText' });
  assert.equal(get('explore-local-empty').hidden, false);
  assert.equal(get('explore-empty').hidden, true);
  assert.equal(get('explore-next').disabled, false);
  assert.equal(get('explore-export').disabled, true);
  get('explore-local-reset').emit('click');
  assert.deepEqual(
    rawRows.map((row) => row.hidden),
    [false, false],
  );
  assert.equal(controller.visiblePacket.rows.length, 2);
  assert.equal(calls.length, 0);
});

test('pending page text and clauses never enter a Next request, and latest text filters its response', async (t) => {
  const { controller, get, calls, context } = exploreHarness(t);
  let release;
  context.client.request = (path, options) => {
    calls.push({ path, options });
    return new Promise((resolve) => {
      release = resolve;
    });
  };
  get('explore-search').value = 'Alpha';
  controller.conditions = [{ column: 'label', op: 'eq', value: 'UNAPPLIED' }];
  controller.markDirty();
  const pending = controller.send(1);
  assert.equal(calls.length, 1);
  assert.ok(!Object.hasOwn(calls[0].options.body, 'search'));
  assert.deepEqual(calls[0].options.body.filters, []);
  assert.equal(calls[0].options.body.cursor, 'next-signed');
  get('explore-search').value = 'LATEST';
  get('explore-search').emit('input');
  release({
    ...controller.context.response,
    dataset_id: 'fleet_daily',
    model_id: model,
    columns: [
      { name: 'period', type: 'DATE' },
      { name: 'label', type: 'VARCHAR' },
      { name: 'amount', type: 'DECIMAL(24,9)' },
    ],
    rows: [
      ['2026-09-04', 'Earlier Alpha', '0.100000000'],
      ['2026-09-05', 'Latest row', '0.200000000'],
    ],
    returned_rows: 2,
    next_cursor: null,
  });
  await pending;
  assert.equal(controller.visiblePacket.rows.length, 1);
  assert.equal(controller.visiblePacket.rows[0][1], 'Latest row');
  assert.equal(controller.applied.search, '');
  assert.equal(controller.dirty, true);
});

test('clearing page text or resetting the draft cannot silently clear an applied database search', (t) => {
  const { controller, get, calls, packet } = exploreHarness(t);
  controller.applied = { ...controller.applied, search: 'Alpha' };
  get('explore-search').value = 'Alpha';
  controller.renderLocal();
  get('explore-local-reset').emit('click');
  assert.equal(controller.visiblePacket.rows.length, packet.rows.length);
  assert.equal(controller.applied.search, 'Alpha');
  assert.equal(controller.dirty, true);
  controller.conditions = [{ column: 'amount', op: 'gte', value: '1' }];
  const cursors = controller.cursors;
  get('explore-reset').emit('click');
  assert.equal(controller.applied.search, 'Alpha');
  assert.equal(controller.cursors, cursors);
  assert.deepEqual(controller.conditions, []);
  assert.equal(calls.length, 0);
});

test('Apply preserves literal outer whitespace consistently with the database search contract', async (t) => {
  const { controller, get, calls } = exploreHarness(t);
  get('explore-search').value = ' 2026 ';
  get('explore-search').emit('input');
  assert.equal(controller.visiblePacket.rows.length, 0);
  controller.apply();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.body.search, ' 2026 ');
  assert.equal(controller.applied.search, ' 2026 ');
  assert.equal(controller.visiblePacket.rows.length, 0);
});

test('local Unicode lowercasing follows database scalar mappings rather than contextual JavaScript casing', () => {
  const packet = { rows: [['ΟΣ'], ['İ']], returned_rows: 2, next_cursor: null };
  assert.deepEqual(filterLoadedPage(packet, 'σ').positions, [0]);
  assert.deepEqual(filterLoadedPage(packet, 'ος').positions, []);
  assert.deepEqual(filterLoadedPage(packet, 'i').positions, [1]);
  assert.deepEqual(filterLoadedPage(packet, 'i\u0307').positions, []);
});
