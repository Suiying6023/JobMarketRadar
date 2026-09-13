/**
 * JobMarketRadar 本地离线管理界面 —— 前端逻辑（原生 ES module，无框架 / 无 CDN）。
 *
 * 设计约束（改动前先读，这些不是风格偏好而是安全边界）：
 *  1. 只调用相对路径的本地 API，绝不发起 BOSS / 外网请求；界面上没有任何
 *     "开始采集""解除冻结""自动开聊"入口，保存配置也不会触发采集。
 *  2. 所有来自接口的字符串一律用 textContent 写入 DOM：本文件不使用 innerHTML，
 *     `h()` 甚至显式拒绝 html 属性 —— 数据里的尖括号只能变成文字，不可能变成标签。
 *  3. 外部链接只在 URL 以 https://www.zhipin.com/job_detail/ 开头时渲染为可点链接，
 *     且一律 target="_blank" rel="noopener noreferrer"；其余情况只显示纯文本。
 *  4. 写操作（保存配置、岗位本地记录）带 X-CSRF-Token，并有 pending / error 反馈；
 *     配置保存遇 409 时提示重新载入，不静默覆盖。
 *  5. 配置保存只改本页编辑到的字段，其余字段原样回传（见 EDITABLE_CONFIG_KEYS）。
 */

// ============================== 常量 ==============================

const VIEWS = ['overview', 'jobs', 'keywords', 'config', 'notes'];

const BUCKET_BADGE = {
  '优先核实': 'badge-priority',
  '需核实门槛': 'badge-gate',
  '暂不优先': 'badge-later',
  '信息不足': 'badge-unknown',
};

const BUCKET_CARD = {
  '优先核实': 'b-priority',
  '需核实门槛': 'b-gate',
  '暂不优先': 'b-later',
  '信息不足': 'b-unknown',
};

/** 允许在页面上编辑的配置字段。其余字段（浏览器、限速批大小、自动开聊策略……）
 *  随草稿原样回传，保证"没碰过的字段不会被前端改掉"。
 *  ⚠️ 后端 CollectionConfig 是 extra="forbid"：前端不得凭空发明字段，只能在
 *  这里已有的键上写值。 */
const EDITABLE_CONFIG_KEYS = [
  'cities', 'keywords', 'pages_per_query', 'pages_per_keyword',
  'max_requests_per_run', 'max_requests_per_day',
  'budget_mode', 'max_page_number', 'max_keywords_per_run',
  'request_delay_seconds', 'detail_delay_seconds', 'candidate',
];

/** 参考线处理方式：自设预算线到线之后怎么办。
 *  reference = 只提示（默认），enforce = 到线停止。 */
const BUDGET_MODES = [
  { value: 'reference', label: '仅作参考，到线提示' },
  { value: 'enforce', label: '强制限制，到线停止' },
];

/** 只有这个前缀的链接才允许渲染成可点链接。 */
const SAFE_JOB_URL_PREFIX = 'https://www.zhipin.com/job_detail/';
const SAFE_JOB_HOST = 'www.zhipin.com';

const SAFETY_LABELS = {
  frozen: '已冻结（离线）',
  offline: '离线（默认拒绝联网）',
  paused: '已暂停',
  ready: '就绪（访问策略允许联网）',
  stopped: '已停止',
  cooldown: '冷却中',
  manual: '等待人工处理',
  timeout: '临时冻结',
};
const FROZEN_RE = /frozen|offline|paused|stop|halt|hold|suspend|cooldown/i;

const TOTAL_DEFS = [
  { key: 'jobs', label: '岗位记录', note: '本地库去重后的岗位条数' },
  { key: 'with_jd', label: '已抓 JD', note: '详情正文已缓存，能读职责与要求' },
  // ⚠️ open_snapshot 是「本地快照没有标关闭」的全部记录（含没有 JD 的），
  //    它不等于"在招"：平台没标关闭只说明快照那一刻没看到关闭标记。
  { key: 'open_snapshot', label: '未标关闭', note: '全部记录中快照未标关闭的条数（含没有 JD 的），不等于在招' },
  { key: 'closed', label: '快照已关闭', note: '抓详情那一刻平台已标关闭 / 失效' },
  { key: 'tagged', label: '已打标', note: '旧模型标签已落库的条数' },
  { key: 'stale', label: '快照偏旧', note: '详情超过 7 天，需要重新核实（分母是有 JD 的岗位）' },
  { key: 'unknown_status', label: '状态未知', note: '没有 JD 正文，或正文缺时间戳' },
  { key: 'duplicate_groups', label: '疑似重复组', note: '公司标识 + 规范化标题 + 城市相同' },
  { key: 'active_legacy', label: '历史 active 分层', note: '沿用历史保留分层字段（active / sample），仅供参考' },
];

/** 后端给了才有、没有就不占位的补充数字。 */
const OPTIONAL_TOTALS = [
  { key: 'companies', label: '公司（按名称去重）', note: '按公司名称去重后的家数，不是企业法人数量：同名会合并，改名会拆开' },
  { key: 'median_salary_k', label: '薪资中位数', note: '挂牌月薪区间中点中位数（K/月）', suffix: 'K' },
  { key: 'duplicate_excess', label: '重复额外记录', note: '疑似重复组多出来的记录数' },
];

const COVERAGE_DEFS = [
  { key: 'with_jd', label: '有 JD 覆盖', cls: 'sea' },
  { key: 'open_snapshot', label: '未标关闭（含无 JD）', cls: 'teal' },
  { key: 'tagged', label: '旧模型已打标', cls: 'slate' },
  { key: 'stale', label: '详情超过 7 天', cls: 'amber' },
  { key: 'unknown_status', label: '状态未知', cls: 'slate' },
  { key: 'closed', label: '快照已标关闭', cls: 'slate' },
];

/** 概览里的列表只展示前 N 行 —— 这是管理界面不是报表，密度优先。 */
const TABLE_ROW_LIMIT = 15;

// ====================== 极简 DOM 工具（零 innerHTML） ======================

const $ = (selector, root = document) => root.querySelector(selector);

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    for (const key of Object.keys(props)) {
      const value = props[key];
      if (value === null || value === undefined || value === false) continue;
      switch (key) {
        case 'class': node.className = String(value); break;
        case 'text': node.textContent = String(value); break;
        case 'html':
          throw new Error('禁止用 innerHTML 写入数据（防 XSS）：请改用 text 或子节点');
        case 'dataset':
          for (const dataKey of Object.keys(value)) node.dataset[dataKey] = String(value[dataKey]);
          break;
        case 'style': Object.assign(node.style, value); break;
        case 'value': node.value = String(value); break;
        case 'checked':
        case 'disabled':
        case 'selected':
        case 'hidden':
          node[key] = true;
          break;
        default:
          if (key.startsWith('on') && typeof value === 'function') {
            node.addEventListener(key.slice(2).toLowerCase(), value);
          } else if (value === true) {
            node.setAttribute(key, '');
          } else {
            node.setAttribute(key, String(value));
          }
      }
    }
  }
  append(node, children);
  return node;
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function setText(node, text) {
  if (node) node.textContent = text === null || text === undefined ? '' : String(text);
  return node;
}

/** 数值转字符串：对象一律降级成 JSON（避免 [object Object] 混进界面）。 */
function valueText(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.length ? value.map(valueText).join('、') : '—';
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch { return '（无法显示）'; }
  }
  return String(value);
}

// ============================== 格式化 ==============================

const INT_FMT = new Intl.NumberFormat('zh-CN');
const TIME_FMT = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function int0(value) {
  const n = num(value);
  return n === null ? 0 : Math.round(n);
}

function fmtInt(value) {
  const n = num(value);
  return n === null ? '—' : INT_FMT.format(Math.round(n));
}

function fmtDecimal(value, digits = 1) {
  const n = num(value);
  if (n === null) return '—';
  return n.toFixed(digits).replace(/\.0+$/, '');
}

function parseTime(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function fmtTime(value) {
  const date = parseTime(value);
  return date ? TIME_FMT.format(date) : (value ? String(value) : '—');
}

function relTime(value) {
  const date = parseTime(value);
  if (!date) return '';
  const diffMs = Date.now() - date.getTime();
  const minutes = Math.round(Math.abs(diffMs) / 60000);
  // safety.resume_after 是「最早可人工解除的时刻」，通常是未来时间：
  // 一律说"前"会把 15 天后显示成"刚刚"，所以未来用"后"。
  const suffix = diffMs < 0 ? '后' : '前';
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟${suffix}`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时${suffix}`;
  const days = Math.round(hours / 24);
  if (days < 31) return `${days} 天${suffix}`;
  const months = Math.round(days / 30);
  if (months < 19) return `${months} 个月${suffix}`;
  return `${Math.round(months / 12)} 年${suffix}`;
}

function ratioText(part, whole) {
  const p = num(part);
  const w = num(whole);
  if (p === null || w === null || w <= 0) return '—';
  return `${fmtDecimal((p / w) * 100)}%`;
}

function ratioOf(part, whole) {
  const p = num(part);
  const w = num(whole);
  if (p === null || w === null || w <= 0) return null;
  return Math.max(0, Math.min(1, p / w));
}

function str(value) {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'boolean') return value ? '是' : '否';
  return '';
}

function strList(value) {
  if (Array.isArray(value)) return value.map(str).filter((item) => item.trim() !== '');
  if (typeof value === 'string' && value.trim() !== '') return [value];
  return [];
}

function deepClone(value) {
  if (value === null || typeof value !== 'object') return value;
  if (typeof structuredClone === 'function') {
    try { return structuredClone(value); } catch { /* 回退到 JSON 深拷贝 */ }
  }
  try { return JSON.parse(JSON.stringify(value)); } catch { return value; }
}

function getPath(object, path) {
  return path.reduce((acc, key) => (acc === null || acc === undefined ? undefined : acc[key]), object);
}

function setPath(object, path, value) {
  let node = object;
  for (let i = 0; i < path.length - 1; i += 1) {
    const key = path[i];
    if (node[key] === null || typeof node[key] !== 'object') {
      node[key] = typeof path[i + 1] === 'number' ? [] : {};
    }
    node = node[key];
  }
  node[path[path.length - 1]] = value;
}

/** 只有 /job_detail/<id>.html 形态的 https 站内链接才允许渲染成 <a>。
 *  后端 safe_job_url() 已经过滤过一次，这里是第二道（前端也可能被别的方式喂数据）。 */
function safeJobUrl(raw) {
  const value = typeof raw === 'string' ? raw.trim() : '';
  if (!value.startsWith(SAFE_JOB_URL_PREFIX)) return null;
  let parsed;
  try { parsed = new URL(value); } catch { return null; }
  if (parsed.protocol !== 'https:') return null;
  if (parsed.hostname !== SAFE_JOB_HOST) return null;
  if (!/^\/job_detail\/[A-Za-z0-9_-]+\.html$/.test(parsed.pathname)) return null;
  return parsed.href;
}

/** 后端把「已标关闭」和「快照未标关闭，当前待核实」都写成含「关闭」的句子，
 *  直接 includes('关闭') 会把后者判成已关闭 —— 先排除否定式再判。 */
function snapshotClosed(status) {
  const text = str(status);
  if (!text) return false;
  if (text.includes('未标关闭') || text.includes('未关闭')) return false;
  return text.includes('关闭') || text.includes('下架') || text.includes('停止招聘') || text.includes('已结束');
}

function externalLink(href, label, className = 'btn btn-sm') {
  return h('a', {
    class: className,
    href,
    target: '_blank',
    rel: 'noopener noreferrer',
    text: label,
  });
}

// ============================== API 层 ==============================

class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = 'ApiError';
    this.status = status || 0;
    this.payload = payload || null;
  }
}

/** 拼相对路径 + 查询串。**不构造绝对 URL**，所有请求都落在同源相对路径上。 */
function withQuery(path, params) {
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

async function request(path, params, options = {}) {
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
    if (!app.state || !app.state.csrf_token) {
      throw new ApiError('缺少 CSRF 令牌，请先重新载入页面。', 0, null);
    }
    init.headers['X-CSRF-Token'] = app.state.csrf_token;
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

const API = {
  state: () => request('api/state'),
  overview: () => request('api/overview'),
  jobs: (params) => request('api/jobs', params),
  job: (id) => request(`api/jobs/${encodeURIComponent(id)}`),
  keywords: () => request('api/keywords'),
  saveConfig: (body) => request('api/config', null, { method: 'POST', body, csrf: true }),
  jobAction: (id, body) => request(`api/jobs/${encodeURIComponent(id)}/action`, null, {
    method: 'POST', body, csrf: true,
  }),
  exportJobsHref: (params) => withQuery('api/export/jobs.csv', params),
};

// ============================== 全局状态 ==============================

const app = {
  /** 顶栏 / 配置的唯一真源（GET /api/state 的响应） */
  state: null,
  stateError: null,
  /** 配置草稿：编辑期间的副本，保存成功后被服务端返回值替换 */
  draft: null,
  dirty: false,
  conflict: false,
  /** 保存请求进行中：期间禁用保存/放弃，避免重复提交 */
  saving: false,
  /** 服务端 config 里是否已有 budget_mode；没有就别硬塞（extra="forbid" 会 422） */
  budgetModeSupported: false,
  budgetModeEdited: false,
  saveProblems: [],
  saveWarnings: [],
  overview: null,
  overviewError: null,
  overviewLoading: false,
  keywords: null,
  keywordsError: null,
  view: 'overview',
  jobs: {
    q: '',
    city: '',
    tier: '',
    page: 1,
    page_size: 30,
    data: null,
    loading: false,
    error: null,
    cursor: -1,
    selectedId: null,
    detail: null,
    detailLoading: false,
    detailError: null,
    noteDraft: null,
  },
};

const busy = { count: 0, text: '' };

let jobsToken = 0;
let overviewToken = 0;
let keywordsToken = 0;
let searchTimer = null;

function isNarrow() {
  return window.matchMedia('(max-width: 1119px)').matches;
}

// ============================== 反馈组件 ==============================

function busyPush(text) {
  busy.count += 1;
  busy.text = text || '同步中…';
  renderBootStatus();
}

function busyPop() {
  busy.count = Math.max(0, busy.count - 1);
  if (busy.count === 0) busy.text = '';
  renderBootStatus();
}

function renderBootStatus() {
  const node = $('#boot-status');
  if (!node) return;
  if (busy.count > 0) {
    node.className = 'pill pill-busy';
    setText(node, busy.text || '同步中…');
  } else if (app.stateError && !app.state) {
    node.className = 'pill pill-bad';
    setText(node, '状态读取失败');
  } else if (app.dirty) {
    node.className = 'pill';
    setText(node, '配置有未保存修改');
  } else {
    node.className = 'pill';
    setText(node, '就绪');
  }
}

function toast(message, kind = 'info', action = null) {
  const box = $('#toasts');
  if (!box) return;
  const node = h('div', { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    h('span', { class: 'toast-text', text: message }));
  if (action && typeof action.run === 'function') {
    node.appendChild(h('button', {
      class: 'btn', type: 'button', text: action.label || '重试',
      onclick: () => { node.remove(); action.run(); },
    }));
  }
  node.appendChild(h('button', {
    class: 'toast-close', type: 'button', 'aria-label': '关闭提示', text: '×',
    onclick: () => node.remove(),
  }));
  box.appendChild(node);
  window.setTimeout(() => node.remove(), kind === 'error' ? 9000 : 4200);
}

function setPending(button, pending, pendingText) {
  if (!button || !button.isConnected) return;
  if (pending) {
    if (button.dataset.label === undefined) button.dataset.label = button.textContent;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    if (pendingText) button.textContent = pendingText;
  } else {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    if (button.dataset.label !== undefined) {
      button.textContent = button.dataset.label;
      delete button.dataset.label;
    }
  }
}

// ====================== 顶栏：离线/冻结状态 ======================

function safetyLabel(mode) {
  const key = String(mode || '').trim();
  if (!key) return '未知（未给出模式）';
  return SAFETY_LABELS[key.toLowerCase()] || key;
}

function renderStatus() {
  const box = $('#safety');
  if (!box) return;
  clear(box);
  box.className = 'safety';
  box.appendChild(h('span', { class: 'safety-dot', 'aria-hidden': 'true' }));
  const text = h('span', { class: 'safety-text' });
  box.appendChild(text);

  if (app.stateError) {
    box.classList.add('safety-error');
    append(text, [h('b', { text: '无法读取离线状态' }), `：${app.stateError}`]);
    box.appendChild(h('button', {
      class: 'btn btn-sm', type: 'button', text: '重试',
      onclick: () => { void reloadStateWithConfirm(); },
    }));
    return;
  }

  if (!app.state) {
    box.classList.add('safety-loading');
    setText(text, '正在读取离线冻结状态…');
    return;
  }

  const safety = app.state.safety;
  const frozen = !safety.mode || FROZEN_RE.test(safety.mode);
  box.classList.add(frozen ? 'safety-frozen' : 'safety-warn');
  append(text, [
    frozen ? '离线状态：' : '访问策略：', h('b', { text: safetyLabel(safety.mode) }),
    ` · 原因：${safety.reason || '未记录原因'}`,
    // resume_after 是「最早可以人工解除的时刻」，是个未来时间点：
    // 只给绝对时间，不套过去时的相对说法（"3 天后"这类相对口径容易读成自动恢复）。
    safety.resume_after ? ` · 最早可人工解除：${fmtTime(safety.resume_after)}` : '',
    safety.manual_release_required ? ' · 需人工解除：本页不提供解除入口，请走项目既有的命令行流程' : '',
  ]);
  box.appendChild(h('span', {
    class: 'safety-note',
    text: '本页只读写本机数据，没有任何采集 / 开聊入口。',
  }));
}

/** 修订号是 64 位内容指纹，顶部只显示前 8 位，完整值放在 title 里 ——
 *  否则顶栏会被一长串十六进制撑宽（1094px 视口下会把整页顶出横向滚动）。 */
function shortRevision(value) {
  const text = valueText(value);
  if (text === '—' || text.length <= 8) return text;
  return `${text.slice(0, 8)}…`;
}

function renderMeta() {
  const pill = $('#meta-pill');
  const footer = $('#footer-meta');
  if (!app.state) {
    setText(pill, '—');
    setText(footer, '版本 —');
    renderBootStatus();
    return;
  }
  const version = app.state.version || '—';
  const revision = valueText(app.state.revision);
  setText(pill, `v${version} · 修订 ${shortRevision(app.state.revision)}`);
  pill.title = `配置修订：${revision}`;
  setText(footer, `版本 ${version} · 配置修订 ${shortRevision(app.state.revision)}`);
  footer.title = `配置修订：${revision}`;
  renderBootStatus();
}

function setActiveNav(view) {
  for (const link of document.querySelectorAll('.nav-link')) {
    if (link.dataset.view === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
}

// ============================== 状态加载 ==============================

function normalizeState(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const safety = data.safety && typeof data.safety === 'object' ? data.safety : {};
  return {
    config: data.config && typeof data.config === 'object' ? data.config : {},
    revision: data.revision === undefined ? null : data.revision,
    csrf_token: str(data.csrf_token),
    safety: {
      mode: str(safety.mode),
      reason: str(safety.reason),
      resume_after: safety.resume_after === undefined || safety.resume_after === null
        ? '' : str(safety.resume_after),
      manual_release_required: safety.manual_release_required === true,
    },
    version: str(data.version),
  };
}

/** 载入 /api/state。会重置配置草稿 —— 调用前若 dirty 必须先确认。 */
async function loadState() {
  busyPush('读取本地状态…');
  try {
    const payload = await API.state();
    app.state = normalizeState(payload);
    app.stateError = null;
    app.draft = deepClone(app.state.config);
    app.dirty = false;
    app.conflict = false;
    app.budgetModeEdited = false;
    app.saveProblems = [];
    app.saveWarnings = [];
  } catch (error) {
    app.stateError = error.message;
  } finally {
    busyPop();
    renderStatus();
    renderMeta();
  }
  return app.state !== null;
}

async function reloadStateWithConfirm() {
  if (app.dirty && !window.confirm('重新载入会丢弃未保存的配置修改，继续？')) return;
  await loadState();
  renderConfig();
  renderKeywords();
  updateSaveBar();
  await showView(app.view, { force: true });
  if (app.state) toast('已重新载入本地状态。', 'ok');
}

// ============================== 概览 ==============================

function normalizeOverview(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const mapList = (value, mapper) => (Array.isArray(value) ? value.map(mapper) : []);
  const mapGroups = (value) => mapList(value, (row) => ({
    name: str(row && row.name) || '未提供',
    jobs: num(row && row.jobs) ?? 0,
    jd: num(row && row.jd) ?? 0,
    median_salary_k: num(row && row.median_salary_k),
  }));
  const window = data.observation_window && typeof data.observation_window === 'object'
    ? data.observation_window : {};
  return {
    scope: str(data.scope),
    as_of: str(data.as_of),
    window: {
      first_seen: str(window.first_seen),
      last_seen: str(window.last_seen),
      latest_detail_or_status: str(window.latest_detail_or_status),
    },
    methodology: strList(data.methodology),
    totals: data.totals && typeof data.totals === 'object' ? data.totals : {},
    cities: mapGroups(data.cities),
    industries: mapGroups(data.industries),
    experience: mapGroups(data.experience),
    education: mapGroups(data.education),
    skills: mapList(data.skills, (row) => ({
      name: str(row && row.name) || '未知技能',
      count: num(row && row.count) ?? 0,
      denominator: num(row && row.denominator),
      percent: num(row && row.percent),
    })),
    quality: mapList(data.quality, (row) => ({
      label: str(row && row.label) || '未命名指标',
      value: row ? row.value : null,
      detail: str(row && row.detail),
    })),
    candidate_funnel: mapList(data.candidate_funnel, (row) => ({
      label: str(row && row.label) || '未命名阶段',
      count: num(row && row.count) ?? 0,
    })),
  };
}

async function loadOverview(force = false) {
  if (app.overviewLoading) return;
  if (app.overview && !force) return;
  const token = ++overviewToken;
  app.overviewLoading = true;
  if (app.view === 'overview') renderOverview();
  try {
    const payload = await API.overview();
    if (token !== overviewToken) return;
    app.overview = normalizeOverview(payload);
    app.overviewError = null;
  } catch (error) {
    if (token !== overviewToken) return;
    app.overviewError = error.message;
  } finally {
    if (token === overviewToken) {
      app.overviewLoading = false;
      renderOverview();
      populateCityOptions();
    }
  }
}

function statCard(def, totals) {
  return h('li', { class: 'stat' },
    h('span', { class: 'stat-value', text: fmtInt(totals[def.key]) }),
    h('span', { class: 'stat-label', text: def.label }),
    h('span', { class: 'stat-note', text: def.note }));
}

function barCell(text, ratio, cls = '') {
  const percent = ratio === null || ratio === undefined ? 0 : Math.max(0, Math.min(100, ratio * 100));
  const width = percent > 0 ? `${Math.max(2, percent)}%` : '0%';
  return h('div', { class: 'bar-cell' },
    h('span', { class: 'bar', 'aria-hidden': 'true' },
      h('span', { class: `bar-fill ${cls}`.trim(), style: { width } })),
    h('span', { class: 'bar-num', text }));
}

function coverageRow(def, totals) {
  const part = num(totals[def.key]);
  const whole = num(totals.jobs);
  const ratio = ratioOf(part, whole);
  return h('div', { class: 'coverage-row' },
    h('div', { class: 'coverage-top' },
      h('span', { text: def.label }),
      h('span', { class: 'val', text: `${fmtInt(part)} / ${fmtInt(whole)} · ${ratioText(part, whole)}` })),
    barCell('', ratio, def.cls));
}

function skillPercent(skill) {
  const count = num(skill.count);
  const denominator = num(skill.denominator);
  if (count !== null && denominator !== null && denominator > 0) {
    const ratio = count / denominator;
    return { ratio, text: `${fmtDecimal(ratio * 100)}%` };
  }
  const percent = num(skill.percent);
  if (percent === null) return { ratio: null, text: '—' };
  // 后端没给分母时按数值大小判断口径：≤1 视为比例，否则视为百分数
  const ratio = percent <= 1 ? percent : percent / 100;
  return { ratio, text: `${fmtDecimal(ratio * 100)}%` };
}

function panel(title, note, ...children) {
  return h('section', { class: 'panel' },
    h('h3', { text: title }),
    note ? h('p', { class: 'panel-note', text: note }) : null,
    ...children);
}

function tableScaffold(headers, rows, caption) {
  return h('div', { class: 'table-wrap' },
    h('table', { class: 'table' },
      h('caption', { text: caption }),
      h('thead', {}, h('tr', {}, headers.map((head) => h('th', {
        scope: 'col', class: head.num ? 'num' : '', text: head.label,
      })))),
      h('tbody', {}, rows)));
}

function limitNote(total, shown) {
  if (total <= shown) return null;
  return h('p', { class: 'hint', text: `仅显示前 ${shown} 行（共 ${total} 行）。完整数据请导出 CSV 或用命令行查询。` });
}

function renderOverview() {
  const root = $('#overview-body');
  if (!root) return;
  clear(root);

  if (!app.overview) {
    if (app.overviewError) {
      root.appendChild(h('p', { class: 'callout callout-bad', role: 'alert', text: `无法读取概览数据：${app.overviewError}` }));
      root.appendChild(h('p', {}, h('button', {
        class: 'btn', type: 'button', text: '重试',
        onclick: () => { void loadOverview(true); },
      })));
    } else {
      root.appendChild(h('p', { class: 'empty', text: '正在读取本地数据…' }));
    }
    return;
  }

  const data = app.overview;
  const totals = data.totals;
  const jobs = num(totals.jobs);

  const windowBits = [];
  if (data.window.first_seen) windowBits.push(`最早采集 ${fmtTime(data.window.first_seen)}`);
  if (data.window.last_seen) windowBits.push(`最近采集 ${fmtTime(data.window.last_seen)}`);
  if (data.window.latest_detail_or_status) {
    windowBits.push(`最近详情/状态 ${fmtTime(data.window.latest_detail_or_status)}`);
  }

  root.appendChild(h('div', { class: 'meta-line' },
    h('span', {}, '统计范围：', h('b', { text: data.scope || '未提供' })),
    h('span', {}, '数据截至：', h('b', { text: fmtTime(data.as_of) }), data.as_of ? `（${relTime(data.as_of)}）` : ''),
    h('span', {}, '配置修订：', h('b', { text: shortRevision(app.state ? app.state.revision : null) })),
    windowBits.length ? h('span', { text: `观测窗口：${windowBits.join(' · ')}` }) : null,
    h('button', {
      class: 'btn btn-sm', type: 'button', text: '刷新统计',
      onclick: () => { void loadOverview(true); },
    })));

  const statCards = TOTAL_DEFS.map((def) => statCard(def, totals));
  for (const def of OPTIONAL_TOTALS) {
    const value = num(totals[def.key]);
    if (value === null) continue;
    statCards.push(h('li', { class: 'stat' },
      h('span', { class: 'stat-value', text: def.suffix ? `${fmtDecimal(value)}${def.suffix}` : fmtInt(value) }),
      h('span', { class: 'stat-label', text: def.label }),
      h('span', { class: 'stat-note', text: def.note })));
  }
  root.appendChild(h('ul', { class: 'stat-grid' }, statCards));

  // ---- 覆盖图：整体覆盖 + 逐项占比 ----
  const coverage = h('div', { class: 'coverage' }, COVERAGE_DEFS.map((def) => coverageRow(def, totals)));
  const stack = h('div', { class: 'stack', 'aria-hidden': 'true' },
    h('span', {
      class: 'bar-fill sea',
      style: { width: `${Math.max(0, Math.min(100, (ratioOf(totals.with_jd, jobs) ?? 0) * 100))}%` },
    }));
  root.appendChild(panel(
    '覆盖与状态',
    '所有比例的分母都是岗位记录数（快照值，不是当前平台状态）。',
    stack,
    h('div', { class: 'legend' },
      h('span', { class: 'legend-item' }, h('span', { class: 'swatch sea' }), `有 JD ${ratioText(totals.with_jd, jobs)}`),
      h('span', { class: 'legend-item' }, h('span', { class: 'swatch empty' }), `无 JD / 未抓详情`)),
    h('div', { style: { marginTop: '10px' } }, coverage)));

  // ---- 城市 ----
  const cities = data.cities.slice().sort((a, b) => b.jobs - a.jobs);
  const cityRows = cities.slice(0, TABLE_ROW_LIMIT).map((row) => h('tr', {},
    h('td', { class: 'kw-name', text: row.name }),
    h('td', { class: 'num', text: fmtInt(row.jobs) }),
    h('td', { class: 'num', text: fmtInt(row.jd) }),
    h('td', {}, barCell(ratioText(row.jd, row.jobs), ratioOf(row.jd, row.jobs), 'sea')),
    h('td', { class: 'num', text: row.median_salary_k === null ? '—' : `${fmtDecimal(row.median_salary_k)}K` })));
  root.appendChild(panel(
    '城市分布',
    '每行是岗位挂牌月薪区间中点的中位数，不是到手薪资。',
    cities.length
      ? tableScaffold(
        [{ label: '城市' }, { label: '岗位', num: true }, { label: '有 JD', num: true },
          { label: 'JD 覆盖' }, { label: '薪资中位数', num: true }],
        cityRows, '城市分布：岗位数、有 JD 数与挂牌薪资中位数')
      : h('p', { class: 'empty', text: '本地库没有城市统计。' }),
    limitNote(cities.length, TABLE_ROW_LIMIT)));

  // ---- 行业 ----
  const industries = data.industries.slice().sort((a, b) => b.jobs - a.jobs);
  const industryRows = industries.slice(0, TABLE_ROW_LIMIT).map((row) => h('tr', {},
    h('td', { class: 'kw-name', text: row.name }),
    h('td', { class: 'num', text: fmtInt(row.jobs) }),
    h('td', { class: 'num', text: fmtInt(row.jd) }),
    h('td', {}, barCell(ratioText(row.jd, row.jobs), ratioOf(row.jd, row.jobs), 'sea')),
    h('td', { class: 'num', text: row.median_salary_k === null ? '—' : `${fmtDecimal(row.median_salary_k)}K` })));
  root.appendChild(panel(
    '行业分布',
    '行业来自平台给公司打的标签；平台没给的统一记作「未提供」，单独成行。',
    industries.length
      ? tableScaffold(
        [{ label: '行业' }, { label: '岗位', num: true }, { label: '有 JD', num: true },
          { label: 'JD 覆盖' }, { label: '薪资中位数', num: true }],
        industryRows, '行业分布：岗位数、有 JD 数与挂牌薪资中位数')
      : h('p', { class: 'empty', text: '本地库没有行业统计。' }),
    limitNote(industries.length, TABLE_ROW_LIMIT)));

  // ---- 技能 ----
  const skills = data.skills.slice().sort((a, b) => b.count - a.count);
  const skillRows = skills.slice(0, TABLE_ROW_LIMIT).map((row) => {
    const info = skillPercent(row);
    return h('tr', {},
      h('td', { class: 'kw-name', text: row.name }),
      h('td', { class: 'num', text: fmtInt(row.count) }),
      h('td', { class: 'num', text: row.denominator === null ? '—' : fmtInt(row.denominator) }),
      h('td', {}, barCell(info.text, info.ratio, 'teal')));
  });
  root.appendChild(panel(
    '技能命中',
    '分母是有 JD 正文的岗位数；同一条 JD 可以命中多个技能，所以比例可以相加超过 100%，不能当作市场占比。',
    skills.length
      ? tableScaffold(
        [{ label: '技能' }, { label: '命中', num: true }, { label: '分母', num: true }, { label: '占比' }],
        skillRows, '技能在本库内的命中数与占比')
      : h('p', { class: 'empty', text: '本地库没有技能统计。' })));

  // ---- 画像复核分桶：四个桶互斥，不是漏斗，所以不给"较上一步" ----
  const buckets = data.candidate_funnel;
  const bucketMax = buckets.reduce((max, row) => Math.max(max, num(row.count) ?? 0), 0);
  const bucketRows = buckets.map((row) => h('div', { class: 'funnel-row' },
    h('div', { class: 'funnel-top' },
      h('span', { text: row.label }),
      h('span', { class: 'val', text: `${fmtInt(row.count)} · 占全库 ${ratioText(row.count, jobs)}` })),
    barCell('', bucketMax > 0 ? (num(row.count) ?? 0) / bucketMax : null, 'teal')));
  root.appendChild(panel(
    '画像复核分桶',
    '每条岗位只落进一个桶（互斥），四个桶相加等于全库岗位数。它是复核顺序建议，不是流程，也没有先后转化关系。',
    buckets.length
      ? h('div', { class: 'funnel' }, bucketRows)
      : h('p', { class: 'empty', text: '当前没有分桶统计。' })));

  // ---- 数据质量项 ----
  const qualityRows = data.quality.map((row) => h('div', { class: 'quality-row' },
    h('span', { class: 'q-label', text: row.label }),
    h('span', { class: 'q-value', text: valueText(row.value) }),
    h('span', { class: 'q-detail', text: row.detail || '—' })));
  root.appendChild(panel(
    '数据质量项',
    '本地库的自检结果：每一项是一个检查项，后面是它的数值与含义。',
    qualityRows.length ? h('div', { class: 'quality-list' }, qualityRows)
      : h('p', { class: 'empty', text: '当前没有质量检查项。' })));

  // ---- 经验与学历分布 ----
  const groupTable = (rows, firstHeader, caption) => (rows.length
    ? tableScaffold(
      [{ label: firstHeader }, { label: '岗位', num: true }, { label: '有 JD', num: true },
        { label: 'JD 覆盖' }, { label: '薪资中位数', num: true }],
      rows.slice(0, TABLE_ROW_LIMIT).map((row) => h('tr', {},
        h('td', { class: 'kw-name', text: row.name }),
        h('td', { class: 'num', text: fmtInt(row.jobs) }),
        h('td', { class: 'num', text: fmtInt(row.jd) }),
        h('td', {}, barCell(ratioText(row.jd, row.jobs), ratioOf(row.jd, row.jobs), 'sea')),
        h('td', { class: 'num', text: row.median_salary_k === null ? '—' : `${fmtDecimal(row.median_salary_k)}K` }))),
      caption)
    : h('p', { class: 'empty', text: '当前没有这一维度的统计。' }));
  const experience = data.experience.slice().sort((a, b) => b.jobs - a.jobs);
  const education = data.education.slice().sort((a, b) => b.jobs - a.jobs);
  root.appendChild(panel(
    '经验与学历分布',
    '分母是全库；这两项决定画像门槛能不能对上。',
    groupTable(experience, '经验要求', '经验要求分布'),
    h('div', { style: { marginTop: '12px' } }, groupTable(education, '学历要求', '学历要求分布'))));

  // ---- 口径说明 ----
  root.appendChild(panel(
    '统计口径',
    '每个数字在本地库里的含义。读的时候请连同「数据说明」页的边界一起读。',
    h('div', { class: 'quality-list' }, TOTAL_DEFS.map((def) => h('div', { class: 'quality-row' },
      h('span', { class: 'q-label', text: def.label }),
      h('span', { class: 'q-value', text: fmtInt(totals[def.key]) }),
      h('span', { class: 'q-detail', text: def.note }))))));

  // ---- 统计方法与口径：逐条说明这些数字是怎么来的 ----
  if (data.methodology.length) {
    root.appendChild(panel(
      '统计方法与口径',
      '这些数字是怎么算出来的，逐条列出。',
      h('ul', { class: 'detail-list' }, data.methodology.map((text) => h('li', { text })))));
  }
}

// ============================== 岗位池 ==============================

function normalizeItem(raw) {
  const item = raw && typeof raw === 'object' ? raw : {};
  const decision = item.decision && typeof item.decision === 'object' ? item.decision : {};
  const bucket = str(decision.bucket);
  return {
    id: item.id === undefined || item.id === null ? '' : String(item.id),
    title: str(item.title) || '（无标题）',
    company: str(item.company),
    city: str(item.city),
    salary: str(item.salary),
    experience: str(item.experience),
    education: str(item.education),
    url: str(item.url),
    detail_fetched_at: str(item.detail_fetched_at),
    stale: item.stale === true,
    decision: {
      bucket: bucket || '信息不足',
      score: num(decision.score),
      reasons: strList(decision.reasons),
      concerns: strList(decision.concerns),
      evidence: strList(decision.evidence),
    },
    skills: strList(item.skills),
    applied: item.applied === true,
    bookmarked: item.bookmarked === true,
    note: str(item.note),
    job_description: str(item.job_description),
    matched_queries: strList(item.matched_queries),
    legacy_match: item.legacy_match === undefined ? null : item.legacy_match,
    // 后端附带的补充字段：没有就留空，界面上对应行不渲染
    snapshot_age_days: num(item.snapshot_age_days),
    snapshot_status: str(item.snapshot_status),
    duplicate_count: num(item.duplicate_count),
    industry: str(item.industry),
    company_size: str(item.company_size),
    first_seen_at: str(item.first_seen_at),
    legacy_match_reason: str(item.legacy_match_reason),
    legacy_retention: str(item.legacy_retention),
  };
}

function normalizeJobsPayload(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  return {
    total: num(data.total) ?? 0,
    page: num(data.page) ?? 1,
    page_size: num(data.page_size) ?? app.jobs.page_size,
    items: Array.isArray(data.items) ? data.items.map(normalizeItem) : [],
  };
}

function currentJobsParams(extra = {}) {
  return {
    q: app.jobs.q,
    city: app.jobs.city,
    tier: app.jobs.tier,
    page: app.jobs.page,
    page_size: app.jobs.page_size,
    ...extra,
  };
}

async function loadJobs() {
  const token = ++jobsToken;
  app.jobs.loading = true;
  app.jobs.error = null;
  renderJobs();
  try {
    const payload = await API.jobs(currentJobsParams());
    if (token !== jobsToken) return;
    app.jobs.data = normalizeJobsPayload(payload);
  } catch (error) {
    if (token !== jobsToken) return;
    app.jobs.error = error.message;
    app.jobs.data = null;
  } finally {
    if (token === jobsToken) {
      app.jobs.loading = false;
      renderJobs();
    }
  }
}

function renderJobs() {
  renderJobsSummary();
  renderJobList();
  renderPager();
  renderDetail();
  updateExportHref();
}

function renderJobsSummary() {
  const node = $('#jobs-summary');
  if (!node) return;
  if (app.jobs.error) {
    setText(node, `加载失败：${app.jobs.error}`);
    return;
  }
  if (app.jobs.loading && !app.jobs.data) {
    setText(node, '正在读取岗位…');
    return;
  }
  if (!app.jobs.data) {
    setText(node, '尚未加载。');
    return;
  }
  const total = int0(app.jobs.data.total);
  const shown = app.jobs.data.items.length;
  const filterBits = [];
  if (app.jobs.q) filterBits.push(`搜索「${app.jobs.q}」`);
  if (app.jobs.city) filterBits.push(`城市「${app.jobs.city}」`);
  if (app.jobs.tier) filterBits.push(`优先级「${app.jobs.tier}」`);
  setText(node, `共 ${fmtInt(total)} 条 · 本页 ${shown} 条`
    + (filterBits.length ? ` · 筛选：${filterBits.join('，')}` : ' · 无筛选')
    + (app.jobs.loading ? ' · 更新中…' : ''));
}

function chipNodes(items, className, limit) {
  const nodes = [];
  const shown = items.slice(0, limit);
  for (const text of shown) nodes.push(h('span', { class: `chip ${className}`.trim(), title: text, text }));
  if (items.length > shown.length) {
    nodes.push(h('span', { class: 'chip chip-quiet', text: `+${items.length - shown.length}` }));
  }
  return nodes;
}

function detailFetchedNode(item) {
  if (!item.detail_fetched_at) return h('span', { class: 'meta-warn', text: '未抓详情（状态未知）' });
  const age = item.snapshot_age_days;
  const ageText = age === null
    ? relTime(item.detail_fetched_at)
    : (age <= 0.05 ? '刚刚' : `${fmtDecimal(age)} 天前`);
  const node = h('span', {}, `详情快照 ${fmtTime(item.detail_fetched_at)}（${ageText}）`);
  if (item.stale) node.className = 'meta-warn';
  return node;
}

function jobCard(item, index) {
  const bucket = item.decision.bucket;
  const node = h('li', {
    class: `job ${BUCKET_CARD[bucket] || 'b-unknown'}`
      + (app.jobs.selectedId === item.id || app.jobs.cursor === index ? ' sel' : ''),
    dataset: { id: item.id, index: String(index) },
  });

  node.appendChild(h('div', { class: 'job-head' },
    h('h3', { class: 'job-title' },
      h('button', {
        class: 'title-btn', type: 'button', dataset: { act: 'detail', id: item.id },
        text: item.title,
      })),
    h('div', { class: 'job-side' },
      h('span', { class: `badge ${BUCKET_BADGE[bucket] || 'badge-unknown'}`, text: bucket }),
      item.decision.score === null ? null
        : h('span', { class: 'score', title: '本地规则评分（不是平台评分）', text: fmtDecimal(item.decision.score) }))));

  const metas = [];
  if (item.company) metas.push(h('span', { class: 'meta-strong', text: item.company }));
  if (item.city) metas.push(h('span', { text: item.city }));
  if (item.salary) metas.push(h('span', { class: 'salary', text: item.salary }));
  if (item.experience) metas.push(h('span', { text: `经验 ${item.experience}` }));
  if (item.education) metas.push(h('span', { text: `学历 ${item.education}` }));
  if (item.industry) metas.push(h('span', { text: item.industry }));
  metas.push(detailFetchedNode(item));
  if (item.stale && item.detail_fetched_at) {
    metas.push(h('span', { class: 'meta-warn', text: '快照超过 7 天，需重新核实' }));
  }
  if (snapshotClosed(item.snapshot_status)) {
    metas.push(h('span', { class: 'meta-bad', text: '快照已标关闭' }));
  }
  node.appendChild(h('p', { class: 'job-metas' }, metas));

  const tags = [
    ...chipNodes(item.decision.reasons, 'chip-teal', 3),
    ...chipNodes(item.decision.concerns, 'chip-amber', 3),
    ...chipNodes(item.decision.evidence, 'chip-slate', 2),
  ];
  if (item.duplicate_count !== null && item.duplicate_count > 1) {
    tags.push(h('span', {
      class: 'chip chip-quiet',
      title: '公司标识 + 规范化标题 + 城市相同的记录数，只提示合并候选',
      text: `疑似重复 ×${item.duplicate_count}`,
    }));
  }
  if (tags.length) node.appendChild(h('div', { class: 'job-tags' }, tags));

  if (item.skills.length) {
    node.appendChild(h('div', { class: 'job-tags' }, chipNodes(item.skills, '', 8)));
  }

  const actions = h('div', { class: 'job-actions' },
    h('button', {
      class: 'btn btn-sm', type: 'button', dataset: { act: 'detail', id: item.id }, text: '详情 / JD',
    }),
    h('button', {
      class: `btn btn-sm${item.bookmarked ? ' btn-on' : ''}`, type: 'button',
      dataset: { act: 'bookmark', id: item.id },
      'aria-pressed': item.bookmarked ? 'true' : 'false',
      text: item.bookmarked ? '★ 已收藏' : '☆ 收藏',
    }),
    h('button', {
      class: `btn btn-sm${item.applied ? ' btn-on' : ''}`, type: 'button',
      dataset: { act: 'applied', id: item.id },
      'aria-pressed': item.applied ? 'true' : 'false',
      text: item.applied ? '✓ 已投（本地）' : '标记已投（本地）',
    }));

  const flags = h('div', { class: 'job-flags' });
  if (item.note) flags.appendChild(h('span', { class: 'chip chip-sea', title: item.note, text: `备注：${item.note}` }));
  const safe = safeJobUrl(item.url);
  if (safe) flags.appendChild(externalLink(safe, '原站 ↗'));
  else flags.appendChild(h('span', { class: 'chip chip-quiet', text: '无可用原站链接' }));
  if (flags.childNodes.length) actions.appendChild(flags);

  node.appendChild(actions);
  return node;
}

function renderJobList() {
  const list = $('#jobs-list');
  const empty = $('#jobs-empty');
  if (!list) return;
  clear(list);
  const items = app.jobs.data ? app.jobs.data.items : [];
  items.forEach((item, index) => list.appendChild(jobCard(item, index)));

  if (empty) {
    empty.hidden = items.length > 0;
    if (app.jobs.error) setText(empty, `加载失败：${app.jobs.error}`);
    else if (app.jobs.loading && !app.jobs.data) setText(empty, '正在读取岗位…');
    else if (!items.length) setText(empty, '没有匹配的岗位。可以放宽筛选或清空搜索词。');
  }
}

function totalPages() {
  if (!app.jobs.data) return 1;
  const size = int0(app.jobs.data.page_size) || 30;
  return Math.max(1, Math.ceil(int0(app.jobs.data.total) / size));
}

function goToPage(page) {
  const max = totalPages();
  const target = Math.min(Math.max(1, int0(page)), max);
  if (target === int0(app.jobs.page)) return;
  app.jobs.page = target;
  app.jobs.cursor = -1;
  void loadJobs();
  window.scrollTo({ top: 0, behavior: 'auto' });
}

function renderPager() {
  const nav = $('#jobs-pager');
  if (!nav) return;
  clear(nav);
  if (!app.jobs.data) return;
  const page = Math.max(1, int0(app.jobs.data.page));
  const pages = totalPages();
  nav.appendChild(h('button', {
    class: 'btn btn-sm', type: 'button', text: '← 上一页',
    disabled: page <= 1 || app.jobs.loading, onclick: () => goToPage(page - 1),
  }));
  nav.appendChild(h('span', { class: 'pager-info', text: `第 ${page} / ${pages} 页` }));
  nav.appendChild(h('button', {
    class: 'btn btn-sm', type: 'button', text: '下一页 →',
    disabled: page >= pages || app.jobs.loading, onclick: () => goToPage(page + 1),
  }));

  const jump = h('input', {
    type: 'number', min: '1', max: String(pages), value: String(page),
    style: { width: '78px' }, 'aria-label': '跳到第几页',
  });
  jump.addEventListener('change', () => {
    const target = Number(jump.value);
    if (Number.isInteger(target) && target >= 1 && target <= pages) goToPage(target);
    else {
      jump.value = String(page);
      toast(`页码需在 1–${pages} 之间。`, 'error');
    }
  });
  nav.appendChild(h('span', { class: 'pager-jump' }, '跳到', jump, '页'));
  nav.appendChild(h('span', { class: 'pager-info', text: `共 ${fmtInt(app.jobs.data.total)} 条` }));
}

function updateExportHref() {
  const link = $('#export-csv');
  if (!link) return;
  link.href = API.exportJobsHref({ q: app.jobs.q, city: app.jobs.city, tier: app.jobs.tier });
  link.title = '导出当前筛选结果（不分页），由本地服务生成，不联网';
}

function findJob(id) {
  if (app.jobs.data) {
    const hit = app.jobs.data.items.find((item) => item.id === id);
    if (hit) return hit;
  }
  if (app.jobs.detail && app.jobs.detail.id === id) return app.jobs.detail;
  return null;
}

async function loadDetail(id) {
  app.jobs.detailLoading = true;
  app.jobs.detailError = null;
  if (app.jobs.detail && app.jobs.detail.id !== id) app.jobs.detail = null;
  renderDetail();
  try {
    const payload = await API.job(id);
    if (app.jobs.selectedId !== id) return;
    app.jobs.detail = normalizeItem(payload);
  } catch (error) {
    if (app.jobs.selectedId !== id) return;
    app.jobs.detailError = error.message;
  } finally {
    if (app.jobs.selectedId === id) {
      app.jobs.detailLoading = false;
      renderDetail();
    }
  }
}

function openDetail(id) {
  app.jobs.selectedId = id;
  app.jobs.cursor = -1;
  app.jobs.detail = null;
  app.jobs.detailError = null;
  app.jobs.noteDraft = null;
  renderJobList();
  renderDetail();
  if (isNarrow()) {
    document.body.classList.add('detail-open');
    const backdrop = $('#detail-backdrop');
    if (backdrop) backdrop.hidden = false;
    const close = $('#detail-close');
    if (close) close.focus();
  }
  syncDetailInert();
  void loadDetail(id);
}

function closeDetail() {
  const previous = app.jobs.selectedId;
  app.jobs.selectedId = null;
  app.jobs.detail = null;
  app.jobs.detailError = null;
  app.jobs.noteDraft = null;
  document.body.classList.remove('detail-open');
  const backdrop = $('#detail-backdrop');
  if (backdrop) backdrop.hidden = true;
  renderJobList();
  renderDetail();
  syncDetailInert();
  if (previous) {
    for (const button of document.querySelectorAll('#jobs-list .title-btn')) {
      if (button.dataset.id === previous) { button.focus(); break; }
    }
  }
}

function renderDetail() {
  const body = $('#detail-body');
  if (!body) return;
  clear(body);

  const id = app.jobs.selectedId;
  if (!id) {
    body.appendChild(h('p', { class: 'empty', text: '从左侧列表选择一条岗位，这里显示本地缓存的 JD 原文、决策依据与本地记录。' }));
    return;
  }
  if (app.jobs.detailError) {
    body.appendChild(h('p', { class: 'callout callout-bad', role: 'alert', text: `详情加载失败：${app.jobs.detailError}` }));
    body.appendChild(h('button', {
      class: 'btn btn-sm', type: 'button', text: '重试',
      onclick: () => { void loadDetail(id); },
    }));
    return;
  }
  if (!app.jobs.detail || app.jobs.detailLoading) {
    body.appendChild(h('p', { class: 'empty', text: '正在读取详情…' }));
    return;
  }

  const item = app.jobs.detail;

  body.appendChild(h('h4', { class: 'detail-title', text: item.title }));
  const sub = [];
  if (item.company) sub.push(h('span', { class: 'meta-strong', text: item.company }));
  if (item.city) sub.push(h('span', { text: item.city }));
  if (item.salary) sub.push(h('span', { class: 'salary', text: item.salary }));
  if (item.experience) sub.push(h('span', { text: `经验 ${item.experience}` }));
  if (item.education) sub.push(h('span', { text: `学历 ${item.education}` }));
  sub.push(h('span', { class: `badge ${BUCKET_BADGE[item.decision.bucket] || 'badge-unknown'}`, text: item.decision.bucket }));
  if (item.decision.score !== null) sub.push(h('span', { class: 'score', text: fmtDecimal(item.decision.score) }));
  body.appendChild(h('p', { class: 'detail-sub' }, sub));

  const safe = safeJobUrl(item.url);
  body.appendChild(h('div', { class: 'detail-block' },
    h('h4', { text: '原站链接' }),
    safe
      ? h('div', { class: 'chips' }, externalLink(safe, '在 BOSS 打开原岗位 ↗'))
      : h('p', { class: 'hint', text: '这条记录的 URL 不在允许的 https://www.zhipin.com/job_detail/ 前缀内，为安全起见不渲染可点链接。' })));

  // ---- 决策依据 ----
  const decisionBlock = h('div', { class: 'detail-block' }, h('h4', { text: '决策依据（本地规则）' }));
  const groups = [
    ['命中理由', item.decision.reasons, 'reasons'],
    ['顾虑 / 门槛', item.decision.concerns, 'concerns'],
    ['依据', item.decision.evidence, 'evidence'],
  ];
  let hasDecision = false;
  for (const [label, list, cls] of groups) {
    if (!list.length) continue;
    hasDecision = true;
    decisionBlock.appendChild(h('p', { class: 'hint', text: label }));
    decisionBlock.appendChild(h('ul', { class: `detail-list ${cls}` }, list.map((text) => h('li', { text }))));
  }
  if (!hasDecision) decisionBlock.appendChild(h('p', { class: 'hint', text: '本地规则未给出依据（可能还没打标或缺少 JD）。' }));
  body.appendChild(decisionBlock);

  // ---- 本地记录 ----
  const recordBlock = h('div', { class: 'detail-block' }, h('h4', { text: '本地记录（只写本机数据库）' }));
  recordBlock.appendChild(h('div', { class: 'job-actions' },
    h('button', {
      class: `btn btn-sm${item.bookmarked ? ' btn-on' : ''}`, type: 'button',
      dataset: { act: 'bookmark', id: item.id },
      'aria-pressed': item.bookmarked ? 'true' : 'false',
      text: item.bookmarked ? '★ 已收藏' : '☆ 收藏',
    }),
    h('button', {
      class: `btn btn-sm${item.applied ? ' btn-on' : ''}`, type: 'button',
      dataset: { act: 'applied', id: item.id },
      'aria-pressed': item.applied ? 'true' : 'false',
      text: item.applied ? '✓ 已投（本地）' : '标记已投（本地）',
    }),
    h('button', {
      class: 'btn btn-sm btn-danger', type: 'button',
      dataset: { act: 'reset', id: item.id }, text: '重置本地记录',
    })));

  const noteValue = app.jobs.noteDraft && app.jobs.noteDraft.id === item.id
    ? app.jobs.noteDraft.value
    : item.note;
  const textarea = h('textarea', {
    id: 'detail-note', rows: '3', maxlength: '2000',
    'aria-label': '本条岗位的本地备注',
    placeholder: '例如：已确认岗位仍在招 / 需要补充作品集 / 等回复',
    value: noteValue,
  });
  const noteStatus = h('p', { class: 'hint', text: item.note ? `已保存备注：${item.note}` : '暂无备注。' });
  const saveNote = h('button', {
    class: 'btn btn-sm btn-primary', type: 'button', dataset: { act: 'note-save', id: item.id },
    text: '保存备注',
  });
  textarea.addEventListener('input', () => {
    app.jobs.noteDraft = { id: item.id, value: textarea.value };
    const changed = textarea.value !== item.note;
    setText(noteStatus, changed ? '备注已修改，尚未保存。' : (item.note ? `已保存备注：${item.note}` : '暂无备注。'));
    noteStatus.className = changed ? 'hint savebar-warn' : 'hint';
  });
  recordBlock.appendChild(h('div', { style: { marginTop: '8px' } }, textarea));
  recordBlock.appendChild(h('div', { class: 'job-actions' }, saveNote, noteStatus));
  body.appendChild(recordBlock);

  // ---- 元信息 ----
  const metaRows = [
    h('div', {}, h('dt', { text: '岗位 ID' }), h('dd', { class: 'mono', text: item.id || '—' })),
    h('div', {}, h('dt', { text: '详情快照' }),
      h('dd', { text: item.detail_fetched_at ? `${fmtTime(item.detail_fetched_at)}（${relTime(item.detail_fetched_at)}）` : '未抓详情' })),
    h('div', {}, h('dt', { text: '快照偏旧' }), h('dd', { text: item.stale ? '是，需要重新核实' : '否' })),
  ];
  if (item.snapshot_age_days !== null) {
    metaRows.push(h('div', {}, h('dt', { text: '快照年龄' }), h('dd', { text: `${fmtDecimal(item.snapshot_age_days)} 天` })));
  }
  if (item.snapshot_status) {
    metaRows.push(h('div', {}, h('dt', { text: '快照状态' }),
      h('dd', { class: snapshotClosed(item.snapshot_status) ? 'meta-bad' : '', text: item.snapshot_status })));
  }
  if (item.duplicate_count !== null) {
    metaRows.push(h('div', {}, h('dt', { text: '疑似重复' }), h('dd', { text: `${fmtInt(item.duplicate_count)} 条同标识记录` })));
  }
  if (item.first_seen_at) {
    metaRows.push(h('div', {}, h('dt', { text: '首次采集' }), h('dd', { text: fmtTime(item.first_seen_at) })));
  }
  if (item.company_size) {
    metaRows.push(h('div', {}, h('dt', { text: '公司规模' }), h('dd', { text: item.company_size })));
  }
  if (item.industry) {
    metaRows.push(h('div', {}, h('dt', { text: '行业' }), h('dd', { text: item.industry })));
  }
  metaRows.push(h('div', {}, h('dt', { text: '历史标签判定' }), h('dd', { text: valueText(item.legacy_match) })));
  if (item.legacy_match_reason) {
    metaRows.push(h('div', {}, h('dt', { text: '历史判定理由' }), h('dd', { text: item.legacy_match_reason })));
  }
  if (item.legacy_retention) {
    metaRows.push(h('div', {}, h('dt', { text: '历史保留分层' }), h('dd', { text: item.legacy_retention })));
  }
  const meta = h('dl', { class: 'kv-list' }, metaRows);
  const metaBlock = h('div', { class: 'detail-block' }, h('h4', { text: '元信息' }), meta);
  if (item.matched_queries.length) {
    metaBlock.appendChild(h('p', { class: 'hint', text: '命中关键词' }));
    metaBlock.appendChild(h('div', { class: 'chips' }, item.matched_queries.map((text) => h('span', { class: 'chip chip-sea', text }))));
  }
  if (item.skills.length) {
    metaBlock.appendChild(h('p', { class: 'hint', text: '技能' }));
    metaBlock.appendChild(h('div', { class: 'chips' }, item.skills.map((text) => h('span', { class: 'chip', text }))));
  }
  body.appendChild(metaBlock);

  // ---- JD 原文 ----
  body.appendChild(h('div', { class: 'detail-block' },
    h('h4', { text: 'JD 原文（本地缓存文本）' }),
    item.job_description
      ? h('pre', { class: 'jd-raw', text: item.job_description })
      : h('p', { class: 'hint', text: '本地未缓存 JD 正文（这条岗位还没抓过详情，或详情抓取失败）。' })));
}

function applyLocalAction(item, action, value) {
  if (action === 'bookmark') item.bookmarked = value === true;
  else if (action === 'applied') item.applied = value === true;
  else if (action === 'note') item.note = str(value);
  else if (action === 'reset') {
    item.bookmarked = false;
    item.applied = false;
    item.note = '';
  }
}

function mergeLocalFields(item, payload) {
  const source = payload && typeof payload === 'object'
    ? (payload.item && typeof payload.item === 'object' ? payload.item : payload)
    : null;
  if (!source) return false;
  let merged = false;
  if (typeof source.bookmarked === 'boolean') { item.bookmarked = source.bookmarked; merged = true; }
  if (typeof source.applied === 'boolean') { item.applied = source.applied; merged = true; }
  if (typeof source.note === 'string') { item.note = source.note; merged = true; }
  return merged;
}

const ACTION_LABELS = {
  bookmark: '收藏',
  applied: '已投',
  note: '备注',
  reset: '本地记录',
};

async function runJobAction(id, action, value, button) {
  // 列表项与详情项是两个对象，写操作要同时落在两边，否则详情面板会显示旧状态
  const listItem = app.jobs.data ? app.jobs.data.items.find((entry) => entry.id === id) : null;
  const detailItem = app.jobs.detail && app.jobs.detail.id === id ? app.jobs.detail : null;
  const targets = [];
  if (listItem) targets.push(listItem);
  if (detailItem && detailItem !== listItem) targets.push(detailItem);
  const snapshots = targets.map((target) => ({
    target,
    bookmarked: target.bookmarked,
    applied: target.applied,
    note: target.note,
  }));

  for (const target of targets) applyLocalAction(target, action, value);
  if (action === 'note' || action === 'reset') app.jobs.noteDraft = null;
  renderJobList();
  renderDetail();

  setPending(button, true, action === 'reset' ? '重置中…' : '保存中…');
  busyPush('写入本地记录…');
  try {
    const payload = await API.jobAction(id, { action, value });
    for (const target of targets) {
      if (!mergeLocalFields(target, payload)) applyLocalAction(target, action, value);
    }
    const title = (listItem || detailItem || { title: id }).title;
    toast(`已更新本地${ACTION_LABELS[action] || '记录'}：${String(title).slice(0, 16)}`, 'ok');
  } catch (error) {
    for (const snapshot of snapshots) {
      snapshot.target.bookmarked = snapshot.bookmarked;
      snapshot.target.applied = snapshot.applied;
      snapshot.target.note = snapshot.note;
    }
    toast(`本地记录未写入：${error.message}`, 'error');
  } finally {
    busyPop();
    setPending(button, false);
    renderJobList();
    renderDetail();
  }
}

function populateCityOptions() {
  const select = $('#f-city');
  if (!select) return;
  const current = app.jobs.city;
  const names = new Set();
  if (app.overview) for (const row of app.overview.cities) if (row.name) names.add(row.name);
  const cities = app.draft && Array.isArray(app.draft.cities) ? app.draft.cities : [];
  for (const city of cities) {
    const name = str(city && city.name).trim();
    if (name) names.add(name);
  }
  clear(select);
  select.appendChild(h('option', { value: '', text: '全部城市' }));
  for (const name of [...names].sort((a, b) => a.localeCompare(b, 'zh'))) {
    select.appendChild(h('option', { value: name, text: name }));
  }
  select.value = current && names.has(current) ? current : '';
  if (select.value !== current) app.jobs.city = select.value;
}

function resetJobFilters() {
  app.jobs.q = '';
  app.jobs.city = '';
  app.jobs.tier = '';
  app.jobs.page = 1;
  app.jobs.cursor = -1;
  const q = $('#f-q');
  const city = $('#f-city');
  const tier = $('#f-tier');
  if (q) q.value = '';
  if (city) city.value = '';
  if (tier) tier.value = '';
  void loadJobs();
}

// ============================== 关键词 ==============================

async function loadKeywords(force = false) {
  if (app.keywords && !force) return;
  const token = ++keywordsToken;
  renderKeywords();
  try {
    const payload = await API.keywords();
    if (token !== keywordsToken) return;
    const data = payload && typeof payload === 'object' ? payload : {};
    app.keywords = {
      items: Array.isArray(data.items) ? data.items.map((row) => ({
        keyword: str(row && row.keyword),
        jobs: num(row && row.jobs),
        with_jd: num(row && row.with_jd),
        open_snapshot: num(row && row.open_snapshot),
        unique_jobs: num(row && row.unique_jobs),
        current_pages: num(row && row.current_pages),
        last_page: row ? row.last_page : null,
      })).filter((row) => row.keyword !== '') : [],
      caveat: str(data.caveat),
    };
    app.keywordsError = null;
  } catch (error) {
    if (token !== keywordsToken) return;
    app.keywordsError = error.message;
    app.keywords = null;
  } finally {
    if (token === keywordsToken) renderKeywords();
  }
}

function keywordStat(row, key) {
  const value = row ? row[key] : null;
  return value === null || value === undefined || value === '' ? '—' : fmtInt(value);
}

/** 每词页深输入框。
 *  ⚠️ 改动后**不重建表格**：只回调 onChanged 让所属行自己更新样式。
 *  重建会让下一次 mousedown 落在已被移除的节点上，浏览器便不派发 click。 */
function keywordDepthInput(keyword, index, onChanged) {
  const overrides = app.draft && app.draft.pages_per_keyword && typeof app.draft.pages_per_keyword === 'object'
    ? app.draft.pages_per_keyword
    : {};
  const current = num(overrides[keyword]);
  const fallback = num(app.draft ? app.draft.pages_per_query : null);
  const input = h('input', {
    type: 'number', id: `kw-depth-${index}`, min: '1', max: '30', step: '1',
    value: current === null ? '' : String(current),
    placeholder: fallback === null ? '' : String(fallback),
    'aria-label': `「${keyword}」的翻页深度：留空则使用默认页深${fallback === null ? '' : ` ${fallback} 页`}`,
  });
  input.addEventListener('change', () => {
    const raw = input.value.trim();
    if (raw === '') {
      input.setAttribute('aria-invalid', 'false');
      if (overrides[keyword] !== undefined) {
        delete overrides[keyword];
        markDirty();
        onChanged();
      }
      return;
    }
    const parsed = Number(raw);
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 30) {
      input.setAttribute('aria-invalid', 'true');
      toast('每词页深需为 1–30 的整数。', 'error');
      input.value = current === null ? '' : String(current);
      return;
    }
    input.setAttribute('aria-invalid', 'false');
    overrides[keyword] = parsed;
    markDirty();
    onChanged();
  });
  return input;
}

/** 已保存的"每词页深显式覆盖"值（没有覆盖时为 null）。
 *  ⚠️ 不能拿它和 keyword_stat.current_pages 比：后者是**生效值**（未显式设置时
 *  等于默认页深），拿生效值和"没有覆盖"比，会让每个用默认页深的词都显示"未保存"。 */
function savedKeywordOverride(keyword) {
  const saved = app.state && app.state.config ? app.state.config.pages_per_keyword : null;
  return saved && typeof saved === 'object' ? num(saved[keyword]) : null;
}

function keywordRow(keyword, index, stat, rowIndex) {
  const overrides = app.draft && app.draft.pages_per_keyword ? app.draft.pages_per_keyword : {};
  const unsavedChip = h('span', { class: 'chip chip-teal', text: '未保存', hidden: true });
  let syncRow = () => {};
  const row = h('tr', {},
    h('td', { class: 'kw-name' }, keyword),
    h('td', { class: 'num', text: keywordStat(stat, 'jobs') }),
    h('td', { class: 'num', text: keywordStat(stat, 'with_jd') }),
    h('td', { class: 'num', text: keywordStat(stat, 'open_snapshot') }),
    h('td', { class: 'num', text: keywordStat(stat, 'unique_jobs') }),
    h('td', {}, keywordDepthInput(keyword, index, () => syncRow())),
    h('td', { class: 'num', text: stat ? valueText(stat.last_page) : '—' }),
    h('td', { class: 'cell-actions' },
      unsavedChip,
      h('button', {
        class: 'btn btn-sm btn-danger', type: 'button',
        dataset: { act: 'kw-remove', keyword },
        'aria-label': `从配置中移除关键词 ${keyword}`,
        text: '移除',
      })));
  row.dataset.row = String(rowIndex);
  syncRow = () => {
    // 只比较"草稿里的显式覆盖"和"已保存的显式覆盖"：两边都没设置 = 用默认值 = 干净
    const draftDepth = num(overrides[keyword]);
    const savedDepth = savedKeywordOverride(keyword);
    const edited = (draftDepth === null ? null : draftDepth) !== (savedDepth === null ? null : savedDepth);
    row.classList.toggle('row-edited', edited);
    unsavedChip.hidden = !edited;
  };
  syncRow();
  return row;
}

function historicalKeywordRow(keyword, stat, rowIndex) {
  const row = h('tr', { class: 'row-hist' },
    h('td', { class: 'kw-name' }, h('span', { class: 'chip chip-quiet', text: '仅历史' }), ' ', keyword),
    h('td', { class: 'num', text: keywordStat(stat, 'jobs') }),
    h('td', { class: 'num', text: keywordStat(stat, 'with_jd') }),
    h('td', { class: 'num', text: keywordStat(stat, 'open_snapshot') }),
    h('td', { class: 'num', text: keywordStat(stat, 'unique_jobs') }),
    h('td', { class: 'num', text: stat ? valueText(stat.current_pages) : '—' }),
    h('td', { class: 'num', text: stat ? valueText(stat.last_page) : '—' }),
    h('td', { class: 'cell-actions' },
      h('button', {
        class: 'btn btn-sm', type: 'button',
        dataset: { act: 'kw-add-back', keyword },
        'aria-label': `把历史关键词 ${keyword} 加回配置`,
        text: '加回配置',
      })));
  row.dataset.row = String(rowIndex);
  return row;
}

function renderKeywords() {
  const tbody = $('#kw-tbody');
  const caveat = $('#kw-caveat');
  if (caveat) {
    if (app.keywordsError) {
      caveat.className = 'callout callout-bad';
      setText(caveat, `关键词统计读取失败：${app.keywordsError}`);
    } else {
      caveat.className = 'callout';
      setText(caveat, app.keywords && app.keywords.caveat
        ? app.keywords.caveat
        : '统计来自本地库的历史采集结果；新增关键词在采集前不会有任何数据。');
    }
  }
  if (!tbody) return;
  clear(tbody);

  if (!app.draft) {
    tbody.appendChild(h('tr', {}, h('td', { colspan: '8', class: 'empty', text: '正在载入配置…' })));
    return;
  }
  // 草稿可能来自老配置：这两个容器必须存在且类型正确，否则后面的写入会落到临时对象上
  if (!Array.isArray(app.draft.keywords)) app.draft.keywords = [];
  if (!app.draft.pages_per_keyword || typeof app.draft.pages_per_keyword !== 'object'
    || Array.isArray(app.draft.pages_per_keyword)) {
    app.draft.pages_per_keyword = {};
  }

  const configured = app.draft.keywords;
  const stats = new Map();
  if (app.keywords) for (const row of app.keywords.items) stats.set(row.keyword, row);
  if (app.keywordsError) {
    tbody.appendChild(h('tr', {}, h('td', { colspan: '8', class: 'empty', text: `统计读取失败：${app.keywordsError}` })));
  }

  if (!configured.length) {
    tbody.appendChild(h('tr', {}, h('td', { colspan: '8', class: 'empty', text: '配置里还没有关键词。' })));
  }
  configured.forEach((keyword, index) => {
    tbody.appendChild(keywordRow(keyword, index, stats.get(keyword), index));
  });

  const historical = [...stats.keys()].filter((keyword) => !configured.includes(keyword));
  for (const keyword of historical) {
    tbody.appendChild(historicalKeywordRow(keyword, stats.get(keyword), configured.length + historical.indexOf(keyword)));
  }

  updateSaveBar();
}

function addKeywords(raw) {
  if (!app.draft) return 0;
  if (!Array.isArray(app.draft.keywords)) app.draft.keywords = [];
  const parts = String(raw || '')
    .split(/[,，、;；\n\t]+/)
    .map((item) => item.trim())
    .filter((item) => item !== '');
  let added = 0;
  let duplicated = 0;
  for (const part of parts) {
    if (app.draft.keywords.includes(part)) { duplicated += 1; continue; }
    app.draft.keywords.push(part);
    added += 1;
  }
  if (added) markDirty();
  return { added, duplicated, total: parts.length };
}

function removeKeyword(keyword) {
  if (!app.draft) return;
  app.draft.keywords = (Array.isArray(app.draft.keywords) ? app.draft.keywords : [])
    .filter((item) => item !== keyword);
  if (app.draft.pages_per_keyword && typeof app.draft.pages_per_keyword === 'object') {
    delete app.draft.pages_per_keyword[keyword];
  }
  markDirty();
  renderKeywords();
}

// ============================== 配置 ==============================

/** 数字字段校验器注册表：保存前一并跑一遍，把错误集中展示。 */
const configValidators = new Map();

function fieldBlock(id, labelText, control, hintText) {
  const message = h('p', { class: 'field-msg', id: `${id}-msg` });
  const node = h('div', { class: 'field' },
    h('label', { for: id, text: labelText }),
    control,
    hintText ? h('p', { class: 'hint', id: `${id}-hint`, text: hintText }) : null,
    message);
  control.setAttribute('aria-describedby', hintText ? `${id}-hint ${id}-msg` : `${id}-msg`);
  return { node, message, control };
}

function numberField(options) {
  const input = h('input', {
    type: 'number', id: options.id, inputmode: 'decimal',
    min: String(options.min), max: String(options.max), step: String(options.step ?? 1),
  });
  const initial = options.get();
  input.value = initial === null || initial === undefined ? '' : String(initial);
  const block = fieldBlock(options.id, options.label, input, options.hint);

  const validate = () => {
    if (options.disabled && options.disabled()) {
      input.setAttribute('aria-invalid', 'false');
      setText(block.message, '');
      return '';
    }
    const raw = input.value.trim();
    if (raw === '') {
      if (options.nullable) {
        input.setAttribute('aria-invalid', 'false');
        setText(block.message, '');
        options.set(null);
        return '';
      }
      input.setAttribute('aria-invalid', 'true');
      setText(block.message, '需要填写数字。');
      return '需要填写数字。';
    }
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) {
      input.setAttribute('aria-invalid', 'true');
      setText(block.message, '需要是数字。');
      return '需要是数字。';
    }
    if (options.integer && !Number.isInteger(parsed)) {
      input.setAttribute('aria-invalid', 'true');
      setText(block.message, '需要是整数。');
      return '需要是整数。';
    }
    if (parsed < options.min || parsed > options.max) {
      const text = `需在 ${options.min}–${options.max} 之间。`;
      input.setAttribute('aria-invalid', 'true');
      setText(block.message, text);
      return text;
    }
    input.setAttribute('aria-invalid', 'false');
    setText(block.message, '');
    options.set(parsed);
    return '';
  };

  input.addEventListener('input', () => { validate(); markDirty(); });
  input.addEventListener('change', () => { validate(); markDirty(); });
  configValidators.set(options.id, { label: options.label, validate, input });

  return { node: block.node, input, validate };
}

function textField(options) {
  const input = h('input', {
    type: 'text', id: options.id, autocomplete: 'off',
    value: options.get() === null || options.get() === undefined ? '' : String(options.get()),
    placeholder: options.placeholder || '',
  });
  if (options.maxlength) input.setAttribute('maxlength', String(options.maxlength));
  const block = fieldBlock(options.id, options.label, input, options.hint);
  if (options.datalistId && Array.isArray(options.datalist)) {
    input.setAttribute('list', options.datalistId);
    block.node.appendChild(h('datalist', { id: options.datalistId },
      options.datalist.map((item) => h('option', { value: item }))));
  }
  input.addEventListener('input', () => { options.set(input.value); markDirty(); });
  return { node: block.node, input };
}

/** 下拉选择框。服务端给了本页不认识的取值时，原样列出并保持选中，不静默改值。 */
function selectField(options) {
  const select = h('select', { id: options.id });
  const known = options.options.map((option) => option.value);
  const current = options.get() === null || options.get() === undefined ? '' : String(options.get());
  for (const option of options.options) {
    select.appendChild(h('option', { value: option.value, text: option.label }));
  }
  if (current && !known.includes(current)) {
    select.appendChild(h('option', { value: current, text: `${current}（未识别的取值）` }));
  }
  select.value = current || (options.options[0] ? options.options[0].value : '');
  const block = fieldBlock(options.id, options.label, select, options.hint);
  select.addEventListener('change', () => { options.set(select.value); markDirty(); });
  return { node: block.node, select };
}

function checkField(options) {
  const input = h('input', { type: 'checkbox', id: options.id });
  input.checked = options.checked === true;
  const node = h('div', { class: 'field' },
    h('label', { class: 'switch', for: options.id }, input, h('span', { text: options.label })),
    options.hint ? h('p', { class: 'hint', text: options.hint }) : null);
  input.addEventListener('change', () => { options.onChange(input.checked); markDirty(); });
  return { node, input };
}

function tagEditor(options) {
  const list = options.values;
  const chips = h('div', { class: 'tags' });
  const input = h('input', {
    // id 必须等于 options.id：上面 fieldBlock 生成的 <label for> 指向它
    type: 'text', id: options.id, class: 'tag-input', autocomplete: 'off',
    placeholder: options.placeholder || '输入后回车添加（支持逗号分隔）',
  });
  const block = fieldBlock(options.id, options.label, chips, options.hint);
  chips.appendChild(input);

  const draw = () => {
    for (const node of [...chips.children]) if (node !== input) node.remove();
    list.forEach((value, index) => {
      const remove = h('button', {
        class: 'tag-remove', type: 'button', text: '×',
        'aria-label': `移除 ${value}`,
        onclick: () => { list.splice(index, 1); draw(); markDirty(); },
      });
      chips.insertBefore(h('span', { class: 'tag' }, h('span', { text: value }), remove), input);
    });
    if (!list.length) {
      chips.insertBefore(h('span', { class: 'tags-empty', text: options.emptyText || '（空）' }), input);
    }
  };

  const commit = () => {
    const parts = input.value.split(/[,，、;；\n\t]+/).map((item) => item.trim()).filter((item) => item !== '');
    if (!parts.length) { input.value = ''; return; }
    let added = 0;
    let skipped = 0;
    for (const part of parts) {
      const value = options.normalize ? options.normalize(part) : part;
      if (!value || list.includes(value)) continue;
      // 后端对每个名单有 max_length（例如 preferred_cities 20 项），超了会 422：
      // 这里先截住并说清楚，别让用户点保存才被拒。
      if (list.length >= (options.maxItems || Number.MAX_SAFE_INTEGER)) { skipped += 1; continue; }
      list.push(value);
      added += 1;
    }
    input.value = '';
    if (skipped) toast(`「${options.label}」最多 ${options.maxItems} 项，已忽略 ${skipped} 项。`, 'error');
    if (added) { draw(); markDirty(); }
  };

  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); commit(); }
    else if (event.key === 'Backspace' && input.value === '' && list.length) {
      list.pop(); draw(); markDirty();
    }
  });
  input.addEventListener('blur', commit);

  draw();
  return { node: block.node, input, draw };
}

function ensureArray(object, path) {
  let value = getPath(object, path);
  if (!Array.isArray(value)) {
    value = [];
    setPath(object, path, value);
  }
  return value;
}

function citySection(config) {
  const cities = Array.isArray(config.cities) ? config.cities : (config.cities = []);
  const rows = h('div', {});
  const draw = () => {
    clear(rows);
    cities.forEach((city, index) => {
      if (!city || typeof city !== 'object') { cities[index] = { name: '', boss_code: null }; }
      const entry = cities[index];
      const nameInput = h('input', {
        type: 'text', id: `cfg-city-name-${index}`, autocomplete: 'off',
        value: str(entry.name), placeholder: '例如：杭州',
      });
      nameInput.addEventListener('input', () => { entry.name = nameInput.value.trim(); markDirty(); });
      const codeInput = h('input', {
        type: 'text', id: `cfg-city-code-${index}`, autocomplete: 'off', inputmode: 'numeric',
        value: entry.boss_code === null || entry.boss_code === undefined ? '' : String(entry.boss_code),
        placeholder: '留空则按城市名自动补全',
      });
      codeInput.addEventListener('input', () => {
        const raw = codeInput.value.trim();
        entry.boss_code = raw === '' ? null : raw;
        markDirty();
      });
      rows.appendChild(h('div', { class: 'city-row' },
        fieldBlock(`cfg-city-name-${index}`, '城市名', nameInput).node,
        fieldBlock(`cfg-city-code-${index}`, 'BOSS 城市码', codeInput,
          '不是行政区划号；留空时按已知城市表自动补全。').node,
        h('button', {
          class: 'btn btn-sm btn-danger', type: 'button', text: '删除',
          'aria-label': `删除城市 ${str(entry.name) || index + 1}`,
          onclick: () => { cities.splice(index, 1); markDirty(); draw(); },
        })));
    });
    if (!cities.length) rows.appendChild(h('p', { class: 'empty', text: '尚未配置采集城市。' }));
  };
  draw();

  const node = h('fieldset', { class: 'group' },
    h('legend', { text: '采集城市' }),
    h('p', { class: 'group-note', text: '采集按这里的城市逐个搜索；城市码只影响平台侧的城市参数。' }),
    rows,
    h('div', { style: { marginTop: '10px' } },
      h('button', {
        class: 'btn btn-sm', type: 'button', text: '添加城市',
        onclick: () => { cities.push({ name: '', boss_code: null }); markDirty(); draw(); },
      })));
  return node;
}

/** 参考线处理方式 + 两条自设上限。
 *  日请求量 / 页深 / 关键词数都是本地经验参考线：参考模式只提示，强制模式才拦停。
 *  它们与平台的真实限制、当前冻结状态是两回事。 */
function referenceLineSection(config) {
  const grid = h('div', { class: 'grid' });

  grid.appendChild(selectField({
    id: 'cfg-budget-mode', label: '参考线处理方式',
    options: BUDGET_MODES,
    hint: '到线之后怎么办：只提示，还是直接停止本轮。这是本地自设的参考线，不是平台的安全额度。',
    get: () => str(config.budget_mode) || 'reference',
    set: (value) => { config.budget_mode = value; app.budgetModeEdited = true; },
  }).node);

  grid.appendChild(numberField({
    id: 'cfg-max-page-number', label: '单关键词最大页数（参考线）', min: 1, max: 30, integer: true,
    hint: '每个关键词最多翻多少页：参考模式下超出只提示，强制模式下超过即停止。',
    get: () => num(config.max_page_number) ?? 10,
    set: (value) => { if (value !== null) config.max_page_number = value; },
  }).node);

  grid.appendChild(numberField({
    id: 'cfg-max-keywords', label: '单轮关键词数（参考线）', min: 1, max: 50, integer: true,
    hint: '一轮最多搜索多少个关键词：参考模式下超出只提示，强制模式下超过即停止。',
    get: () => num(config.max_keywords_per_run) ?? 3,
    set: (value) => { if (value !== null) config.max_keywords_per_run = value; },
  }).node);

  return h('fieldset', { class: 'group' },
    h('legend', { text: '参考线处理方式' }),
    h('p', {
      class: 'group-note',
      text: '单日/单轮请求量、翻页深度、关键词数都是本机自设的经验参考线，写大了不代表平台允许，'
        + '写小了也不代表平台会保护你。平台的真实限制由风控状态决定，与这里的数字无关。',
    }),
    grid);
}

function budgetSection(config) {
  const fields = h('div', { class: 'grid' });
  const numeric = [
    {
      id: 'cfg-pages-per-query', label: '默认每词翻页深度（参考线）', min: 1, max: 10, integer: true,
      path: ['pages_per_query'], hint: '未在「关键词」页单独设置页深的关键词用这个值，属于经验参考。',
    },
    {
      id: 'cfg-max-run', label: '单轮请求参考线', min: 1, max: 5000, integer: true,
      path: ['max_requests_per_run'], hint: '单个采集进程的请求数参考线，不代表平台的安全额度。',
    },
    {
      id: 'cfg-max-day', label: '单日请求参考线', min: 1, max: 5000, integer: true,
      path: ['max_requests_per_day'], hint: '跨进程的每日请求数参考线，同样只是自设值，不是平台硬红线。',
    },
  ];
  for (const def of numeric) {
    fields.appendChild(numberField({
      id: def.id, label: def.label, min: def.min, max: def.max, integer: def.integer, hint: def.hint,
      get: () => num(getPath(config, def.path)),
      set: (value) => { if (value !== null) setPath(config, def.path, value); },
    }).node);
  }

  const delays = h('div', { class: 'field field-wide' },
    h('span', { class: 'field-label' }, '请求间隔（秒）'),
    h('div', { class: 'field-inline' },
      numberField({
        id: 'cfg-req-min', label: '列表最小', min: 0, max: 600, step: 0.5,
        get: () => num(getPath(config, ['request_delay_seconds', 0])),
        set: (value) => { if (value !== null) setPath(config, ['request_delay_seconds', 0], value); },
      }).node,
      numberField({
        id: 'cfg-req-max', label: '列表最大', min: 0, max: 600, step: 0.5,
        get: () => num(getPath(config, ['request_delay_seconds', 1])),
        set: (value) => { if (value !== null) setPath(config, ['request_delay_seconds', 1], value); },
      }).node,
      numberField({
        id: 'cfg-detail-min', label: '详情最小', min: 0, max: 600, step: 0.5,
        get: () => num(getPath(config, ['detail_delay_seconds', 0])),
        set: (value) => { if (value !== null) setPath(config, ['detail_delay_seconds', 0], value); },
      }).node,
      numberField({
        id: 'cfg-detail-max', label: '详情最大', min: 0, max: 600, step: 0.5,
        get: () => num(getPath(config, ['detail_delay_seconds', 1])),
        set: (value) => { if (value !== null) setPath(config, ['detail_delay_seconds', 1], value); },
      }).node),
    h('p', {
      class: 'hint',
      text: '每个请求之间随机等待的区间：列表用一组、详情用一组。这是本地自设的节流参数，'
        + '与平台侧的实际判定没有已知的确定关系，改大改小都不保证结果。',
    }));

  const node = h('fieldset', { class: 'group' },
    h('legend', { text: '采集预算与间隔' }),
    h('p', { class: 'group-note', text: '请求量是自设参考线，到线后如何处理见上面的「参考线处理方式」；保存不会真的发出任何请求。' }),
    fields,
    delays);
  return node;
}

function candidateSection(config) {
  if (!config.candidate || typeof config.candidate !== 'object') {
    // 服务端允许 candidate 为 null（等价于全默认值）。这里补成显式默认值，
    // 否则必填的数字框会是空的，用户一保存就被前端校验拦住。
    config.candidate = {
      cities: [], preferred_cities: [], graduation_year: null, education: '本科',
      max_experience_years: 0, salary_min_k: 0, salary_max_k: 40,
      roles: [], skills: [], exclude_internships: true, exclude_staffing: true,
      excluded_companies: [],
    };
  }
  const candidate = config.candidate;
  const node = h('fieldset', { class: 'group' },
    h('legend', { text: '候选人画像' }),
    h('p', { class: 'group-note', text: '这些条件决定哪些岗位被算作"够得着"，只影响本地筛选与排序，不会改动平台上的任何设置。' }));

  const grid = h('div', { class: 'grid' });

  grid.appendChild(tagEditor({
    id: 'cfg-cand-cities', label: '目标城市', values: ensureArray(candidate, ['cities']),
    hint: '留空 = 不限城市（全国）。名单外的岗位照样留在库里、也能被检索到，只是在本工作台里标成「暂不优先」；'
      + '不会写回原始岗位库，也不改动任何原始记录。',
    emptyText: '空 = 全国',
    placeholder: '输入城市名后回车（例如：杭州）',
    maxItems: 200,
  }).node);

  grid.appendChild(tagEditor({
    id: 'cfg-cand-preferred', label: '优先城市（同级加分）', values: ensureArray(candidate, ['preferred_cities']),
    hint: '列表内所有城市同级（命中即加同样的分），不区分先后、也不做准入；哪些算目标城市由上面的「目标城市」决定。',
    emptyText: '未设置',
    placeholder: '输入城市名后回车',
    maxItems: 20,
  }).node);

  const gradValue = num(candidate.graduation_year);
  const gradUnlimited = checkField({
    id: 'cfg-grad-any', label: '不限届别', checked: gradValue === null,
    hint: '勾选后不按毕业年份过滤。',
    onChange: (checked) => {
      if (checked) {
        candidate.graduation_year = null;
        const input = $('#cfg-grad-year');
        if (input) { input.value = ''; input.disabled = true; }
      } else {
        const input = $('#cfg-grad-year');
        if (input) input.disabled = false;
        if (candidate.graduation_year === null) candidate.graduation_year = new Date().getFullYear();
        if (input) input.value = String(candidate.graduation_year);
      }
    },
  });
  const gradField = numberField({
    id: 'cfg-grad-year', label: '毕业届别（年份）', min: 1990, max: 2100, integer: true, nullable: true,
    hint: '例如 2026 表示 26 届。',
    disabled: () => candidate.graduation_year === null,
    get: () => (candidate.graduation_year === undefined ? null : candidate.graduation_year),
    set: (value) => { candidate.graduation_year = value; },
  });
  gradField.input.disabled = gradValue === null;
  grid.appendChild(h('div', { class: 'field' }, h('span', { class: 'field-label' }, '届别'), gradUnlimited.node, gradField.node));

  grid.appendChild(textField({
    id: 'cfg-education', label: '学历要求', placeholder: '例如：本科', maxlength: 30,
    datalistId: 'cfg-education-options',
    datalist: ['不限', '大专', '本科', '硕士', '博士'],
    hint: '按平台返回值匹配，常见值为「本科」「硕士」「学历不限」（最多 30 字）。',
    get: () => str(candidate.education),
    set: (value) => { candidate.education = value; },
  }).node);

  grid.appendChild(numberField({
    id: 'cfg-max-exp', label: '经验年限参考上限（年）', min: 0, max: 40, integer: true,
    hint: '按岗位原文识别门槛：「1 年以内」这类写法包含 0 年；「经验不限 / 在校 / 应届」视为友好。'
      + '只有明显超出（≥3 年且超出上限 2 年以上）才判不符，其余标成需核实（可能可用项目经验替代）。',
    get: () => num(candidate.max_experience_years),
    set: (value) => { if (value !== null) candidate.max_experience_years = value; },
  }).node);

  grid.appendChild(numberField({
    id: 'cfg-salary-min', label: '期望薪资下限（K）', min: 0, max: 300, step: 0.5,
    hint: '按区间交集判定：只有岗位挂牌上限低于这个下限时才算薪资不符（可填 0–300）。',
    get: () => num(candidate.salary_min_k),
    set: (value) => { if (value !== null) candidate.salary_min_k = value; },
  }).node);

  grid.appendChild(numberField({
    id: 'cfg-salary-max', label: '期望薪资上限（K）', min: 0.5, max: 300, step: 0.5,
    hint: '岗位挂牌下限高于这个上限时只是标「需核实职级」，不会判成够不着（必须大于 0）。',
    get: () => num(candidate.salary_max_k),
    set: (value) => { if (value !== null) candidate.salary_max_k = value; },
  }).node);

  grid.appendChild(tagEditor({
    id: 'cfg-roles', label: '目标角色 / 方向', values: ensureArray(candidate, ['roles']),
    hint: '用于判断岗位方向是否对口。',
    emptyText: '未设置',
    placeholder: '例如：Agent 开发',
    maxItems: 60,
  }).node);

  grid.appendChild(tagEditor({
    id: 'cfg-skills', label: '技能关键词', values: ensureArray(candidate, ['skills']),
    hint: '命中越多，技术栈匹配得分越高（只加分，不作门槛）。',
    emptyText: '未设置',
    placeholder: '例如：Python',
    maxItems: 100,
  }).node);

  grid.appendChild(tagEditor({
    id: 'cfg-excluded-companies', label: '公司黑名单', values: ensureArray(candidate, ['excluded_companies']),
    hint: '命中公司名关键词的岗位会被标低优先级；原始记录不会被改动。',
    emptyText: '未设置',
    placeholder: '例如：某某人力',
    maxItems: 100,
  }).node);

  grid.appendChild(h('div', { class: 'field field-wide' },
    checkField({
      id: 'cfg-exclude-internships', label: '排除实习岗', checked: candidate.exclude_internships === true,
      onChange: (checked) => { candidate.exclude_internships = checked; },
    }).node,
    checkField({
      id: 'cfg-exclude-staffing', label: '排除外包 / 人力派遣', checked: candidate.exclude_staffing === true,
      onChange: (checked) => { candidate.exclude_staffing = checked; },
    }).node));

  node.appendChild(grid);
  return node;
}

function readonlySection(config) {
  // 不把内部字段名堆给用户看（曾列出 network_enabled 等 20 个键）：
  // 只需要说明"其余设置保持原值、去哪里改"。
  const autoChat = config && config.auto_chat && typeof config.auto_chat === 'object' ? config.auto_chat : null;
  const node = h('fieldset', { class: 'group' },
    h('legend', { text: '其他设置' }),
    h('p', {
      class: 'group-note',
      text: '其他高级设置保持原值，可在本地配置文件中管理；本页保存时不会改动它们。',
    }));
  if (autoChat) {
    node.appendChild(h('p', {
      class: 'hint',
      text: `自动开聊策略当前为：${autoChat.enabled === true ? '开启' : '关闭'}。本页不提供修改入口，保存时原样保留。`,
    }));
  }
  return node;
}

function renderConfig() {
  const root = $('#config-fields');
  if (!root) return;
  clear(root);
  configValidators.clear();
  if (!app.draft) {
    root.appendChild(h('p', { class: 'empty', text: app.stateError ? `配置读取失败：${app.stateError}` : '正在载入配置…' }));
    updateSaveBar();
    return;
  }
  if (app.stateError) {
    root.appendChild(h('p', { class: 'callout callout-bad', role: 'alert', text: `配置可能不是最新的：${app.stateError}` }));
  }
  const config = app.draft;
  app.budgetModeSupported = Object.prototype.hasOwnProperty.call(config, 'budget_mode');
  root.appendChild(citySection(config));
  root.appendChild(referenceLineSection(config));
  root.appendChild(budgetSection(config));
  root.appendChild(candidateSection(config));
  root.appendChild(readonlySection(config));
  updateSaveBar();
}

/** 校验配置草稿。
 *  返回 problems（阻断保存）与 warnings（只提示）。每条 problem 是
 *  `{ message, focus }`：focus 是应该获得焦点的控件 id，便于"保存失败后直接跳到出错的框"。 */
function validateConfig() {
  const problems = [];
  const warnings = [];
  const config = app.draft;
  if (!config) return { problems: [{ message: '配置尚未载入。' }], warnings };

  for (const entry of configValidators.values()) {
    const message = entry.validate();
    if (message) problems.push({ message: `${entry.label}：${message}`, focus: entry.input.id });
  }

  const cities = Array.isArray(config.cities) ? config.cities : [];
  if (!cities.length) problems.push({ message: '采集城市：至少需要配置一个城市。' });
  const seenCities = new Set();
  cities.forEach((city, index) => {
    const name = str(city && city.name).trim();
    if (!name) {
      problems.push({ message: `采集城市：第 ${index + 1} 行的城市名为空。`, focus: `cfg-city-name-${index}` });
    } else if (seenCities.has(name)) {
      problems.push({ message: `采集城市：「${name}」重复配置。`, focus: `cfg-city-name-${index}` });
    }
    if (name) seenCities.add(name);
    const code = city && city.boss_code;
    if (code !== undefined && code !== null && String(code).trim() !== ''
      && !/^\d{4,12}$/.test(String(code).trim())) {
      problems.push({
        message: `采集城市：「${name || `第 ${index + 1} 行`}」的城市码应为 4–12 位数字，或留空自动补全。`,
        focus: `cfg-city-code-${index}`,
      });
    }
  });

  const keywords = Array.isArray(config.keywords) ? config.keywords : [];
  const emptyKeywords = keywords.filter((keyword) => !str(keyword).trim()).length;
  if (emptyKeywords) problems.push({ message: `关键词：有 ${emptyKeywords} 个空关键词，请删除或补全。` });
  if (!keywords.length) warnings.push('关键词为空：配置可以保存，但下一次采集不会有任何搜索词。');

  const duplicates = keywords.filter((keyword, index) => keywords.indexOf(keyword) !== index);
  if (duplicates.length) {
    problems.push({ message: `关键词：存在重复项（${[...new Set(duplicates)].join('、')}）。` });
  }

  const overrides = config.pages_per_keyword && typeof config.pages_per_keyword === 'object'
    ? config.pages_per_keyword : {};
  for (const key of Object.keys(overrides)) {
    if (!keywords.includes(key)) {
      delete overrides[key];
      warnings.push(`已清理无效的每词页深：${key}（该关键词不在配置里）。`);
      continue;
    }
    const value = num(overrides[key]);
    if (value === null || !Number.isInteger(value) || value < 1 || value > 30) {
      const rowIndex = keywords.indexOf(key);
      problems.push({
        message: `每词页深「${key}」：需为 1–30 的整数。`,
        focus: rowIndex >= 0 ? `kw-depth-${rowIndex}` : undefined,
      });
    }
  }

  const delayPairs = [
    ['request_delay_seconds', '列表请求间隔', 'cfg-req-min', 'cfg-req-max'],
    ['detail_delay_seconds', '详情请求间隔', 'cfg-detail-min', 'cfg-detail-max'],
  ];
  for (const [key, label, minId, maxId] of delayPairs) {
    const values = Array.isArray(config[key]) ? config[key] : [];
    const min = num(values[0]);
    const max = num(values[1]);
    if (min === null || max === null) {
      problems.push({ message: `${label}：需要同时填写最小值和最大值。`, focus: minId });
    } else if (max < min) {
      problems.push({ message: `${label}：最大值不能小于最小值。`, focus: maxId });
    } else if (min < 3) {
      warnings.push(`${label}最小值 ${min} 秒低于常用的 3 秒参考值（自设参考，不是平台规定）。`);
    }
  }

  if (num(config.max_requests_per_run) !== null && num(config.max_requests_per_day) !== null
    && num(config.max_requests_per_run) > num(config.max_requests_per_day)) {
    warnings.push('单轮请求上限高于单日上限：实际会在日线处被截断。');
  }

  const candidate = config.candidate && typeof config.candidate === 'object' ? config.candidate : {};
  const salaryMin = num(candidate.salary_min_k);
  const salaryMax = num(candidate.salary_max_k);
  if (salaryMin !== null && salaryMax !== null && salaryMax < salaryMin) {
    problems.push({ message: '候选人薪资：上限不能小于下限。', focus: 'cfg-salary-max' });
  }

  // ---- 参考线处理方式 ----
  const mode = str(config.budget_mode) || 'reference';
  const modeValues = BUDGET_MODES.map((option) => option.value);
  if (!modeValues.includes(mode)) {
    problems.push({ message: `参考线处理方式：取值 ${mode} 不是可选项。`, focus: 'cfg-budget-mode' });
  }
  // 服务端 config 里还没有这个键时，保存时不会硬塞（extra="forbid" 会 422）：
  // 要么保持原样直接保存，要么先重启本地服务。
  if (!app.budgetModeSupported && app.budgetModeEdited) {
    warnings.push('当前运行的服务还没有「参考线处理方式」字段：这一项可能保存不进去，重启本地服务后再试。');
  }
  if (mode === 'enforce') {
    const cap = num(config.max_page_number) ?? 10;
    const over = Object.keys(overrides).filter((key) => (num(overrides[key]) ?? 0) > cap);
    if (over.length) {
      warnings.push(`强制限制模式下，${over.length} 个关键词的页深超过参考线上限 ${cap} 页，会被截断到 ${cap} 页。`);
    }
  }

  return { problems, warnings };
}

function markDirty() {
  const wasDirty = app.dirty;
  app.dirty = true;
  if (!wasDirty && app.conflict) app.conflict = false;
  // 提示类问题（间隔偏小、日线矛盾等）随编辑实时更新；报错列表只在已经报过错时刷新，
  // 否则用户刚开始改一个数字就会看到一堆红字。
  const { problems, warnings } = validateConfig();
  app.saveWarnings = warnings;
  if (app.saveProblems.length) app.saveProblems = problems;
  updateSaveBar();
  renderBootStatus();
}

/** 窄屏时详情是抽屉：没选中岗位时把它设为 inert，避免 Tab 跑进看不见的面板。 */
function syncDetailInert() {
  const panel = $('#job-detail');
  if (!panel) return;
  if (isNarrow() && !app.jobs.selectedId) panel.setAttribute('inert', '');
  else panel.removeAttribute('inert');
}

/** 保存条：结构只建一次，之后原地更新。
 *
 * ⚠️ 不要在 markDirty 时重建保存条的 DOM：输入框失焦会触发 change 事件，而
 * mousedown 之后、mouseup 之前把按钮从文档里换掉，浏览器就不会派发 click ——
 * 表现为"第一次点保存没反应，得点第二次"。所以这里只改文本与 disabled。 */
const saveBarRefs = new Map();

function buildSaveBar(root) {
  const status = h('p', { class: 'savebar-status', role: 'status', 'aria-live': 'polite' });
  const saveButton = h('button', {
    class: 'btn btn-primary', type: 'button', text: '保存配置',
    onclick: (event) => { void saveConfig(event.currentTarget); },
  });
  const discardButton = h('button', {
    class: 'btn btn-quiet', type: 'button', text: '放弃修改',
    onclick: () => discardDraft(),
  });
  const reloadButton = h('button', {
    class: 'btn', type: 'button', text: '重新载入最新配置', hidden: true,
    onclick: () => { void reloadStateWithConfirm(); },
  });
  const warnings = h('ul', { class: 'savebar-warn', hidden: true });
  const problems = h('ul', { class: 'error-list', role: 'alert', hidden: true });
  const inner = h('div', { class: 'savebar-inner' },
    status,
    h('div', { class: 'savebar-actions' }, saveButton, discardButton, reloadButton),
    h('div', { class: 'savebar-msgs' }, warnings, problems));
  clear(root);
  root.appendChild(inner);
  const refs = { root, inner, status, saveButton, discardButton, reloadButton, warnings, problems };
  saveBarRefs.set(root.id, refs);
  return refs;
}

function fillMessageList(list, items) {
  clear(list);
  for (const item of items) list.appendChild(h('li', { text: typeof item === 'string' ? item : item.message }));
  list.hidden = items.length === 0;
}

function updateSaveBar() {
  for (const id of ['config-savebar', 'kw-savebar']) {
    const root = document.getElementById(id);
    if (!root) continue;
    let refs = saveBarRefs.get(id);
    if (!refs || !refs.inner.isConnected) refs = buildSaveBar(root);

    const loaded = Boolean(app.state);
    const dirty = loaded && app.dirty;
    if (!loaded) {
      setText(refs.status, '配置尚未载入，暂时不能保存。');
    } else if (app.conflict) {
      clear(refs.status);
      append(refs.status, ['配置在别处已被修改（409），本页改动', h('b', { text: '没有保存' }), '。']);
    } else if (dirty) {
      clear(refs.status);
      append(refs.status, [h('b', { text: '有未保存的修改' }), '（保存只写本地配置文件，不会启动采集）']);
    } else {
      setText(refs.status, '配置与本地文件一致');
    }

    refs.inner.className = `savebar-inner${app.conflict ? ' conflict' : (dirty ? ' dirty' : '')}`;
    refs.saveButton.disabled = !dirty || app.conflict || app.saving === true;
    refs.discardButton.disabled = !dirty || app.saving === true;
    refs.reloadButton.hidden = !app.conflict;
    fillMessageList(refs.warnings, app.saveWarnings);
    fillMessageList(refs.problems, app.saveProblems);
  }
}

function focusFirstProblem() {
  const withFocus = app.saveProblems.find((problem) => problem && problem.focus);
  const target = withFocus ? document.getElementById(withFocus.focus) : null;
  const first = target || document.querySelector('#config-fields [aria-invalid="true"]')
    || document.querySelector('#kw-table [aria-invalid="true"]');
  if (first) {
    first.focus();
    if (typeof first.scrollIntoView === 'function') first.scrollIntoView({ block: 'center', behavior: 'auto' });
  }
}

async function saveConfig(button) {
  if (!app.state || !app.draft) return;
  if (app.conflict) {
    toast('配置在别处已被修改，请先重新载入最新配置。', 'error');
    return;
  }
  const { problems, warnings } = validateConfig();
  app.saveProblems = problems;
  app.saveWarnings = warnings;
  updateSaveBar();
  if (problems.length) {
    toast(`配置有 ${problems.length} 处需要修正，未保存。`, 'error');
    focusFirstProblem();
    return;
  }

  // 后端 CollectionConfig 是 extra="forbid"：服务端 config 里没有 budget_mode 时
  // （例如本地服务还是旧版本），不改这一项就不要把键塞过去，否则整份配置都会被 422 拒。
  const payloadConfig = deepClone(app.draft);
  if (!app.budgetModeSupported && !app.budgetModeEdited) delete payloadConfig.budget_mode;

  const body = { revision: app.state.revision, config: payloadConfig };
  app.saving = true;
  setPending(button, true, '保存中…');
  updateSaveBar();
  busyPush('保存配置…');
  try {
    const payload = await API.saveConfig(body);
    app.state = normalizeState(payload);
    app.draft = deepClone(app.state.config);
    app.dirty = false;
    app.conflict = false;
    app.budgetModeEdited = false;
    app.saveProblems = [];
    app.saveWarnings = [];
    app.stateError = null;
    renderStatus();
    renderMeta();
    renderConfig();
    renderKeywords();
    populateCityOptions();
    if (app.keywords) void loadKeywords(true);
    toast(`配置已保存（修订 ${shortRevision(app.state.revision)}）。未启动任何采集。`, 'ok');
  } catch (error) {
    if (error.status === 409) {
      app.conflict = true;
      app.saveProblems = [{
        message: '配置在别处已被修改（409）。为避免覆盖他人的改动，本页没有保存；'
          + '请点「重新载入最新配置」拿到最新版本后重新编辑。',
      }];
      toast('配置已被其他会话修改（409），本页未保存。', 'error');
    } else {
      app.saveProblems = [{ message: `保存失败：${error.message}` }];
      toast(`配置未保存：${error.message}`, 'error');
    }
    updateSaveBar();
  } finally {
    app.saving = false;
    busyPop();
    setPending(button, false);
    updateSaveBar();
  }
}

function discardDraft() {
  if (!app.state) return;
  if (app.dirty && !window.confirm('放弃未保存的配置修改？')) return;
  app.draft = deepClone(app.state.config);
  app.dirty = false;
  app.conflict = false;
  app.budgetModeEdited = false;
  app.saveProblems = [];
  app.saveWarnings = [];
  renderConfig();
  renderKeywords();
  populateCityOptions();
  updateSaveBar();
  renderBootStatus();
  toast('已放弃未保存的修改。');
}

// ============================== 路由与事件 ==============================

function viewFromHash() {
  const raw = (window.location.hash || '').replace(/^#\/?/, '').split(/[?&]/)[0].trim();
  return VIEWS.includes(raw) ? raw : 'overview';
}

async function showView(name, options = {}) {
  const view = VIEWS.includes(name) ? name : 'overview';
  app.view = view;
  for (const candidate of VIEWS) {
    const section = $(`#view-${candidate}`);
    if (section) section.hidden = candidate !== view;
  }
  setActiveNav(view);
  if (view === 'overview') {
    renderOverview();
    await loadOverview(options.force === true);
  } else if (view === 'jobs') {
    renderJobs();
    if (options.force === true || !app.jobs.data) await loadJobs();
  } else if (view === 'keywords') {
    renderKeywords();
    await loadKeywords(options.force === true);
  } else if (view === 'config') {
    renderConfig();
  }
}

function wireEvents() {
  const reload = $('#reload');
  if (reload) reload.addEventListener('click', () => { void reloadStateWithConfirm(); });

  window.addEventListener('hashchange', () => { void showView(viewFromHash()); });

  // ---- 筛选 ----
  const q = $('#f-q');
  if (q) {
    q.addEventListener('input', () => {
      app.jobs.q = q.value.trim();
      app.jobs.page = 1;
      app.jobs.cursor = -1;
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => { void loadJobs(); }, 260);
    });
    q.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        q.value = '';
        app.jobs.q = '';
        app.jobs.page = 1;
        void loadJobs();
      }
    });
  }

  const city = $('#f-city');
  if (city) {
    city.addEventListener('change', () => {
      app.jobs.city = city.value;
      app.jobs.page = 1;
      app.jobs.cursor = -1;
      void loadJobs();
    });
  }

  const tier = $('#f-tier');
  if (tier) {
    tier.addEventListener('change', () => {
      app.jobs.tier = tier.value;
      app.jobs.page = 1;
      app.jobs.cursor = -1;
      void loadJobs();
    });
  }

  const size = $('#f-size');
  if (size) {
    size.addEventListener('change', () => {
      app.jobs.page_size = int0(size.value) || 30;
      app.jobs.page = 1;
      void loadJobs();
    });
  }

  const reset = $('#f-reset');
  if (reset) reset.addEventListener('click', () => resetJobFilters());

  const filters = $('#jobs-filters');
  if (filters) filters.addEventListener('submit', (event) => event.preventDefault());

  // ---- 列表与详情：事件委托 ----
  const list = $('#jobs-list');
  if (list) {
    list.addEventListener('click', (event) => {
      const button = event.target instanceof Element ? event.target.closest('[data-act]') : null;
      if (!button) return;
      const card = button.closest('.job');
      const id = button.dataset.id || (card ? card.dataset.id : '');
      if (!id) return;
      const action = button.dataset.act;
      const item = findJob(id);
      if (action === 'detail') openDetail(id);
      else if (action === 'bookmark') void runJobAction(id, 'bookmark', !(item && item.bookmarked), button);
      else if (action === 'applied') void runJobAction(id, 'applied', !(item && item.applied), button);
    });
  }

  const detail = $('#job-detail');
  if (detail) {
    detail.addEventListener('click', (event) => {
      const button = event.target instanceof Element ? event.target.closest('[data-act]') : null;
      if (!button) return;
      const id = button.dataset.id || app.jobs.selectedId;
      if (!id) return;
      const action = button.dataset.act;
      const item = findJob(id);
      if (action === 'bookmark') void runJobAction(id, 'bookmark', !(item && item.bookmarked), button);
      else if (action === 'applied') void runJobAction(id, 'applied', !(item && item.applied), button);
      else if (action === 'note-save') {
        const textarea = $('#detail-note');
        void runJobAction(id, 'note', textarea ? textarea.value : '', button);
      } else if (action === 'reset') {
        const ok = window.confirm('重置这条岗位的本地记录（收藏 / 已投 / 备注）？只改本机数据库。');
        if (ok) void runJobAction(id, 'reset', true, button);
      }
    });
  }

  const detailClose = $('#detail-close');
  if (detailClose) detailClose.addEventListener('click', () => closeDetail());

  const backdrop = $('#detail-backdrop');
  if (backdrop) backdrop.addEventListener('click', () => closeDetail());

  // ---- 关键词 ----
  const addButton = $('#kw-add-btn');
  const newKeyword = $('#kw-new');
  const addMessage = $('#kw-add-msg');
  const runAdd = () => {
    if (!newKeyword) return;
    const raw = newKeyword.value.trim();
    if (!raw) {
      if (addMessage) { addMessage.className = 'hint savebar-warn'; setText(addMessage, '请输入关键词。'); }
      newKeyword.focus();
      return;
    }
    const result = addKeywords(raw);
    newKeyword.value = '';
    if (addMessage) {
      addMessage.className = 'hint';
      const bits = [];
      if (result.added) bits.push(`已加入 ${result.added} 个关键词（尚未保存）`);
      if (result.duplicated) bits.push(`${result.duplicated} 个已存在`);
      setText(addMessage, bits.join('；') || '没有可添加的关键词。');
    }
    renderKeywords();
    if (newKeyword) newKeyword.focus();
  };
  if (addButton) addButton.addEventListener('click', runAdd);
  if (newKeyword) {
    newKeyword.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); runAdd(); }
    });
  }

  const kwBody = $('#kw-tbody');
  if (kwBody) {
    kwBody.addEventListener('click', (event) => {
      const button = event.target instanceof Element ? event.target.closest('[data-act]') : null;
      if (!button) return;
      const keyword = button.dataset.keyword;
      if (!keyword) return;
      if (button.dataset.act === 'kw-remove') {
        removeKeyword(keyword);
        toast(`已从配置移除关键词「${keyword}」（尚未保存）。`);
      } else if (button.dataset.act === 'kw-add-back') {
        addKeywords(keyword);
        renderKeywords();
        toast(`已把「${keyword}」加回配置（尚未保存）。`);
      }
    });
  }

  // ---- 配置表单：避免回车提交 ----
  const form = $('#config-form');
  if (form) form.addEventListener('submit', (event) => event.preventDefault());

  // ---- 键盘快捷键 ----
  document.addEventListener('keydown', (event) => {
    const target = event.target;
    const tag = target && target.tagName ? target.tagName : '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    if (event.key === 'Escape') {
      if (app.jobs.selectedId) { event.preventDefault(); closeDetail(); }
      return;
    }
    if (app.view !== 'jobs') return;

    const items = app.jobs.data ? app.jobs.data.items : [];
    if (event.key === '/') {
      event.preventDefault();
      const search = $('#f-q');
      if (search) search.focus();
      return;
    }
    if (event.key === 'j' || event.key === 'ArrowDown') {
      if (!items.length) return;
      event.preventDefault();
      app.jobs.cursor = Math.min(app.jobs.cursor + 1, items.length - 1);
      renderJobList();
      const node = document.querySelector(`#jobs-list .job[data-index="${app.jobs.cursor}"]`);
      if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' });
      return;
    }
    if (event.key === 'k' || event.key === 'ArrowUp') {
      if (!items.length) return;
      event.preventDefault();
      app.jobs.cursor = Math.max(app.jobs.cursor - 1, 0);
      renderJobList();
      const node = document.querySelector(`#jobs-list .job[data-index="${app.jobs.cursor}"]`);
      if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' });
      return;
    }
    if (event.key === 'Enter' && app.jobs.cursor >= 0 && items[app.jobs.cursor]) {
      event.preventDefault();
      openDetail(items[app.jobs.cursor].id);
    }
  });

  window.addEventListener('resize', () => {
    if (!isNarrow()) {
      document.body.classList.remove('detail-open');
      const backdrop = $('#detail-backdrop');
      if (backdrop) backdrop.hidden = true;
    }
    syncDetailInert();
  });
}

// ============================== 启动 ==============================

async function boot() {
  wireEvents();
  const view = viewFromHash();
  app.view = view;
  setActiveNav(view);
  renderStatus();
  renderMeta();
  renderBootStatus();
  updateSaveBar();
  syncDetailInert();

  const ok = await loadState();
  updateSaveBar();
  await showView(view, { force: false });

  if (ok) {
    // 城市下拉需要概览里的城市名；即使用户停在别的区也顺手读一次本地统计
    if (!app.overview) void loadOverview(false);
  } else {
    toast('无法连接本地服务：请确认管理服务已在本机运行。', 'error', {
      label: '重试',
      run: () => { void reloadStateWithConfirm(); },
    });
  }
}

void boot();
