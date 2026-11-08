/**
 * @module explore
 * Keep immediate loaded-page filtering distinct from explicit database queries.
 * Drafts never alter applied cursor requests. Generation guards discard obsolete
 * work, and one raw packet plus its visible subset bound browser memory.
 */
import { StaleRequest, verifyPage } from './api.js';
import {
  byId,
  setText,
  visible,
  state,
  showError,
  element,
  datasetTitle,
  columnTitle,
  fillDatasets,
  schema,
  addDays,
  isoDate,
  numericType,
} from './dom.js';
import { renderTable, exportPage } from './table.js';

const NULL_OPS = ['is_null', 'not_null'];
const LABELS = {
  eq: 'Equals',
  ne: 'Does not equal',
  contains: 'Contains',
  not_contains: 'Does not contain',
  starts_with: 'Starts with',
  lt: 'Less than',
  lte: 'At most',
  gt: 'Greater than',
  gte: 'At least',
  between: 'Between (inclusive)',
  is_null: 'Is missing / null',
  not_null: 'Is not missing / null',
};

/** Classify catalog types without converting exact scalar values to Number. */
export function filterKind(type) {
  return type === 'DATE'
    ? 'date'
    : type === 'BOOLEAN'
      ? 'boolean'
      : numericType(type)
        ? 'number'
        : 'text';
}

/** Return only the structured operators supported for the selected column type. */
export function filterOperators(type) {
  const kind = filterKind(type);
  return [
    ...(kind === 'text'
      ? ['eq', 'ne', 'contains', 'not_contains', 'starts_with']
      : kind === 'boolean'
        ? ['eq', 'ne']
        : ['eq', 'ne', 'lt', 'lte', 'gt', 'gte', 'between']),
    ...NULL_OPS,
  ];
}

/** Compare decimal strings exactly, including values beyond JavaScript integer precision. */
export function compareDecimals(first, second) {
  const parts = (value) => {
    const negative = value.startsWith('-');
    const [integer, fraction = ''] = value.replace(/^[+-]/, '').split('.');
    const whole = integer.replace(/^0+/, '') || '0',
      tail = fraction.replace(/0+$/, '');
    return { sign: negative && (whole !== '0' || tail) ? -1 : 1, whole, tail };
  };
  const a = parts(first),
    b = parts(second);
  if (a.sign !== b.sign) return a.sign - b.sign;
  const width = Math.max(a.tail.length, b.tail.length);
  const magnitude =
    a.whole.length - b.whole.length ||
    a.whole.localeCompare(b.whole) ||
    a.tail.padEnd(width, '0').localeCompare(b.tail.padEnd(width, '0'));
  return a.sign * magnitude;
}

/** Validate one exact input against its declared scalar type; never round a user's value. */
function scalarInput(value, type) {
  if (typeof value !== 'string' || value.includes('\0') || [...value].length > 256)
    throw new Error('Use at most 256 characters and no null characters in a filter value.');
  const kind = filterKind(type);
  if (kind === 'date' && !isoDate(value)) throw new Error('Choose a valid calendar date.');
  if (kind === 'boolean' && !['true', 'false'].includes(value))
    throw new Error('Choose True or False.');
  if (kind === 'number') {
    const integer = /^(?:INTEGER|BIGINT)$/.test(type);
    if (!(integer ? /^[+-]?\d+$/ : /^[+-]?\d+(?:\.\d+)?$/).test(value))
      throw new Error(
        'Enter an exact number with digits before the decimal point; do not use exponent notation.',
      );
    if (integer) {
      if (value.length > 32) throw new Error('Use at most 32 characters for an integer.');
      const bits = type === 'INTEGER' ? 32n : 64n,
        number = BigInt(value);
      if (number < -(1n << (bits - 1n)) || number >= 1n << (bits - 1n))
        throw new Error('This integer is outside the column’s supported range.');
    } else {
      const match = type.match(/^DECIMAL\((\d+),(\d+)\)$/);
      if (match) {
        const [whole, fraction = ''] = value.replace(/^[+-]/, '').split('.');
        if (
          value.length > 128 ||
          whole.replace(/^0+/, '').length > Number(match[1]) - Number(match[2]) ||
          fraction.replace(/0+$/, '').length > Number(match[2])
        )
          throw new Error(`Enter a value that fits ${type} without rounding.`);
      }
    }
  }
  return value;
}

/** Normalize up to eight typed AND conditions from draft controls to strict wire objects. */
export function normalizeConditions(columns, drafts) {
  if (!Array.isArray(drafts) || drafts.length > 8)
    throw new Error('Use at most eight column conditions.');
  const types = new Map(columns);
  return drafts.map((draft, index) => {
    try {
      const type = types.get(draft.column);
      if (!type || !filterOperators(type).includes(draft.op))
        throw new Error('Choose a column and a supported condition.');
      const result = { column: draft.column, op: draft.op };
      if (!NULL_OPS.includes(draft.op)) result.value = scalarInput(draft.value, type);
      if (draft.op === 'between') {
        result.upper = scalarInput(draft.upper, type);
        if (
          (filterKind(type) === 'date'
            ? result.value.localeCompare(result.upper)
            : compareDecimals(result.value, result.upper)) > 0
        )
          throw new Error('The lower value must be at most the upper value.');
      }
      return result;
    } catch (error) {
      throw new Error(`Condition ${index + 1}: ${error.message}`);
    }
  });
}

/** Keep the current/next cursor and at most fifty prior page positions, without loading rows. */
export function retainCursors(previous, page, next) {
  const result = new Map(
    [...previous].filter(([position]) => position >= page - 50 && position <= page),
  );
  result.set(page + 1, next);
  return result;
}

/** Match database per-code-point lowercasing without locale or final-sigma rules. */
function searchLower(value) {
  const text = String(value);
  if (!/[^\x00-\x7f]/.test(text)) return text.toLowerCase();
  return Array.from(text, (character) =>
    String.fromCodePoint(character.toLowerCase().codePointAt(0)),
  ).join('');
}

/** Cache canonical cell text for one bounded loaded page, excluding nulls. */
export function loadedSearchIndex(packet) {
  return packet.rows.map((row) => row.filter((value) => value !== null).map(searchLower));
}

/** Filter one loaded packet only; never reinterpret its database continuation. */
export function filterLoadedPage(packet, search, index = loadedSearchIndex(packet)) {
  const needle = searchLower(search);
  const positions = [];
  const rows = packet.rows.filter((row, position) => {
    const matches = !needle || index[position].some((cell) => cell.includes(needle));
    if (matches) positions.push(position);
    return matches;
  });
  return { packet: { ...packet, rows, returned_rows: rows.length }, positions };
}

/** Keep draft, applied database query, raw page and locally visible rows independent. */
export class ExploreController {
  constructor(context) {
    this.context = context;
    this.sequence = 0;
    this.packet = this.visiblePacket = this.applied = this.catalog = null;
    this.searchIndex = [];
    this.conditions = [];
    this.cursors = new Map([[0, null]]);
    this.page = 0;
    this.loaded = this.working = this.dirty = false;
    byId('dataset-select').addEventListener('change', () => {
      this.selectDataset();
      this.apply();
    });
    byId('explore-form').addEventListener('submit', (event) => {
      event.preventDefault();
      this.apply();
    });
    byId('explore-reset').addEventListener('click', () => this.resetDraft());
    byId('explore-add-filter').addEventListener('click', () => this.addCondition());
    byId('explore-prev').addEventListener('click', () => this.send(this.page - 1));
    byId('explore-next').addEventListener('click', () => this.send(this.page + 1));
    byId('explore-export').addEventListener('click', () =>
      exportPage(this.visiblePacket, this.packet?.dataset_id, this.packet?.model_id),
    );
    byId('explore-search').addEventListener('input', () => {
      this.renderLocal();
      this.markDirty();
    });
    byId('explore-search').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') event.preventDefault();
    });
    byId('explore-local-reset').addEventListener('click', () => {
      byId('explore-search').value = '';
      this.renderLocal();
      this.markDirty();
      byId('explore-search').focus();
    });
    for (const id of ['explore-sort', 'explore-direction', 'explore-page-size'])
      byId(id).addEventListener('change', () => this.markDirty());
  }

  dataset() {
    return this.catalog?.datasets.find((dataset) => dataset.id === this.selected);
  }

  /** Explicit model adoption preserves draft intent without silently applying it. */
  configure(catalog) {
    const previous = this.catalog ? this.rawDraft() : null;
    const applied = this.applied;
    this.catalog = catalog;
    this.sequence++;
    const preferred =
      this.selected || (this.context.scope.persona === 'viewer' ? 'fleet_daily' : 'facility_daily');
    fillDatasets(byId('dataset-select'), catalog.datasets, preferred);
    this.selectDataset();
    if (previous?.dataset_id === this.selected) {
      this.conditions = previous.conditions.filter((item) =>
        this.dataset().schema.some(([name]) => name === item.column),
      );
      byId('explore-search').value = previous.search;
      byId('explore-sort').value = previous.sort;
      byId('explore-direction').value = previous.direction;
      byId('explore-page-size').value = previous.limit;
      this.renderConditions();
      if (applied?.dataset_id === this.selected)
        this.applied = { ...applied, model_id: catalog.model_id };
      this.markDirty();
    }
  }

  /** Dataset changes retire every old row/cursor and start a coherent default query. */
  selectDataset() {
    this.sequence++;
    this.packet = this.visiblePacket = this.applied = null;
    this.searchIndex = [];
    this.working = this.loaded = false;
    this.page = 0;
    this.cursors = new Map([[0, null]]);
    this.selected = byId('dataset-select').value;
    const dataset = this.dataset();
    if (!dataset) return;
    setText('dataset-description', dataset.description);
    setText('dataset-row-count', Number(dataset.rows).toLocaleString());
    setText(
      'dataset-grain',
      dataset.primary_key.map((name) => name.replaceAll('_id', '')).join(' + '),
    );
    schema(byId('schema-content'), dataset);
    const sort = byId('explore-sort');
    sort.replaceChildren();
    for (const [name] of dataset.schema) {
      const option = element('option', '', columnTitle(name));
      option.value = name;
      sort.append(option);
    }
    sort.value = dataset.primary_key[0];
    byId('explore-direction').value = 'asc';
    byId('explore-search').value = '';
    this.conditions = [];
    if (dataset.schema.some(([name]) => name === 'period') && dataset.max_period) {
      const end = dataset.max_period;
      const start =
        dataset.min_period && dataset.min_period > addDays(end, -29)
          ? dataset.min_period
          : addDays(end, -29);
      this.conditions.push({
        column: 'period',
        op: 'between',
        value: start,
        upper: end,
      });
    }
    this.renderConditions();
    byId('explore-advanced').open = false;
    byId('explore-table').replaceChildren();
    this.markDirty();
  }

  /** Reset edits and the local filter; applied database scope changes only on Apply. */
  resetDraft() {
    this.conditions = [];
    byId('explore-search').value = '';
    byId('explore-sort').value = this.dataset().primary_key[0];
    byId('explore-direction').value = 'asc';
    this.renderConditions();
    this.renderLocal();
    this.markDirty();
  }

  async activate() {
    if (!this.loaded && !this.working) {
      if (this.applied) this.send(0);
      else this.apply();
    }
  }

  /** Add an explicit native condition row without issuing an analytical request. */
  addCondition() {
    if (this.conditions.length >= 8 || !this.dataset()) return;
    this.conditions.push({
      column: (this.dataset().schema.find(
        ([name]) => !this.conditions.some((item) => item.column === name),
      ) || this.dataset().schema[0])[0],
      op: 'eq',
      value: '',
      upper: '',
    });
    byId('explore-advanced').open = true;
    this.renderConditions();
    byId('explore-filter-list').lastElementChild?.querySelector('select')?.focus();
    this.markDirty();
  }

  /** Render accessible typed inputs; column/operator edits preserve the focused select node. */
  renderConditions() {
    const list = byId('explore-filter-list');
    list.replaceChildren(element('legend', 'sr-only', 'Column conditions'));
    const columns = this.dataset()?.schema || [];
    this.conditions.forEach((draft, index) => {
      const row = element('div', 'column-filter-row');
      row.dataset.filterIndex = index;
      row.setAttribute('role', 'group');
      row.setAttribute('aria-label', `Condition ${index + 1}`);
      const field = (label, control) => {
        const wrapper = element('label', 'field');
        wrapper.append(element('span', '', label), control);
        return wrapper;
      };
      const column = element('select');
      column.dataset.filterPart = 'column';
      for (const [name] of columns) {
        const option = element('option', '', columnTitle(name));
        option.value = name;
        column.append(option);
      }
      column.value = draft.column;
      const operator = element('select');
      operator.dataset.filterPart = 'op';
      const values = element('div', 'condition-values');
      const type = () => columns.find(([name]) => name === draft.column)?.[1] || 'VARCHAR';
      const fillOperators = () => {
        operator.replaceChildren();
        for (const op of filterOperators(type())) {
          const option = element('option', '', LABELS[op]);
          option.value = op;
          operator.append(option);
        }
        operator.value = draft.op;
      };
      const fillValues = () => {
        values.replaceChildren();
        if (NULL_OPS.includes(draft.op)) {
          values.append(element('p', 'condition-hint', 'No value needed.'));
          return;
        }
        const input = (key, label) => {
          const kind = filterKind(type());
          let control;
          if (kind === 'boolean') {
            control = element('select');
            for (const [value, text] of [
              ['', 'Choose…'],
              ['true', 'True'],
              ['false', 'False'],
            ]) {
              const option = element('option', '', text);
              option.value = value;
              control.append(option);
            }
          } else {
            control = element('input');
            control.type = kind === 'date' ? 'date' : 'text';
            control.maxLength = 256;
            if (kind === 'number') {
              control.inputMode = 'decimal';
              control.placeholder = 'Exact number';
            }
            if (kind === 'text') control.placeholder = 'Literal text (empty allowed)';
          }
          control.dataset.filterPart = key;
          control.value = draft[key] || '';
          control.addEventListener('input', () => {
            draft[key] = control.value;
            this.markDirty();
          });
          values.append(field(label, control));
        };
        input('value', draft.op === 'between' ? 'From value' : 'Value');
        if (draft.op === 'between') input('upper', 'Through value');
      };
      column.addEventListener('change', () => {
        draft.column = column.value;
        draft.op = 'eq';
        draft.value = draft.upper = '';
        fillOperators();
        fillValues();
        this.markDirty();
      });
      operator.addEventListener('change', () => {
        draft.op = operator.value;
        fillValues();
        this.markDirty();
      });
      const remove = element('button', 'button button-quiet condition-remove', 'Remove');
      remove.type = 'button';
      remove.setAttribute('aria-label', `Remove condition ${index + 1}`);
      remove.addEventListener('click', () => {
        const index = this.conditions.indexOf(draft);
        if (index < 0) return;
        this.conditions.splice(index, 1);
        this.renderConditions();
        this.markDirty();
        byId('explore-add-filter').focus();
      });
      fillOperators();
      fillValues();
      row.append(
        field(`Column ${index + 1}`, column),
        field('Condition', operator),
        values,
        remove,
      );
      list.append(row);
    });
    byId('explore-add-filter').disabled = this.conditions.length >= 8;
    setText('explore-filter-count', `${this.conditions.length} of 8`);
  }

  /** Capture strings without changing the last applied request or loaded packet. */
  rawDraft() {
    return {
      dataset_id: this.selected,
      search: byId('explore-search').value,
      sort: byId('explore-sort').value,
      direction: byId('explore-direction').value,
      limit: byId('explore-page-size').value,
      conditions: this.conditions.map((item) => ({ ...item })),
    };
  }

  /** Distinguish real unapplied edits from typing back to the applied specification. */
  markDirty() {
    try {
      const draft = this.rawDraft();
      const current = {
        search: draft.search,
        sort: draft.sort,
        direction: draft.direction,
        limit: Number(draft.limit),
        filters: normalizeConditions(this.dataset().schema, this.conditions),
      };
      const previous = this.applied && {
        search: this.applied.search,
        sort: this.applied.sort,
        direction: this.applied.direction,
        limit: this.applied.limit,
        filters: this.applied.filters,
      };
      this.dirty = JSON.stringify(current) !== JSON.stringify(previous);
    } catch {
      this.dirty = true;
    }
    if (byId('explore-draft-warning').textContent !== 'Unapplied changes')
      setText('explore-draft-warning', 'Unapplied changes');
    this.updatePager();
  }

  /** Deliberately query the whole dataset using the current local text and draft clauses. */
  apply() {
    const dataset = this.dataset();
    if (!dataset) return;
    try {
      const draft = this.rawDraft();
      if ([...draft.search].length > 256 || draft.search.includes('\0'))
        throw new Error('Search accepts at most 256 characters and no null characters.');
      const applied = {
        dataset_id: dataset.id,
        search: draft.search,
        sort: draft.sort,
        direction: draft.direction,
        limit: Number(draft.limit),
        filters: normalizeConditions(dataset.schema, this.conditions),
        model_id: this.catalog.model_id,
      };
      if (
        !dataset.schema.some(([name]) => name === applied.sort) ||
        !['asc', 'desc'].includes(applied.direction) ||
        !Number.isInteger(applied.limit) ||
        applied.limit < 1 ||
        applied.limit > 1000
      )
        throw new Error('Choose a column, direction and page size from 1 to 1,000.');
      this.applied = applied;
      this.cursors = new Map([[0, null]]);
      this.page = 0;
      this.markDirty();
      this.send(0);
    } catch (error) {
      showError('explore-error', error);
      byId('explore-advanced').open = true;
    }
  }

  /** Header ordering changes only the applied database order, retaining all unrelated drafts. */
  sortBy(column) {
    if (!this.applied || this.working) return;
    const direction =
      this.applied.sort === column && this.applied.direction === 'asc' ? 'desc' : 'asc';
    const focused = document.activeElement;
    this.restoreSortFocus = focused?.closest?.('.table-sort') ? column : null;
    this.applied = { ...this.applied, sort: column, direction };
    byId('explore-sort').value = column;
    byId('explore-direction').value = direction;
    this.cursors = new Map([[0, null]]);
    this.page = 0;
    this.markDirty();
    this.send(0);
  }

  /** Summarize the actual database request, separately from page-local filtering. */
  appliedSummary(applied) {
    const parts = ['Applied query'];
    if (applied.search) parts.push(`Dataset search “${applied.search}”`);
    for (const item of applied.filters) {
      const value = item.value === undefined ? '' : ` ${item.value}`;
      const upper = item.upper === undefined ? '' : ` to ${item.upper}`;
      parts.push(`${columnTitle(item.column)} ${LABELS[item.op].toLowerCase()}${value}${upper}`);
    }
    parts.push(
      `${columnTitle(applied.sort)} ${applied.direction === 'desc' ? 'descending' : 'ascending'}`,
    );
    return parts.join(' · ');
  }

  /** Fetch only a retained seek page; draft text and clauses cannot alter this request. */
  async send(page) {
    if (!this.applied || !this.cursors.has(page) || (page > 0 && !this.cursors.get(page))) return;
    const applied = this.applied;
    const sequence = ++this.sequence;
    const current = () =>
      this.context.alive() &&
      sequence === this.sequence &&
      this.catalog?.model_id === applied.model_id;
    this.working = true;
    this.packet = this.visiblePacket = null;
    this.searchIndex = [];
    byId('explore-table').replaceChildren();
    state('explore', 'loading');
    visible('explore-content');
    visible('explore-local-empty', false);
    visible('explore-empty', false);
    visible('explore-table', false);
    setText('explore-applied-filters', 'Loading database results…');
    this.updatePager();
    const { dataset_id: datasetId, ...parameters } = applied;
    if (this.cursors.get(page)) parameters.cursor = this.cursors.get(page);
    try {
      const result = await this.context.queue.run(
        () =>
          this.context.client.request(`/data/${encodeURIComponent(datasetId)}`, {
            method: 'POST',
            body: Object.fromEntries(
              Object.entries(parameters).filter(
                ([, value]) => value !== '' && value !== null && value !== undefined,
              ),
            ),
          }),
        current,
      );
      if (!current()) return;
      this.packet = verifyPage(result, datasetId, applied.model_id);
      this.searchIndex = loadedSearchIndex(this.packet);
      this.page = page;
      this.loaded = true;
      this.cursors = retainCursors(this.cursors, page, result.next_cursor);
      renderTable(
        byId('explore-table'),
        result,
        `${datasetTitle(datasetId)} observations, database page ${page + 1}`,
        {
          sort: applied.sort,
          direction: applied.direction,
          showTypes: false,
          onSort: (column) => this.sortBy(column),
        },
      );
      state('explore', 'content');
      setText('explore-applied-filters', this.appliedSummary(applied));
      this.renderLocal();
    } catch (error) {
      if (!(error instanceof StaleRequest) && current()) {
        this.loaded = false;
        state('explore', 'error', error);
        visible('explore-content');
        setText('explore-applied-filters', 'Database results could not be loaded.');
        showError('explore-error', error, () => this.send(page));
      }
    } finally {
      if (current()) {
        this.working = false;
        this.markDirty();
        if (
          this.restoreSortFocus &&
          this.context.isView('explore') &&
          document.activeElement === document.body
        ) {
          const button = [...byId('explore-table').querySelectorAll('.table-sort')].find(
            (item) => item.dataset.sortColumn === this.restoreSortFocus,
          );
          if (this.visiblePacket?.rows.length) button?.focus({ preventScroll: true });
          else byId('explore-search').focus({ preventScroll: true });
        }
        this.restoreSortFocus = null;
      }
    }
  }

  /** Reuse one page's rows and search index; input never schedules analytical work. */
  renderLocal() {
    const text = byId('explore-search').value;
    if (!this.packet) {
      this.visiblePacket = null;
      this.updatePager();
      return;
    }
    const result = filterLoadedPage(this.packet, text, this.searchIndex);
    this.visiblePacket = result.packet;
    const visiblePositions = new Set(result.positions);
    byId('explore-table')
      .querySelectorAll('tbody tr')
      .forEach((row, index) => {
        row.hidden = !visiblePositions.has(index);
      });
    visible('explore-table', result.packet.rows.length > 0);
    const wrapper = byId('explore-table').parentElement;
    if (wrapper) visible(wrapper, result.packet.rows.length > 0);
    visible('explore-empty', this.packet.rows.length === 0);
    visible('explore-local-empty', this.packet.rows.length > 0 && result.packet.rows.length === 0);
    this.updatePager();
  }

  /** Pager state follows the raw server page, while counts and export follow visible rows. */
  updatePager() {
    byId('explore-apply').disabled = this.working;
    byId('explore-form').setAttribute('aria-busy', String(this.working));
    byId('explore-prev').disabled =
      this.working || !this.packet || this.page <= Math.min(...this.cursors.keys());
    byId('explore-next').disabled = this.working || !this.packet?.next_cursor;
    byId('explore-export').disabled = this.working || !this.visiblePacket?.rows.length;
    byId('explore-local-reset').disabled = !byId('explore-search').value;
    for (const button of byId('explore-table').querySelectorAll('.table-sort'))
      button.disabled = this.working;
    visible('explore-draft-warning', this.dirty && !!this.applied);
    const pageLabel = this.packet ? `Page ${this.page + 1}` : '—';
    if (byId('explore-page-label').textContent !== pageLabel)
      setText('explore-page-label', pageLabel);
    const resultMeta = this.packet
      ? `${this.visiblePacket?.rows.length ?? this.packet.rows.length} of ${this.packet.rows.length} loaded rows shown`
      : this.working
        ? 'Loading a database page…'
        : 'No database page loaded';
    if (byId('explore-result-meta').textContent !== resultMeta)
      setText('explore-result-meta', resultMeta);
  }

  dispose() {
    this.sequence++;
    this.packet = this.visiblePacket = this.applied = null;
    this.searchIndex = [];
    this.conditions = [];
    this.cursors.clear();
    byId('explore-search').value = '';
    byId('explore-table').replaceChildren();
    byId('explore-filter-list').replaceChildren();
  }
}
