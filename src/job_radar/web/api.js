/**
 * api.js —— 本地服务 HTTP 层。
 *
 * 边界：
 *  - 只请求同源相对路径（api/...），不构造绝对 URL、不访问外网。
 *  - 所有写操作带 X-CSRF-Token（与旧实现一致），令牌来自 GET /api/state。
 *  - 错误统一抛出 ApiError(message, status)，界面据此展示错误状态。
 */

export class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

let csrfToken = '';

export function setCsrfToken(value) {
  csrfToken = typeof value === 'string' ? value : '';
}

export function getCsrfToken() {
  return csrfToken;
}

/** 拼相对路径 + 查询串，跳过空值。 */
export function withQuery(path, params) {
  const search = new URLSearchParams();
  if (params) {
    for (const key of Object.keys(params)) {
      const value = params[key];
      if (value === undefined || value === null || value === '') continue;
      search.set(key, String(value));
    }
  }
  const query = search.toString();
  return query ? `${path}?${query}` : path;
}

/**
 * @param {string} path 相对路径，例如 'api/tasks'
 * @param {object|null} params 查询参数
 * @param {{method?:string, body?:any, csrf?:boolean, timeoutMs?:number}} options
 */
export async function request(path, params, options = {}) {
  const init = {
    method: options.method || 'GET',
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
    cache: 'no-store',
  };
  if (options.body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(options.body);
  }
  if (options.csrf) {
    if (!csrfToken) throw new ApiError('缺少本机校验令牌，请重新载入页面。', 0, null);
    init.headers['X-CSRF-Token'] = csrfToken;
  }

  let response;
  try {
    response = await fetch(withQuery(path, params), init);
  } catch (error) {
    throw new ApiError(`无法连接本地服务（${error && error.message ? error.message : '网络错误'}）`, 0, null);
  }

  const raw = await response.text().catch(() => '');
  let payload = null;
  if (raw) {
    try { payload = JSON.parse(raw); } catch { payload = null; }
  }
  if (!response.ok) {
    const message = payload && typeof payload.error === 'string' && payload.error
      ? payload.error
      : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(message, response.status, payload);
  }
  if (payload === null) throw new ApiError('本地服务返回了非 JSON 响应。', response.status, null);
  return payload;
}

const enc = (value) => encodeURIComponent(String(value));

export const API = {
  state: () => request('api/state'),
  /** POST /api/config：{config, revision, clear_secrets?}。空 secret 保留原值。 */
  saveConfig: (body) => request('api/config', null, { method: 'POST', body, csrf: true }),
  options: () => request('api/options'),

  tasks: () => request('api/tasks'),
  createTask: (body) => request('api/tasks', null, { method: 'POST', body, csrf: true }),
  task: (id) => request(`api/tasks/${enc(id)}`),
  taskEvents: (id, after = 0, limit = 200) => request(`api/tasks/${enc(id)}/events`, { after, limit }),
  stopTask: (id) => request(`api/tasks/${enc(id)}/stop`, null, { method: 'POST', body: {}, csrf: true }),
  resumeTask: (id) => request(`api/tasks/${enc(id)}/resume`, null, { method: 'POST', body: { confirmed: true }, csrf: true }),
  focusTaskWindow: (id) => request(`api/tasks/${enc(id)}/verification`, null, { method: 'POST', body: {}, csrf: true }),

  /** POST /api/access：{action:"pause",reason} 或 {action:"release",confirmed:true}。 */
  access: (body) => request('api/access', null, { method: 'POST', body, csrf: true }),

  overview: () => request('api/overview'),
  keywords: () => request('api/keywords'),
  jobs: (params) => request('api/jobs', params),
  job: (id) => request(`api/jobs/${enc(id)}`),
  jobAction: (id, body) => request(`api/jobs/${enc(id)}/action`, null, { method: 'POST', body, csrf: true }),
  exportJobsHref: (params) => withQuery('api/export/jobs.csv', params),
};
