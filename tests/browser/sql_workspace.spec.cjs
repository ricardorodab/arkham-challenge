'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const axe = require('axe-core');
const {
  test, expect, login, navigate, sqlEditor, fillSql, expectNoHorizontalBodyOverflow,
} = require('./helpers.cjs');

/** Save optional shareable previews without encoding an operator-specific path in the repository. */
async function preview(page, testInfo, filename, { preserveFocus = false } = {}) {
  const destination = process.env.OUTAGE_SQL_PREVIEW_DIR
    ? path.join(path.resolve(process.env.OUTAGE_SQL_PREVIEW_DIR), filename)
    : testInfo.outputPath(filename);
  await fs.mkdir(path.dirname(destination), { recursive: true });
  if (!preserveFocus) await page.evaluate(() => document.activeElement?.blur());
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: destination, fullPage: true });
}

/** Observe execution requests independently from editor rendering and catalog loading. */
function queryRequests(page) {
  const requests = [];
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/v1/query') requests.push(request);
  });
  return requests;
}

async function enhancedEditor(page) {
  const editor = page.locator('#sql-editor-host .cm-content');
  await expect(editor).toBeVisible();
  await expect(editor).toHaveAccessibleName('SQL query');
  return editor;
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

/** Keep application styles clean while the browser's copied token style stays blocked. */
function assertEditorCsp(events) {
  expect(events.length).toBeLessThanOrEqual(1);
  for (const event of events) {
    expect(event).toEqual({
      directive: 'style-src-attr', blocked: 'inline', source: '', line: 0,
      phase: 'native-selection-replacement',
    });
  }
}

test('every dataset has three distinct loadable starters without manufacturing recent history or executing SQL', async ({ page }) => {
  const requests = queryRequests(page);
  await login(page, 'analyst');
  await navigate(page, 'sql');
  await enhancedEditor(page);
  await expect(page.locator('#sql-complete, #sql-insert, #sql-suggestions')).toHaveCount(0);
  const datasets = await page.locator('#sql-dataset option').evaluateAll(
    options => options.map(option => option.value).filter(Boolean),
  );
  expect(datasets).toHaveLength(10);
  for (const dataset of datasets) {
    await page.locator('#sql-dataset').selectOption(dataset);
    await expect(page.locator('#sql-examples [data-example-level]')).toHaveCount(3);
    const statements = [];
    for (const level of ['easy', 'medium', 'hard']) {
      const button = page.locator(`#sql-examples [data-example-level="${level}"]`);
      await expect(button.locator('.sql-example-level')).toHaveCount(0);
      await expect(button.locator('strong')).not.toBeEmpty();
      await button.click();
      const statement = await page.locator('#sql-editor').inputValue();
      expect(statement).toMatch(new RegExp(`FROM\\s+"?${dataset}"?`, 'i'));
      statements.push(statement);
      await expect(sqlEditor(page)).toBeFocused();
    }
    expect(new Set(statements).size, `${dataset} needs distinct query levels`).toBe(3);
  }
  expect(requests).toHaveLength(0);
  await expect(page.locator('#sql-history option')).toHaveCount(1);
  await expect(page.locator('#sql-history')).toContainText(/No queries|No recent/i);
});

test('schema fields insert at the SQL selection, participate in undo, and update the pending draft', async ({ page }) => {
  await login(page, 'analyst');
  await navigate(page, 'sql');
  const editor = await enhancedEditor(page);
  await fillSql(page, 'SELECT ');
  await editor.press('End');
  await page.locator('#sql-schema').getByRole('button', { name: 'Insert column outage_mw', exact: true }).click();
  await expect(page.locator('#sql-editor')).toHaveValue('SELECT "outage_mw"');
  await expect(editor).toBeFocused();
  await editor.press('Control+z');
  await expect(page.locator('#sql-editor')).toHaveValue('SELECT ');
  await editor.press('Control+y');
  await expect(page.locator('#sql-editor')).toHaveValue('SELECT "outage_mw"');
});

test('SQL highlighting, automatic and explicit suggestions, completion keys, and Run shortcut work without a keyboard trap', async ({ page }) => {
  const requests = queryRequests(page);
  await login(page, 'analyst');
  await navigate(page, 'sql');
  const editor = await enhancedEditor(page);
  await fillSql(page, 'SELECT ');
  await editor.press('End');
  await editor.pressSequentially('cap');
  const choices = page.locator('.cm-tooltip-autocomplete [role="listbox"]');
  await expect(choices).toBeVisible();
  await expect(choices).toContainText('capacity_mw');
  await editor.press('Enter');
  await expect(page.locator('#sql-editor')).toHaveValue(/SELECT "capacity_mw"$/);
  expect(requests).toHaveLength(0);
  await fillSql(page, 'SELECT ');
  await editor.press('Control+Space');
  await expect(choices).toBeVisible();
  await expect(choices).not.toContainText(/\bJOIN\b|\bDELETE\b|\bINSERT\b|\bWITH\b/);
  await editor.press('Escape');
  await expect(choices).toHaveCount(0);
  await editor.press('Tab');
  await expect(editor).not.toBeFocused();
  await fillSql(page, 'SELECT COUNT(*) AS observations FROM fleet_daily');
  const colors = await page.locator('#sql-editor-host .cm-line span').evaluateAll(
    nodes => [...new Set(nodes.map(node => getComputedStyle(node).color))],
  );
  expect(colors.length, 'SQL tokens should have visible syntax colors').toBeGreaterThan(1);
  await editor.press('Control+Enter');
  await expect(page.locator('#sql-table tbody tr')).toHaveCount(1);
  expect(requests).toHaveLength(1);
});

test('dataset changes and a new viewer session cannot retain detail-column suggestions or private editor text', async ({ page }) => {
  await login(page, 'analyst');
  await navigate(page, 'sql');
  const editor = await enhancedEditor(page);
  await page.locator('#sql-dataset').selectOption('generator_daily');
  await fillSql(page, 'SELECT generator_');
  await editor.press('Control+Space');
  await expect(page.locator('.cm-tooltip-autocomplete')).toContainText('generator_id');
  await page.locator('#sql-dataset').selectOption('fleet_daily');
  await fillSql(page, 'SELECT generator_');
  await editor.press('Control+Space');
  expect((await page.locator('.cm-tooltip-autocomplete').allTextContents()).join(' ')).not.toContain('generator_id');
  await page.locator('#logout-button').click();
  await expect(page).toHaveURL(/\/login\/(?:\?.*)?$/);
  await expect(page.locator('.cm-editor')).toHaveCount(0);
  await login(page, 'viewer');
  await navigate(page, 'sql');
  await enhancedEditor(page);
  const datasets = await page.locator('#sql-dataset option').evaluateAll(
    options => options.map(option => option.value).filter(Boolean).sort(),
  );
  expect(datasets).toEqual(['fleet_daily', 'national_daily']);
  await expect(page.locator('#sql-editor')).not.toHaveValue(/generator_/);
  await fillSql(page, 'SELECT generator_');
  await sqlEditor(page).press('Control+Space');
  expect((await page.locator('.cm-tooltip-autocomplete').allTextContents()).join(' ')).not.toContain('generator_id');
});

test('a missing enhanced-editor bundle leaves a usable labeled textarea and runnable query starters', async ({ page }) => {
  await page.route('**/assets/sql_editor.bundle.js', route => route.abort('failed'));
  await login(page, 'viewer');
  await navigate(page, 'sql');
  await expect(page.locator('#sql-editor')).toBeVisible();
  await expect(page.locator('.cm-editor')).toHaveCount(0);
  await page.locator('#sql-examples [data-example-level="easy"]').click();
  await expect(page.locator('#sql-editor')).toBeFocused();
  await fillSql(page, 'SELECT COUNT(*) AS observations FROM fleet_daily');
  await sqlEditor(page).press('Control+Enter');
  await expect(page.locator('#sql-table tbody tr')).toHaveCount(1);
});

test('strict CSP preserves editor input and accessible completion at desktop and mobile', async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    window.__sqlCspViolations = [];
    window.__sqlCspPhase = 'boot';
    document.addEventListener('securitypolicyviolation', event => {
      window.__sqlCspViolations.push({
        directive: event.effectiveDirective, blocked: event.blockedURI,
        phase: window.__sqlCspPhase, source: event.sourceFile, line: event.lineNumber,
      });
    });
  });
  const external = [];
  page.on('request', request => {
    const url = new URL(request.url());
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== 'http://127.0.0.1:8766') external.push(url.origin);
  });
  await login(page, 'analyst');
  await navigate(page, 'sql');
  const editor = await enhancedEditor(page);
  await page.evaluate(() => { window.__sqlCspPhase = 'query-edit'; });
  await page.locator('#sql-examples [data-example-level="easy"]').click();
  await page.evaluate(() => { window.__sqlCspPhase = 'query-run'; });
  await editor.press('Control+Enter');
  await expect(page.locator('#sql-table tbody tr')).toHaveCount(3);
  await page.evaluate(() => { window.__sqlCspPhase = 'desktop-preview'; });
  await preview(page, testInfo, 'sql-workspace-desktop.png');
  expect(await page.evaluate(() => window.__sqlCspViolations)).toEqual([]);
  await page.evaluate(() => { window.__sqlCspPhase = 'native-selection-replacement'; });
  // Chromium can clone a highlighted token's inline color/weight when ordinary
  // keys replace a selection. CSP deliberately blocks that native style copy;
  // the editor must still retain exact SQL and provide working completion.
  await editor.press('ControlOrMeta+a');
  await editor.pressSequentially('SELECT cap');
  await expect(page.locator('#sql-editor')).toHaveValue('SELECT cap');
  await page.evaluate(() => new Promise(requestAnimationFrame));
  await page.evaluate(() => { window.__sqlCspPhase = 'completion'; });
  await editor.press('Control+Space');
  await expect(page.locator('.cm-tooltip-autocomplete')).toBeVisible();
  assertEditorCsp(await page.evaluate(() => window.__sqlCspViolations));
  await preview(page, testInfo, 'sql-workspace-suggestions.png', { preserveFocus: true });
  await page.evaluate(() => { window.__sqlCspPhase = 'axe-desktop'; });
  await audit(page, testInfo, 'sql-completion-desktop');
  await page.evaluate(() => { window.__sqlCspPhase = 'mobile-editor'; });
  await page.setViewportSize({ width: 390, height: 844 });
  await editor.scrollIntoViewIfNeeded();
  await expectNoHorizontalBodyOverflow(page);
  await expect(page.locator('.cm-tooltip-autocomplete')).toBeInViewport({ ratio: 0.8 });
  await page.evaluate(() => { window.__sqlCspPhase = 'axe-mobile'; });
  await audit(page, testInfo, 'sql-completion-mobile');
  await page.evaluate(() => { window.__sqlCspPhase = 'mobile-editor'; });
  await editor.press('Escape');
  await page.locator('#sql-examples [data-example-level="easy"]').click();
  await preview(page, testInfo, 'sql-workspace-mobile.png');
  assertEditorCsp(await page.evaluate(() => window.__sqlCspViolations));
  expect(external).toEqual([]);
});
