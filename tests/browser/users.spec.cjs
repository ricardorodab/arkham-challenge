'use strict';

const axe = require('axe-core');
const fs = require('node:fs/promises');
const path = require('node:path');
const { test, expect, login, navigate, expectNoHorizontalBodyOverflow } = require('./helpers.cjs');

async function adminDirectory(page) {
  await login(page, 'admin');
  await expect(page.locator('#overview-content')).toBeVisible();
  await navigate(page, 'users');
  await expect(page.locator('#users-table-body tr')).toHaveCount(3);
}

async function signIn(page, username, password) {
  await page.goto('/login/');
  await page.getByLabel('Username', { exact: true }).fill(username);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await expect(page.locator('#login-submit')).toBeEnabled();
  await page.locator('#login-submit').click();
  await expect(page).toHaveURL(/\/(?:#.*)?$/);
}

async function createUser(page, username, role = 'analyst') {
  await page.locator('#users-create').click();
  const dialog = page.locator('#user-dialog');
  await dialog.getByLabel('Username', { exact: true }).fill(username);
  await dialog.getByLabel('First name (optional)', { exact: true }).fill('Casey');
  await dialog.getByLabel('Last name (optional)', { exact: true }).fill('Reed');
  await dialog.getByRole('combobox', { name: 'Access level', exact: true }).selectOption(role);
  const response = page.waitForResponse(
    (r) => r.url().endsWith('/api/v1/users') && r.request().method() === 'POST',
  );
  await dialog.getByRole('button', { name: 'Create user', exact: true }).click();
  const packet = await (await response).json();
  await expect(
    dialog.getByRole('heading', { name: 'Copy the new password', exact: true }),
  ).toBeVisible();
  const password = await dialog.getByLabel('Generated password', { exact: true }).inputValue();
  expect(/^[A-Za-z0-9_-]{32}$/.test(password), 'Generated password has the expected format').toBe(
    true,
  );
  expect(password === packet.generated_password, 'The one-time input matches its response').toBe(
    true,
  );
  await expect(dialog.getByLabel('Generated password', { exact: true })).toHaveAttribute(
    'type',
    'password',
  );
  await dialog.getByRole('button', { name: 'Show password', exact: true }).click();
  await expect(dialog.getByLabel('Generated password', { exact: true })).toHaveAttribute(
    'type',
    'text',
  );
  await dialog.getByRole('button', { name: 'Hide password', exact: true }).click();
  await dialog.getByRole('button', { name: 'I saved the password', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.locator('.user-generated-password')).toHaveCount(0);
  const storage = await page.evaluate(() =>
    JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }),
  );
  expect(storage.includes(password), 'Generated passwords must not enter browser storage').toBe(
    false,
  );
  return { ...packet.user, password };
}

async function manage(page, username) {
  await page.getByRole('button', { name: `Manage user ${username}`, exact: true }).click();
  const dialog = page.locator('#user-dialog');
  await expect(dialog.getByRole('heading', { name: 'Manage user', exact: true })).toBeVisible();
  return dialog;
}

async function audit(page, testInfo, label) {
  await page.evaluate(axe.source);
  const result = await page.evaluate(() =>
    window.axe.run(document, {
      runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] },
    }),
  );
  await testInfo.attach(`axe-users-${label}`, {
    body: JSON.stringify(result),
    contentType: 'application/json',
  });
  expect(
    result.violations.map((item) => ({
      id: item.id,
      nodes: item.nodes.map((node) => node.target),
    })),
  ).toEqual([]);
}

async function preview(page, testInfo, filename) {
  const directory = process.env.OUTAGE_USERS_PREVIEW_DIR;
  const destination = directory ? path.join(directory, filename) : testInfo.outputPath(filename);
  if (directory) await fs.mkdir(directory, { recursive: true });
  await page.evaluate(() => {
    document.activeElement?.blur();
    window.scrollTo(0, 0);
  });
  await page.screenshot({ path: destination, fullPage: true });
}

test('Admin creates, renames, changes access, resets passwords and deletes an account', async ({
  page,
}) => {
  await adminDirectory(page);
  const user = await createUser(page, 'casey.reed');
  let dialog = await manage(page, user.username);
  await dialog.getByLabel('Username', { exact: true }).fill('casey.updated');
  await dialog.getByLabel('First name (optional)', { exact: true }).fill('Casey Updated');
  await dialog.getByRole('combobox', { name: 'Access level', exact: true }).selectOption('viewer');
  await dialog.getByRole('combobox', { name: 'Account status', exact: true }).selectOption('false');
  await dialog.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const row = page.locator('#users-table-body tr').filter({ hasText: 'casey.updated' });
  await expect(row).toContainText('Casey Updated Reed');
  await expect(row).toContainText('Viewer');
  await expect(row).toContainText('Inactive');
  dialog = await manage(page, 'casey.updated');
  await dialog.getByRole('combobox', { name: 'Account status', exact: true }).selectOption('true');
  await dialog.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(row).toContainText('Active');
  dialog = await manage(page, 'casey.updated');
  await dialog.getByRole('button', { name: 'Reset password', exact: true }).click();
  await dialog.getByRole('button', { name: 'Generate new password', exact: true }).click();
  const generated = dialog.getByLabel('Generated password', { exact: true });
  await expect(generated).toBeVisible();
  const replacement = await generated.inputValue();
  expect(replacement !== user.password, 'A reset replaces the previous password').toBe(true);
  await dialog.getByRole('button', { name: 'I saved the password', exact: true }).click();
  const readerContext = await page
    .context()
    .browser()
    .newContext({ baseURL: 'http://127.0.0.1:8766' });
  try {
    const reader = await readerContext.newPage();
    await signIn(reader, 'casey.updated', replacement);
    await expect(reader.locator('.account-persona')).toContainText('Viewer');
    await expect(reader.locator('a[data-view="users"]')).toHaveCount(0);
    dialog = await manage(page, 'casey.updated');
    await dialog.getByRole('button', { name: 'Delete user', exact: true }).click();
    await expect(dialog.getByRole('group', { name: 'Confirm user deletion' })).toContainText(
      'cannot be undone',
    );
    await dialog.getByRole('button', { name: 'Permanently delete user', exact: true }).click();
    await expect(dialog).not.toBeVisible();
    await expect(row).toHaveCount(0);
    expect((await reader.request.get('/api/v1/auth/session')).status()).toBe(401);
  } finally {
    await readerContext.close();
  }
});

test('non-Admins cannot reach account management, even through direct requests or hash navigation', async ({
  page,
}) => {
  await login(page, 'analyst');
  await expect(page.locator('a[data-view="users"]')).toHaveCount(0);
  await expect(page.locator('#view-users')).toHaveCount(0);
  expect((await page.request.get('/api/v1/users')).status()).toBe(403);
  await page.goto('/#users');
  await expect(page.locator('#view-denied')).toContainText('Not allowed');
  await expect(page.locator('#user-dialog')).toHaveCount(0);
});

test('Admin profile edits preserve the workspace and self-reset reveals credentials before requiring sign-in', async ({
  page,
}) => {
  await adminDirectory(page);
  let dialog = await manage(page, 'admin');
  await expect(dialog.getByRole('combobox', { name: 'Access level', exact: true })).toBeDisabled();
  await expect(
    dialog.getByRole('combobox', { name: 'Account status', exact: true }),
  ).toBeDisabled();
  await expect(dialog.getByRole('button', { name: 'Delete user', exact: true })).toBeDisabled();
  await dialog.getByLabel('Username', { exact: true }).fill('workspace.admin');
  await dialog.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(page.locator('.account-name')).toHaveText('workspace.admin');
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.locator('#app-root')).toBeVisible();
  expect((await page.request.get('/api/v1/auth/session')).status()).toBe(200);
  dialog = await manage(page, 'workspace.admin');
  await dialog.getByRole('button', { name: 'Reset password', exact: true }).click();
  await dialog.getByRole('button', { name: 'Generate new password', exact: true }).click();
  const input = dialog.getByLabel('Generated password', { exact: true });
  await expect(input).toBeVisible();
  const replacement = await input.inputValue();
  expect((await page.request.get('/api/v1/auth/session')).status()).toBe(401);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect
    .poll(async () => (await input.inputValue()) === replacement, {
      message: 'Focus must retain the one-time self-reset credential until acknowledgement',
    })
    .toBe(true);
  await dialog.getByRole('button', { name: 'I saved it · Sign in again', exact: true }).click();
  await expect(page).toHaveURL(/\/login\/\?reason=password-reset$/);
  await expect(page.locator('.user-generated-password')).toHaveCount(0);
  await signIn(page, 'workspace.admin', replacement);
  await expect(page.locator('a[data-view="users"]')).toBeVisible();
});

test('account directory and dialogs are accessible, responsive and clear one-time credentials on blur', async ({
  page,
}, testInfo) => {
  await adminDirectory(page);
  await audit(page, testInfo, 'directory-desktop');
  await preview(page, testInfo, 'users-desktop.png');
  await page.locator('#users-create').click();
  const dialog = page.locator('#user-dialog');
  await expect(dialog.getByLabel('Username', { exact: true })).toHaveValue('');
  await audit(page, testInfo, 'create-desktop');
  await preview(page, testInfo, 'user-create-desktop.png');
  await dialog.press('Escape');
  await expect(dialog).not.toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expectNoHorizontalBodyOverflow(page);
  await audit(page, testInfo, 'directory-mobile');
  await preview(page, testInfo, 'users-mobile.png');
  await page.locator('#users-create').click();
  await expect(dialog.getByLabel('Username', { exact: true })).toBeFocused();
  await expectNoHorizontalBodyOverflow(page);
  await audit(page, testInfo, 'create-mobile');
  await preview(page, testInfo, 'user-create-mobile.png');
  await dialog.press('Escape');
  await expect(dialog).not.toBeVisible();
  await expect(page.locator('#users-create')).toBeFocused();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await createUser(page, 'transient.password');
  await manage(page, 'transient.password');
  await dialog.getByRole('button', { name: 'Reset password', exact: true }).click();
  await dialog.getByRole('button', { name: 'Generate new password', exact: true }).click();
  await expect(dialog.getByLabel('Generated password', { exact: true })).toBeVisible();
  await audit(page, testInfo, 'credentials');
  await page.evaluate(() => window.dispatchEvent(new Event('blur')));
  await expect(dialog).not.toBeVisible();
  await expect(page.locator('.user-generated-password')).toHaveCount(0);
});

test('a delayed account-creation reply stays out of the background DOM and resumes masked after session verification', async ({
  page,
}) => {
  await adminDirectory(page);
  let releaseCreate, releaseSession, reportCreated, reportSession;
  const createGate = new Promise((resolve) => {
    releaseCreate = resolve;
  });
  const sessionGate = new Promise((resolve) => {
    releaseSession = resolve;
  });
  const created = new Promise((resolve) => {
    reportCreated = resolve;
  });
  const sessionRequested = new Promise((resolve) => {
    reportSession = resolve;
  });
  let generatedPassword = '';
  let sessionChecks = 0;

  /** Return exposure booleans only, keeping fixture credentials out of assertion output. */
  async function noCredentialExposure() {
    const exposure = await page.evaluate(
      (secret) => ({
        markup: document.documentElement.outerHTML.includes(secret),
        inputs: [...document.querySelectorAll('input')].some((input) => input.value === secret),
        storage: JSON.stringify({
          local: { ...localStorage },
          session: { ...sessionStorage },
        }).includes(secret),
      }),
      generatedPassword,
    );
    expect(exposure).toEqual({ markup: false, inputs: false, storage: false });
  }

  await page.route(
    (url) => url.pathname === '/api/v1/users',
    async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      const response = await route.fetch();
      const packet = await response.json();
      generatedPassword = packet.generated_password;
      reportCreated();
      await createGate;
      await route.fulfill({ response });
    },
  );
  try {
    await page.locator('#users-create').click();
    const dialog = page.locator('#user-dialog');
    await dialog.getByLabel('Username', { exact: true }).fill('background.reply');
    const returned = page.waitForResponse(
      (response) =>
        response.url().endsWith('/api/v1/users') && response.request().method() === 'POST',
    );
    await dialog.getByRole('button', { name: 'Create user', exact: true }).click();
    await created;
    expect(typeof generatedPassword === 'string' && generatedPassword.length === 32).toBe(true);
    await page.evaluate(() => window.dispatchEvent(new Event('blur')));
    releaseCreate();
    await returned;
    await expect(page.locator('#global-notice')).toContainText('A generated password is ready.');
    await expect(page.locator('.user-generated-password')).toHaveCount(0);
    await noCredentialExposure();

    // Hold both the workspace and credential-resume session checks. The secret
    // must remain absent until a fresh foreground identity has been verified.
    await page.route(
      (url) => url.pathname === '/api/v1/auth/session',
      async (route) => {
        sessionChecks++;
        reportSession();
        await sessionGate;
        await route.continue();
      },
    );
    await page.bringToFront();
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await sessionRequested;
    await expect(page.locator('.user-generated-password')).toHaveCount(0);
    await noCredentialExposure();
    releaseSession();
    const input = dialog.getByLabel('Generated password', { exact: true });
    await expect(input).toBeVisible();
    await expect(input).toHaveAttribute('type', 'password');
    await expect.poll(async () => (await input.inputValue()) === generatedPassword).toBe(true);
    expect(sessionChecks).toBeGreaterThanOrEqual(1);
    await dialog.getByRole('button', { name: 'I saved the password', exact: true }).click();
    await expect(page.locator('.user-generated-password')).toHaveCount(0);
    await noCredentialExposure();
    await expect(
      page.getByRole('button', { name: 'Manage user background.reply', exact: true }),
    ).toBeVisible();
  } finally {
    releaseCreate();
    releaseSession();
  }
});
