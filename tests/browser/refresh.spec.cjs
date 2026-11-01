'use strict';

const { test, expect, login, navigate, startRefreshWorker } = require('./helpers.cjs');

test('Admin sees real queued/running/success and explicitly applies the new immutable snapshot', async ({ page }) => {
  await login(page, 'admin');
  await expect(page.locator('#metric-offline-share')).toContainText(/20/);
  await navigate(page, 'refresh');
  const queuedResponse = page.waitForResponse(response =>
    response.url().endsWith('/api/v1/refresh') && response.request().method() === 'POST',
  );
  await page.locator('#refresh-start').click();
  expect((await queuedResponse).status()).toBe(202);
  await expect(page.locator('#refresh-status')).toHaveAttribute('data-status', 'queued');
  const worker = startRefreshWorker('success', 3);
  await expect(page.locator('#refresh-status')).toHaveAttribute('data-status', 'running');
  await worker.result;
  await expect(page.locator('#refresh-status')).toHaveAttribute('data-status', 'succeeded');
  await expect(page.locator('#apply-snapshot')).toBeVisible();
  await page.locator('#apply-snapshot').click();
  await navigate(page, 'overview');
  await expect(page.locator('#metric-offline-share')).toContainText(/30(?:\.0+)?\s*%/);
  await expect(page.locator('#metric-outage')).toContainText(/600/);
});

test('a failed real synthetic refresh leaves the old snapshot available and announces the failure', async ({ page }) => {
  await login(page, 'admin');
  await expect(page.locator('#metric-offline-share')).toContainText(/20/);
  const before = await (await page.request.get('/api/v1/catalog')).json();
  await navigate(page, 'refresh');
  await page.locator('#refresh-start').click();
  await expect(page.locator('#refresh-status')).toHaveAttribute('data-status', 'queued');
  const worker = startRefreshWorker('failure', 2);
  await worker.result;
  await expect(page.locator('#refresh-status')).toHaveAttribute('data-status', 'failed');
  await expect(page.locator('#refresh-message')).toContainText(/failed|not complete|retry|publication/i);
  const after = await (await page.request.get('/api/v1/catalog')).json();
  expect(after.model_id).toBe(before.model_id);
  expect(after.source_snapshot_id).toBe(before.source_snapshot_id);
  await navigate(page, 'overview');
  await expect(page.locator('#metric-offline-share')).toContainText(/20/);
});
