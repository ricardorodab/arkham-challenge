import { t } from './i18n.js';
/**
 * @module api
 * Same-origin HTTP client and per-workspace analytical scheduling.
 *
 * This code calls the sibling JSON API in the same Django deployment; it does
 * not authorize datasets or execute SQL. The session cookie is HttpOnly; this
 * client keeps CSRF tokens in memory. The server independently enforces
 * account-wide admission.
 */
/**
 * Safe transport/API failure carrying status, code, request ID and retry metadata.
 */
export class ApiError extends Error {
  /**
   * Construct a client error without including private response bodies or diagnostics.
   * @param {string} message Safe display message.
   * @param {Object} [options] HTTP/status correlation and bounded retry metadata.
   */
  constructor(
    message,
    { status = 0, code = 'network_error', requestId = '', retryAfter = 0 } = {},
  ) {
    super(t(message));
    Object.assign(this, { name: 'ApiError', status, code, requestId, retryAfter });
  }
}

/**
 * Internal control-flow marker for work whose captured workspace state is obsolete.
 */
export class StaleRequest extends Error {}

/**
 * JSON client with in-memory CSRF rotation and a session-loss notification hook.
 */
export class ApiClient {
  /**
   * Create a client; the optional hook disposes UI state after authoritative denial.
   * @param {Object} [options] Optional onSessionLost callback.
   */
  constructor({ onSessionLost = null } = {}) {
    this.csrf = null;
    this.onSessionLost = onSessionLost;
  }

  /**
   * Fetch the CSRF bootstrap endpoint and replace the in-memory token.
   * @returns {Promise<string>} Token to accompany subsequent unsafe requests.
   */
  async bootstrap() {
    const data = await this.request('/auth/csrf');
    this.csrf = data.csrf_token;
    return this.csrf;
  }

  /**
   * Send JSON to /api/v1 with same-origin cookies, no-store and a 22-second transport timeout.
   *
   * Only an explicit CSRF rejection receives one token-refresh retry. Network
   * uncertainty is not interpreted as confirmed logout; API policy denials may
   * notify the session-loss hook. The transport timeout does not prove server cancellation.
   * @param {string} path API-relative absolute path, not a host URL.
   * @param {Object} [options] Method, JSON body, headers and one-shot CSRF retry flag.
   * @returns {Promise<Object>} Successful decoded JSON.
   * @throws {ApiError} Safe transport, protocol or API failure.
   */
  async request(path, { method = 'GET', body, headers = {}, retryCsrf = true } = {}) {
    if (!path.startsWith('/') || path.startsWith('//') || /[?#]/.test(path.split('?')[0])) {
      throw new ApiError('The request could not be prepared.', { code: 'invalid_request' });
    }
    // Aborting fetch bounds client waiting, not the lifetime of server-side analytical work.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 22000);
    const requestHeaders = { ...headers, Accept: 'application/json' };
    if (body !== undefined) requestHeaders['Content-Type'] = 'application/json';
    if (method !== 'GET' && this.csrf) requestHeaders['X-CSRFToken'] = this.csrf;
    let response;
    let data;
    try {
      response = await fetch(`/api/v1${path}`, {
        method,
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        headers: requestHeaders,
        signal: controller.signal,
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      });
      if (!response.headers.get('Content-Type')?.includes('json')) {
        throw new ApiError('The server returned an unexpected response. Please try again.', {
          code: 'invalid_response',
        });
      }
      data = await response.json();
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(
        error.name === 'AbortError'
          ? 'The request took too long. Check your connection and try again.'
          : 'Could not reach the workspace. Check your connection and try again.',
        { code: error.name === 'AbortError' ? 'request_timeout' : 'network_error' },
      );
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) {
      if (response.status === 403 && data.code === 'csrf_failed' && method !== 'GET' && retryCsrf) {
        await this.bootstrap();
        return this.request(path, { method, body, headers, retryCsrf: false });
      }
      const error = new ApiError(
        typeof data.detail === 'string' ? data.detail : 'The request could not be completed.',
        {
          status: response.status,
          code: data.code || 'request_failed',
          requestId: data.request_id || response.headers.get('X-Request-ID') || '',
          retryAfter: Math.max(0, Math.min(300, Number(response.headers.get('Retry-After')) || 0)),
        },
      );
      if (
        this.onSessionLost &&
        (response.status === 401 ||
          ['permissions_changed', 'persona_required'].includes(error.code))
      ) {
        this.onSessionLost(error);
      }
      throw error;
    }
    return data;
  }
}

/**
 * Serialize analytical work within one workspace/tab, not across all account sessions.
 */
export class AnalyticalQueue {
  /**
   * Initialize a settled promise tail so a failed task cannot poison later work.
   */
  constructor() {
    this.tail = Promise.resolve();
  }
  /**
   * Queue work and reject obsolete state before execution and after every wait/result.
   *
   * Retry only bounded 429 capacity replies, up to three attempts; generic query
   * errors propagate. Backend admission remains authoritative across tabs/processes.
   * @param {function(): Promise<*>} task Deferred analytical request.
   * @param {function(): boolean} [isCurrent] Captured generation/model guard.
   * @returns {Promise<*>} Task value or a StaleRequest/API failure.
   */
  run(task, isCurrent = () => true) {
    // Recover the previous tail only to allow later tasks; each caller still receives its own failure.
    const result = this.tail
      .catch(() => {})
      .then(async () => {
        if (!isCurrent()) throw new StaleRequest();
        let value;
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            value = await task();
            break;
          } catch (error) {
            if (
              !(error instanceof ApiError) ||
              error.status !== 429 ||
              error.retryAfter > 5 ||
              attempt === 2
            )
              throw error;
            await new Promise((resolve) =>
              setTimeout(resolve, Math.max(1, error.retryAfter) * 1000),
            );
            if (!isCurrent()) throw new StaleRequest();
          }
        }
        if (!isCurrent()) throw new StaleRequest();
        return value;
      });
    this.tail = result.catch(() => {});
    return result;
  }
}

/**
 * Encode supplied nonempty scalar parameters without changing zero or false values.
 * @param {Object} values Parameter mapping.
 * @returns {string} Empty string or a query string including its leading question mark.
 */
export function queryString(values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && value !== '') query.set(key, String(value));
  }
  const result = query.toString();
  return result ? `?${result}` : '';
}

/**
 * Validate expected dataset/model identity and the bounded positional result shape.
 *
 * This accepts finite primitive scalars, not a complete semantic type schema;
 * exactness depends on the backend serializing decimals/large integers as strings.
 * @param {Object} data Decoded page packet.
 * @param {string} datasetId Expected logical source.
 * @param {string} modelId Expected immutable model.
 * @returns {Object} The unchanged validated packet.
 * @throws {ApiError} Invalid identity, column/row bounds or scalar shape.
 */
export function verifyPage(data, datasetId, modelId) {
  if (
    !data ||
    data.dataset_id !== datasetId ||
    data.model_id !== modelId ||
    !Array.isArray(data.columns) ||
    !Array.isArray(data.rows) ||
    data.columns.length > 150 ||
    data.rows.length > 1000 ||
    data.columns.some(
      (column) => typeof column?.name !== 'string' || typeof column?.type !== 'string',
    ) ||
    data.rows.some(
      (row) =>
        !Array.isArray(row) ||
        row.length !== data.columns.length ||
        row.some(
          (value) =>
            (typeof value === 'number' && !Number.isFinite(value)) ||
            (value !== null && !['string', 'number', 'boolean'].includes(typeof value)),
        ),
    )
  ) {
    throw new ApiError('The result did not match this dataset and data version. Please retry.', {
      code: 'invalid_response',
    });
  }
  return data;
}
