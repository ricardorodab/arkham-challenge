import test from 'node:test';
import assert from 'node:assert/strict';
import { sqlExamples } from '../../src/outage_explorer/frontend/static/outage_explorer/sql_examples.js';
import { validateChart } from '../../src/outage_explorer/frontend/static/outage_explorer/query_chart.js';
import { collectRetainedResult } from '../../src/outage_explorer/frontend/static/outage_explorer/query_results.js';

function inLanguage(language, operation) {
  const previous = globalThis.document;
  globalThis.document = { documentElement: { lang: language } };
  try {
    return operation();
  } finally {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  }
}

test('query starters localize authored descriptions without changing SQL or dataset identifiers', () => {
  const dataset = {
    id: 'dim_date',
    schema: ['period', 'year', 'month', 'day'].map((name) => [name, 'INTEGER']),
  };
  const english = inLanguage('en', () => sqlExamples(dataset));
  const spanish = inLanguage('es', () => sqlExamples(dataset));
  assert.equal(spanish.length, 3);
  assert.equal(spanish[0].title, 'Explorar fechas observadas');
  assert.match(spanish[0].description, /^Consulte las fechas/);
  assert.deepEqual(
    spanish.map((item) => item.sql),
    english.map((item) => item.sql),
  );
  assert.deepEqual(
    spanish.map((item) => [item.id, item.level]),
    english.map((item) => [item.id, item.level]),
  );
  assert.match(spanish[0].sql, /FROM "dim_date"/);
});

test('Spanish chart warnings preserve original series text, identifiers and exact numeric scalars', () => {
  const packet = {
    columns: [
      { name: 'period', type: 'DATE' },
      { name: 'outage', type: 'DECIMAL(24,9)' },
      { name: 'facility_name', type: 'VARCHAR' },
    ],
    rows: [
      ['2026-01-01', '1234.567890123', 'Create user'],
      [null, '0.000000000', 'Create user'],
    ],
    result_rows: 2,
    returned_rows: 2,
    next_cursor: null,
    truncated: true,
  };
  const original = structuredClone(packet);
  const model = inLanguage('es', () =>
    validateChart(packet, {
      date_column: 'period',
      value_column: 'outage',
      series_column: 'facility_name',
    }),
  );
  assert.equal(model.series[0].name, '"Create user"');
  assert.equal(model.series[0].points[0].exact, '1234.567890123');
  assert.equal(model.config.series_column, 'facility_name');
  assert.match(model.warnings[0], /^Resultado limitado:/);
  assert.match(model.warnings[1], /^Se excluye del gráfico 1 fila/);
  assert.deepEqual(packet, original);
  inLanguage('es', () =>
    assert.throws(
      () => validateChart(packet, { date_column: 'facility_name', value_column: 'outage' }),
      (error) =>
        error.code === 'date_column_required' && /^Seleccione una columna DATE/.test(error.message),
    ),
  );
});

test('Spanish retained-result errors change only the public explanation', async () => {
  const previous = globalThis.document;
  globalThis.document = { documentElement: { lang: 'es' } };
  try {
    await assert.rejects(
      collectRetainedResult({ columns: [], rows: [], result_rows: 1001 }, async () => null),
      /^Error: Este resultado supera el límite de filas del gráfico\./,
    );
  } finally {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  }
});
