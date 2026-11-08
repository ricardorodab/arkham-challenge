import test from 'node:test';
import assert from 'node:assert/strict';
import { EditorState } from '@codemirror/state';
import { CompletionContext } from '@codemirror/autocomplete';
import { sql } from '@codemirror/lang-sql';
import {
  schemaCompletions,
  sqlCompletionSource,
} from '../../src/outage_explorer/frontend/editor/sql_editor.js';

const dataset = {
  id: 'fleet_daily',
  schema: [
    ['period', 'DATE'],
    ['capacity_mw', 'DECIMAL(24,9)'],
  ],
};

function complete(text, selected = dataset, explicit = true) {
  const state = EditorState.create({ doc: text, extensions: [sql()] });
  return sqlCompletionSource(() => selected)(new CompletionContext(state, text.length, explicit));
}

test('editor suggestions contain only the selected schema and supported SQL vocabulary', () => {
  const suggestions = complete('').options;
  assert.ok(
    suggestions.some(
      (option) => option.label === 'capacity_mw' && option.apply === '"capacity_mw"',
    ),
  );
  assert.ok(suggestions.some((option) => option.label === 'SELECT'));
  assert.ok(suggestions.some((option) => option.label === 'COUNT'));
  for (const disallowed of [
    'JOIN',
    'INSERT',
    'DELETE',
    'WITH',
    'OVER',
    'NULLIF',
    'CASE',
    'DISTINCT',
  ]) {
    assert.equal(
      suggestions.some((option) => option.label === disallowed),
      false,
    );
  }
  assert.equal(
    suggestions.some((option) => option.label === 'generator_daily'),
    false,
  );
  assert.equal(
    complete('', null).options.some((option) => option.type === 'property'),
    false,
  );
});

test('FROM completion lists only the selected table and qualified references list only its fields', () => {
  assert.deepEqual(
    complete('SELECT * FROM ').options.map((option) => option.label),
    ['fleet_daily'],
  );
  assert.deepEqual(
    complete('SELECT f.').options.map((option) => option.label),
    ['period', 'capacity_mw'],
  );
  const quoted = complete('SELECT "cap');
  assert.equal(quoted.from, 7);
  assert.deepEqual(
    quoted.options.map((option) => option.label),
    ['capacity_mw'],
  );
  assert.equal(quoted.filter, false);
});

test('typing inside SQL strings or comments does not offer code completion', () => {
  for (const text of ["SELECT 'cap", 'SELECT 1 -- cap', 'SELECT /* cap']) {
    assert.equal(complete(text), null, text);
  }
  assert.equal(complete('SELECT ', dataset, false), null);
  assert.ok(complete('SELECT ca', dataset, false));
});

test('completion replaces the complete token when editing inside an existing name', () => {
  for (const text of ['SELECT capacity_mw', 'SELECT "capacity_mw"']) {
    const state = EditorState.create({ doc: text, extensions: [sql()] });
    const position = text.indexOf('capacity') + 3;
    const result = sqlCompletionSource(() => dataset)(new CompletionContext(state, position, true));
    assert.equal(result.from, 7);
    assert.equal(result.to, text.length);
  }
});

test('schema completion is copied and identifier insertion escapes double quotes', () => {
  const input = { id: 'safe', schema: [['a"b', 'VARCHAR']] };
  const options = schemaCompletions(input);
  input.schema[0][0] = 'mutated';
  assert.equal(options[1].label, 'a"b');
  assert.equal(options[1].apply, '"a""b"');
  assert.deepEqual(schemaCompletions({ id: 'safe' }), []);
});
