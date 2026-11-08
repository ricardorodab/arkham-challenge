/** Collect a complete bounded SQL result before presenting a time-series chart. */
import { t } from './i18n.js';
import { StaleRequest, verifyPage } from './api.js';

/**
 * Follow only server-issued cursors, keeping one query/model/schema and bounded memory.
 * The caller supplies its captured request and generation checks; rows are never persisted.
 */
export async function collectRetainedResult(first, loadNext, isCurrent = () => true) {
  const rows = [];
  const seen = new Set();
  const schema = JSON.stringify(first.columns);
  const total = first.result_rows;
  if (!Number.isInteger(total) || total < 0 || total > 1000)
    throw new Error(t('This result exceeds the supported chart row limit.'));
  let packet = first;
  let bytes = 0;
  for (let page = 0; page <= 1000; page++) {
    if (!isCurrent()) throw new StaleRequest();
    verifyPage(packet, first.dataset_id, first.model_id);
    if (JSON.stringify(packet.columns) !== schema || packet.result_rows !== total)
      throw new Error(t('The query result changed while loading its chart. Run it again.'));
    bytes += new TextEncoder().encode(JSON.stringify(packet)).length;
    if (bytes > 8 * 1024 * 1024 || rows.length + packet.rows.length > 1000)
      throw new Error(
        t('The retained result is too large to chart. Select fewer columns or rows.'),
      );
    rows.push(...packet.rows);
    const cursor = packet.next_cursor;
    if (!cursor) {
      if (rows.length !== total)
        throw new Error(t('The result is incomplete. Run the query again before charting.'));
      return { ...first, rows, returned_rows: rows.length, next_cursor: null };
    }
    if (!packet.rows.length || rows.length >= total || seen.has(cursor))
      throw new Error(t('The query returned an inconsistent continuation. Run it again.'));
    seen.add(cursor);
    packet = await loadNext(cursor);
  }
  throw new Error(t('The result has too many pages to chart.'));
}
