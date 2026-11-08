'use strict';

const {
  test, expect, fixture, login, navigate, readDownload, parseCSV, sqlEditor, fillSql,
} = require('./helpers.cjs');

async function rowsReady(page, table, minimum = 1) {
  await expect.poll(() => page.locator(`${table} tbody tr`).count()).toBeGreaterThanOrEqual(minimum);
}

async function choosePageSize(page, selector, preferred = '25') {
  const values = await page.locator(`${selector} option`).evaluateAll(
    options => options.map(option => option.value),
  );
  const choice = values.includes(preferred) ? preferred : values.find(value => Number(value) < 60);
  expect(choice, 'The explorer needs a useful small page size').toBeTruthy();
  await page.locator(selector).selectOption(choice);
  return Number(choice);
}

test('US-08 latest metrics and table reflect actual observations; missing day is unavailable rather than zero', async ({ page }) => {
  await login(page, 'viewer');
  await expect(page.locator('#metric-offline-share')).toContainText(/20(?:\.0+)?\s*%/);
  await expect(page.locator('#metric-outage')).toContainText(/400/);
  await expect(page.locator('#metric-capacity')).toContainText(/2[ ,]?000/);
  await expect(page.locator('#fleet-chart')).toBeVisible();
  await expect(page.locator('#fleet-chart')).toHaveAccessibleName(/.+/);
  await rowsReady(page, '#overview-table', 3);
  expect(await page.locator('#overview-table tbody tr').count()).toBe(3);
  await expect(page.locator('#overview-table')).not.toContainText(fixture.missing_period);
  await page.locator('#overview-start').fill(fixture.missing_period);
  await page.locator('#overview-end').fill(fixture.missing_period);
  await page.locator('#overview-apply').click();
  await expect(page.locator('#overview-empty')).toBeVisible();
  await expect(page.locator('#overview-content')).toBeHidden();
  await expect(page.locator('#metric-offline-share:visible')).toHaveCount(0);
  await expect(page.locator('#overview-table tbody tr:visible')).toHaveCount(0);
});

test('explorer applies unified date/ID conditions and real seek pages; CSV contains only locally visible rows safely', async ({ page }) => {
  await login(page, 'analyst');
  await navigate(page, 'explore');
  await page.locator('#dataset-select').selectOption('generator_daily');
  await expect(page.locator('#explore-table tbody tr')).toHaveCount(25);
  await page.locator('#explore-advanced > summary').click();
  const size = await choosePageSize(page, '#explore-page-size');
  const dateCondition = page.locator('[data-filter-index="0"]');
  await dateCondition.locator('[data-filter-part="value"]').fill(fixture.periods[0]);
  await dateCondition.locator('[data-filter-part="upper"]').fill(fixture.periods.at(-1));
  await page.locator('#explore-apply').click();
  await rowsReady(page, '#explore-table', size);
  const initial = await page.locator('#explore-table tbody tr').first().textContent();
  const label = await page.locator('#explore-page-label').innerText();
  const [nextRequest] = await Promise.all([
    page.waitForRequest(request => {
      const url = new URL(request.url());
      return url.pathname === '/api/v1/data/generator_daily' && request.method() === 'POST' && !!request.postDataJSON()?.cursor;
    }),
    page.locator('#explore-next').click(),
  ]);
  expect([undefined, null, '']).toContain(nextRequest.postDataJSON().facility_id);
  await expect(page.locator('#explore-page-label')).not.toHaveText(label);
  await expect(page.locator('#explore-table tbody tr').first()).not.toHaveText(initial);
  await page.locator('#explore-prev').click();
  await expect(page.locator('#explore-table tbody tr').first()).toHaveText(initial);
  // Paging remains attached to applied conditions while the editor holds drafts.
  await page.locator('#explore-add-filter').click();
  const entity = page.locator('[data-filter-index="1"]');
  await entity.locator('[data-filter-part="column"]').selectOption('facility_id');
  await entity.locator('[data-filter-part="op"]').selectOption('eq');
  await entity.locator('[data-filter-part="value"]').fill('001');
  await expect(page.locator('#explore-next')).toBeEnabled();
  await page.locator('#explore-apply').click();
  await rowsReady(page, '#explore-table', size);
  await expect(page.locator('#explore-table')).toContainText('=2+3');
  await page.locator('#explore-search').fill(fixture.periods[0]);
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(20);
  const displayed = await page.locator('#explore-table tbody tr:visible').count();
  const [download] = await Promise.all([
    page.waitForEvent('download'), page.locator('#explore-export').click(),
  ]);
  const csv = await readDownload(download);
  expect(csv.charCodeAt(0)).toBe(0xFEFF);
  const parsed = parseCSV(csv);
  expect(parsed.length).toBe(displayed + 1);
  const facilityColumn = parsed[0].indexOf('facility_id');
  const nameColumn = parsed[0].indexOf('facility_name');
  expect(facilityColumn).toBeGreaterThanOrEqual(0);
  expect(nameColumn).toBeGreaterThanOrEqual(0);
  expect(parsed.slice(1).every(row => row[facilityColumn] === "'001")).toBe(true);
  expect(parsed.slice(1).every(row => row[nameColumn] === "'=2+3")).toBe(true);
});

test('SQL runs with Ctrl+Enter, supports column insertion, scoped history, literal text and read-only backend errors', async ({ page }) => {
  await login(page, 'analyst');
  const sqlNavigation = page.locator('a[data-view="sql"]');
  await sqlNavigation.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('#view-sql')).toBeVisible();
  await page.locator('#sql-dataset').selectOption('facility_daily');
  await page.locator('#sql-schema').getByRole('button', { name: 'Insert column facility_name', exact: true }).click();
  await expect(page.locator('#sql-editor')).toHaveValue(/facility_name/);
  const statement = 'SELECT period, facility_id, facility_name FROM facility_daily ORDER BY period, facility_id';
  await fillSql(page, statement);
  await sqlEditor(page).press('Control+Enter');
  await rowsReady(page, '#sql-table', 6);
  await expect(page.locator('#sql-table')).toContainText('<img src=x onerror="window.__browserXss=1">');
  await expect(page.locator('#sql-table img')).toHaveCount(0);
  expect(await page.evaluate(() => window.__browserXss)).toBeUndefined();
  const stored = await page.evaluate(() => Object.values(sessionStorage).join('\n'));
  expect(stored).toContain(statement);
  expect(stored).not.toContain('=2+3');
  await fillSql(page, 'DELETE FROM facility_daily');
  await page.locator('#sql-run').click();
  await expect(page.locator('#sql-error')).toBeVisible();
  await expect(page.locator('#sql-error')).toContainText(/SELECT|supported|read.only/i);
  await page.locator('#logout-button').click();
  await expect(page).toHaveURL(/\/login\/(?:\?.*)?$/);
  expect(await page.evaluate(() => Object.values(sessionStorage).join('\n'))).not.toContain(statement);
  await login(page, 'viewer');
  await navigate(page, 'sql');
  await expect(page.locator('#sql-history')).not.toContainText('facility_daily');
});

test('query results are capped and paginated, and CSV never exports off-page rows', async ({ page }) => {
  await login(page, 'analyst');
  await navigate(page, 'sql');
  await page.locator('#sql-dataset').selectOption('generator_daily');
  await fillSql(page, 'SELECT period, facility_id, generator_id FROM generator_daily ORDER BY period, facility_id, generator_id');
  await page.locator('#sql-max-rows').selectOption('100');
  if (await page.locator('#sql-page-size').evaluate(node => node.tagName) === 'SELECT') {
    await page.locator('#sql-page-size').selectOption('25');
  } else await page.locator('#sql-page-size').fill('25');
  await page.locator('#sql-run').click();
  await rowsReady(page, '#sql-table', 25);
  await expect(page.locator('#sql-table tbody tr')).toHaveCount(25);
  await expect(page.locator('#sql-truncated')).toBeVisible();
  const first = await page.locator('#sql-table tbody tr').first().textContent();
  await page.locator('#sql-next').click();
  await expect(page.locator('#sql-table tbody tr').first()).not.toHaveText(first);
  const [download] = await Promise.all([
    page.waitForEvent('download'), page.locator('#sql-export').click(),
  ]);
  expect(parseCSV(await readDownload(download)).length).toBe(26);
  await page.locator('#sql-prev').click();
  await expect(page.locator('#sql-table tbody tr').first()).toHaveText(first);
});

module.exports = { rowsReady };
