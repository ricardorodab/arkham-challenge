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
  await navigate(page, 'overview');
  await page.setViewportSize({ width: 320, height: 844 });
  await expect(page.locator('#metric-offline-share')).toContainText(/20/);
  await expectNoHorizontalBodyOverflow(page);
  const narrowMetrics = await page.evaluate(() => {
    const capacity = document.getElementById('metric-capacity');
    const originalCapacity = capacity.textContent;
    // This rendered-text layout sample reproduces the actual real-data 320px
    // regression; it does not change the synthetic source or API observations.
    capacity.textContent = '100,056.7';
    try {
      const values = [...document.querySelectorAll('#overview-content .metric-value')]
        .map(node => {
          const range = document.createRange();
          range.selectNodeContents(node);
          const text = range.getBoundingClientRect();
          const card = node.closest('.metric-card');
          const bounds = card.getBoundingClientRect();
          const style = getComputedStyle(card);
          return {
            id: node.id,
            text: node.textContent,
            height: text.height,
            lineHeight: Number.parseFloat(getComputedStyle(node).lineHeight),
            textBounds: { left: text.left, right: text.right, top: text.top, bottom: text.bottom },
            contentBounds: {
              left: bounds.left + Number.parseFloat(style.borderLeftWidth) + Number.parseFloat(style.paddingLeft),
              right: bounds.right - Number.parseFloat(style.borderRightWidth) - Number.parseFloat(style.paddingRight),
              top: bounds.top + Number.parseFloat(style.borderTopWidth) + Number.parseFloat(style.paddingTop),
              bottom: bounds.bottom - Number.parseFloat(style.borderBottomWidth) - Number.parseFloat(style.paddingBottom),
            },
          };
        });
      return { values, viewport: innerWidth, documentWidth: document.documentElement.scrollWidth };
    } finally {
      capacity.textContent = originalCapacity;
    }
  });
  await testInfo.attach('metric-reflow-320', {
    body: JSON.stringify(narrowMetrics, null, 2), contentType: 'application/json',
  });
  expect(narrowMetrics.values).toHaveLength(4);
  expect(narrowMetrics.documentWidth).toBeLessThanOrEqual(narrowMetrics.viewport + 1);
  for (const value of narrowMetrics.values) {
    expect(value.height, `${value.id} must contain visible text`).toBeGreaterThan(0);
    expect(value.lineHeight, `${value.id} needs a measurable line height`).toBeGreaterThan(0);
    expect(value.height, `${value.id} must fit on one line at 320px`).toBeLessThanOrEqual(value.lineHeight + 1);
    expect(value.textBounds.left, `${value.id} left edge`).toBeGreaterThanOrEqual(value.contentBounds.left - 1);
    expect(value.textBounds.right, `${value.id} right edge`).toBeLessThanOrEqual(value.contentBounds.right + 1);
    expect(value.textBounds.top, `${value.id} top edge`).toBeGreaterThanOrEqual(value.contentBounds.top - 1);
    expect(value.textBounds.bottom, `${value.id} bottom edge`).toBeLessThanOrEqual(value.contentBounds.bottom + 1);
  }
  await expect(page.locator('#metric-capacity')).toContainText(/2[ ,]?000/);
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
