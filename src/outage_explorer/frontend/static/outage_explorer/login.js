/**
 * @module login
 * Authenticate through the same-origin service API from the public shell.
 *
 * Submission waits for CSRF bootstrap. Credentials travel only in a JSON POST;
 * login rotates the in-memory token and retires prior browser query history.
 * HTML routes are shells in the same Django project, not a separate server.
 */
import { ApiClient } from './api.js';
import { byId, showError, visible, setText } from './dom.js';
import { clearPrivateHistory } from './sql.js';

const client = new ApiClient();
const form = byId('login-form');
let ready = false;
const reason = new URLSearchParams(location.search).get('reason');
const reasonMessage =
  reason === 'expired'
    ? 'Your session ended. Sign in to continue.'
    : reason === 'permissions'
      ? 'Your access changed. Sign in with an authorized account.'
      : reason === 'signed-out'
        ? 'You have been signed out.'
        : reason === 'password-reset'
          ? 'Your password was reset. Sign in with your new password.'
          : '';
/**
 * Bootstrap the CSRF token before enabling login; leave failures explicitly retryable.
 */
async function prepare() {
  byId('login-submit').disabled = true;
  visible('login-error', false);
  setText('login-status', 'Preparing secure sign-in…');
  try {
    await client.bootstrap();
    ready = true;
    byId('login-submit').disabled = false;
    setText('login-status', reasonMessage);
  } catch (error) {
    showError('login-error', error, prepare);
    setText('login-status', '');
  }
}

/** Toggle password visibility without changing its value or submitting credentials. */
byId('login-toggle-password').addEventListener('click', () => {
  const input = byId('password'),
    showing = input.type === 'password';
  input.type = showing ? 'text' : 'password';
  byId('login-toggle-password').textContent = showing ? 'Hide' : 'Show';
  byId('login-toggle-password').setAttribute('aria-pressed', showing ? 'true' : 'false');
});

/** Authenticate through JSON POST, retire old history on success, and restore retryable form state. */
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!ready || byId('login-submit').disabled) return;
  visible('login-error', false);
  byId('login-submit').disabled = true;
  form.setAttribute('aria-busy', 'true');
  setText('login-status', 'Signing in…');
  try {
    const result = await client.request('/auth/login', {
      method: 'POST',
      body: { username: byId('username').value, password: byId('password').value },
    });
    client.csrf = result.csrf_token;
    byId('password').value = '';
    clearPrivateHistory();
    try {
      const channel = new BroadcastChannel('outage-explorer-auth');
      channel.postMessage({ kind: 'signed-in' });
      channel.close();
    } catch {
      /* Focus checks cover unsupported browsers. */
    }
    location.replace('/');
  } catch (error) {
    showError('login-error', error);
    setText('login-status', '');
    if (error.status === 401) {
      byId('password').value = '';
      byId('password').focus();
    }
    byId('login-submit').disabled = false;
  } finally {
    form.setAttribute('aria-busy', 'false');
  }
});
prepare();
