/** Same-origin JSON transport. Cookies stay HttpOnly; CSRF tokens stay in memory. */
export class ApiError extends Error {
  constructor(
    message,
    { status = 0, code = 'network_error', requestId = '', retryAfter = 0 } = {},
  ) {
    super(message);
    Object.assign(this, { name: 'ApiError', status, code, requestId, retryAfter });
  }
}

export class StaleRequest extends Error {}

export class ApiClient {
  constructor({ onSessionLost = null } = {}) {
    this.csrf = null;
    this.onSessionLost = onSessionLost;
  }

  async bootstrap() {
    const data = await this.request('/auth/csrf');
    this.csrf = data.csrf_token;
    return this.csrf;
  }

  async request(path, { method = 'GET', body, headers = {}, retryCsrf = true } = {}) {
    if (!path.startsWith('/') || path.startsWith('//') || /[?#]/.test(path.split('?')[0])) {
      throw new ApiError('The request could not be prepared.', { code: 'invalid_request' });
    }
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

/** Never overlap analytical requests from one account or render obsolete work. */
export class AnalyticalQueue {
  constructor() {
    this.tail = Promise.resolve();
  }
  run(task, isCurrent = () => true) {
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

export function queryString(values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && value !== '') query.set(key, String(value));
  }
  const result = query.toString();
  return result ? `?${result}` : '';
}

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
