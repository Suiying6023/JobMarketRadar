/**
 * core.js —— DOM 构造、格式化与事件总线。
 *
 * 安全边界（改动前先读）：
 *  1. 本模块不使用 innerHTML / outerHTML / insertAdjacentHTML，h() 甚至显式拒绝
 *     html 属性。所有来自接口的字符串只经 textContent 进入 DOM，尖括号只能是文字。
 *  2. 不构造绝对 URL：所有外链先过 safeJobUrl()，只有 BOSS 站内 job_detail 链接可点击。
 */

// ============================== DOM 工具 ==============================

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

/**
 * 创建元素。props 里的 on* 一律走 addEventListener（兼容 CSP：不允许内联事件属性）。
 * @param {string} tag
 * @param {object|null} props
 * @param {...any} children
 */
export function h(tag, props, ...children) {
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
        // 只经 CSSOM 写内联样式（CSP style-src 不含 unsafe-inline 时，style 属性会被拦）
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

export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function setText(node, text) {
  if (node) node.textContent = text === null || text === undefined ? '' : String(text);
  return node;
}

/** 把任意值渲染成一行可读文本；对象降级为 JSON，避免 [object Object] 混进界面。 */
export function valueText(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (Array.isArray(value)) return value.length ? value.map(valueText).join('、') : '（空）';
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch { return '（无法显示）'; }
  }
  return String(value);
}

// ============================== 类型与格式化 ==============================

const INT_FMT = new Intl.NumberFormat('zh-CN');
const TIME_FMT = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });

export function num(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function int0(value) {
  const n = num(value);
  return n === null ? 0 : Math.round(n);
}

export function clamp(value, lo, hi) {
  return Math.min(hi, Math.max(lo, value));
}

export function fmtInt(value) {
  const n = num(value);
  return n === null ? '—' : INT_FMT.format(Math.round(n));
}

export function fmtDecimal(value, digits = 1) {
  const n = num(value);
  if (n === null) return '—';
  return n.toFixed(digits).replace(/\.0+$/, '');
}

export function str(value) {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'boolean') return value ? '是' : '否';
  return '';
}

export function strList(value) {
  if (Array.isArray(value)) return value.map(str).filter((item) => item.trim() !== '');
  if (typeof value === 'string' && value.trim() !== '') return [value];
  return [];
}

export function parseTime(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function fmtTime(value) {
  const date = parseTime(value);
  if (date) return TIME_FMT.format(date);
  return value ? String(value) : '—';
}

/** 相对时间。未来时间用「后」，避免把 15 天后显示成「刚刚」。 */
export function relTime(value) {
  const date = parseTime(value);
  if (!date) return '';
  const diffMs = Date.now() - date.getTime();
  const suffix = diffMs < 0 ? '后' : '前';
  const minutes = Math.round(Math.abs(diffMs) / 60000);
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

export function ratioText(part, whole) {
  const p = num(part);
  const w = num(whole);
  if (p === null || w === null || w <= 0) return '—';
  return `${fmtDecimal((p / w) * 100)}%`;
}

export function ratioOf(part, whole) {
  const p = num(part);
  const w = num(whole);
  if (p === null || w === null || w <= 0) return null;
  return clamp(p / w, 0, 1);
}

export function deepClone(value) {
  if (value === null || typeof value !== 'object') return value;
  if (typeof structuredClone === 'function') {
    try { return structuredClone(value); } catch { /* 回退到 JSON 深拷贝 */ }
  }
  try { return JSON.parse(JSON.stringify(value)); } catch { return value; }
}

export function getPath(object, path) {
  return path.reduce((acc, key) => (acc === null || acc === undefined ? undefined : acc[key]), object);
}

export function setPath(object, path, value) {
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

export function ensureArray(object, path) {
  let value = getPath(object, path);
  if (!Array.isArray(value)) {
    value = [];
    setPath(object, path, value);
  }
  return value;
}

/** 统一词条比较键：NFKC 规范 + 去首尾空白 + 大小写折叠。用于去重与统计对齐。 */
export function termKey(value) {
  return String(value === null || value === undefined ? '' : value)
    .normalize('NFKC').trim().toLocaleLowerCase('en-US');
}

export function uid(prefix = 'id') {
  uid.seq = (uid.seq || 0) + 1;
  return `${prefix}-${uid.seq}`;
}

// ============================== 外链白名单 ==============================

const SAFE_JOB_URL_PREFIX = 'https://www.zhipin.com/job_detail/';
const SAFE_JOB_HOST = 'www.zhipin.com';

/** 只有 https://www.zhipin.com/job_detail/<id>.html 形态才允许渲染成可点链接。 */
export function safeJobUrl(raw) {
  const value = typeof raw === 'string' ? raw.trim() : '';
  if (!value.startsWith(SAFE_JOB_URL_PREFIX)) return null;
  let parsed;
  try { parsed = new URL(value); } catch { return null; }
  if (parsed.protocol !== 'https:') return null;
  if (parsed.hostname !== SAFE_JOB_HOST) return null;
  if (!/^\/job_detail\/[A-Za-z0-9_~-]+\.html$/.test(parsed.pathname)) return null;
  return parsed.href;
}

export function externalLink(href, label, className = 'btn btn-sm') {
  return h('a', { class: className, href, target: '_blank', rel: 'noopener noreferrer', text: label });
}

// ============================== 事件总线 ==============================

const listeners = new Map();

export const bus = {
  on(name, fn) {
    if (!listeners.has(name)) listeners.set(name, new Set());
    listeners.get(name).add(fn);
    return () => bus.off(name, fn);
  },
  off(name, fn) {
    const set = listeners.get(name);
    if (set) set.delete(fn);
  },
  emit(name, payload) {
    const set = listeners.get(name);
    if (!set) return;
    for (const fn of Array.from(set)) {
      try { fn(payload); } catch (error) { console.error('[bus]', name, error); }
    }
  },
};
