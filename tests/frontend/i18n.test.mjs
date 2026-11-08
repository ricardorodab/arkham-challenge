/** Locale presentation must never rewrite identifiers, user content or exact numeric values. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  t,
  getLanguage,
  getLocale,
} from '../../src/outage_explorer/frontend/static/outage_explorer/i18n.js';
import {
  columnTitle,
  datasetTitle,
  scalarLabel,
  dateLabel,
} from '../../src/outage_explorer/frontend/static/outage_explorer/dom.js';
import { schemaCompletions } from '../../src/outage_explorer/frontend/editor/sql_editor.js';

function language(value, check) {
  const previous = globalThis.document;
  globalThis.document = { documentElement: { lang: value } };
  try {
    check();
  } finally {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  }
}

test('interface language defaults to English and selects an explicit formatting locale', () => {
  language('en', () => {
    assert.equal(getLanguage(), 'en');
    assert.equal(getLocale(), 'en-US');
    assert.equal(t('Fleet overview'), 'Fleet overview');
  });
  language('es', () => {
    assert.equal(getLanguage(), 'es');
    assert.equal(getLocale(), 'es-ES');
    assert.equal(t('Fleet overview'), 'Resumen de flota');
  });
});

test('named translation values remain literal text and missing messages are stable', () => {
  language('es', () => {
    const name = '<img src=x onerror=alert(1)> $&';
    assert.equal(t('Insert column {name}', { name }), `Insertar columna ${name}`);
    assert.equal(t('constructor'), 'constructor');
    assert.equal(t('Custom SQL alias'), 'Custom SQL alias');
    assert.equal(t('Unknown {value}'), 'Unknown {value}');
  });
});

test('known dataset and column labels translate while custom aliases remain source text', () => {
  language('es', () => {
    assert.equal(datasetTitle('facility_daily'), 'Observaciones por central');
    assert.equal(columnTitle('facility_id'), 'ID de central');
    assert.equal(columnTitle('generator_count'), 'Número de generadores');
    assert.equal(columnTitle('my_custom_alias'), 'my custom alias');
  });
});

test('Spanish numeric formatting preserves every digit beyond floating-point precision', () => {
  language('es', () => {
    assert.equal(
      scalarLabel('900719925474099312345.123456789', 'DECIMAL(38,9)'),
      '900.719.925.474.099.312.345,123456789',
    );
    assert.equal(scalarLabel('00123', 'VARCHAR'), '00123');
    assert.equal(scalarLabel('Palisades', 'VARCHAR'), 'Palisades');
    assert.equal(scalarLabel(null, 'DECIMAL(24,9)'), '—');
    assert.equal(scalarLabel('0.000000000', 'DECIMAL(24,9)'), '0');
  });
});

test('localized observation dates retain their UTC calendar day', () => {
  language('es', () => {
    const label = dateLabel('2026-11-06');
    assert.match(label, /6.*nov.*2026/);
    assert.equal(dateLabel('not-a-date'), '—');
  });
});

test('SQL completion helpers translate prose without translating inserted identifiers', () => {
  language('es', () => {
    const choices = schemaCompletions({
      id: 'facility_daily',
      schema: [['facility_id', 'VARCHAR']],
    });
    assert.equal(choices[0].detail, 'Conjunto de datos seleccionado');
    assert.equal(choices[0].apply, '"facility_daily"');
    assert.equal(choices[1].label, 'facility_id');
    assert.equal(choices[1].apply, '"facility_id"');
  });
});
