'use strict';

const {
  test, expect, login, navigate, control, parseCSV, readDownload,
  expectNoHorizontalBodyOverflow,
} = require('./helpers.cjs');

const generator = /\/api\/v1\/data\/generator_daily(?:\?|$)/;
const isGenerator = response => generator.test(response.url()) && response.request().method() === 'POST';

async function generatorPage(page) {
  await login(page, 'analyst');
  await navigate(page, 'explore');
  const next = page.waitForResponse(isGenerator);
  await page.locator('#dataset-select').selectOption('generator_daily');
  const response = await next;
  expect(response.status()).toBe(200);
  const packet = await response.json();
  await expect(page.locator('#explore-apply')).toBeEnabled();
  await expect(page.locator('#explore-table tbody tr')).toHaveCount(packet.rows.length);
  return packet;
}

async function apply(page) {
  const next = page.waitForResponse(isGenerator);
  await page.locator('#explore-apply').click();
  const response = await next;
  expect(response.status()).toBe(200);
  const packet = await response.json();
  await expect(page.locator('#explore-apply')).toBeEnabled();
  return packet;
}

async function advanced(page) {
  if (!await page.locator('#explore-advanced').evaluate(details => details.open)) {
    await page.locator('#explore-advanced > summary').click();
  }
}

async function editCondition(page, index, column, op, value, upper) {
  await advanced(page);
  const row = page.locator(`[data-filter-index="${index}"]`);
  await row.locator('[data-filter-part="column"]').selectOption(column);
  await row.locator('[data-filter-part="op"]').selectOption(op);
  if (value !== undefined) await row.locator('[data-filter-part="value"]').fill(value);
  if (upper !== undefined) await row.locator('[data-filter-part="upper"]').fill(upper);
}

async function addCondition(page, index, column, op, value, upper) {
  await advanced(page);
  await page.locator('#explore-add-filter').click();
  await editCondition(page, index, column, op, value, upper);
}

function trackQueries(page) {
  const requests = [];
  page.on('request', request => {
    if (generator.test(request.url()) && request.method() === 'POST') requests.push(request.postDataJSON());
  });
  return requests;
}

async function localInputEvent(page, value, inputType) {
  // Exercise paste/cut input events without changing the operator's OS clipboard.
  await page.locator('#explore-search').evaluate((input, edit) => {
    input.value = edit.value;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: edit.inputType }));
  }, { value, inputType });
}

test('loaded-row filtering is immediate, makes no requests on typing or Enter, and exports only visible rows safely', async ({ page }) => {
  const packet = await generatorPage(page);
  expect(packet.rows).toHaveLength(25);
  await expect(page.getByRole('searchbox', { name: 'Filter loaded rows', exact: true })).toBeVisible();
  expect(await page.locator('#explore-search').evaluate(input => input.closest('form'))).toBeNull();
  expect(await page.locator('#explore-advanced').evaluate(details => details.open)).toBe(false);
  await expect(page.locator('#explore-apply')).toBeVisible();
  await expect(page.locator('#explore-filter-count')).toContainText('1 of 8');
  await expect(page.locator('#explore-table .column-type')).toHaveCount(0);
  for (const id of ['explore-start', 'explore-end', 'explore-facility', 'explore-generator']) await expect(page.locator(`#${id}`)).toHaveCount(0);
  const requests = trackQueries(page), firstPage = await page.locator('#explore-page-label').textContent();
  await page.locator('#explore-search').fill('=2+3');
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(20);
  await expect(page.locator('#explore-table tbody tr')).toHaveCount(25);
  await expect(page.locator('#explore-result-meta')).toContainText('20 of 25 loaded rows shown');
  await page.locator('#explore-search').press('Enter');
  await page.waitForTimeout(350); // Catch an accidental debounced submit, not a performance assertion.
  expect(requests).toHaveLength(0);
  await expect(page.locator('#explore-page-label')).toHaveText(firstPage);
  const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#explore-export').click()]);
  const csv = await readDownload(download), exported = parseCSV(csv);
  expect(csv.charCodeAt(0)).toBe(0xFEFF);
  expect(exported).toHaveLength(21);
  expect(exported.slice(1).every(row => row[exported[0].indexOf('facility_name')] === "'=2+3")).toBe(true);
  expect(exported.slice(1).every(row => row[exported[0].indexOf('facility_id')] === "'001")).toBe(true);
  await localInputEvent(page, '', 'deleteByCut');
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(25);
  await localInputEvent(page, '=2+3', 'insertFromPaste');
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(20);
  await page.locator('#explore-local-reset').click();
  await expect(page.locator('#explore-search')).toHaveValue('');
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(25);
  expect(requests).toHaveLength(0);
});

test('Apply finds rows beyond the loaded page while paging and keyboard sorting keep the applied query despite drafts', async ({ page }) => {
  control('large-explore');
  const initial = await generatorPage(page);
  await expect(page.locator('#dataset-row-count')).toHaveText('12,000');
  expect(initial.rows.every(row => row[2] !== '01999')).toBe(true);
  const requests = trackQueries(page);
  await page.locator('#explore-search').fill('01999');
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(0);
  await expect(page.locator('#explore-local-empty')).toBeVisible();
  await expect(page.locator('#explore-empty')).toBeHidden();
  await expect(page.locator('#explore-next')).toBeEnabled();
  await expect(page.locator('#explore-export')).toBeDisabled();
  expect(requests).toHaveLength(0);
  const found = await apply(page);
  expect(found.rows.filter(row => row[2] === '01999')).toHaveLength(6);
  expect(found.rows.every(row => row.some(value => value !== null && String(value).toLowerCase().includes('01999')))).toBe(true);
  expect(found.model_id).toBe(initial.model_id);
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(found.rows.length);
  await expect(page.locator('#explore-applied-filters')).toContainText('01999');
  await page.locator('#explore-local-reset').click();
  await expect(page.locator('#explore-search')).toHaveValue('');
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(found.rows.length);
  await expect(page.locator('#explore-draft-warning')).toBeVisible();
  await expect(page.locator('#explore-applied-filters')).toContainText('01999');
  await page.locator('#explore-search').fill('019');
  const many = await apply(page);
  expect(many.rows).toHaveLength(25); expect(many.next_cursor).toBeTruthy();
  const firstRow = await page.locator('#explore-table tbody tr:visible').first().textContent();
  await page.locator('#explore-search').fill('UNAPPLIED_NO_LOCAL_MATCH');
  await addCondition(page, 1, 'capacity_mw', 'gte', '0.9');
  await page.locator('#explore-sort').selectOption('capacity_mw');
  await page.locator('#explore-direction').selectOption('desc');
  await page.locator('#explore-page-size').selectOption('50');
  await expect(page.locator('#explore-next')).toBeEnabled();
  const next = page.waitForResponse(isGenerator);
  await page.locator('#explore-next').click();
  const second = await (await next).json();
  expect(second.query).toEqual(many.query);
  expect(requests.at(-1).search).toBe('019');
  expect(requests.at(-1).limit).toBe(25);
  expect(requests.at(-1).filters).toEqual(requests.at(-2).filters);
  await expect(page.locator('#explore-page-label')).toHaveText('Page 2');
  await expect(page.locator('#explore-local-empty')).toBeVisible();
  await expect(page.locator('#explore-next')).toBeEnabled();
  await page.locator('#explore-local-reset').click();
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(25);
  await page.locator('#explore-prev').click();
  await expect(page.locator('#explore-page-label')).toHaveText('Page 1');
  await expect(page.locator('#explore-table tbody tr:visible').first()).toHaveText(firstRow);
  const header = page.getByRole('button', { name: 'Sort current results by Offline (MW), ascending, across the dataset', exact: true });
  await header.focus();
  const sorting = page.waitForResponse(isGenerator);
  await header.press('Enter');
  const sorted = await (await sorting).json();
  expect(sorted.query.search).toBe('019');
  expect(sorted.query.filters).toEqual(many.query.filters);
  expect(sorted.query.order[0]).toEqual({ column: 'outage_mw', direction: 'asc' });
  expect(requests.at(-1).limit).toBe(25);
  await expect(page.locator('#explore-search')).toHaveValue('');
  await expect(page.locator('#explore-draft-warning')).toBeVisible();
  await expect(page.locator('#explore-table th[aria-sort="ascending"]')).toContainText('Offline (MW)');
  await expect(page.getByRole('button', { name: 'Sort current results by Offline (MW), descending, across the dataset', exact: true })).toBeFocused();
  await page.locator('#explore-search').fill('2026-09-03');
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(25);
  const descending = page.getByRole('button', { name: 'Sort current results by Offline (MW), descending, across the dataset', exact: true });
  await descending.focus();
  const reordered = page.waitForResponse(isGenerator);
  await descending.press('Enter');
  const hiddenPage = await (await reordered).json();
  expect(hiddenPage.query.search).toBe('019');
  expect(hiddenPage.query.order[0].direction).toBe('desc');
  await expect(page.locator('#explore-local-empty')).toBeVisible();
  await expect(page.locator('#explore-search')).toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoHorizontalBodyOverflow(page);
});

test('an in-flight Apply captures its immutable database plan and filters the new page with the latest local text', async ({ page }) => {
  await generatorPage(page);
  await addCondition(page, 1, 'facility_id', 'eq', '001');
  await page.locator('#explore-search').fill('01');
  let release, observed;
  const gate = new Promise(resolve => { release = resolve; });
  const incoming = new Promise(resolve => { observed = resolve; });
  let captured;
  await page.route(generator, async route => {
    captured = route.request().postDataJSON(); observed();
    const response = await route.fetch();
    await gate; await route.fulfill({ response });
  });
  const delivered = page.waitForResponse(isGenerator);
  try {
    await page.locator('#explore-apply').click(); await incoming;
    await page.locator('#explore-search').fill('LATEST_LOCAL_NO_MATCH');
    await page.locator('[data-filter-index="1"] [data-filter-part="value"]').fill('002');
    expect(captured.search).toBe('01');
    expect(captured.filters).toContainEqual({ column: 'facility_id', op: 'eq', value: '001' });
  } finally { release(); }
  const packet = await (await delivered).json();
  expect(packet.rows.length).toBeGreaterThan(0);
  await expect(page.locator('#explore-apply')).toBeEnabled();
  await expect(page.locator('#explore-search')).toHaveValue('LATEST_LOCAL_NO_MATCH');
  await expect(page.locator('#explore-local-empty')).toBeVisible();
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(0);
  await expect(page.locator('#explore-applied-filters')).toContainText('001');
  await expect(page.locator('#explore-draft-warning')).toBeVisible();
  await page.locator('#explore-local-reset').click();
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(packet.rows.length);
  expect(packet.rows.every(row => row[1] === '001')).toBe(true);
});

test('the unified builder preserves exact IDs and dates; Reset drafts does not fetch and server-empty differs from local-empty', async ({ page }) => {
  await generatorPage(page);
  await addCondition(page, 1, 'facility_id', 'eq', '001');
  await addCondition(page, 2, 'generator_id', 'eq', '01');
  const exact = await apply(page);
  expect(exact.rows).toHaveLength(3);
  expect(exact.rows.every(row => row[1] === '001' && row[2] === '01')).toBe(true);
  await editCondition(page, 0, 'period', 'between', '2026-09-01', '2026-09-03');
  const dates = await apply(page); expect(dates.rows).toHaveLength(2);
  const requests = trackQueries(page);
  await page.locator('#explore-search').fill('%_');
  await expect(page.locator('#explore-local-empty')).toBeVisible();
  await expect(page.locator('#explore-empty')).toBeHidden();
  await page.locator('#explore-reset').click(); expect(requests).toHaveLength(0);
  await expect(page.locator('#explore-search')).toHaveValue('');
  await expect(page.locator('#explore-filter-count')).toContainText('0 of 8');
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(2);
  await expect(page.locator('#explore-draft-warning')).toBeVisible();
  const all = await apply(page);
  expect(all.query.search).toBe(''); expect(all.query.filters).toEqual([]);
  expect(all.query.order[0]).toEqual({ column: 'period', direction: 'asc' });
  await addCondition(page, 0, 'outage_mw', 'is_null');
  await expect(page.locator('[data-filter-index="0"] [data-filter-part="value"]')).toHaveCount(0);
  const empty = await apply(page); expect(empty.rows).toHaveLength(0);
  await expect(page.locator('#explore-empty')).toBeVisible();
  await expect(page.locator('#explore-local-empty')).toBeHidden();
  await expect(page.locator('#explore-search')).toBeVisible();
  await expect(page.locator('#explore-export')).toBeDisabled();
  const count = requests.length;
  await page.locator('#explore-search').fill('anything');
  await expect(page.locator('#explore-empty')).toBeVisible(); expect(requests).toHaveLength(count);
});

test('local matching uses individual raw cells and renders literal Unicode markup-shaped fixture text safely', async ({ page }) => {
  const literal = 'Énergie \'50%_ Ω ΟΣ İ <img src=x onerror="window.__localFilterXss=1">';
  await page.route(generator, async route => {
    const response = await route.fetch(), packet = await response.json();
    packet.rows[0][packet.columns.findIndex(column => column.name === 'facility_name')] = literal;
    await route.fulfill({ response, json: packet });
  });
  const packet = await generatorPage(page), requests = trackQueries(page);
  const search = page.locator('#explore-search');
  await search.fill('2026-09-01'); await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(25);
  await search.fill('Sep 1, 2026'); await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(0);
  await search.fill(`${packet.rows[0][0]} ${packet.rows[0][1]}`);
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(0);
  await search.fill('éNERGIE \'50%_ ω'); await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(1);
  await search.fill('οσ i'); await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(1);
  await search.fill('i\u0307'); await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(0);
  await search.fill('éNERGIE \'50%_ ω'); await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(1);
  await expect(page.locator('#explore-table tbody tr:visible')).toContainText(literal);
  await expect(page.locator('#explore-table img')).toHaveCount(0);
  expect(await page.evaluate(() => window.__localFilterXss)).toBeUndefined();
  const [download] = await Promise.all([page.waitForEvent('download'), page.locator('#explore-export').click()]);
  const exported = parseCSV(await readDownload(download));
  expect(exported).toHaveLength(2); expect(exported[1][exported[0].indexOf('facility_name')]).toBe(literal);
  expect(requests).toHaveLength(0);
});

test('literal outer whitespace is preserved in both the local filter and the applied database search', async ({ page }) => {
  await generatorPage(page);
  const requests = trackQueries(page);
  await page.locator('#explore-search').fill(' 2026 ');
  await expect(page.locator('#explore-local-empty')).toBeVisible();
  await expect(page.locator('#explore-table tbody tr')).toHaveCount(25);
  expect(requests).toHaveLength(0);
  const packet = await apply(page);
  expect(requests).toHaveLength(1);
  expect(requests[0].search).toBe(' 2026 ');
  expect(packet.query.search).toBe(' 2026 ');
  expect(packet.rows).toHaveLength(0);
  await expect(page.locator('#explore-search')).toHaveValue(' 2026 ');
  await expect(page.locator('#explore-table tbody tr')).toHaveCount(0);
  await expect(page.locator('#explore-empty')).toBeVisible();
  await expect(page.locator('#explore-local-empty')).toBeHidden();
});
