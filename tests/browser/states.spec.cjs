'use strict';

const { test, expect, login, navigate } = require('./helpers.cjs');

const generator = /\/api\/v1\/data\/generator_daily(?:\?|$)/;
function denied(route, status, code, detail) {
  return route.fulfill({
    status, contentType: 'application/problem+json', headers: { 'Retry-After': '1' },
    body: JSON.stringify({
      type: `urn:outage-explorer:error:${code}`, title: code.replaceAll('_', ' '),
      status, code, detail, request_id: '0123456789abcdef0123456789abcdef',
    }),
  });
}

test('loading is announced and a forbidden dataset result becomes an explicit403 state', async ({ page }) => {
  await login(page, 'analyst');
  await expect(page.locator('#metric-offline-share')).toContainText(/20/);
  await navigate(page, 'explore');
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let requested;
  const incoming = new Promise(resolve => { requested = resolve; });
  await page.route(generator, async route => {
    requested();
    await gate;
    await denied(route, 403, 'dataset_forbidden', 'The selected dataset is no longer permitted.');
  });
  await page.locator('#dataset-select').selectOption('generator_daily');
  await incoming;
  await expect(page.locator('#explore-loading')).toBeVisible();
  await expect(page.locator('#explore-form')).toHaveAttribute('aria-busy', 'true');
  await expect(page.locator('#explore-export')).toBeDisabled();
  release();
  await expect(page.locator('#explore-error')).toBeVisible();
  await expect(page.locator('#explore-error')).toContainText(/not allowed|not permitted/i);
  await expect(page.locator('#explore-loading')).toBeHidden();
  // The local filter remains available for a corrected explicit query, while
  // denied responses never leave analytical rows or exportable data visible.
  await expect(page.locator('#explore-content')).toBeVisible();
  await expect(page.locator('#explore-search')).toBeVisible();
  await expect(page.locator('#explore-table')).toBeHidden();
  await expect(page.locator('#explore-table tbody tr')).toHaveCount(0);
  await expect(page.locator('#explore-export')).toBeDisabled();
});

test('429 capacity errors expose retry guidance and a retry can recover normally', async ({ page }) => {
  await login(page, 'analyst');
  await expect(page.locator('#metric-offline-share')).toContainText(/20/);
  await navigate(page, 'explore');
  let requests = 0;
  await page.route(generator, async route => {
    requests++;
    if (requests <= 3) await denied(route, 429, 'query_capacity', 'Query capacity is busy; retry shortly.');
    else await route.continue();
  });
  await page.locator('#dataset-select').selectOption('generator_daily');
  await expect(page.locator('#explore-error')).toBeVisible();
  await expect(page.locator('#explore-error')).toContainText(/busy|wait|retry/i);
  await page.locator('#explore-error').getByRole('button', { name: /try again|retry/i }).click();
  await expect(page.locator('#explore-content')).toBeVisible();
  await expect(page.locator('#explore-table tbody tr')).toHaveCount(25);
  expect(requests).toBeGreaterThanOrEqual(4);
});

test('a late result from an obsolete dataset is never rendered over the new selection', async ({ page }) => {
  await login(page, 'analyst');
  await expect(page.locator('#metric-offline-share')).toContainText(/20/);
  const catalog = await (await page.request.get('/api/v1/catalog')).json();
  await navigate(page, 'explore');
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let requested;
  const incoming = new Promise(resolve => { requested = resolve; });
  const canary = 'OBSOLETE_BROWSER_RESPONSE_MUST_NEVER_RENDER';
  await page.evaluate(text => {
    window.__staleResponseRendered = false;
    new MutationObserver(() => {
      if (document.querySelector('#explore-table')?.textContent.includes(text)) {
        window.__staleResponseRendered = true;
      }
    }).observe(document.querySelector('#explore-table'), {
      childList: true, subtree: true, characterData: true,
    });
  }, canary);
  await page.route(generator, async route => {
    requested(); await gate;
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      dataset_id: 'generator_daily', model_id: catalog.model_id,
      source_snapshot_id: catalog.source_snapshot_id,
      columns: [{ name: 'facility_name', type: 'VARCHAR' }], rows: [[canary]],
      returned_rows: 1, next_cursor: null, filters: { start: null, end: null, facility_id: null },
    }) }).catch(() => {});
  });
  await page.locator('#dataset-select').selectOption('generator_daily');
  await incoming;
  await page.locator('#dataset-select').selectOption('facility_daily');
  release();
  await expect(page.locator('#explore-content')).toBeVisible();
  await expect(page.locator('#explore-table tbody tr')).toHaveCount(6);
  await expect(page.locator('#explore-table')).not.toContainText(canary);
  expect(await page.evaluate(() => window.__staleResponseRendered)).toBe(false);
  await expect(page.locator('#dataset-select')).toHaveValue('facility_daily');
  await expect(page.locator('#explore-error')).toBeHidden();
});
