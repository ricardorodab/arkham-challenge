import test from 'node:test';
import assert from 'node:assert/strict';
import { StaleRequest } from '../../src/outage_explorer/frontend/static/outage_explorer/api.js';
import { collectRetainedResult } from '../../src/outage_explorer/frontend/static/outage_explorer/query_results.js';

const model = 'a'.repeat(32);
const packet = (rows, next_cursor = null, overrides = {}) => ({
  dataset_id: 'fleet_daily',
  model_id: model,
  columns: [
    { name: 'period', type: 'DATE' },
    { name: 'value', type: 'DECIMAL(24,9)' },
  ],
  rows,
  result_rows: 2,
  returned_rows: rows.length,
  next_cursor,
  truncated: true,
  ...overrides,
});

test('SQL charts collect all retained pages without changing exact values or hiding a cap', async () => {
  const first = packet([['2026-09-01', '0.000000001']], 'next');
  const result = await collectRetainedResult(first, async (cursor) => {
    assert.equal(cursor, 'next');
    return packet([['2026-09-02', '9007199254740993.123456789']]);
  });
  assert.equal(first.rows.length, 1);
  assert.equal(result.rows.length, 2);
  assert.equal(result.next_cursor, null);
  assert.equal(result.truncated, true);
  assert.equal(result.rows[1][1], '9007199254740993.123456789');
});

test('SQL chart collection rejects partial, changed, cyclic and stale results', async () => {
  await assert.rejects(
    collectRetainedResult(packet([['2026-09-01', '1']]), () => {}),
    /incomplete/,
  );
  await assert.rejects(
    collectRetainedResult(packet([['2026-09-01', '1']], 'next'), async () =>
      packet([['2026-09-02', '2']], null, { result_rows: 3 }),
    ),
    /changed/,
  );
  await assert.rejects(
    collectRetainedResult(packet([['2026-09-01', '1']], 'next', { result_rows: 3 }), async () =>
      packet([['2026-09-02', '2']], 'next', { result_rows: 3 }),
    ),
    /inconsistent/,
  );
  let current = true;
  await assert.rejects(
    collectRetainedResult(
      packet([['2026-09-01', '1']], 'next'),
      async () => {
        current = false;
        return packet([['2026-09-02', '2']]);
      },
      () => current,
    ),
    StaleRequest,
  );
});
