'use strict';

const axe = require('axe-core');
const {
  test, expect, login, navigate, expectNoHorizontalBodyOverflow, expectChartAxisContrast,
} = require('./helpers.cjs');

async function audit(page, testInfo, label) {
  await page.evaluate(axe.source);
  const result = await page.evaluate(async () => window.axe.run(document, {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] },
  }));
  await testInfo.attach(`axe-${label}`, {
    body: JSON.stringify(result, null, 2), contentType: 'application/json',
  });
  const failures = result.violations.map(violation => ({
    id: violation.id, impact: violation.impact,
    nodes: violation.nodes.map(node => ({ target: node.target, summary: node.failureSummary })),
  }));
  expect.soft(failures, `WCAG A/AA violations in ${label}`).toEqual([]);
}

test('login has labeled keyboard controls and passes WCAG2/2.1 A/AA at desktop and mobile', async ({ page }, testInfo) => {
  await page.goto('/login/');
  await page.locator('#username').focus();
  await page.keyboard.press('Tab');
  await expect(page.locator('#password')).toBeFocused();
  await audit(page, testInfo, 'login-desktop');
  await page.screenshot({ path: testInfo.outputPath('login-1440.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoHorizontalBodyOverflow(page);
  await audit(page, testInfo, 'login-mobile');
  await page.screenshot({ path: testInfo.outputPath('login-390.png'), fullPage: true });
});

test('overview, explorer and SQL remain accessible and contain body overflow at390px', async ({ page }, testInfo) => {
  await login(page, 'analyst');
  await expect(page.locator('#metric-offline-share')).toContainText(/20/);
  for (const view of ['overview', 'explore', 'sql']) {
    await navigate(page, view);
    if (view === 'explore') await expect(page.locator('#explore-table')).toBeVisible();
    if (view === 'overview') await expectChartAxisContrast(page, testInfo, 'desktop');
    await audit(page, testInfo, `${view}-desktop`);
    await page.screenshot({ path: testInfo.outputPath(`${view}-1440.png`), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await expectNoHorizontalBodyOverflow(page);
    if (view === 'overview') await expectChartAxisContrast(page, testInfo, 'mobile');
    await audit(page, testInfo, `${view}-mobile`);
    await page.screenshot({ path: testInfo.outputPath(`${view}-390.png`), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
  }
});

test('Admin refresh controls pass the same accessibility and mobile layout checks', async ({ page }, testInfo) => {
  await login(page, 'admin');
  await navigate(page, 'refresh');
  await audit(page, testInfo, 'refresh-desktop');
  await page.screenshot({ path: testInfo.outputPath('refresh-1440.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoHorizontalBodyOverflow(page);
  await audit(page, testInfo, 'refresh-mobile');
  await page.screenshot({ path: testInfo.outputPath('refresh-390.png'), fullPage: true });
});
