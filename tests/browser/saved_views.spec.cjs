'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const axe = require('axe-core');
const {
  test, expect, login, navigate, fillSql, control, expectNoHorizontalBodyOverflow,
} = require('./helpers.cjs');

const FLEET_SQL = 'SELECT period, capacity_mw, outage_mw, offline_pct\nFROM fleet_daily\nORDER BY period';

/** Send an authenticated fixture mutation using the application's normal CSRF bootstrap. */
async function mutate(page, method, endpoint, body) {
  const { csrf_token: token } = await (await page.request.get('/api/v1/auth/csrf')).json();
  return page.request.fetch(`/api/v1${endpoint}`, {
    method, data: body, headers: { 'X-CSRFToken': token, Origin: 'http://127.0.0.1:8766' },
  });
}

async function createView(page, overrides = {}) {
  await expect(page.locator('#metric-offline-share')).toContainText('%');
  const catalog = await (await page.request.get('/api/v1/catalog')).json();
  const definition = {
    name: 'Fleet trend', dataset_id: 'fleet_daily', sql: FLEET_SQL, max_rows: 100,
    model_id: catalog.model_id, chart: { enabled: false }, shared_roles: [], ...overrides,
  };
  const response = await mutate(page, 'POST', '/saved-views', definition);
  expect(response.ok(), `Fixture view creation returned ${response.status()}`).toBe(true);
  return (await response.json()).view;
}

async function runSql(page, statement = FLEET_SQL, dataset = 'fleet_daily', cap = '100') {
  await navigate(page, 'sql');
  await page.locator('#sql-dataset').selectOption(dataset);
  await page.locator('#sql-max-rows').selectOption(cap);
  await page.locator('#sql-page-size').selectOption('25');
  await fillSql(page, statement);
  const response = page.waitForResponse(item =>
    item.url().endsWith('/api/v1/query') && item.request().method() === 'POST',
  );
  await page.locator('#sql-run').click();
  const packet = await (await response).json();
  await expect(page.locator('#sql-content')).toBeVisible();
  await expect(page.locator('#sql-table tbody tr')).toHaveCount(Math.min(packet.returned_rows, 25));
  return packet;
}

async function showGraph(page, { date = 'period', value = 'outage_mw', series = '' } = {}) {
  const disclosure = page.locator('.sql-chart-panel');
  if (!await disclosure.evaluate(node => node.open)) await disclosure.locator('summary').click();
  await page.locator('#sql-chart-date').selectOption(date);
  await page.locator('#sql-chart-value').selectOption(value);
  await page.locator('#sql-chart-series').selectOption(series);
  await page.locator('#sql-chart-show').click();
}

async function saveDialog(page, name) {
  await page.locator('#sql-save-view').click();
  const dialog = page.locator('#saved-view-dialog');
  await expect(dialog).toBeVisible();
  await dialog.getByRole('textbox', { name: 'View name', exact: true }).fill(name);
  return dialog;
}

async function submitView(page, dialog, label = 'Save view', method = 'POST') {
  const incoming = page.waitForResponse(response =>
    /\/api\/v1\/saved-views(?:\/[a-f0-9-]+)?$/.test(response.url())
      && response.request().method() === method,
  );
  await dialog.getByRole('button', { name: label, exact: true }).click();
  const response = await incoming;
  expect(response.ok(), `Save returned ${response.status()}`).toBe(true);
  const { view } = await response.json();
  await expect(dialog).toBeHidden();
  return view;
}

function card(page, id) {
  return page.locator(`#saved-views-list [data-view-id="${id}"]`);
}

async function audit(page, testInfo, label) {
  await page.evaluate(axe.source);
  const result = await page.evaluate(async () => window.axe.run(document, {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] },
  }));
  await testInfo.attach(`axe-${label}`, {
    body: JSON.stringify(result, null, 2), contentType: 'application/json',
  });
  expect(result.violations.map(item => ({
    id: item.id, nodes: item.nodes.map(node => ({ target: node.target, summary: node.failureSummary })),
  }))).toEqual([]);
}

async function preview(page, testInfo, filename, selector) {
  const directory = process.env.OUTAGE_SQL_PREVIEW_DIR;
  const destination = directory ? path.join(path.resolve(directory), filename) : testInfo.outputPath(filename);
  await fs.mkdir(path.dirname(destination), { recursive: true });
  if (await page.locator('#view-sql').isVisible())
    await page.getByRole('textbox', { name: 'SQL query', exact: true }).press('Escape');
  await page.evaluate(() => document.activeElement?.blur());
  await page.evaluate(() => window.scrollTo(0, 0));
  if (selector === '#custom-views-section' || page.viewportSize().width < 600)
    await page.screenshot({ path: destination, fullPage: true });
  else await page.locator(selector).screenshot({ path: destination });
}

/** Check rendered text size, rather than unscaled SVG font attributes, after responsive redraw. */
async function readableChart(container) {
  const graph = container.locator('svg[role="img"]');
  await expect(graph).toBeVisible();
  await expect.poll(async () => graph.evaluate(svg => {
    const width = svg.getBoundingClientRect().width;
    const bound = Math.max(280, Math.min(1100, svg.closest('.query-time-chart, .saved-view-chart')?.clientWidth || width));
    const box = svg.viewBox.baseVal;
    return Math.abs(box.width - bound);
  }), { message: 'The chart viewBox should redraw for the current container width' }).toBeLessThan(1);
  const sizes = await graph.locator('text').evaluateAll(nodes => nodes.map(node => {
    const svg = node.ownerSVGElement;
    return Number.parseFloat(getComputedStyle(node).fontSize)
      * svg.getBoundingClientRect().width / svg.viewBox.baseVal.width;
  }));
  expect(sizes.length, 'The time graph should contain axis labels').toBeGreaterThan(0);
  for (const size of sizes) {
    expect(Number.isFinite(size)).toBe(true);
    expect(size + 0.01, 'Rendered axis labels should remain at least 11 CSS pixels').toBeGreaterThanOrEqual(11);
  }
}

test('SQL graph becomes a persistent private Overview view with accessible editing, sharing, revisions and explicit deletion', async ({ page }, testInfo) => {
  const executions = [];
  page.on('request', request => {
    if (/\/api\/v1\/(?:query|saved-views\/[a-f0-9-]+\/run)$/.test(request.url())) executions.push(request);
  });
  await login(page, 'analyst');
  await runSql(page);
  await showGraph(page);
  await expect(page.locator('#sql-query-chart svg[role="img"] circle')).toHaveCount(3);
  await readableChart(page.locator('#sql-query-chart'));
  await preview(page, testInfo, 'sql-saved-chart.png', '#view-sql');
  const sqlExecutions = executions.length;
  await page.setViewportSize({ width: 390, height: 844 });
  await readableChart(page.locator('#sql-query-chart'));
  expect(executions.length).toBe(sqlExecutions);
  await expectNoHorizontalBodyOverflow(page);
  await preview(page, testInfo, 'sql-saved-chart-mobile.png', '#view-sql');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await readableChart(page.locator('#sql-query-chart'));
  const dialog = await saveDialog(page, 'Fleet outage trend');
  await expect(dialog.getByRole('checkbox', { name: 'Include a time-series graph' })).toBeChecked();
  await expect(dialog.getByRole('checkbox', { name: 'Viewers', exact: true })).not.toBeChecked();
  await audit(page, testInfo, 'saved-view-dialog-desktop');
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoHorizontalBodyOverflow(page);
  await audit(page, testInfo, 'saved-view-dialog-mobile');
  await page.setViewportSize({ width: 1440, height: 1000 });
  const created = await submitView(page, dialog);
  expect(created.revision).toBe(1);
  expect(created.shared_roles).toEqual([]);
  expect(created.chart.enabled).toBe(true);
  await navigate(page, 'overview');
  let view = card(page, created.id);
  await expect(view).toContainText('Fleet outage trend');
  await expect(view.locator('table')).toHaveCount(0);
  await view.getByRole('button', { name: 'Load view', exact: true }).click();
  await expect(view.locator('table tbody tr')).toHaveCount(3);
  await expect(view.locator('svg[role="img"] circle')).toHaveCount(3);
  await readableChart(view.locator('.saved-view-chart'));
  await audit(page, testInfo, 'saved-overview-desktop');
  await preview(page, testInfo, 'saved-view-overview.png', '#custom-views-section');
  const savedExecutions = executions.length;
  await page.setViewportSize({ width: 390, height: 844 });
  await readableChart(view.locator('.saved-view-chart'));
  expect(executions.length).toBe(savedExecutions);
  await expectNoHorizontalBodyOverflow(page);
  await audit(page, testInfo, 'saved-overview-mobile');
  await preview(page, testInfo, 'saved-view-overview-mobile.png', '#custom-views-section');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.reload();
  view = card(page, created.id);
  await expect(view).toContainText('Fleet outage trend');
  await expect(view.locator('table')).toHaveCount(0);
  await view.getByRole('button', { name: 'Edit view', exact: true }).click();
  const edit = page.locator('#saved-view-dialog');
  await edit.getByRole('textbox', { name: 'View name', exact: true }).fill('Fleet trend for the team');
  await edit.getByRole('checkbox', { name: 'Viewers', exact: true }).check();
  const updated = await submitView(page, edit, 'Save changes', 'PUT');
  expect(updated.revision).toBe(2);
  expect(updated.shared_roles).toContain('viewer');
  await expect(card(page, created.id)).toContainText('Fleet trend for the team');
  let executed = 0;
  page.on('request', request => { if (request.url().endsWith('/api/v1/query')) executed++; });
  await card(page, created.id).getByRole('button', { name: 'Open in SQL', exact: true }).click();
  await expect(page.locator('#view-sql')).toBeVisible();
  await expect(page.locator('#sql-editor')).toHaveValue(FLEET_SQL);
  expect(executed).toBe(0);
  await fillSql(page, `${FLEET_SQL}\nLIMIT 2`);
  await page.locator('#sql-run').click();
  await expect(page.locator('#sql-table tbody tr')).toHaveCount(2);
  await expect(page.locator('#sql-save-view')).toHaveText('Update saved view');
  await page.locator('#sql-save-view').click();
  const revised = await submitView(page, page.locator('#saved-view-dialog'), 'Save changes', 'PUT');
  expect(revised.revision).toBe(3);
  expect(revised.sql).toBe(`${FLEET_SQL}\nLIMIT 2`);
  await navigate(page, 'overview');
  await card(page, created.id).getByRole('button', { name: 'Delete', exact: true }).click();
  await card(page, created.id).getByRole('button', { name: 'Keep view', exact: true }).click();
  await expect(card(page, created.id)).toBeVisible();
  await card(page, created.id).getByRole('button', { name: 'Delete', exact: true }).click();
  const deleted = page.waitForRequest(request =>
    request.url().endsWith(`/saved-views/${created.id}`) && request.method() === 'DELETE',
  );
  await card(page, created.id).getByRole('button', { name: 'Delete saved view', exact: true }).click();
  expect((await deleted).postDataJSON().revision).toBe(3);
  await expect(card(page, created.id)).toHaveCount(0);
});

test('shared views preserve dataset permissions and ownership for Viewer and non-owner Admin', async ({ page }) => {
  await login(page, 'analyst');
  const shared = await createView(page, { name: 'Shared fleet', shared_roles: ['viewer', 'admin'] });
  await createView(page, { name: 'Private owner sentinel' });
  await createView(page, {
    name: 'Detail audience sentinel', dataset_id: 'facility_daily',
    sql: 'SELECT period, outage_mw AS restricted_detail_measure FROM facility_daily ORDER BY period, facility_id',
    shared_roles: ['analyst', 'admin'],
  });
  await page.locator('#logout-button').click();
  await login(page, 'admin');
  await expect(card(page, shared.id)).toBeVisible();
  await expect(card(page, shared.id).getByRole('button', { name: 'Edit view', exact: true })).toHaveCount(0);
  await expect(card(page, shared.id).getByRole('button', { name: 'Delete', exact: true })).toHaveCount(0);
  expect((await mutate(page, 'DELETE', `/saved-views/${shared.id}`, { revision: shared.revision })).status()).toBe(404);
  await page.locator('#logout-button').click();
  await login(page, 'viewer');
  await expect(page.locator('#saved-views-list [data-view-id]')).toHaveCount(1);
  const visible = card(page, shared.id);
  await visible.getByRole('button', { name: 'Load view', exact: true }).click();
  await expect(visible.locator('table tbody tr')).toHaveCount(3);
  await expect(visible.getByRole('button', { name: /Edit view|Delete|Open in SQL/ })).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('Private owner sentinel');
  await expect(page.locator('body')).not.toContainText('Detail audience sentinel');
  await expect(page.locator('body')).not.toContainText('restricted_detail_measure');
  const listing = await (await page.request.get('/api/v1/saved-views')).json();
  expect(listing.can_create).toBe(false);
  expect(listing.views.map(view => view.id)).toEqual([shared.id]);
  await runSql(page);
  await expect(page.locator('#sql-save-view')).toBeHidden();
  await expect(page.locator('#sql-save-view')).toBeDisabled();
  const catalog = await (await page.request.get('/api/v1/catalog')).json();
  expect((await mutate(page, 'POST', '/saved-views', {
    name: 'Viewer cannot author', dataset_id: 'fleet_daily', sql: FLEET_SQL,
    max_rows: 100, model_id: catalog.model_id, chart: { enabled: false }, shared_roles: [],
  })).status()).toBe(403);
});

test('charts collect every retained page, disclose caps and reject duplicate times while table-only outputs remain savable', async ({ page }) => {
  await login(page, 'analyst');
  await runSql(page, 'SELECT period, outage_mw FROM generator_daily ORDER BY period, facility_id, generator_id', 'generator_daily');
  await showGraph(page);
  await expect(page.locator('#sql-chart-status')).toContainText(/More than one row.*same time/);
  await expect(page.locator('#sql-query-chart svg')).toHaveCount(0);
  await expect(page.locator('#sql-save-view')).toBeEnabled();
  // Synthetic offsets create distinct fixture times while retaining two facility series.
  const sql = 'SELECT period + CAST(generator_id AS INTEGER) * 10 AS observation_date, facility_id, outage_mw, capacity_mw\nFROM generator_daily\nORDER BY observation_date, facility_id';
  const packet = await runSql(page, sql, 'generator_daily');
  expect(packet.result_rows).toBe(100);
  expect(packet.truncated).toBe(true);
  const continuations = [];
  page.on('request', request => {
    if (request.url().endsWith('/api/v1/query') && request.postDataJSON()?.cursor) continuations.push(request);
  });
  await showGraph(page, { date: 'observation_date', value: 'capacity_mw', series: 'facility_id' });
  await expect(page.locator('#sql-query-chart svg[role="img"] circle')).toHaveCount(100);
  await readableChart(page.locator('#sql-query-chart'));
  expect(continuations).toHaveLength(3);
  await page.setViewportSize({ width: 390, height: 844 });
  await readableChart(page.locator('#sql-query-chart'));
  expect(continuations).toHaveLength(3);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await readableChart(page.locator('#sql-query-chart'));
  await expect(page.locator('#sql-query-chart')).toContainText(/Capped query result.*100 retained rows/);
  await expect(page.locator('#sql-table tbody tr')).toHaveCount(25);
  await page.locator('#sql-next').click();
  await expect(page.locator('#sql-page-label')).toContainText('Page 2');
  await page.locator('#sql-prev').click();
  await expect(page.locator('#sql-page-label')).toContainText('Page 1');
  await expect(page.locator('#sql-chart-value')).toHaveValue('capacity_mw');
  const chartDialog = await saveDialog(page, 'Capacity across retained dates');
  await expect(chartDialog.getByLabel('Numeric value column', { exact: true })).toHaveValue('capacity_mw');
  const chartView = await submitView(page, chartDialog);
  expect(chartView.chart.value_column).toBe('capacity_mw');
  await runSql(page, 'SELECT facility_id, generator_id FROM dim_generator ORDER BY facility_id, generator_id', 'dim_generator');
  await expect(page.locator('#sql-chart-show')).toBeDisabled();
  const dialog = await saveDialog(page, 'Generator directory');
  await expect(dialog.getByRole('checkbox', { name: 'Include a time-series graph' })).not.toBeChecked();
  await expect(dialog.getByRole('checkbox', { name: 'Viewers', exact: true })).toBeDisabled();
  const view = await submitView(page, dialog);
  expect(view.chart.enabled).toBe(false);
  await navigate(page, 'overview');
  await card(page, view.id).getByRole('button', { name: 'Load view', exact: true }).click();
  await expect(card(page, view.id).locator('table tbody tr')).toHaveCount(25);
  await expect(card(page, view.id).locator('svg[role="img"]')).toHaveCount(0);
  await navigate(page, 'sql');
  await page.locator('#sql-dataset').selectOption('fleet_daily');
  await fillSql(page, 'SELECT period, outage_mw FROM fleet_daily WHERE outage_mw < 0');
  await page.locator('#sql-run').click();
  await expect(page.locator('#sql-empty')).toBeVisible();
  await expect(page.locator('#sql-save-view')).toBeVisible();
  await expect(page.locator('#sql-save-view')).toBeEnabled();
  const empty = await submitView(page, await saveDialog(page, 'No negative outages'));
  await navigate(page, 'overview');
  await card(page, empty.id).getByRole('button', { name: 'Load view', exact: true }).click();
  await expect(card(page, empty.id)).toContainText('This query returned no rows on the active publication.');
});

test('saved names render as text, revisions detect concurrent edits, and restricted SQL cannot be saved', async ({ page }) => {
  await login(page, 'analyst');
  await runSql(page);
  const name = '<img src=x onerror="window.savedViewXss=1">';
  const created = await submitView(page, await saveDialog(page, name));
  await navigate(page, 'overview');
  // The out-of-band writer models a second tab after the foreground analytic
  // request releases this account's admission slot; the stale editor stays open.
  await expect(page.locator('#overview-content')).toBeVisible();
  await expect(card(page, created.id)).toContainText(name);
  await expect(card(page, created.id).locator('img')).toHaveCount(0);
  expect(await page.evaluate(() => window.savedViewXss)).toBeUndefined();
  await card(page, created.id).getByRole('button', { name: 'Edit view', exact: true }).click();
  const catalog = await (await page.request.get('/api/v1/catalog')).json();
  const definition = {
    dataset_id: created.dataset_id, sql: created.sql, max_rows: created.max_rows,
    chart: created.chart, shared_roles: created.shared_roles,
    model_id: catalog.model_id, name: 'Changed in another tab',
    revision: created.revision,
  };
  const changed = await mutate(page, 'PUT', `/saved-views/${created.id}`, definition);
  expect(changed.ok()).toBe(true);
  const dialog = page.locator('#saved-view-dialog');
  await dialog.getByRole('textbox', { name: 'View name', exact: true }).fill('Stale overwrite');
  await dialog.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText(/changed|reload/i);
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  const forbidden = await mutate(page, 'POST', '/saved-views', {
    name: 'Unsupported mutation', dataset_id: 'fleet_daily', model_id: catalog.model_id,
    sql: 'DELETE FROM fleet_daily', max_rows: 100, chart: { enabled: false }, shared_roles: [],
  });
  expect(forbidden.status()).toBe(422);
});

test('publication adoption invalidates previews and late saved results cannot survive session logout', async ({ page }) => {
  await login(page, 'analyst');
  const created = await createView(page);
  await page.locator('#saved-views-refresh').click();
  let view = card(page, created.id);
  await view.getByRole('button', { name: 'Load view', exact: true }).click();
  await expect(view.locator('table tbody tr')).toHaveCount(3);
  const before = await page.locator('#status-version').innerText();
  const publication = JSON.parse(control('new-publication'));
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.locator('#apply-snapshot')).toBeVisible();
  await page.locator('#apply-snapshot').click();
  await expect(page.locator('#status-version')).not.toHaveText(before);
  view = card(page, created.id);
  await expect(view).toBeVisible();
  await expect(view.locator('table')).toHaveCount(0);
  const loaded = page.waitForResponse(response => response.url().endsWith(`/saved-views/${created.id}/run`));
  await view.getByRole('button', { name: 'Load view', exact: true }).click();
  expect((await (await loaded).json()).result.model_id).toBe(publication.model_id);
  await expect(view.locator('table tbody tr')).toHaveCount(3);
  await expect(view.locator('table tbody tr').last()).toContainText('600');
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let incoming;
  const requested = new Promise(resolve => { incoming = resolve; });
  await page.route(`**/saved-views/${created.id}/run`, async route => {
    const response = await route.fetch();
    incoming();
    await gate;
    await route.fulfill({ response }).catch(() => {});
  });
  await view.getByRole('button', { name: 'Reload view', exact: true }).click();
  await requested;
  await page.locator('#logout-button').click();
  await expect(page).toHaveURL(/\/login\/(?:\?.*)?$/);
  release();
  await login(page, 'viewer');
  await expect(page.locator('#saved-views-list [data-view-id]')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText(created.name);
  expect(await page.evaluate(() => Object.values(sessionStorage).join('\n'))).not.toContain(FLEET_SQL);
});
