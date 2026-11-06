/**
 * @module table
 * Render exact typed API pages and export only the displayed page.
 *
 * External values become text nodes. Numeric strings stay strings; CSV quotes
 * every cell and guards spreadsheet-shaped text without claiming control over
 * the receiving spreadsheet application's type inference.
 */
import { element, columnTitle, numericType, scalarLabel } from './dom.js';

/**
 * Replace a semantic table from a validated positional packet, using only text nodes.
 *
 * Exact scalar strings remain unchanged in cell titles; null is explicitly
 * undefined/unreported, never zero. Headers retain SQL types and column scope.
 */
export function renderTable(table, data, caption) {
  table.replaceChildren();
  table.append(element('caption', 'sr-only', caption));
  const head = document.createElement('thead'),
    heading = document.createElement('tr');
  for (const column of data.columns) {
    const cell = element('th', numericType(column.type) ? 'cell-number' : '');
    cell.scope = 'col';
    cell.append(
      element('span', 'column-name', columnTitle(column.name)),
      element('span', 'column-type', column.type),
    );
    heading.append(cell);
  }
  head.append(heading);
  table.append(head);
  const body = document.createElement('tbody');
  for (const values of data.rows) {
    const row = document.createElement('tr');
    values.forEach((value, index) => {
      const type = data.columns[index].type;
      const cell = element(
        'td',
        value === null ? 'cell-null' : numericType(type) ? 'cell-number' : '',
        scalarLabel(value, type),
      );
      if (value === null) cell.setAttribute('aria-label', 'Not reported or undefined');
      else cell.title = String(value);
      row.append(cell);
    });
    body.append(row);
  }
  table.append(body);
}

/**
 * Quote one CSV cell and guard formula-shaped or zero-padded text without numeric coercion.
 *
 * Typed numeric text is preserved. Receiving spreadsheet type inference remains
 * outside this exporter's contract.
 * @returns {string} Quoted CSV field with doubled embedded quotes.
 */
export function csvCell(value, type = 'VARCHAR') {
  let text = value === null || value === undefined ? '' : String(value);
  if (
    !numericType(type) &&
    (/^[\s\u0000-\u001f]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text) || /^0\d+$/.test(text))
  )
    text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}
/**
 * Serialize exactly the supplied page with a UTF-8 BOM, guarded headers and CRLF records.
 */
export function csvText(data) {
  return (
    '\ufeff' +
    [
      data.columns.map((column) => csvCell(column.name)).join(','),
      ...data.rows.map((row) =>
        row.map((value, index) => csvCell(value, data.columns[index].type)).join(','),
      ),
    ].join('\r\n') +
    '\r\n'
  );
}
/**
 * Download only the currently displayed packet with logical dataset/model context.
 *
 * No background request expands the export. Release the temporary object URL
 * after the download event has had time to consume it.
 */
export function exportPage(data, datasetId, modelId) {
  if (!data?.rows?.length) return;
  const file = new Blob([csvText(data)], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(file),
    link = document.createElement('a');
  link.href = url;
  link.download = `${datasetId}-${modelId.slice(0, 8)}-page.csv`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
