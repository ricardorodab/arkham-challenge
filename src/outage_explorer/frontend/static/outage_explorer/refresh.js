/**
 * @module refresh
 * Present durable Admin-owned refresh jobs through the service API.
 *
 * HTTP enqueue does not run ingestion. A separate backend worker consumes jobs.
 * Mode-bound idempotency survives unknown POST outcomes; generation/job guards
 * protect against stale history and polling replies. Worker events describe actual
 * stages/counts; only the fixed model-export step has a percentage.
 */
import { t, getLocale } from './i18n.js';
import { StaleRequest } from './api.js';
import { byId, setText, visible, showError, element } from './dom.js';

const ACTIVE = new Set(['queued', 'running']);

const STAGES = Object.freeze({
  preparing: 'Preparing the refresh',
  metadata: 'Reading source metadata',
  extracting: 'Downloading observations',
  validating_source: 'Validating source observations',
  model_sources: 'Preparing model sources',
  model_facts: 'Building daily facts',
  model_fleet: 'Calculating fleet metrics',
  model_reconciliation: 'Reconciling source totals',
  model_quality: 'Checking data quality',
  validating_model: 'Validating the model',
  exporting_model: 'Exporting model datasets',
  verifying_model: 'Verifying model files',
  publishing: 'Publishing verified data',
  completed: 'Refresh completed',
});
const ROUTES = Object.freeze({
  national: 'National',
  facility: 'Facility',
  generator: 'Generator',
});
const count = (value) => Number.isSafeInteger(value) && value >= 0;

/** Format only reported, nonnegative integer counters; absent is not zero. */
function sourceCounts(value) {
  const parts = [];
  if (count(value?.rows_received))
    parts.push(
      t(value.rows_received === 1 ? '{count} row received' : '{count} rows received', {
        count: value.rows_received.toLocaleString(getLocale()),
      }),
    );
  if (count(value?.pages_fetched))
    parts.push(
      t(value.pages_fetched === 1 ? '{count} page fetched' : '{count} pages fetched', {
        count: value.pages_fetched.toLocaleString(getLocale()),
      }),
    );
  return parts.join(' · ');
}

/**
 * Derive text-only presentation from allowlisted stages/routes and typed counters.
 * Unknown/legacy snapshots remain indeterminate; queued jobs report no work.
 */
export function refreshProgress(job) {
  const value = job?.progress;
  const progress =
    job?.status !== 'queued' &&
    value &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    typeof value.stage === 'string' &&
    Object.hasOwn(STAGES, value.stage)
      ? value
      : {};
  const stage = t(STAGES[progress.stage] || '');
  const route =
    typeof progress.route === 'string' && Object.hasOwn(ROUTES, progress.route)
      ? t(ROUTES[progress.route])
      : '';
  const stageText =
    stage +
    (route && ['metadata', 'extracting', 'validating_source'].includes(progress.stage)
      ? ` — ${route}`
      : '');
  const routes = [];
  if (progress.routes && typeof progress.routes === 'object' && !Array.isArray(progress.routes))
    for (const [id, label] of Object.entries(ROUTES)) {
      if (!Object.hasOwn(progress.routes, id)) continue;
      const text = sourceCounts(progress.routes[id]);
      if (text) routes.push(`${t(label)}: ${text}`);
    }
  // This denominator describes a small fixed export list, never overall work.
  const determinate =
    progress.stage === 'exporting_model' &&
    count(progress.completed) &&
    count(progress.total) &&
    progress.total > 0 &&
    progress.total <= 1000 &&
    progress.completed <= progress.total;
  return {
    stageKey: progress.stage || '',
    stage: stageText,
    counts: sourceCounts(progress),
    routes,
    completed: determinate ? progress.completed : null,
    total: determinate ? progress.total : null,
    exportLabel: determinate
      ? t('Model export {percent}%', {
          percent: Math.floor((progress.completed / progress.total) * 100),
        })
      : '',
    updatedAt:
      typeof progress.updated_at === 'string' && Number.isFinite(Date.parse(progress.updated_at))
        ? progress.updated_at
        : null,
  };
}

/** Render a bounded elapsed duration without estimating remaining work. */
function duration(milliseconds) {
  const seconds = Math.floor(Math.max(0, milliseconds) / 1000);
  const hours = Math.floor(seconds / 3600),
    minutes = Math.floor((seconds % 3600) / 60);
  return [hours ? `${hours}h` : '', minutes || hours ? `${minutes}m` : '', `${seconds % 60}s`]
    .filter(Boolean)
    .join(' ');
}

/**
 * Use server event times for elapsed/last-update text, never worker-liveness claims.
 * Terminal elapsed time ends at finished_at; its last event is an absolute timestamp.
 */
export function refreshTimes(job, now = Date.now()) {
  if (!job || job.status === 'queued') return { elapsed: '', updated: '', updatedAt: null };
  const started = typeof job.started_at === 'string' ? Date.parse(job.started_at) : NaN;
  const end =
    job.status === 'running'
      ? now
      : typeof job.finished_at === 'string'
        ? Date.parse(job.finished_at)
        : NaN;
  const progress = refreshProgress(job);
  const elapsed =
    Number.isFinite(started) && Number.isFinite(end) && end >= started
      ? duration(end - started)
      : '';
  const updated = progress.updatedAt
    ? job.status === 'running'
      ? t('{duration} ago', { duration: duration(now - Date.parse(progress.updatedAt)) })
      : new Date(progress.updatedAt).toLocaleString(getLocale())
    : '';
  return { elapsed, updated, updatedAt: progress.updatedAt };
}

/**
 * Present Admin-owned job lifecycle independently of the active analytical model.
 */
export class RefreshController {
  /**
   * Bind queue, poll and explicit-adoption controls; API policy remains authoritative.
   */
  constructor(context) {
    this.context = context;
    this.sequence = 0;
    this.timer = null;
    this.job = null;
    this.idempotency = null;
    this.loaded = false;
    byId('refresh-form').addEventListener('submit', (event) => {
      event.preventDefault();
      this.start();
    });
    byId('refresh-check').addEventListener('click', () => this.poll());
    byId('refresh-use-latest').addEventListener('click', () => context.applyLatest());
    byId('refresh-message').setAttribute('aria-live', 'off');
  }
  /**
   * Load owned recent history once, selecting an active job without replacing newer local work.
   */
  async load() {
    if (this.loaded) return;
    this.loaded = true;
    const sequence = ++this.sequence;
    try {
      const result = await this.context.client.request('/refresh?limit=10');
      if (!this.context.alive() || sequence !== this.sequence) return;
      this.renderJobs(result.jobs);
      if (result.jobs.length) {
        this.job = result.jobs.find((job) => ACTIVE.has(job.status)) || result.jobs[0];
        this.render();
        if (ACTIVE.has(this.job.status)) this.schedule();
      }
    } catch (error) {
      if (this.context.alive() && sequence === this.sequence) {
        showError('refresh-error', error, () => {
          this.loaded = false;
          this.load();
        });
        this.loaded = false;
      }
    }
  }

  /**
   * Enqueue a mode-bound idempotent job and retain the key when the POST outcome is uncertain.
   *
   * Generation guards prevent delayed history from overwriting a newly accepted job.
   * No browser callback performs ingestion; the separate backend worker owns that work.
   */
  async start() {
    if (this.busy || ACTIVE.has(this.job?.status)) return;
    const sequence = ++this.sequence;
    const mode = document.querySelector('input[name="refresh-mode"]:checked')?.value || 'recent';
    if (!this.idempotency || this.idempotency.mode !== mode)
      this.idempotency = { mode, key: crypto.randomUUID() };
    this.busy = true;
    visible('refresh-error', false);
    visible('refresh-loading');
    byId('refresh-start').disabled = true;
    try {
      const job = await this.context.client.request('/refresh', {
        method: 'POST',
        body: { mode },
        headers: { 'Idempotency-Key': this.idempotency.key },
      });
      if (!this.context.alive() || sequence !== this.sequence) return;
      this.job = job;
      this.idempotency = null;
      this.render();
      this.schedule();
      const history = await this.context.client.request('/refresh?limit=10');
      if (this.context.alive() && sequence === this.sequence) this.renderJobs(history.jobs);
    } catch (error) {
      if (this.context.alive() && sequence === this.sequence) {
        showError('refresh-error', error, () => this.start());
        this.loaded = false;
        this.load();
      }
    } finally {
      this.busy = false;
      visible('refresh-loading', false);
      if (this.context.alive()) byId('refresh-start').disabled = ACTIVE.has(this.job?.status);
    }
  }

  /**
   * Schedule only active jobs, polling every two seconds visible or eight seconds hidden.
   */
  schedule() {
    clearTimeout(this.timer);
    if (ACTIVE.has(this.job?.status) && this.context.alive())
      this.timer = setTimeout(() => this.poll(), document.hidden ? 8000 : 2000);
  }
  /**
   * Refresh the captured job only if both its identity and controller generation remain current.
   *
   * Success checks freshness without adopting data. Interruption is a job outcome,
   * not proof that the separate analytical publication transaction rolled back.
   */
  async poll() {
    if (!this.job || this.polling || !this.context.alive()) return;
    const sequence = this.sequence,
      jobId = this.job.id;
    this.polling = true;
    byId('refresh-check').disabled = true;
    try {
      const job = await this.context.client.request(`/refresh/${encodeURIComponent(this.job.id)}`);
      if (!this.context.alive() || sequence !== this.sequence || jobId !== this.job?.id) return;
      const changed = job.status !== this.job.status;
      this.job = job;
      visible('refresh-error', false);
      this.render();
      if (this.history) this.renderJobs(this.history);
      if (changed && job.status === 'succeeded') await this.context.checkLatest();
      if (changed && !ACTIVE.has(job.status)) {
        const history = await this.context.client.request('/refresh?limit=10');
        if (this.context.alive() && sequence === this.sequence) this.renderJobs(history.jobs);
      }
    } catch (error) {
      if (!(error instanceof StaleRequest) && this.context.alive() && sequence === this.sequence)
        showError('refresh-error', error, () => this.poll());
    } finally {
      this.polling = false;
      if (this.context.alive()) byId('refresh-check').disabled = false;
      this.schedule();
    }
  }

  /** Update only non-live timing text; this clock makes no worker-health inference. */
  renderTiming() {
    const timing = refreshTimes(this.job);
    setText('refresh-elapsed', timing.elapsed);
    visible('refresh-elapsed-row', !!timing.elapsed);
    setText('refresh-updated', timing.updated);
    visible('refresh-updated-row', !!timing.updated);
    if (timing.updatedAt) byId('refresh-updated').dateTime = timing.updatedAt;
  }

  /**
   * Render every progress snapshot, announcing stage/status changes without count chatter.
   * Only model export uses a denominator; all other running work is indeterminate.
   */
  render() {
    const job = this.job;
    if (!job) return;
    const detail = refreshProgress(job);
    visible('refresh-use-latest', job.status === 'succeeded' && this.context.hasUpdate());
    visible('refresh-status');
    const messages = {
      queued: t('Refresh queued. Waiting for the refresh worker.'),
      running: t('Refresh is running.'),
      succeeded: t('Refresh completed. A verified data version is ready.'),
      failed: job.message || t('Refresh failed. Review the outcome before trying again.'),
      interrupted:
        job.message || t('Refresh was interrupted. Review publication status before retrying.'),
    };
    const message = t(messages[job.status] || 'Refresh status is unavailable.');
    const announcement = `${message}${detail.stageKey ? ` ${t(STAGES[detail.stageKey])}.` : ''}`;
    const signature = JSON.stringify([job.id, job.status, message, detail.stageKey]);
    if (signature !== this.announcementSignature) {
      setText('refresh-announcement', announcement);
      this.announcementSignature = signature;
    }
    setText('refresh-message', message);
    setText('refresh-job-id', t('Job {id}', { id: job.id }));
    setText(
      'refresh-step-label',
      ACTIVE.has(job.status) ? t('Current step') : t('Last reported step'),
    );
    setText(
      'refresh-step',
      detail.stage || (job.status === 'queued' ? t('Not started') : t('No step details reported')),
    );
    setText('refresh-counts', detail.counts);
    visible('refresh-counts', !!detail.counts);
    const routes = byId('refresh-routes');
    const routeSignature = JSON.stringify(detail.routes);
    if (routeSignature !== this.routeSignature) {
      routes.replaceChildren(...detail.routes.map((text) => element('li', '', text)));
      this.routeSignature = routeSignature;
    }
    visible(routes, detail.routes.length > 0);
    setText('refresh-progress-label', detail.exportLabel);
    visible('refresh-progress-label', !!detail.exportLabel);
    const progress = byId('refresh-progress');
    progress.max = detail.total || 1;
    if (detail.total) progress.value = detail.completed;
    else progress.removeAttribute('value');
    progress.setAttribute('aria-label', detail.total ? t('Model export') : t('Refresh activity'));
    progress.setAttribute(
      'aria-valuetext',
      detail.exportLabel || t('Total refresh work is not known.'),
    );
    visible(progress, job.status === 'running' || !!detail.total);
    setText(
      'refresh-progress-note',
      detail.total
        ? t('Dataset export only; this is not overall refresh progress.')
        : t('The total amount of work is not known.'),
    );
    visible('refresh-progress-note', job.status === 'running' || !!detail.total);
    byId('refresh-status').dataset.status = job.status;
    byId('refresh-start').disabled = ACTIVE.has(job.status) || this.busy;
    this.renderTiming();
    if (job.status === 'running' && !this.clock)
      this.clock = setInterval(() => {
        if (this.context.alive()) this.renderTiming();
      }, 1000);
    else if (job.status !== 'running') {
      clearInterval(this.clock);
      this.clock = null;
    }
  }

  /**
   * Render owned job history while merging the fresher currently tracked job by ID.
   */
  renderJobs(jobs) {
    this.history = jobs;
    const current = jobs.map((item) => (item.id === this.job?.id ? this.job : item));
    const signature = JSON.stringify(
      current.map((job) => {
        const progress = refreshProgress(job);
        return [
          job.id,
          job.mode,
          job.created_at,
          job.status,
          progress.stage,
          progress.counts,
          progress.exportLabel,
        ];
      }),
    );
    if (signature === this.historySignature) return;
    this.historySignature = signature;
    const container = byId('refresh-jobs');
    container.replaceChildren();
    if (!jobs.length) {
      container.append(element('p', 'empty-state', t('No refresh jobs yet.')));
      return;
    }
    for (const job of current) {
      // The tracked job contributes its latest status and progress without another HTTP request.
      const row = element('div', 'refresh-job');
      const detail = element('div', 'refresh-job-detail');
      detail.append(
        element(
          'strong',
          '',
          job.mode === 'full' ? t('Full history refresh') : t('Recent data refresh'),
        ),
      );
      const time = new Date(job.created_at);
      const stamp = element(
        'time',
        '',
        Number.isFinite(time.getTime()) ? time.toLocaleString(getLocale()) : job.created_at,
      );
      stamp.dateTime = job.created_at;
      detail.append(stamp);
      const progress = refreshProgress(job);
      const summary = [progress.stage, progress.counts, progress.exportLabel]
        .filter(Boolean)
        .join(' · ');
      if (summary) detail.append(element('p', 'refresh-job-progress', summary));
      const status = element('span', 'badge', t(job.status));
      status.dataset.status = job.status;
      row.append(detail, status);
      container.append(row);
    }
  }
  /**
   * Invalidate replies, stop polling and clear retained job/idempotency presentation state.
   */
  dispose() {
    this.sequence++;
    clearTimeout(this.timer);
    clearInterval(this.clock);
    this.clock = null;
    this.job = null;
    this.idempotency = null;
    this.history = null;
    this.historySignature = null;
    byId('refresh-jobs').replaceChildren();
    setText('refresh-message', '');
    setText('refresh-job-id', '');
    setText('refresh-announcement', '');
    visible('refresh-status', false);
  }
}
