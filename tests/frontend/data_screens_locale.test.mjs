import assert from 'node:assert/strict';
import test from 'node:test';
import { normalizeConditions } from '../../src/outage_explorer/frontend/static/outage_explorer/explore.js';
import {
  refreshProgress,
  refreshTimes,
} from '../../src/outage_explorer/frontend/static/outage_explorer/refresh.js';
import { csvText } from '../../src/outage_explorer/frontend/static/outage_explorer/table.js';

/** Run a presentation sample with a document locale, restoring the surrounding environment. */
function inLanguage(language, callback) {
  const original = globalThis.document;
  globalThis.document = { documentElement: { lang: language } };
  try {
    return callback();
  } finally {
    if (original === undefined) delete globalThis.document;
    else globalThis.document = original;
  }
}

test('Spanish browse validation changes the explanation without changing exact values or wire keys', () => {
  inLanguage('es', () => {
    const conditions = normalizeConditions(
      [['capacity_mw', 'DECIMAL(24,9)']],
      [{ column: 'capacity_mw', op: 'gte', value: '900719925474099.123456789' }],
    );
    assert.deepEqual(conditions, [
      { column: 'capacity_mw', op: 'gte', value: '900719925474099.123456789' },
    ]);
    const localized = normalizeConditions(
      [['capacity_mw', 'DECIMAL(24,9)']],
      [{ column: 'capacity_mw', op: 'gte', value: '+900719925474099,123456789' }],
    );
    assert.equal(localized[0].value, '+900719925474099.123456789');
    for (const ambiguous of ['1.234,5', '1,234.5', '1,2,3'])
      assert.throws(() =>
        normalizeConditions(
          [['capacity_mw', 'DECIMAL(24,9)']],
          [{ column: 'capacity_mw', op: 'gte', value: ambiguous }],
        ),
      );
    assert.throws(() =>
      normalizeConditions(
        [['facility_id', 'INTEGER']],
        [{ column: 'facility_id', op: 'eq', value: '1,0' }],
      ),
    );
    assert.throws(
      () =>
        normalizeConditions([['capacity_mw', 'DECIMAL(24,9)']], [{ column: 'unknown', op: 'eq' }]),
      /^Error: Condición 1: .*columna.*condición/,
    );
  });
  inLanguage('en', () => {
    assert.throws(() =>
      normalizeConditions(
        [['capacity_mw', 'DECIMAL(24,9)']],
        [{ column: 'capacity_mw', op: 'gte', value: '123,5' }],
      ),
    );
  });
});

test('Spanish refresh presentation localizes stages and counters while retaining typed progress semantics', () => {
  inLanguage('es', () => {
    const detail = refreshProgress({
      status: 'running',
      progress: {
        stage: 'extracting',
        route: 'national',
        rows_received: 12345,
        pages_fetched: 2,
        routes: { facility: { rows_received: 0 } },
      },
    });
    assert.equal(detail.stageKey, 'extracting');
    assert.equal(detail.stage, 'Descargando observaciones — Nacional');
    assert.equal(detail.counts, '12.345 filas recibidas · 2 páginas descargadas');
    assert.deepEqual(detail.routes, ['Central: 0 filas recibidas']);
    assert.equal(detail.total, null);
    assert.equal(detail.exportLabel, '');
    const exporting = refreshProgress({
      status: 'running',
      progress: { stage: 'exporting_model', completed: 3, total: 10 },
    });
    assert.equal(exporting.exportLabel, 'Exportación del modelo: 30%');
    assert.equal(exporting.completed, 3);
    assert.equal(exporting.total, 10);
  });
  inLanguage('en', () => {
    assert.equal(
      refreshProgress({
        status: 'running',
        progress: { stage: 'exporting_model', completed: 3, total: 10 },
      }).exportLabel,
      'Model export 30%',
    );
  });
});

test('Spanish elapsed-time copy uses actual event times rather than a completion estimate', () => {
  inLanguage('es', () => {
    const now = Date.parse('2026-09-01T10:01:05Z');
    const timing = refreshTimes(
      {
        status: 'running',
        started_at: '2026-09-01T10:00:00Z',
        progress: { stage: 'extracting', updated_at: '2026-09-01T10:01:00Z' },
      },
      now,
    );
    assert.equal(timing.elapsed, '1m 5s');
    assert.equal(timing.updated, 'hace 5s');
    assert.equal(timing.updatedAt, '2026-09-01T10:01:00Z');
  });
});

test('Spanish presentation never translates source strings, SQL aliases or canonical CSV values', () => {
  inLanguage('es', () => {
    const exported = csvText({
      columns: [
        { name: 'Facility', type: 'VARCHAR' },
        { name: 'reported_outage_pct', type: 'DECIMAL(24,9)' },
      ],
      rows: [
        ['True', '100.000000000'],
        ['Condition', '0.000000000'],
      ],
    });
    assert.equal(
      exported,
      '\ufeff"Facility","reported_outage_pct"\r\n"True","100.000000000"\r\n"Condition","0.000000000"\r\n',
    );
  });
});
