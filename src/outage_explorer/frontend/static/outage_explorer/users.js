/**
 * @module users
 * Admin account management with bounded server paging and transient credentials.
 *
 * Product authorization, identity freshness and mutation versions are enforced by
 * the service. Generated passwords are never persisted. A background reply stays
 * briefly in memory until a verified foreground session can display it; displayed
 * values are removed when the credential dialog closes or loses window focus.
 */
import { t, getLocale } from './i18n.js';
import { queryString } from './api.js';
import { byId, element, setText, visible } from './dom.js';

const ROLES = [
  ['viewer', 'Viewer'],
  ['analyst', 'Analyst'],
  ['admin', 'Admin'],
];
const PAGE_SIZE = 25;
const PENDING_CREDENTIAL_LIFETIME_MS = 5 * 60 * 1000;

/** Create a native action button without interpreting external HTML. */
function button(label, action, className = 'button button-secondary button-small') {
  const node = element('button', className, label);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

/** Display a safe API failure in the supplied live error container. */
function failure(node, error) {
  node.replaceChildren(
    element('strong', '', t('Unable to complete this action')),
    element('p', '', error.message || t('Please refresh the list and try again.')),
  );
  if (error.requestId)
    node.append(
      element(
        'p',
        'request-reference',
        t('Reference: {reference}', { reference: error.requestId }),
      ),
    );
  node.hidden = false;
}

/** Construct a labeled native text field with a stable accessible name. */
function textField(label, name, value = '', { required = false, maxLength = 150 } = {}) {
  const wrapper = element('div', 'field');
  const caption = element('label', '', label);
  const input = element('input');
  input.id = `user-${name}`;
  caption.htmlFor = input.id;
  input.type = 'text';
  input.name = name;
  input.value = value;
  input.required = required;
  input.maxLength = maxLength;
  input.autocomplete = 'off';
  input.spellcheck = false;
  wrapper.append(caption, input);
  return { wrapper, input };
}

/** Format an account's optional display name without modifying its login name. */
function displayName(user) {
  return [user.first_name, user.last_name].filter(Boolean).join(' ') || '—';
}

/** Format one server timestamp; absent sign-ins are explicitly distinguished. */
function signInLabel(value) {
  if (!value) return t('Never signed in');
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return t('Unavailable');
  return new Intl.DateTimeFormat(getLocale(), { dateStyle: 'medium' }).format(parsed);
}

/**
 * Own the Admin account list, mutation dialogs and one-time credential lifecycle.
 * Pending credentials have a bounded in-memory lifetime and never enter browser
 * storage. Password values enter the DOM only in a foreground credential dialog.
 */
export class UsersController {
  constructor(context) {
    this.context = context;
    this.disposed = false;
    this.generation = 0;
    this.dialogGeneration = 0;
    this.cursors = [null];
    this.page = 0;
    this.search = '';
    this.loaded = false;
    this.loading = false;
    this.busy = false;
    this.credentialOpen = false;
    this.awaitingSelfPasswordReset = false;
    this.pendingCredentials = null;
    this.pendingCredentialTimer = null;
    this.pendingCredentialResume = null;
    this.programmaticCloseEvents = 0;
    this.foreground = !document.hidden && document.hasFocus();
    this.dialog = byId('user-dialog');
    this.returnFocus = null;
    this.dialog?.addEventListener('cancel', (event) => {
      event.preventDefault();
      if (!this.busy) this.closeDialog();
    });
    this.dialog?.addEventListener('close', () => {
      // Programmatic transitions may leave a background reply pending instead
      // of opening its replacement dialog immediately. Ignore their queued event.
      if (this.programmaticCloseEvents) {
        this.programmaticCloseEvents--;
        return;
      }
      if (!this.dialog.open) this.clearCredentials();
    });
    byId('users-create')?.addEventListener('click', () => this.openEditor());
    byId('users-search-form')?.addEventListener('submit', (event) => {
      event.preventDefault();
      this.search = byId('users-search').value.trim();
      this.cursors = [null];
      this.page = 0;
      this.load();
    });
    byId('users-reload')?.addEventListener('click', () => {
      this.cursors = [null];
      this.page = 0;
      this.load();
    });
    byId('users-prev')?.addEventListener('click', () => {
      if (this.page > 0) {
        this.page--;
        this.load();
      }
    });
    byId('users-next')?.addEventListener('click', () => {
      if (this.cursors[this.page + 1]) {
        this.page++;
        this.load();
      }
    });
    this.onBlur = () => {
      this.foreground = false;
      if (this.credentialOpen) this.clearCredentials();
    };
    this.onFocus = () => {
      this.foreground = !document.hidden && document.hasFocus();
      this.resumeCredentials();
    };
    this.onVisibility = () => {
      this.foreground = !document.hidden && document.hasFocus();
      if (document.hidden) {
        if (this.credentialOpen) this.clearCredentials();
      } else this.resumeCredentials();
    };
    window.addEventListener('blur', this.onBlur);
    window.addEventListener('focus', this.onFocus);
    document.addEventListener('visibilitychange', this.onVisibility);
  }

  /** Reject late results from a retired view, request generation or session. */
  current(generation = this.generation) {
    return (
      !this.disposed &&
      this.context.alive() &&
      this.context.isView('users') &&
      generation === this.generation
    );
  }

  /** Load the current directory page when the Admin view becomes active. */
  activate() {
    if (!this.loading && this.current()) this.load();
  }

  /** Query one bounded directory page; navigation never retains a full user list. */
  async load() {
    const generation = ++this.generation;
    if (!this.current(generation) || this.awaitingSelfPasswordReset) return;
    this.loading = true;
    byId('users-content').setAttribute('aria-busy', 'true');
    visible('users-loading');
    visible('users-error', false);
    byId('users-prev').disabled = byId('users-next').disabled = true;
    setText('users-status', t('Loading accounts…'));
    try {
      const packet = await this.context.client.request(
        `/users${queryString({ search: this.search, page_size: PAGE_SIZE, cursor: this.cursors[this.page] })}`,
      );
      if (!this.current(generation)) return;
      this.cursors.length = this.page + 1;
      if (packet.next_cursor) this.cursors.push(packet.next_cursor);
      this.loaded = true;
      this.render(packet.users);
      setText(
        'users-status',
        t(
          packet.users.length === 1
            ? '{count} account on this page{search}.'
            : '{count} accounts on this page{search}.',
          { count: packet.users.length, search: this.search ? t(' · Search applied') : '' },
        ),
      );
      setText('users-page', t('Page {page}', { page: this.page + 1 }));
      byId('users-prev').disabled = this.page === 0;
      byId('users-next').disabled = !packet.next_cursor;
    } catch (error) {
      if (!this.current(generation)) return;
      failure(byId('users-error'), error);
      setText('users-status', t('The account list could not be loaded.'));
    } finally {
      if (generation === this.generation) this.loading = false;
      if (this.current(generation)) {
        visible('users-loading', false);
        byId('users-content').setAttribute('aria-busy', 'false');
      }
    }
  }

  /** Render server fields as text and bind actions to their exact mutation version. */
  render(users) {
    const tbody = byId('users-table-body');
    const focusedUser = tbody.contains(document.activeElement)
      ? document.activeElement.closest('[data-user-id]')?.dataset.userId
      : null;
    tbody.replaceChildren();
    visible('users-empty', users.length === 0);
    visible('users-table-wrap', users.length > 0);
    for (const user of users) {
      const row = element('tr');
      row.dataset.userId = String(user.id);
      const account = element('td');
      const identity = element('div', 'user-identity');
      identity.append(element('strong', '', user.username));
      if (user.is_self) identity.append(element('span', 'badge', t('You')));
      account.append(identity);
      const name = element('td', '', displayName(user));
      const role = element('td');
      role.append(
        element(
          'span',
          'user-role',
          t(ROLES.find(([value]) => value === user.role)?.[1] || user.role),
        ),
      );
      const status = element('td');
      const badge = element(
        'span',
        'badge user-status',
        user.is_active ? t('Active') : t('Inactive'),
      );
      badge.dataset.active = String(user.is_active);
      status.append(badge);
      const lastSignIn = element('td', 'user-last-sign-in', signInLabel(user.last_login));
      const actions = element('td', 'user-row-actions');
      const edit = button(t('Manage user'), () => this.openEditor(user));
      edit.setAttribute('aria-label', t('Manage user {username}', { username: user.username }));
      actions.append(edit);
      row.append(account, name, role, status, lastSignIn, actions);
      tbody.append(row);
    }
    if (focusedUser) {
      const row = [...tbody.children].find((node) => node.dataset.userId === focusedUser);
      const target = row?.querySelector('button') || byId('users-status');
      if (!row) target.tabIndex = -1;
      target.focus({ preventScroll: true });
    }
  }

  /** Open a create or version-bound edit form with explicit account actions. */
  openEditor(user = null) {
    if (!this.current() || this.busy) return;
    if (this.pendingCredentials) {
      this.resumeCredentials();
      return;
    }
    this.closeDialog();
    this.returnFocus = document.activeElement;
    const dialogGeneration = ++this.dialogGeneration;
    const form = element('form', 'user-editor-form');
    const title = element('h2', '', user ? t('Manage user') : t('Create user'));
    title.id = 'user-dialog-title';
    const description = element(
      'p',
      'user-dialog-description',
      user
        ? t('Manage the account “{username}”. Role and status changes take effect immediately.', {
            username: user.username,
          })
        : t(
            'Choose an account name and access level. A secure random password will be generated when the account is created.',
          ),
    );
    const error = element('div', 'notice notice-error');
    error.setAttribute('role', 'alert');
    error.hidden = true;
    const fields = element('div', 'user-editor-fields');
    const username = textField(t('Username'), 'username', user?.username || '', { required: true });
    username.input.autocapitalize = 'none';
    username.input.setAttribute('aria-describedby', 'user-username-help');
    const usernameHelp = element(
      'p',
      'user-field-help',
      t('Used to sign in. Up to 150 characters; letters, numbers and @ . + - _ are supported.'),
    );
    usernameHelp.id = 'user-username-help';
    username.wrapper.append(usernameHelp);
    username.wrapper.classList.add('user-field-wide');
    const firstName = textField(t('First name (optional)'), 'first_name', user?.first_name || '');
    const lastName = textField(t('Last name (optional)'), 'last_name', user?.last_name || '');
    const roleField = element('div', 'field');
    const roleLabel = element('label', '', t('Access level'));
    const role = element('select');
    role.id = 'user-role';
    roleLabel.htmlFor = role.id;
    role.name = 'role';
    for (const [value, label] of ROLES) {
      const option = element('option', '', t(label));
      option.value = value;
      role.append(option);
    }
    role.value = user?.role || 'viewer';
    role.disabled = !!user?.is_self;
    roleField.append(roleLabel, role);
    const statusField = element('div', 'field');
    const statusLabel = element('label', '', t('Account status'));
    const status = element('select');
    status.id = 'user-active';
    statusLabel.htmlFor = status.id;
    status.name = 'is_active';
    for (const [value, label] of [
      ['true', t('Active')],
      ['false', t('Inactive')],
    ]) {
      const option = element('option', '', t(label));
      option.value = value;
      status.append(option);
    }
    status.value = String(user?.is_active ?? true);
    status.disabled = !!user?.is_self;
    statusField.append(statusLabel, status);
    fields.append(username.wrapper, firstName.wrapper, lastName.wrapper, roleField, statusField);
    const roleHelp = element(
      'p',
      'user-field-help user-field-wide',
      t(
        'Viewers explore national data and permitted shared views. Analysts create queries and saved views. Admins also manage accounts and refresh data.',
      ),
    );
    fields.append(roleHelp);
    if (user?.is_self)
      fields.append(
        element(
          'p',
          'user-field-help user-field-wide',
          t(
            'Your own access level and active status are protected. Another Admin can change them.',
          ),
        ),
      );
    const controls = element('div', 'user-dialog-actions');
    const cancel = button(t('Cancel'), () => this.closeDialog());
    const save = element('button', 'button', user ? t('Save changes') : t('Create user'));
    save.type = 'submit';
    controls.append(cancel, save);
    form.append(title, description, error, fields, controls);
    this.dialog.append(form);
    if (user) this.addAccountActions(user, dialogGeneration);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (this.busy || !this.current() || dialogGeneration !== this.dialogGeneration) return;
      const payload = {
        username: username.input.value.trim(),
        first_name: firstName.input.value.trim(),
        last_name: lastName.input.value.trim(),
        role: role.value,
        is_active: status.value === 'true',
        ...(user ? { version: user.version } : {}),
      };
      this.setBusy(true);
      error.hidden = true;
      try {
        const packet = await this.context.client.request(user ? `/users/${user.id}` : '/users', {
          method: user ? 'PUT' : 'POST',
          body: payload,
        });
        if (!this.current() || dialogGeneration !== this.dialogGeneration) return;
        this.setBusy(false);
        this.closeDialog();
        if (packet.user.is_self) this.context.onSelfUserUpdated?.(packet.user);
        if (packet.generated_password) this.showCredentials(packet);
        else
          this.context.notice?.(
            t('Account “{username}” updated.', { username: packet.user.username }),
          );
        this.cursors = [null];
        this.page = 0;
        this.load();
      } catch (problem) {
        if (!this.current() || dialogGeneration !== this.dialogGeneration) return;
        this.setBusy(false);
        failure(error, problem);
      }
    });
    this.dialog.showModal();
    username.input.focus();
  }

  /** Keep password resets and permanent deletion separate from profile edits. */
  addAccountActions(user, dialogGeneration) {
    const section = element('section', 'user-account-actions');
    const heading = element('h3', '', t('Account actions'));
    section.append(
      heading,
      element(
        'p',
        'user-field-help',
        t(
          'Password resets sign this account out of existing sessions. Deletion also removes its saved views and cached results; refresh history is retained.',
        ),
      ),
    );
    const actions = element('div', 'user-account-action-buttons');
    const confirmation = element('div', 'user-action-confirm');
    confirmation.hidden = true;
    confirmation.setAttribute('role', 'group');
    const reset = button(t('Reset password'), () =>
      this.confirmAction('password', user, confirmation, dialogGeneration),
    );
    const remove = button(
      t('Delete user'),
      () => this.confirmAction('delete', user, confirmation, dialogGeneration),
      'button button-danger button-small',
    );
    remove.disabled = !!user.is_self;
    if (user.is_self) remove.title = t('You cannot delete the account you are signed in with.');
    actions.append(reset, remove);
    section.append(actions, confirmation);
    this.dialog.append(section);
  }

  /** Require a second explicit action before resetting credentials or deleting an account. */
  confirmAction(kind, user, container, dialogGeneration) {
    if (this.busy || !this.current()) return;
    const returnFocus = document.activeElement;
    const deleting = kind === 'delete';
    container.replaceChildren();
    container.hidden = false;
    container.setAttribute(
      'aria-label',
      deleting ? t('Confirm user deletion') : t('Confirm password reset'),
    );
    const summary = element(
      'p',
      '',
      deleting
        ? t(
            'Permanently delete “{username}”? This action cannot be undone. Accounts with an active refresh job cannot be deleted until the job finishes.',
            { username: user.username },
          )
        : t(
            'Generate a new password for “{username}”? The current password will stop working.{selfNotice}',
            {
              username: user.username,
              selfNotice: user.is_self
                ? t(
                    ' Copy the new password before closing the next dialog; you will need to sign in again.',
                  )
                : '',
            },
          ),
    );
    const error = element('div', 'notice notice-error');
    error.setAttribute('role', 'alert');
    error.hidden = true;
    const actions = element('div', 'user-dialog-actions');
    const cancel = button(t('Keep account unchanged'), () => {
      container.hidden = true;
      container.replaceChildren();
      if (returnFocus?.isConnected) returnFocus.focus();
    });
    const confirm = button(
      deleting ? t('Permanently delete user') : t('Generate new password'),
      async () => {
        if (this.busy || !this.current() || dialogGeneration !== this.dialogGeneration) return;
        this.setBusy(true);
        error.hidden = true;
        if (!deleting && user.is_self) this.awaitingSelfPasswordReset = true;
        try {
          const packet = await this.context.client.request(
            `/users/${user.id}${deleting ? '' : '/password'}`,
            {
              method: deleting ? 'DELETE' : 'POST',
              body: { version: user.version },
            },
          );
          if (!this.current() || dialogGeneration !== this.dialogGeneration) return;
          this.setBusy(false);
          // Retain the self-reset marker until the one-time credentials are acknowledged.
          this.closeDialog({ acknowledge: false });
          if (!deleting) this.showCredentials(packet);
          else
            this.context.notice?.(t('Account “{username}” deleted.', { username: user.username }));
          if (!this.awaitingSelfPasswordReset) {
            this.cursors = [null];
            this.page = 0;
            this.load();
          }
        } catch (problem) {
          this.awaitingSelfPasswordReset = false;
          if (user.is_self && !deleting) this.context.onSessionCheckRequired?.();
          if (!this.current() || dialogGeneration !== this.dialogGeneration) return;
          this.setBusy(false);
          failure(error, problem);
        }
      },
      deleting ? 'button button-danger' : 'button',
    );
    actions.append(cancel, confirm);
    container.append(summary, error, actions);
    cancel.focus();
  }

  /** Disable all dialog actions during a mutation without changing field values. */
  setBusy(value) {
    this.busy = value;
    this.dialog?.setAttribute('aria-busy', String(value));
    for (const node of this.dialog?.querySelectorAll('button') || []) node.disabled = value;
    if (!value) {
      const selfDelete = this.dialog?.querySelector('.user-account-action-buttons .button-danger');
      const selfRole = this.dialog?.querySelector('[name="role"]:disabled');
      if (selfDelete && selfRole) selfDelete.disabled = true;
    }
  }

  /** Receive a credential reply without ever writing its value into a background DOM. */
  showCredentials(packet) {
    if (!this.current()) return;
    if (this.isForeground()) {
      this.renderCredentials(packet);
      return;
    }
    this.discardPendingCredentials();
    const pending = {
      user: { username: packet.user.username },
      generated_password: packet.generated_password,
      reauthenticate: packet.reauthenticate === true,
      expiresAt: Date.now() + PENDING_CREDENTIAL_LIFETIME_MS,
    };
    this.pendingCredentials = pending;
    this.awaitingSelfPasswordReset = pending.reauthenticate;
    this.pendingCredentialTimer = setTimeout(
      () => this.expirePendingCredentials(pending),
      PENDING_CREDENTIAL_LIFETIME_MS,
    );
    this.pendingCredentialTimer.unref?.();
    this.context.notice?.(
      t('A generated password is ready. Return to this window within five minutes to copy it.'),
    );
  }

  /** Require both browser visibility and window focus before revealing even a masked value. */
  isForeground() {
    return this.foreground !== false && !document.hidden && document.hasFocus();
  }

  /** Drop pending secret references, retry controls and the associated expiration timer. */
  discardPendingCredentials() {
    if (this.pendingCredentials) this.pendingCredentials.generated_password = '';
    this.pendingCredentials = null;
    clearTimeout(this.pendingCredentialTimer);
    this.pendingCredentialTimer = null;
    this.pendingCredentialResume = null;
    this.pendingCredentialRetry?.remove();
    this.pendingCredentialRetry = null;
  }

  /** Resume a background reply only in the same current, freshly verified Admin session. */
  async resumeCredentials() {
    const pending = this.pendingCredentials;
    if (!pending || !this.current() || !this.isForeground()) return;
    if (pending.expiresAt <= Date.now()) {
      this.expirePendingCredentials(pending);
      return;
    }
    if (this.pendingCredentialResume) return this.pendingCredentialResume;
    const resume = Promise.resolve().then(async () => {
      try {
        // A successful self-reset deliberately revoked this session. Its only
        // permitted continuation is displaying the returned password, then sign-in.
        if (!pending.reauthenticate) {
          const session = await this.context.client.request('/auth/session');
          if (
            session.user?.persona !== 'admin' ||
            session.user?.username !== this.context.scope.username ||
            session.session_scope !== this.context.scope.session_scope
          ) {
            this.clearCredentials();
            this.context.onSessionCheckRequired?.();
            return;
          }
        }
        if (this.pendingCredentials !== pending || !this.current() || !this.isForeground()) return;
        if (pending.expiresAt <= Date.now()) {
          this.expirePendingCredentials(pending);
          return;
        }
        const packet = {
          user: pending.user,
          generated_password: pending.generated_password,
          reauthenticate: pending.reauthenticate,
        };
        this.discardPendingCredentials();
        this.renderCredentials(packet);
      } catch {
        if (this.pendingCredentials !== pending || !this.current()) return;
        this.context.notice?.(
          t(
            'The generated password is waiting, but your Admin session could not be verified. Try again before it expires.',
          ),
        );
        if (this.isForeground()) {
          this.pendingCredentialRetry?.remove();
          this.pendingCredentialRetry = button(t('Show generated password'), () =>
            this.resumeCredentials(),
          );
          byId('users-status')?.append(' ', this.pendingCredentialRetry);
        }
      } finally {
        if (this.pendingCredentialResume === resume) this.pendingCredentialResume = null;
      }
    });
    this.pendingCredentialResume = resume;
    return resume;
  }

  /** Expire an undelivered reply rather than retaining credentials in an abandoned tab. */
  expirePendingCredentials(pending) {
    if (this.pendingCredentials !== pending) return;
    const selfReset = pending.reauthenticate;
    this.closeDialog();
    if (!selfReset && this.current()) {
      this.context.notice?.(
        t(
          'The one-time password expired before it was copied. Reset that account’s password to generate another.',
        ),
      );
    }
  }

  /** Display a generated password once, without any attribute, history or storage copy. */
  renderCredentials(packet) {
    if (!this.current() || !this.isForeground()) return;
    this.returnFocus = document.activeElement;
    this.credentialOpen = true;
    this.awaitingSelfPasswordReset = packet.reauthenticate === true;
    const dialogGeneration = ++this.dialogGeneration;
    const title = element('h2', '', t('Copy the new password'));
    title.id = 'user-dialog-title';
    const description = element(
      'p',
      'user-dialog-description',
      t(
        'A new password was generated for “{username}”. Store it securely before closing this dialog. It will not be shown again.',
        { username: packet.user.username },
      ),
    );
    const account = element(
      'p',
      'user-credentials-account',
      t('Username: {username}', { username: packet.user.username }),
    );
    const label = element('label', 'field');
    const input = element('input', 'user-generated-password');
    input.type = 'password';
    input.value = packet.generated_password;
    input.readOnly = true;
    input.autocomplete = 'new-password';
    input.spellcheck = false;
    input.autocapitalize = 'none';
    label.append(element('span', '', t('Generated password')), input);
    const status = element('p', 'user-field-help');
    status.setAttribute('role', 'status');
    const passwordActions = element('div', 'user-credential-actions');
    const toggle = button(t('Show password'), () => {
      const reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      toggle.textContent = reveal ? t('Hide password') : t('Show password');
      toggle.setAttribute('aria-pressed', String(reveal));
    });
    toggle.setAttribute('aria-pressed', 'false');
    const copy = button(t('Copy password'), async () => {
      if (!this.credentialOpen || dialogGeneration !== this.dialogGeneration) return;
      try {
        if (!navigator.clipboard?.writeText) throw new Error('unavailable');
        await navigator.clipboard.writeText(input.value);
        if (this.credentialOpen && dialogGeneration === this.dialogGeneration)
          status.textContent = t('Password copied. Clear the clipboard after storing it securely.');
      } catch {
        if (this.credentialOpen && dialogGeneration === this.dialogGeneration) {
          input.type = 'text';
          input.focus();
          input.select();
          toggle.textContent = t('Hide password');
          toggle.setAttribute('aria-pressed', 'true');
          status.textContent = t(
            'Select and copy the password manually; clipboard access is unavailable.',
          );
        }
      }
    });
    passwordActions.append(toggle, copy);
    const note = element(
      'p',
      'user-field-help',
      t('This dialog clears when you close it, leave the page, or switch away from this window.'),
    );
    const actions = element('div', 'user-dialog-actions');
    const done = button(
      this.awaitingSelfPasswordReset ? t('I saved it · Sign in again') : t('I saved the password'),
      () => this.closeDialog(),
      'button',
    );
    actions.append(done);
    this.dialog.append(title, description, account, label, passwordActions, status, note, actions);
    this.dialog.showModal();
    done.focus();
  }

  /** Report whether the one-time credential dialog currently contains a password. */
  hasCredentials() {
    return this.credentialOpen;
  }

  /** Clear dialog contents and acknowledge self-reset only after removing the password. */
  closeDialog({ acknowledge = true } = {}) {
    const signInAgain = acknowledge && this.awaitingSelfPasswordReset;
    if (acknowledge) this.awaitingSelfPasswordReset = false;
    this.discardPendingCredentials();
    this.credentialOpen = false;
    this.busy = false;
    this.dialogGeneration++;
    for (const input of this.dialog?.querySelectorAll('.user-generated-password') || [])
      input.value = '';
    this.dialog?.replaceChildren();
    this.dialog?.removeAttribute('aria-busy');
    if (this.dialog?.open) {
      this.programmaticCloseEvents = (this.programmaticCloseEvents || 0) + 1;
      this.dialog.close();
    }
    const focus = this.returnFocus;
    this.returnFocus = null;
    if (!this.disposed && focus?.isConnected && this.isForeground())
      focus.focus({ preventScroll: true });
    if (signInAgain) this.context.onSelfPasswordReset?.();
  }

  /** Remove transient secrets and pending dialog results when the view is left. */
  clearCredentials() {
    this.closeDialog();
  }

  /** Retire private directory metadata, dialogs and event listeners on session disposal. */
  dispose() {
    this.disposed = true;
    this.generation++;
    this.closeDialog();
    byId('users-table-body')?.replaceChildren();
    window.removeEventListener('blur', this.onBlur);
    window.removeEventListener('focus', this.onFocus);
    document.removeEventListener('visibilitychange', this.onVisibility);
  }
}
