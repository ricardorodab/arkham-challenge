import { element, columnTitle, numericType, scalarLabel } from './dom.js';

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

/** Quote all cells and neutralize spreadsheet formulas without coercing IDs. */
export function csvCell(value, type = 'VARCHAR') {
  let text = value === null || value === undefined ? '' : String(value);
  if (
    !numericType(type) &&
    (/^[\s\u0000-\u001f]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text) || /^0\d+$/.test(text))
  )
    text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}
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
