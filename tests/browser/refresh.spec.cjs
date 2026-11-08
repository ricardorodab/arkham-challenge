'use strict';

const { test, expect, login, navigate, control, startRefreshWorker } = require('./helpers.cjs');

test('an empty workspace loads queued history and explicitly adopts its first completed refresh', async ({ page }) => {
  control('unpublish');
  await login(page, 'admin');
  await expect(page.locator('#global-notice')).toContainText('No analytical model is published.');
  const csrf = await (await page.request.get('/api/v1/auth/csrf')).json();
  const response = await page.request.post('/api/v1/refresh', {
    data: { mode: 'full' }, headers: { 'X-CSRFToken': csrf.csrf_token },
  });
  expect(response.status()).toBe(202);
  const queued = await response.json();
  await page.reload();
  await navigate(page, 'refresh');
  await expect(page.locator('#refresh-status')).toHaveAttribute('data-status', 'queued');
  await expect(page.locator('#refresh-job-id')).toContainText(queued.id);
  const worker = startRefreshWorker('success', 3);
  await worker.result;
  const completed = await (await page.request.get(queued.status_url)).json();
  expect(completed.progress.stage).toBe('completed');
  expect(completed.progress.rows_received).toBe(129);
  expect(completed.progress.pages_fetched).toBe(3);
  await expect(page.locator('#refresh-status')).toHaveAttribute('data-status', 'succeeded');
  await expect(page.locator('#refresh-use-latest')).toBeVisible();
  await expect(page.locator('#global-notice')).toContainText('No analytical model is published.');
  await page.locator('#refresh-use-latest').click();
  await expect(page.locator('#global-notice')).not.toContainText('No analytical model is published.');
  await navigate(page, 'overview');
  await expect(page.locator('#metric-offline-share')).toContainText(/30(?:\.0+)?\s*%/);
});

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


test('progress snapshots repaint counts, export steps and retained outcomes without count announcements', async ({ page }) => {
  const now = Date.now();
  let job = { id: 'progress-presentation', mode: 'recent', status: 'running', message: '',
    created_at: new Date(now - 130000).toISOString(), started_at: new Date(now - 125000).toISOString(), finished_at: null,
    progress: { stage: 'extracting', route: 'facility', rows_received: 100, pages_fetched: 2, updated_at: new Date(now - 65000).toISOString() },
  };
  await page.route(/\/api\/v1\/refresh(?:[/?].*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    const url = new URL(route.request().url());
    return route.fulfill({ json: url.pathname.endsWith('/refresh') ? { jobs: [job] } : job });
  });
  await login(page, 'admin');
  await navigate(page, 'refresh');
  await expect(page.locator('#refresh-step')).toContainText('Facility');
  await expect(page.locator('#refresh-counts')).toHaveText('100 rows received · 2 pages fetched');
  await expect(page.locator('#refresh-progress')).not.toHaveAttribute('value');
  await expect(page.locator('#refresh-updated')).toContainText('ago');
  await expect(page.locator('.refresh-detail')).toHaveAttribute('aria-live', 'off');
  await page.evaluate(() => {
    window.__progressAnnouncements = [];
    new MutationObserver(() => window.__progressAnnouncements.push(document.getElementById('refresh-announcement').textContent))
      .observe(document.getElementById('refresh-announcement'), { childList: true, characterData: true, subtree: true });
  });
  job = { ...job, progress: { ...job.progress, rows_received: 129, pages_fetched: 3 } };
  await page.locator('#refresh-check').click();
  await expect(page.locator('#refresh-counts')).toHaveText('129 rows received · 3 pages fetched');
  expect(await page.evaluate(() => window.__progressAnnouncements)).toEqual([]);
  job = { ...job, progress: { ...job.progress, stage: 'exporting_model', completed: 3, total: 10 } };
  await page.locator('#refresh-check').click();
  await expect(page.locator('#refresh-progress-label')).toHaveText('Model export 30%');
  await expect(page.locator('#refresh-progress')).toHaveAttribute('value', '3');
  await expect(page.locator('#refresh-progress')).toHaveAttribute('max', '10');
  expect(await page.evaluate(() => window.__progressAnnouncements.some(text => text.includes('Exporting model datasets')))).toBe(true);
  await page.reload();
  await navigate(page, 'refresh');
  await expect(page.locator('#refresh-progress-label')).toHaveText('Model export 30%');
  job = { ...job, status: 'failed', message: 'Refresh failed safely.', finished_at: new Date().toISOString() };
  await page.locator('#refresh-check').click();
  await expect(page.locator('#refresh-status')).toHaveAttribute('data-status', 'failed');
  await expect(page.locator('#refresh-step-label')).toHaveText('Last reported step');
  await expect(page.locator('#refresh-counts')).toContainText('129 rows received');
  job = { ...job, progress: undefined };
  await page.reload();
  await navigate(page, 'refresh');
  await expect(page.locator('#refresh-step')).toHaveText('No step details reported');
  await expect(page.locator('#refresh-progress')).toBeHidden();
  await expect(page.locator('#refresh-counts')).toBeHidden();
});

test('real synthetic model progress displays actual counters and produces a verified screenshot', async ({ page }, testInfo) => {
  const fs = require('node:fs/promises');
  const path = require('node:path');
  await login(page, 'admin');
  await navigate(page, 'refresh');
  const response = page.waitForResponse(response => response.url().endsWith('/api/v1/refresh') && response.request().method() === 'POST');
  await page.locator('#refresh-start').click();
  const job = await (await response).json();
  const worker = startRefreshWorker('success', 0, 0.7);
  let observed;
  await expect.poll(async () => {
    const value = await (await page.request.get(job.status_url)).json();
    if (value.status === 'running' && value.progress?.stage === 'exporting_model' && value.progress.completed > 0 && value.progress.completed < value.progress.total) {
      observed = value;
      return true;
    }
    return false;
  }, { timeout: 60000, intervals: [100, 150, 200] }).toBe(true);
  expect(observed.progress.rows_received).toBe(129);
  expect(observed.progress.pages_fetched).toBe(3);
  await page.locator('#refresh-check').click();
  await expect(page.locator('#refresh-step')).toHaveText('Exporting model datasets');
  await expect(page.locator('#refresh-counts')).toHaveText('129 rows received · 3 pages fetched');
  await expect(page.locator('#refresh-progress-label')).toHaveText(/Model export \d+%/);
  const artifact = process.env.OUTAGE_PROGRESS_SCREENSHOT || testInfo.outputPath('refresh-progress.png');
  await fs.mkdir(path.dirname(artifact), { recursive: true });
  await page.locator('#refresh-status').screenshot({ path: artifact });
  await testInfo.attach('actual-running-progress', { path: artifact, contentType: 'image/png' });
  await testInfo.attach('actual-running-event', { body: JSON.stringify({ fixtureRunId: process.env.OUTAGE_BROWSER_RUN_ID, jobId: job.id, progress: observed.progress }), contentType: 'application/json' });
  await worker.result;
  await expect(page.locator('#refresh-status')).toHaveAttribute('data-status', 'succeeded');
});
