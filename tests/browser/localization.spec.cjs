'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const axe = require('axe-core');
const {
  test,
  expect,
  login,
  FRONTEND_TEST_PASSWORDS,
  expectNoHorizontalBodyOverflow,
  expectChartAxisContrast,
} = require('./helpers.cjs');

const SQL =
  "SELECT period, outage_mw, 'Active' AS source_status\nFROM fleet_daily\nORDER BY period";

/** Change only the native interface preference; the application performs a full reload. */
async function language(page, value) {
  const changed = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === '/language/' && response.request().method() === 'POST',
  );
  await page.locator('#language-select').selectOption(value);
  expect((await changed).status()).toBe(302);
  await expect(page.locator('html')).toHaveAttribute('lang', value);
  await expect(page.locator('#language-select')).toHaveValue(value);
}

async function spanishLogin(page, role = 'admin') {
  await page.goto('/login/');
  await language(page, 'es');
  await page.locator('#username').fill(role);
  await page.locator('#password').fill(FRONTEND_TEST_PASSWORDS[role]);
  await expect(page.locator('#login-submit')).toBeEnabled();
  await page.locator('#login-submit').click();
  await expect(page.locator('#overview-content')).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'es');
}

async function navigate(page, view) {
  const link = page.locator(`a[data-view="${view}"]`);
  if (!(await link.isVisible())) await page.locator('#nav-toggle').click();
  await link.click();
  await expect(page.locator(`#view-${view}`)).toBeVisible();
}

/** Type canonical SQL through the localized editor, avoiding browser rich-text replacement. */
async function fillSql(page, statement) {
  const editor = page.getByRole('textbox', { name: 'Consulta SQL', exact: true });
  await expect(editor).toBeVisible();
  if (await editor.evaluate((node) => node.isContentEditable)) {
    await editor.click();
    await editor.press('ControlOrMeta+a');
    await editor.press('Backspace');
    await page.keyboard.insertText(statement);
  } else await editor.fill(statement);
  await expect(page.locator('#sql-editor')).toHaveValue(statement);
  return editor;
}

async function fleetSql(page, statement = SQL) {
  await navigate(page, 'sql');
  await page.locator('#sql-dataset').selectOption('fleet_daily');
  await fillSql(page, statement);
  const result = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/v1/query') && response.request().method() === 'POST',
  );
  await page.locator('#sql-run').click();
  const response = await result;
  expect(response.ok()).toBe(true);
  const packet = await response.json();
  await expect(page.locator('#sql-table tbody tr')).toHaveCount(packet.returned_rows);
  return packet;
}

async function audit(page, testInfo, label) {
  await page.evaluate(axe.source);
  const result = await page.evaluate(() =>
    window.axe.run(document, {
      runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] },
    }),
  );
  await testInfo.attach(`axe-i18n-${label}`, {
    body: JSON.stringify(result, null, 2),
    contentType: 'application/json',
  });
  expect(
    result.violations.map((item) => ({
      id: item.id,
      nodes: item.nodes.map((node) => ({ target: node.target, summary: node.failureSummary })),
    })),
  ).toEqual([]);
}

/** Detect clipped localized actions even when an overflow-hidden panel contains body overflow. */
async function expectSqlActionsContained(page) {
  await expect
    .poll(() =>
      page.locator('.sql-editor-panel').evaluate((panel) => {
        const bounds = panel.getBoundingClientRect();
        return [...panel.querySelectorAll('.editor-footer button')]
          .filter((button) => !button.hidden)
          .every((button) => {
            const action = button.getBoundingClientRect();
            return action.left >= bounds.left && action.right <= bounds.right;
          });
      }),
    )
    .toBe(true);
}

async function preview(page, testInfo, name) {
  const directory = process.env.OUTAGE_I18N_PREVIEW_DIR;
  const destination = directory
    ? path.join(path.resolve(directory), name)
    : testInfo.outputPath(name);
  await fs.mkdir(path.dirname(destination), { recursive: true });
  if (await page.locator('#view-sql').isVisible())
    await page.getByRole('textbox', { name: 'Consulta SQL', exact: true }).press('Escape');
  await page.evaluate(async () => {
    document.activeElement?.blur();
    const editorScroll = document.querySelector('#sql-editor-host .cm-scroller');
    if (editorScroll) editorScroll.scrollLeft = 0;
    window.scrollTo(0, 0);
    await document.fonts.ready;
    await new Promise(requestAnimationFrame);
  });
  await page.screenshot({ path: destination, fullPage: true });
}

test('native language selection translates sign-in and persists through reload, sign-in and logout', async ({
  page,
}, testInfo) => {
  await page.goto('/login/');
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.getByRole('combobox', { name: 'Language', exact: true })).toBeVisible();
  await language(page, 'es');
  await expect(page.getByRole('combobox', { name: 'Idioma', exact: true })).toBeVisible();
  await expect(page.locator('h1')).toContainText(/bienvenida/i);
  await expect(page.getByLabel('Nombre de usuario', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Contraseña', { exact: true })).toBeVisible();
  await expect(page.locator('#login-submit')).toContainText('Iniciar sesión');
  const preference = (await page.context().cookies()).find(
    (cookie) => cookie.name === 'outage_language',
  );
  expect(preference?.value).toBe('es');
  await preview(page, testInfo, 'login-es-desktop.png');
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoHorizontalBodyOverflow(page);
  await audit(page, testInfo, 'login-mobile');
  await preview(page, testInfo, 'login-es-mobile.png');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('lang', 'es');
  await page.locator('#username').fill('viewer');
  await page.locator('#password').fill(FRONTEND_TEST_PASSWORDS.viewer);
  await page.locator('#login-submit').click();
  await expect(page.locator('#overview-content')).toBeVisible();
  await expect(page.locator('#page-title')).toHaveText('Resumen de flota');
  await expect(page.getByRole('button', { name: 'Cerrar sesión', exact: true })).toBeVisible();
  await page.locator('#logout-button').click();
  await expect(page).toHaveURL(/\/login\//);
  await expect(page.locator('html')).toHaveAttribute('lang', 'es');
  await expect(page.locator('#login-submit')).toContainText('Iniciar sesión');
});

test('Spanish exploration, query examples, completion and saved views preserve canonical SQL and source values', async ({
  page,
}) => {
  await spanishLogin(page);
  await navigate(page, 'explore');
  await expect(page.locator('#page-title')).toHaveText('Explorar datos');
  await page.locator('#dataset-select').selectOption('fleet_daily');
  await expect(page.locator('#explore-table tbody tr')).toHaveCount(3);
  await expect(
    page.getByRole('searchbox', { name: 'Filtrar filas cargadas', exact: true }),
  ).toBeVisible();
  await expect(page.locator('#explore-apply')).toContainText('Aplicar consulta');
  await page.locator('#explore-search').fill('2026-09-04');
  await expect(page.locator('#explore-table tbody tr:visible')).toHaveCount(1);
  await page.locator('#explore-search').fill('');
  await page.locator('#explore-advanced > summary').click();
  await page.locator('#explore-add-filter').click();
  const clause = page.locator('[data-filter-index="1"]');
  await clause.locator('[data-filter-part="column"]').selectOption('outage_mw');
  await clause.locator('[data-filter-part="op"]').selectOption('gte');
  await clause.locator('[data-filter-part="value"]').fill('100,5');
  const applied = page.waitForRequest(
    (request) => request.url().endsWith('/api/v1/data/fleet_daily') && request.method() === 'POST',
  );
  await page.locator('#explore-apply').click();
  expect((await applied).postDataJSON().filters).toContainEqual({
    column: 'outage_mw',
    op: 'gte',
    value: '100.5',
  });
  await expect(page.locator('#explore-table tbody tr')).toHaveCount(2);
  await navigate(page, 'sql');
  await expect(page.locator('#page-title')).toHaveText('Consultas SQL');
  await page.locator('#sql-dataset').selectOption('fleet_daily');
  await expect(page.locator('#sql-examples')).toContainText('Consultar capacidad reciente');
  await expect(page.locator('#sql-examples')).not.toContainText('Inspect recent');
  for (const level of ['easy', 'medium', 'hard']) {
    await page.locator(`[data-example-level="${level}"]`).click();
    await expect(page.locator('#sql-editor')).toHaveValue(/FROM "fleet_daily"/);
  }
  const editor = await fillSql(page, 'SELECT cap');
  await editor.press('Control+Space');
  await expect(page.locator('.cm-tooltip-autocomplete')).toContainText('capacity_mw');
  await editor.press('Enter');
  await expect(page.locator('#sql-editor')).toHaveValue('SELECT "capacity_mw"');
  const packet = await fleetSql(page);
  expect(packet.dataset_id).toBe('fleet_daily');
  expect(packet.columns.map((column) => column.name)).toEqual([
    'period',
    'outage_mw',
    'source_status',
  ]);
  expect(packet.rows[0][0]).toBe('2026-09-01');
  expect(String(packet.rows[0][1])).toMatch(/^200(?:\.0+)?$/);
  expect(packet.rows.every((row) => row[2] === 'Active')).toBe(true);
  await expect(page.locator('#sql-table')).toContainText('Active');
  await page.locator('#sql-save-view').click();
  const dialog = page.locator('#saved-view-dialog');
  await expect(dialog.getByLabel('Nombre de la vista', { exact: true })).toBeVisible();
  await expect(
    dialog.getByRole('checkbox', { name: 'Incluir un gráfico temporal', exact: true }),
  ).toBeVisible();
  await dialog.getByLabel('Nombre de la vista', { exact: true }).fill('Active');
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/v1/saved-views') && response.request().method() === 'POST',
  );
  await dialog.getByRole('button', { name: 'Guardar vista', exact: true }).click();
  expect((await (await saved).json()).view.name).toBe('Active');
  await expect(dialog).toBeHidden();
  await navigate(page, 'overview');
  await expect(page.locator('#saved-views-list h3')).toHaveText('Active');
});

test('switching language on SQL preserves the route, authentication, publication and capabilities', async ({
  page,
}) => {
  await login(page, 'analyst');
  await expect(page.locator('#overview-content')).toBeVisible();
  await navigate(page, 'sql');
  const original = await (await page.request.get('/api/v1/auth/session')).json();
  const catalog = await (await page.request.get('/api/v1/catalog')).json();
  const nonce = await page.locator('#sql-editor-host').getAttribute('data-style-nonce');
  await language(page, 'es');
  await expect(page).toHaveURL(/\/#sql$/);
  await expect(page.locator('#page-title')).toHaveText('Consultas SQL');
  await expect(page.locator('#sql-editor-host .cm-content')).toBeVisible();
  const current = await (await page.request.get('/api/v1/auth/session')).json();
  const localized = await (await page.request.get('/api/v1/catalog')).json();
  expect(current.session_scope === original.session_scope).toBe(true);
  expect(current.user.persona).toBe('analyst');
  expect(localized.model_id).toBe(catalog.model_id);
  expect(localized.datasets.map((dataset) => dataset.id).sort()).toEqual(
    catalog.datasets.map((dataset) => dataset.id).sort(),
  );
  expect((await page.locator('#sql-editor-host').getAttribute('data-style-nonce')) !== nonce).toBe(
    true,
  );
  await expect(page.locator('a[data-view="users"], a[data-view="refresh"]')).toHaveCount(0);
  await language(page, 'en');
  await expect(page).toHaveURL(/\/#sql$/);
  await expect(page.locator('#page-title')).toHaveText('SQL workspace');
  await expect(page.getByRole('textbox', { name: 'SQL query', exact: true })).toBeVisible();
});

test('Spanish Admin account dialogs translate labels while usernames and one-time passwords remain unchanged', async ({
  page,
}, testInfo) => {
  await spanishLogin(page);
  await navigate(page, 'users');
  await expect(page.locator('#page-title')).toHaveText('Gestión de usuarios');
  await expect(page.locator('#users-table-body tr')).toHaveCount(3);
  await page.locator('#users-create').click();
  const dialog = page.locator('#user-dialog');
  await expect(dialog.getByRole('heading', { name: 'Crear usuario', exact: true })).toBeVisible();
  await expect(dialog.getByLabel('Nombre de usuario', { exact: true })).toHaveValue('');
  await audit(page, testInfo, 'user-create-desktop');
  await preview(page, testInfo, 'user-create-es-desktop.png');
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoHorizontalBodyOverflow(page);
  await audit(page, testInfo, 'user-create-mobile');
  await preview(page, testInfo, 'user-create-es-mobile.png');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await dialog.getByLabel('Nombre de usuario', { exact: true }).fill('usuario.prueba');
  await dialog
    .getByRole('combobox', { name: 'Nivel de acceso', exact: true })
    .selectOption('analyst');
  await dialog.getByRole('button', { name: 'Crear usuario', exact: true }).click();
  await expect(
    dialog.getByRole('heading', { name: 'Copiar la nueva contraseña', exact: true }),
  ).toBeVisible();
  const password = dialog.getByLabel('Contraseña generada', { exact: true });
  await expect(password).toHaveAttribute('type', 'password');
  expect(/^[A-Za-z0-9_-]{32}$/.test(await password.inputValue())).toBe(true);
  await dialog.getByRole('button', { name: 'Ya guardé la contraseña', exact: true }).click();
  await expect(dialog).toBeHidden();
  await page
    .getByRole('button', { name: 'Administrar usuario usuario.prueba', exact: true })
    .click();
  await expect(
    dialog.getByRole('heading', { name: 'Administrar usuario', exact: true }),
  ).toBeVisible();
  await dialog.getByRole('button', { name: 'Restablecer contraseña', exact: true }).click();
  await dialog.getByRole('button', { name: 'Generar nueva contraseña', exact: true }).click();
  await expect(dialog.getByLabel('Contraseña generada', { exact: true })).toHaveAttribute(
    'type',
    'password',
  );
  await dialog.getByRole('button', { name: 'Ya guardé la contraseña', exact: true }).click();
  await expect(page.locator('.user-generated-password')).toHaveCount(0);
});

test('Spanish Viewer permissions and error prose do not translate protocol names or grant Admin access', async ({
  page,
}) => {
  await spanishLogin(page, 'viewer');
  await expect(
    page.locator('a[data-view="users"], a[data-view="refresh"], #view-users, #user-dialog'),
  ).toHaveCount(0);
  await expect(page.locator('#saved-views-status')).toHaveText(
    'No hay vistas compartidas disponibles para su cuenta.',
  );
  const forbidden = await page.request.get('/api/v1/users');
  expect(forbidden.status()).toBe(403);
  const problem = await forbidden.json();
  expect(problem.code).toMatch(/admin/);
  expect(problem.detail).not.toMatch(/requires an Admin|requires Admin/);
  await navigate(page, 'sql');
  await fillSql(page, 'DELETE FROM fleet_daily');
  const incoming = page.waitForResponse((response) => response.url().endsWith('/api/v1/query'));
  await page.locator('#sql-run').click();
  const error = await incoming;
  expect(error.status()).toBe(422);
  expect((await error.json()).code).toBe('unsupported_sql');
  await expect(page.locator('#sql-error')).toContainText(/SELECT|consulta/);
  await expect(page.locator('#sql-error')).not.toContainText('SQL uses a feature outside');
  await page.goto('/#users');
  await expect(page.locator('#view-denied')).toContainText('Acceso no permitido');
});

test('Spanish workspace remains accessible and readable at desktop, 390px and 320px without unsafe fixture previews', async ({
  page,
}, testInfo) => {
  await spanishLogin(page);
  const views = ['overview', 'explore', 'sql', 'refresh', 'users'];
  for (const view of views) {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await navigate(page, view);
    if (view === 'explore') {
      await page.locator('#dataset-select').selectOption('fleet_daily');
      await expect(page.locator('#explore-table tbody tr')).toHaveCount(3);
    }
    if (view === 'sql') {
      await fleetSql(
        page,
        'SELECT period, capacity_mw, outage_mw\nFROM fleet_daily\nORDER BY period',
      );
      await expectSqlActionsContained(page);
    }
    if (view === 'refresh') {
      await expect(page.locator('#refresh-start')).toContainText('Poner actualización en cola');
      await expect(page.locator('label[for="language-select"]')).toHaveText('Idioma');
    }
    if (view === 'users') await expect(page.locator('#users-table-body tr')).toHaveCount(3);
    if (view === 'overview') await expectChartAxisContrast(page, testInfo, 'es-desktop');
    await audit(page, testInfo, `${view}-desktop`);
    await preview(page, testInfo, `${view}-es-desktop.png`);
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await expectNoHorizontalBodyOverflow(page);
      if (view === 'sql') await expectSqlActionsContained(page);
      if (view === 'overview') {
        await expectChartAxisContrast(page, testInfo, `es-${width}`);
        for (const id of ['overview-start', 'overview-end']) {
          const font = await page
            .locator(`#${id}`)
            .evaluate((node) => Number.parseFloat(getComputedStyle(node).fontSize));
          expect(font).toBeGreaterThanOrEqual(16);
        }
      }
      await audit(page, testInfo, `${view}-${width}`);
      await preview(page, testInfo, `${view}-es-${width}.png`);
    }
  }
});
