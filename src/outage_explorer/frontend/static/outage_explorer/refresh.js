/**
 * @module refresh
 * Present durable Admin-owned refresh jobs through the service API.
 *
 * HTTP enqueue does not run ingestion. A separate backend worker consumes jobs.
 * Mode-bound idempotency survives unknown POST outcomes; generation/job guards
 * protect against stale history and polling replies. Progress is indeterminate.
 */
import { StaleRequest } from './api.js';
import { byId, setText, visible, showError, element } from './dom.js';

const ACTIVE = new Set(['queued', 'running']);
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
    byId('refresh-message').setAttribute('aria-live', 'polite');
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

  /**
   * Render lifecycle state with indeterminate active progress and an explicit adoption action.
   *
   * A progress percentage is not inferred from elapsed time or unrelated row counts.
   */
  render() {
    const job = this.job;
    if (!job) return;
    visible('refresh-use-latest', job.status === 'succeeded' && this.context.hasUpdate());
    // Do not replace accessible status content when the observed job state has not changed.
    const signature = `${job.id}:${job.status}:${job.message}`;
    if (signature === this.renderedSignature) return;
    this.renderedSignature = signature;
    visible('refresh-status');
    const messages = {
      queued: 'Refresh queued. Waiting for the refresh worker.',
      running:
        'Refresh is running. The published data stays available while the new version is checked.',
      succeeded: 'Refresh completed. A verified data version is ready.',
      failed: job.message || 'Refresh failed. Review the outcome before trying again.',
      interrupted:
        job.message || 'Refresh was interrupted. Review publication status before retrying.',
    };
    setText('refresh-message', messages[job.status] || job.message);
    setText('refresh-job-id', `Job ${job.id}`);
    const progress = byId('refresh-progress');
    progress.max = 1;
    if (ACTIVE.has(job.status)) progress.removeAttribute('value');
    else progress.value = job.status === 'succeeded' ? 1 : 0;
    progress.setAttribute('aria-label', `Refresh ${job.status}`);
    progress.setAttribute('aria-valuetext', messages[job.status] || job.status);
    byId('refresh-status').dataset.status = job.status;
    byId('refresh-start').disabled = ACTIVE.has(job.status) || this.busy;
  }

  /**
   * Render owned job history while merging the fresher currently tracked job by ID.
   */
  renderJobs(jobs) {
    const container = byId('refresh-jobs');
    container.replaceChildren();
    if (!jobs.length) {
      container.append(element('p', 'empty-state', 'No refresh jobs yet.'));
      return;
    }
    for (const item of jobs) {
      // History may be older than the last poll/POST response for the currently tracked job.
      const job = item.id === this.job?.id ? this.job : item;
      const row = element('div', 'refresh-job');
      const detail = element('div', 'refresh-job-detail');
      detail.append(
        element('strong', '', job.mode === 'full' ? 'Full history refresh' : 'Recent data refresh'),
      );
      const time = new Date(job.created_at);
      const stamp = element(
        'time',
        '',
        Number.isFinite(time.getTime()) ? time.toLocaleString('en-US') : job.created_at,
      );
      stamp.dateTime = job.created_at;
      detail.append(stamp);
      const status = element('span', 'badge', job.status);
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
    this.job = null;
    this.idempotency = null;
    byId('refresh-jobs').replaceChildren();
    setText('refresh-message', '');
    setText('refresh-job-id', '');
  }
}
