/**
 * @module app
 * Compose the browser workspace, immutable model pin and session lifecycle.
 *
 * Controllers share one catalog/model and one tab-local analytical queue. A new
 * publication is a candidate until explicitly adopted. Persisted actor policy
 * remains server-owned; client disposal and shields limit stale private state.
 */
import { ApiClient, AnalyticalQueue } from './api.js';
import { byId, setText, visible, element, showError, dateLabel } from './dom.js';
import { OverviewController } from './overview.js';
import { ExploreController } from './explore.js';
import { SqlController, clearPrivateHistory } from './sql.js';
import { RefreshController } from './refresh.js';
import { SavedViewsController } from './saved_views.js';
import { UsersController } from './users.js';

/**
 * Own shared model/catalog state, navigation and browser session disposal.
 */
export class Workspace {
  /**
   * Bind controllers to the identity-only bootstrap and install lifecycle listeners.
   * @param {Object} scope Username, persona and opaque server session scope.
   */
  constructor(scope) {
    this.scope = scope;
    this.ended = false;
    this.catalog = null;
    this.candidate = null;
    this.queue = new AnalyticalQueue();
    this.client = new ApiClient({
      onSessionLost: (error) => {
        // A self-reset intentionally revokes this session. Deliver its one-time
        // credentials before the controller acknowledges the required sign-in.
        if (!this.users?.awaitingSelfPasswordReset)
          this.expire(error.status === 403 ? 'permissions' : 'expired');
      },
    });
    // Shared closures read current workspace state; controllers never retain a second actor authority.
    const context = {
      scope,
      client: this.client,
      queue: this.queue,
      alive: () => !this.ended,
      isView: (view) => this.view === view,
      getCatalog: () => this.catalog,
      checkLatest: () => this.checkLatest(),
      applyLatest: () => this.applyLatest(),
      hasUpdate: () => !!this.candidate,
      notice: (message) => this.notice(message),
      onSelfUserUpdated: (user) => this.updateOwnUser(user),
      onSelfPasswordReset: () => this.expire('password-reset'),
      onSessionCheckRequired: () => this.checkSession(true),
    };
    context.onSavedViewCapabilities = () => this.sql?.updatePager();
    this.savedViews = new SavedViewsController(context, {
      onLoadSql: (view) => {
        if (!this.catalog?.datasets.some((item) => item.id === view.dataset_id)) return;
        byId('sql-dataset').value = view.dataset_id;
        if (
          ![...byId('sql-max-rows').options].some((item) => item.value === String(view.max_rows))
        ) {
          const option = element('option', '', `${view.max_rows} rows`);
          option.value = String(view.max_rows);
          byId('sql-max-rows').append(option);
        }
        byId('sql-max-rows').value = String(view.max_rows);
        this.sql.selectDataset(view.sql);
        this.sql.savedViewEditing = view;
        this.sql.updatePager();
        if (location.hash !== '#sql') history.pushState(null, '', '#sql');
        this.route(false);
        this.sql.focusEditor();
      },
    });
    context.savedViews = this.savedViews;
    this.overview = new OverviewController(context);
    this.explore = new ExploreController(context);
    this.sql = new SqlController(context);
    this.refresh = scope.persona === 'admin' ? new RefreshController(context) : null;
    this.users = scope.persona === 'admin' ? new UsersController(context) : null;
    byId('sql-run').disabled = true;
    byId('logout-button').addEventListener('click', () => this.signOut());
    byId('apply-snapshot').addEventListener('click', () => this.applyLatest());
    byId('nav-toggle').addEventListener('click', () => this.toggleNav());
    for (const link of document.querySelectorAll('a[data-view]'))
      link.addEventListener('click', (event) => {
        event.preventDefault();
        const next = link.dataset.view;
        if (location.hash === `#${next}`) this.route(true);
        else location.hash = next;
      });
    window.addEventListener('hashchange', () => this.route(true));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && document.body.classList.contains('nav-open')) {
        this.toggleNav(false);
        byId('nav-toggle').focus();
      }
    });
    try {
      this.channel = new BroadcastChannel('outage-explorer-auth');
      this.channel.onmessage = (event) => {
        if (event.data?.kind === 'signed-out') this.expire('signed-out');
        else if (event.data?.kind === 'signed-in') {
          this.clearSensitive();
          location.reload();
        }
      };
    } catch {
      /* Focus and visibility checks cover browsers without BroadcastChannel. */
    }
    // A restored document can retain private pixels even with no-store; confirm before revealing it.
    window.addEventListener('pageshow', (event) => {
      if (event.persisted) this.checkSession(true);
    });
    window.addEventListener('focus', () => this.checkSession(true));
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) this.checkSession(true);
    });
    this.sessionTimer = setInterval(() => {
      if (!document.hidden) this.checkSession(false);
    }, 60000);
    this.route(false);
  }

  /**
   * Bootstrap CSRF and the authorized catalog, preserving a retryable loading failure.
   */
  async init() {
    visible('global-notice', false);
    try {
      await this.client.bootstrap();
      const catalog = await this.client.request('/catalog');
      if (this.ended) return;
      this.configure(catalog);
      this.route(false);
    } catch (error) {
      if (this.ended) return;
      showError('global-notice', error, () => this.init());
      for (const view of ['overview', 'explore', 'sql']) visible(`${view}-loading`, false);
    }
  }

  /**
   * Adopt one validated model for every analytical controller and invalidate prior work.
   * @param {Object} catalog Server-authorized metadata for one immutable model.
   */
  configure(catalog) {
    if (!catalog || !/^[0-9a-f]{32}$/.test(catalog.model_id) || !Array.isArray(catalog.datasets))
      throw new Error('The dataset catalog could not be loaded.');
    this.catalog = catalog;
    this.candidate = null;
    visible('snapshot-notice', false);
    setText('status-version', catalog.model_id.slice(0, 8));
    byId('status-version').title = catalog.model_id;
    const fleet = catalog.datasets.find((dataset) => dataset.id === 'fleet_daily');
    setText('status-through', dateLabel(fleet?.max_period));
    this.overview.configure(catalog);
    this.explore.configure(catalog);
    this.sql.configure(catalog);
    this.savedViews?.configure(catalog);
    this.savedViews?.load();
    if (catalog.warnings?.length) this.notice(catalog.warnings.join(' '));
  }

  /**
   * Synchronize mobile navigation visibility with its expanded accessibility state.
   * @param {boolean} [value] Explicit open state, or toggle when omitted.
   */
  toggleNav(value) {
    const opened = value ?? !document.body.classList.contains('nav-open');
    document.body.classList.toggle('nav-open', opened);
    byId('nav-toggle').setAttribute('aria-expanded', opened ? 'true' : 'false');
  }

  /**
   * Select an allowed hash view, update navigation/focus and activate its controller.
   *
   * Persona-based markup/navigation is a usability restriction; API actor policy
   * remains the permission boundary even if browser state is manipulated.
   * @param {boolean} focus Whether navigation should move focus to the page heading.
   */
  route(focus) {
    if (this.ended) return;
    const requested = location.hash.slice(1) || 'overview';
    if (this.view === 'users' && requested !== 'users') this.users?.clearCredentials();
    if (this.ended) return;
    const allowed = [
      'overview',
      'explore',
      'sql',
      ...(this.scope.persona === 'admin' ? ['refresh', 'users'] : []),
    ];
    this.view = allowed.includes(requested) ? requested : 'denied';
    for (const section of document.querySelectorAll('.workspace-view'))
      section.hidden = section.id !== `view-${this.view}`;
    const titles = {
      overview: 'Fleet overview',
      explore: 'Explore data',
      sql: 'SQL workspace',
      refresh: 'Data refresh',
      users: 'User management',
      denied: 'Access restricted',
    };
    setText('page-title', titles[this.view]);
    document.title = `${titles[this.view]} · Outage Explorer`;
    for (const link of document.querySelectorAll('a[data-view]')) {
      const active = link.dataset.view === this.view;
      link.classList.toggle('is-active', active);
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    this.toggleNav(false);
    if (this.view === 'denied') {
      let denied = byId('view-denied');
      if (!denied) {
        denied = element('section', 'workspace-view empty-state');
        denied.id = 'view-denied';
        denied.append(
          element('h2', '', 'Not allowed'),
          element('p', '', 'This view is not available with your current access.'),
        );
        const back = element('a', 'button', 'Return to fleet overview');
        back.href = '#overview';
        denied.append(back);
        byId('main-content').prepend(denied);
      }
      denied.hidden = false;
    }
    if (focus) {
      byId('page-title').tabIndex = -1;
      byId('page-title').focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: 'instant' });
    }
    if (this.view === 'refresh') this.refresh.load();
    if (this.view === 'users') this.users?.activate();
    if (!this.catalog) return;
    if (this.view === 'overview') {
      this.overview.load();
      this.savedViews?.load();
    }
    if (this.view === 'explore') this.explore.activate();
  }

  /**
   * Announce a workspace status using text-only DOM content.
   * @param {string} message Safe status text.
   */
  notice(message) {
    const container = byId('global-notice');
    container.replaceChildren(element('p', 'state-message', message));
    container.setAttribute('role', 'status');
    container.hidden = false;
  }

  /** Keep local identity labels aligned after an Admin edits their own profile. */
  updateOwnUser(user) {
    if (this.ended || !user?.is_self || typeof user.username !== 'string') return;
    this.scope.username = user.username;
    const name = document.querySelector('.account-name');
    const avatar = document.querySelector('.sidebar-account .avatar');
    if (name) name.textContent = user.username;
    if (avatar) avatar.textContent = user.username.slice(0, 1).toUpperCase();
  }

  /**
   * Discover a first or newer catalog as an explicit-adoption candidate, coalescing checks.
   *
   * A failed check leaves the active immutable investigation usable.
   */
  async checkLatest() {
    if (this.ended) return;
    if (this.latestPromise) return this.latestPromise;
    // Deduplicate overlapping freshness checks without switching the active investigation.
    this.latestPromise = (async () => {
      try {
        const latest = await this.client.request('/catalog');
        if (this.ended) return;
        this.candidate = latest.model_id !== this.catalog?.model_id ? latest : null;
        visible('snapshot-notice', !!this.candidate);
        this.refresh?.render();
      } catch (error) {
        if (this.ended)
          return; /* Existing pinned data remains usable on a freshness-check failure. */
      } finally {
        this.latestPromise = null;
      }
    })();
    return this.latestPromise;
  }

  /**
   * Explicitly adopt a candidate publication and reset all model-dependent controller work.
   */
  async applyLatest() {
    await this.checkLatest();
    if (this.ended) return;
    if (!this.candidate) {
      this.notice('You are already viewing the latest available publication.');
      return;
    }
    const next = this.candidate;
    this.configure(next);
    this.notice('The workspace is now using the latest verified publication.');
    this.route(false);
    this.refresh?.render();
  }

  /**
   * Confirm the persisted session scope even before the first analytical publication.
   *
   * A protective focus/restore check escalates an already-running background
   * check. Unknown network outcomes keep the shield; unchanged username/persona
   * is insufficient when the actual server session has been replaced.
   * @param {boolean} protectContent Hide content while confirmation is pending.
   */
  async checkSession(protectContent) {
    if (this.ended || this.users?.awaitingSelfPasswordReset) return;
    // Latch protection before the in-flight check guard so focus can escalate a background check.
    if (protectContent) {
      this.protectSessionCheck = true;
      byId('app-root').hidden = true;
    }
    if (this.sessionCheck) return;
    this.sessionCheck = true;
    try {
      const session = await this.client.request('/auth/session');
      if (this.ended || this.users?.awaitingSelfPasswordReset) return;
      if (
        session.user.username !== this.scope.username ||
        session.user.persona !== this.scope.persona ||
        session.session_scope !== this.scope.session_scope
      ) {
        this.expire('permissions');
        return;
      }
      this.protectSessionCheck = false;
      byId('app-root').hidden = false;
      byId('session-shield')?.remove();
      this.checkLatest();
    } catch (error) {
      if (!this.ended && !this.users?.awaitingSelfPasswordReset && this.protectSessionCheck)
        this.shield(
          'Unable to verify your session. Your workspace is hidden until access can be confirmed.',
          () => this.checkSession(true),
        );
    } finally {
      this.sessionCheck = false;
    }
  }

  /**
   * Hide the workspace and focus a safe retry action while access is unconfirmed.
   * @param {string} message Explanation of the unresolved operation.
   * @param {function(): *} retry Explicit confirmation retry.
   */
  shield(message, retry) {
    byId('app-root').hidden = true;
    let shield = byId('session-shield');
    if (!shield) {
      shield = element('main', 'session-shield panel');
      shield.id = 'session-shield';
      document.body.append(shield);
    }
    shield.replaceChildren(
      element('h1', '', 'Your workspace is protected'),
      element('p', '', message),
    );
    const button = element('button', 'button', 'Try again');
    button.type = 'button';
    button.addEventListener('click', retry);
    shield.append(button);
    button.focus();
  }
  /**
   * End controller lifetimes and clear rendered rows, drafts, history, timers and catalogs.
   *
   * Already observed information or downloaded exports cannot be remotely erased.
   */
  clearSensitive() {
    this.ended = true;
    clearInterval(this.sessionTimer);
    byId('app-root').hidden = true;
    this.overview.dispose();
    this.explore.dispose();
    this.sql.dispose();
    this.savedViews?.dispose();
    this.refresh?.dispose();
    this.users?.dispose();
    clearPrivateHistory();
    this.catalog = null;
    this.candidate = null;
  }
  /**
   * Dispose private state once and replace navigation with the fixed login destination.
   * @param {string} reason Known noncredential status reason for login presentation.
   */
  expire(reason) {
    if (this.ended) return;
    this.clearSensitive();
    location.replace(`/login/?reason=${reason}`);
  }
  /**
   * Clear private content before logout, then confirm the server outcome or retain a retry shield.
   *
   * An unknown network response never becomes a claim that server logout succeeded.
   */
  async signOut() {
    if (this.signingOut) return;
    this.signingOut = true;
    if (!this.ended) this.clearSensitive();
    try {
      await this.client.request('/auth/logout', { method: 'POST', body: {} });
      this.channel?.postMessage({ kind: 'signed-out' });
      this.channel?.close();
      location.replace('/login/?reason=signed-out');
    } catch (error) {
      if (error.status === 401) {
        this.channel?.postMessage({ kind: 'signed-out' });
        location.replace('/login/?reason=signed-out');
      } else
        this.shield(
          'Workspace content has been cleared, but sign-out could not be confirmed. Reconnect and try again.',
          () => this.signOut(),
        );
    } finally {
      this.signingOut = false;
    }
  }
}

if (typeof document !== 'undefined' && byId('frontend-session')) {
  const bootstrap = JSON.parse(byId('frontend-session').textContent);
  new Workspace(bootstrap).init();
}
