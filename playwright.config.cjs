// Browser acceptance uses one fresh synthetic service, one worker and no retries.
// Explicit output paths intentionally create new work/ artifacts; they are not
// links to the preserved historical work archive. Never reuse an operator server.
'use strict';

const { defineConfig } = require('@playwright/test');
const crypto = require('node:crypto');

// A new run never opens the operator's .service/ or analytical data directory.
process.env.OUTAGE_BROWSER_RUN_ID ||= crypto.randomUUID().replaceAll('-', '');

module.exports = defineConfig({
  testDir: './tests/browser',
  testMatch: '**/*.spec.cjs',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  metadata: {
    fixture: 'isolated-synthetic-only',
    fixtureRunId: process.env.OUTAGE_BROWSER_RUN_ID,
    browserBaseURL: 'http://127.0.0.1:8766',
  },
  timeout: 90_000,
  expect: { timeout: 15_000 },
  outputDir: 'work/browser-results/artifacts',
  reporter: [
    ['list'],
    ['json', { outputFile: 'work/browser-results/results.json' }],
    ['html', { outputFolder: 'work/browser-report', open: 'never' }],
  ],
  use: {
    baseURL: 'http://127.0.0.1:8766',
    viewport: { width: 1440, height: 1000 },
    browserName: 'chromium',
    actionTimeout: 15_000,
    navigationTimeout: 20_000,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
  },
  webServer: {
    command: 'node tests/browser/start-server.cjs',
    url: 'http://127.0.0.1:8766/api/v1/health/live',
    reuseExistingServer: false,
    timeout: 90_000,
    stdout: 'pipe',
    stderr: 'pipe',
    env: { OUTAGE_BROWSER_RUN_ID: process.env.OUTAGE_BROWSER_RUN_ID },
  },
});
