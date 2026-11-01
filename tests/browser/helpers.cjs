'use strict';

const { test: baseTest, expect } = require('@playwright/test');
const { execFileSync, spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const { ROOT, PYTHON, environment, fixture } = require('./environment.cjs');

const FRONTEND_TEST_PASSWORDS = Object.freeze({ ...fixture.passwords });
const workers = new Set();

function control(...args) {
  return execFileSync(PYTHON, ['-m', 'tests.browser.control', ...args], {
    cwd: ROOT, env: environment(), encoding: 'utf8', windowsHide: true, timeout: 45_000,
  });
}

function startRefreshWorker(outcome = 'success', delay = 2) {
  const child = spawn(PYTHON, ['-m', 'tests.browser.control', 'worker',
    '--outcome', outcome, '--delay', String(delay)], {
    cwd: ROOT, env: environment(), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  workers.add(child);
  let output = '';
  child.stdout.on('data', chunk => { output += chunk; });
  child.stderr.on('data', chunk => { output += chunk; });
  child.result = new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('exit', code => {
      workers.delete(child);
      if (code === 0) resolve(output);
      else reject(new Error(`Synthetic worker exited ${code}: ${output.slice(-3000)}`));
    });
  });
  // An interrupted test still cleans the process without an unhandled rejection.
  child.result.catch(() => {});
  return child;
}

async function cleanupWorkers() {
  await Promise.all([...workers].map(async child => {
    if (child.exitCode === null) child.kill('SIGTERM');
    await child.result.catch(() => {});
  }));
}

async function login(page, role = 'analyst', { keyboard = false } = {}) {
  await page.goto('/login/');
  await expect(page.locator('#login-form')).toBeVisible();
  await page.locator('#username').fill(role);
  await page.locator('#password').fill(FRONTEND_TEST_PASSWORDS[role]);
  await expect(page.locator('#login-submit')).toBeEnabled();
  if (keyboard) await page.locator('#password').press('Enter');
  else await page.locator('#login-submit').click();
  await expect(page).toHaveURL(/\/(?:#.*)?$/);
  await expect(page.locator('#logout-button')).toBeVisible();
  return role;
}

async function navigate(page, view) {
  await page.locator(`a[data-view="${view}"]`).click();
  await expect(page.locator(`#view-${view}`)).toBeVisible();
}

async function readDownload(download) {
  const file = await download.path();
  return fs.readFile(file, 'utf8');
}

function parseCSV(text) {
  const rows = [];
  let row = [], value = '', quoted = false;
  text = text.replace(/^\uFEFF/, '');
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (char === '"') {
      if (quoted && text[index + 1] === '"') { value += '"'; index++; }
      else quoted = !quoted;
    } else if (char === ',' && !quoted) { row.push(value); value = ''; }
    else if ((char === '\n' || char === '\r') && !quoted) {
      if (char === '\r' && text[index + 1] === '\n') index++;
      row.push(value); rows.push(row); row = []; value = '';
    } else value += char;
  }
  if (value || row.length) { row.push(value); rows.push(row); }
  return rows;
}

async function expectNoHorizontalBodyOverflow(page) {
  const measurement = await page.evaluate(() => ({
    width: document.documentElement.clientWidth,
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
  }));
  expect(measurement.body).toBeLessThanOrEqual(measurement.width + 1);
  expect(measurement.document).toBeLessThanOrEqual(measurement.width + 1);
}

function relativeLuminance(rgb) {
  const channels = rgb.map(value => {
    const srgb = value / 255;
    return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrastRatio(foreground, background) {
  const first = relativeLuminance(foreground), second = relativeLuminance(background);
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
}

async function expectChartAxisContrast(page, testInfo, label) {
  // Axe cannot reliably infer SVG backgrounds. Check rendered axis labels
  // against their actual solid panel, using WCAG sRGB relative luminance.
  // Tooltip text is intentionally excluded: it has its own dark background.
  const samples = await page.locator('#fleet-chart .chart-label').evaluateAll(nodes => nodes.map(node => ({
    text: node.textContent,
    foreground: getComputedStyle(node).fill,
    background: getComputedStyle(node.closest('.chart-panel')).backgroundColor,
  })));
  expect(samples.length, 'A populated chart must have real SVG axis labels').toBeGreaterThan(0);
  const rgb = value => {
    const match = value.match(/^rgba?\(([^)]+)\)$/);
    expect(match, `Supported computed RGB color: ${value}`).toBeTruthy();
    const parts = match[1].split(',').map(Number);
    expect(parts.length === 3 || parts[3] === 1, 'Contrast inputs must be opaque').toBe(true);
    return parts.slice(0, 3);
  };
  const measured = samples.map(sample => ({
    ...sample, ratio: contrastRatio(rgb(sample.foreground), rgb(sample.background)),
  }));
  await testInfo.attach(`computed-axis-contrast-${label}`, {
    body: JSON.stringify(measured, null, 2), contentType: 'application/json',
  });
  for (const sample of measured) {
    expect(sample.ratio, `SVG axis ${sample.text}: ${sample.foreground} on ${sample.background}`)
      .toBeGreaterThanOrEqual(4.5);
  }
}

const test = baseTest.extend({
  // An automatic fixture applies across every spec even when this module is
  // cached by the Playwright worker; module-level hooks would bind one file.
  browserIsolation: [async ({}, use) => {
    control('reset');
    try { await use(); }
    finally { await cleanupWorkers(); }
  }, { auto: true }],
});

module.exports = {
  test, expect, FRONTEND_TEST_PASSWORDS, fixture, login, navigate,
  control, startRefreshWorker, readDownload, parseCSV, expectNoHorizontalBodyOverflow,
  expectChartAxisContrast,
};
