import test from 'node:test';
import assert from 'node:assert/strict';
import { StaleRequest } from '../../src/outage_explorer/frontend/static/outage_explorer/api.js';
import {
  collectSavedViewResult,
  SavedViewsController,
} from '../../src/outage_explorer/frontend/static/outage_explorer/saved_views.js';

const model = 'a'.repeat(32);
const view = {
  id: '891b9b11-0961-430e-8d78-17fb189216c0',
  revision: 3,
  dataset_id: 'fleet_daily',
  max_rows: 100,
};
const columns = [
  { name: 'period', type: 'DATE' },
  { name: 'offline_pct', type: 'DECIMAL(24,9)' },
];
const page = (rows, next = null, extra = {}) => ({
  view,
  result: {
    dataset_id: view.dataset_id,
    model_id: model,
    columns,
    rows,
    returned_rows: rows.length,
    result_rows: 2,
    next_cursor: next,
    truncated: false,
    ...extra,
  },
});

test('saved preview collects the whole retained result with immutable revision and exact scalar text', async () => {
  const calls = [];
  const responses = [
    page([['2026-09-01', '0.000000001']], 'continue', { truncated: true }),
    page([['2026-09-02', '9007199254740993.123456789']], null, { truncated: true }),
  ];
  const result = await collectSavedViewResult(
    async (body) => {
      calls.push(body);
      return responses.shift();
    },
    view,
    model,
  );
  assert.equal(calls.length, 2);
  assert.equal(calls[0].revision, view.revision);
  assert.equal(calls[0].model_id, model);
  assert.equal(calls[0].page_size, 100);
  assert.equal(calls[1].cursor, 'continue');
  assert.deepEqual(result.rows, [
    ['2026-09-01', '0.000000001'],
    ['2026-09-02', '9007199254740993.123456789'],
  ]);
  assert.equal(result.returned_rows, 2);
  assert.equal(result.next_cursor, null);
  assert.equal(result.truncated, true);
});

test('a changed saved revision cannot relabel an earlier execution', async () => {
  const response = page([['2026-09-01', '1']], null, { result_rows: 1 });
  response.view = { ...view, revision: 4 };
  await assert.rejects(
    collectSavedViewResult(async () => response, view, model),
    /changed/,
  );
});

test('saved preview rejects incomplete pages, repeated cursors and mixed output schemas', async () => {
  await assert.rejects(
    collectSavedViewResult(async () => page([['2026-09-01', '1']]), view, model),
    /complete retained result/,
  );
  let count = 0;
  await assert.rejects(
    collectSavedViewResult(
      async () => {
        count++;
        return page([[`2026-09-0${count}`, '1']], 'same');
      },
      view,
      model,
    ),
    /continuation/,
  );
  count = 0;
  await assert.rejects(
    collectSavedViewResult(
      async () => {
        count++;
        return count === 1
          ? page([['2026-09-01', '1']], 'next')
          : page([['2026-09-02', '2']], null, {
              columns: [columns[0], { name: 'different', type: 'DECIMAL(24,9)' }],
            });
      },
      view,
      model,
    ),
    /changed while/,
  );
});

test('retired workspace state cannot deliver a saved result or enqueue its continuation', async () => {
  let current = true;
  let calls = 0;
  await assert.rejects(
    collectSavedViewResult(
      async () => {
        calls++;
        current = false;
        return page([['2026-09-01', '1']], 'next');
      },
      view,
      model,
      () => current,
    ),
    StaleRequest,
  );
  assert.equal(calls, 1);
});

test('saved preview bounds total decoded bytes independently of the row count', async () => {
  const response = page([['2026-09-01', 'x'.repeat(8 * 1024 * 1024)]], null, {
    result_rows: 1,
    columns: [columns[0], { name: 'large_text', type: 'VARCHAR' }],
  });
  await assert.rejects(
    collectSavedViewResult(async () => response, view, model),
    /too large/,
  );
});

test('saved chart redraws only for a meaningful current visible width change without querying', () => {
  const chart = { isConnected: true };
  const state = {
    view: { ...view, chart: { enabled: true } },
    packet: { model_id: model },
    chartElement: chart,
    chartWidth: 720,
  };
  let alive = true;
  let overview = true;
  let requests = 0;
  let redraws = 0;
  const controller = Object.assign(Object.create(SavedViewsController.prototype), {
    disposed: false,
    context: {
      alive: () => alive,
      isView: () => overview,
      client: { request: () => requests++ },
    },
    catalog: { model_id: model },
    cards: new Map([[view.id, state]]),
    chartStates: new Map([[chart, state]]),
    drawChart: () => {
      redraws++;
      return true;
    },
  });
  const resize = (width) => controller.resizeCharts([{ target: chart, contentRect: { width } }]);
  resize(320);
  assert.equal(redraws, 1);
  assert.equal(state.chartWidth, 320);
  resize(320.5);
  resize(0);
  assert.equal(redraws, 1);
  overview = false;
  resize(500);
  overview = true;
  alive = false;
  resize(500);
  alive = true;
  state.packet.model_id = 'b'.repeat(32);
  resize(500);
  state.packet.model_id = model;
  chart.isConnected = false;
  resize(500);
  assert.equal(redraws, 1);
  assert.equal(requests, 0);
});

test('replacing or retiring saved previews disconnects their resize observation and stale callbacks', () => {
  const chart = { isConnected: true };
  const state = { view, chartElement: chart, chartWidth: 720 };
  const observed = [];
  let disconnected = 0;
  const controller = Object.assign(Object.create(SavedViewsController.prototype), {
    cards: new Map([[view.id, state]]),
    chartStates: new Map([[chart, state]]),
    chartObserver: {
      unobserve: (node) => observed.push(node),
      disconnect: () => disconnected++,
    },
  });
  controller.detachChart(state);
  assert.deepEqual(observed, [chart]);
  assert.equal(controller.chartStates.size, 0);
  assert.equal(state.chartElement, null);
  assert.equal(state.chartWidth, null);
  state.chartElement = chart;
  state.chartWidth = 320;
  controller.chartStates.set(chart, state);
  controller.clearCharts();
  assert.equal(disconnected, 1);
  assert.equal(controller.chartStates.size, 0);
  assert.equal(state.chartElement, null);
  assert.equal(state.chartWidth, null);
});
