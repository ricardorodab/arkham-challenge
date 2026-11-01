'use strict';

const { test, expect, login, navigate, control } = require('./helpers.cjs');

test('protected shell redirects before data; keyboard login and logout clear back-navigation', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveURL(/\/login\/(?:\?.*)?$/);
  await expect(page.locator('#logout-button')).toHaveCount(0);
  await login(page, 'analyst', { keyboard: true });
  await navigate(page, 'sql');
  await expect(page.locator('#sql-editor')).toBeVisible();
  await page.locator('#logout-button').click();
  await expect(page).toHaveURL(/\/login\/(?:\?.*)?$/);
  await page.goBack();
  await expect(page).toHaveURL(/\/login\/(?:\?.*)?$/);
  await expect(page.locator('#sql-editor')).toHaveCount(0);
});

test('invalid credentials are announced without user enumeration; password toggle remains keyboard operable', async ({ page }) => {
  await page.goto('/login/');
  await page.locator('#username').fill('viewer');
  await page.locator('#password').fill('Intentionally-wrong-synthetic-password');
  await page.locator('#login-toggle-password').focus();
  await page.keyboard.press('Space');
  await expect(page.locator('#password')).toHaveAttribute('type', 'text');
  await page.keyboard.press('Space');
  await expect(page.locator('#password')).toHaveAttribute('type', 'password');
  await page.locator('#login-submit').click();
  await expect(page.locator('#login-error')).toBeVisible();
  await expect(page.locator('#login-error')).toContainText(/credentials|sign.in|password/i);
  await expect(page).toHaveURL(/\/login\/(?:\?.*)?$/);
  await expect(page.locator('#password')).toBeFocused();
});

for (const role of ['viewer', 'analyst', 'admin']) {
  test(`${role} gets only its permitted dataset choices and refresh controls`, async ({ page }) => {
    const requested = [];
    page.on('request', request => {
      const match = new URL(request.url()).pathname.match(/^\/api\/v1\/data\/([^/]+)$/);
      if (match) requested.push(match[1]);
    });
    await login(page, role);
    await expect(page.locator('#metric-offline-share')).toContainText(/20/);
    await navigate(page, 'explore');
    const options = await page.locator('#dataset-select option').evaluateAll(
      elements => elements.map(element => element.value).filter(Boolean),
    );
    expect(options).toContain('fleet_daily');
    expect(options).toContain('national_daily');
    if (role === 'viewer') {
      expect(options.sort()).toEqual(['fleet_daily', 'national_daily']);
      await expect(page.locator('#dataset-select option[value="generator_daily"]')).toHaveCount(0);
      expect((await page.locator('#explore-table th').allTextContents()).join(' '))
        .not.toMatch(/facility|generator/i);
      await navigate(page, 'sql');
      const sqlChoices = await page.locator('#sql-dataset option').evaluateAll(
        elements => elements.map(element => element.value).filter(Boolean),
      );
      expect(sqlChoices.sort()).toEqual(['fleet_daily', 'national_daily']);
      expect(requested.every(dataset => ['fleet_daily', 'national_daily'].includes(dataset))).toBe(true);
    } else {
      expect(options).toContain('generator_daily');
      expect(options).toContain('facility_daily');
    }
    if (role !== 'admin') {
      await expect(page.locator('a[data-view="refresh"], #refresh-start, #view-refresh')).toHaveCount(0);
      await page.goto('/#refresh');
      await expect(page.locator('#refresh-start')).toHaveCount(0);
    } else {
      await navigate(page, 'refresh');
      await expect(page.locator('#refresh-start')).toBeVisible();
    }
  });
}

test('logout is observed by another tab and revoked server sessions clear the protected shell', async ({ page, context }) => {
  await login(page, 'analyst');
  const other = await context.newPage();
  await other.goto('/#sql');
  await expect(other.locator('#sql-editor')).toBeVisible();
  await page.locator('#logout-button').click();
  await other.bringToFront();
  await expect(other).toHaveURL(/\/login\/(?:\?.*)?$/);
  await expect(other.locator('#sql-editor')).toHaveCount(0);
  await login(other, 'analyst');
  await navigate(other, 'sql');
  const privateQuery = 'SELECT COUNT(*) AS private_revoke_count FROM fleet_daily';
  await other.locator('#sql-editor').fill(privateQuery);
  await other.locator('#sql-run').click();
  await expect(other.locator('#sql-table tbody tr')).toHaveCount(1);
  expect(await other.evaluate(() => Object.values(sessionStorage).join('\n'))).toContain(privateQuery);
  control('revoke', 'analyst', '--kind', 'sessions');
  await page.bringToFront();
  await other.bringToFront();
  // Focus can revoke the shell before a navigation click is possible. If it
  // has not done so yet, the next real link action also verifies the session.
  await other.evaluate(() => document.querySelector('a[data-view="explore"]')?.click()).catch(() => {});
  await expect(other).toHaveURL(/\/login\/(?:\?.*)?$/);
  await expect(other.locator('#explore-table')).toHaveCount(0);
  expect(await other.evaluate(() => Object.values(sessionStorage).join('\n'))).not.toContain(privateQuery);
});
