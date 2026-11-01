import { ApiClient, AnalyticalQueue } from './api.js';
import { byId, setText, visible, element, showError, dateLabel } from './dom.js';
import { OverviewController } from './overview.js';
import { ExploreController } from './explore.js';
import { SqlController, clearPrivateHistory } from './sql.js';
import { RefreshController } from './refresh.js';

export class Workspace {
  constructor(scope) {
    this.scope = scope;
    this.ended = false;
    this.catalog = null;
    this.candidate = null;
    this.queue = new AnalyticalQueue();
    this.client = new ApiClient({
      onSessionLost: (error) => this.expire(error.status === 403 ? 'permissions' : 'expired'),
    });
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
    };
    this.overview = new OverviewController(context);
    this.explore = new ExploreController(context);
    this.sql = new SqlController(context);
    this.refresh = scope.persona === 'admin' ? new RefreshController(context) : null;
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
    if (catalog.warnings?.length) this.notice(catalog.warnings.join(' '));
  }

  toggleNav(value) {
    const opened = value ?? !document.body.classList.contains('nav-open');
    document.body.classList.toggle('nav-open', opened);
    byId('nav-toggle').setAttribute('aria-expanded', opened ? 'true' : 'false');
  }

  route(focus) {
    if (this.ended) return;
    const requested = location.hash.slice(1) || 'overview';
    const allowed = [
      'overview',
      'explore',
      'sql',
      ...(this.scope.persona === 'admin' ? ['refresh'] : []),
    ];
    this.view = allowed.includes(requested) ? requested : 'denied';
    for (const section of document.querySelectorAll('.workspace-view'))
      section.hidden = section.id !== `view-${this.view}`;
    const titles = {
      overview: 'Fleet overview',
      explore: 'Explore data',
      sql: 'SQL workspace',
      refresh: 'Data refresh',
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
    if (!this.catalog) return;
    if (this.view === 'overview') this.overview.load();
    if (this.view === 'explore') this.explore.activate();
    if (this.view === 'refresh') this.refresh.load();
  }

  notice(message) {
    const container = byId('global-notice');
    container.replaceChildren(element('p', 'state-message', message));
    container.setAttribute('role', 'status');
    container.hidden = false;
  }

  async checkLatest() {
    if (this.ended || !this.catalog) return;
    if (this.latestPromise) return this.latestPromise;
    this.latestPromise = (async () => {
      try {
        const latest = await this.client.request('/catalog');
        if (this.ended) return;
        this.candidate = latest.model_id !== this.catalog.model_id ? latest : null;
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

  async checkSession(protectContent) {
    if (this.ended || !this.catalog) return;
    if (protectContent) {
      this.protectSessionCheck = true;
      byId('app-root').hidden = true;
    }
    if (this.sessionCheck) return;
    this.sessionCheck = true;
    try {
      const session = await this.client.request('/auth/session');
      if (this.ended) return;
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
      if (!this.ended && this.protectSessionCheck)
        this.shield(
          'Unable to verify your session. Your workspace is hidden until access can be confirmed.',
          () => this.checkSession(true),
        );
    } finally {
      this.sessionCheck = false;
    }
  }

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
  clearSensitive() {
    this.ended = true;
    clearInterval(this.sessionTimer);
    byId('app-root').hidden = true;
    this.overview.dispose();
    this.explore.dispose();
    this.sql.dispose();
    this.refresh?.dispose();
    clearPrivateHistory();
    this.catalog = null;
    this.candidate = null;
  }
  expire(reason) {
    if (this.ended) return;
    this.clearSensitive();
    location.replace(`/login/?reason=${reason}`);
  }
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
