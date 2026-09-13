/**
 * view-terms.js —— 词表集中管理：搜索词、目标角色、技能关键词与各类排除词。
 *
 * 所有词表读写同一份配置草稿（app.draft），保存走配置页底部共用的保存条。
 * 覆盖统计来自 /api/keywords（只对搜索词有意义）。
 */
import { API } from './api.js';
import { fmtInt, h, num, termKey } from './core.js';
import { app, candidateDefaults, markDirty, updateSaveBar } from './store.js';
import { emptyBlock, errorBlock, loadingBlock, toast } from './ui.js';
import { termGroup } from './components.js';

const termStats = {
  map: new Map(),
  caveat: '',
  error: null,
  loading: false,
};

let groups = [];
let built = false;

function normalizeStats(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const map = new Map();
  for (const row of Array.isArray(data.items) ? data.items : []) {
    const keyword = row && row.keyword !== undefined ? String(row.keyword) : '';
    if (!keyword) continue;
    map.set(termKey(keyword), {
      keyword,
      jobs: num(row.jobs) ?? 0,
      with_jd: num(row.with_jd) ?? 0,
      open_snapshot: num(row.open_snapshot) ?? 0,
      unique_jobs: num(row.unique_jobs) ?? 0,
      current_pages: num(row.current_pages),
      last_page: num(row.last_page) ?? 0,
      enabled: row.enabled === true,
    });
  }
  return { map, caveat: typeof data.caveat === 'string' ? data.caveat : '' };
}

export async function loadTermStats(force = false) {
  if (termStats.loading) return;
  if (termStats.map.size && !force) return;
  termStats.loading = true;
  try {
    const parsed = normalizeStats(await API.keywords());
    termStats.map = parsed.map;
    termStats.caveat = parsed.caveat;
    termStats.error = null;
  } catch (error) {
    termStats.error = error.message;
  } finally {
    termStats.loading = false;
    refreshGroups();
  }
}

function refreshGroups() {
  for (const group of groups) group.refresh();
}

function listHandle(path) {
  return {
    get: () => {
      const value = path.reduce((node, key) => (node === null || node === undefined ? undefined : node[key]), app.draft);
      return Array.isArray(value) ? value : [];
    },
    set: (next) => {
      if (!app.draft) return;
      let node = app.draft;
      for (let i = 0; i < path.length - 1; i += 1) {
        if (node[path[i]] === null || typeof node[path[i]] !== 'object') node[path[i]] = {};
        node = node[path[i]];
      }
      node[path[path.length - 1]] = next;
    },
  };
}

function ensureCandidate() {
  if (!app.draft) return {};
  if (!app.draft.candidate || typeof app.draft.candidate !== 'object') {
    app.draft.candidate = candidateDefaults();
  }
  return app.draft.candidate;
}

function keywordStats(value) {
  const row = termStats.map.get(termKey(value));
  if (!row) return [{ text: '—', num: true }, { text: '—', num: true }, { text: '—', num: true }];
  return [
    { text: fmtInt(row.jobs), num: true },
    { text: fmtInt(row.unique_jobs), num: true },
    { text: row.last_page ? String(row.last_page) : '—', num: true },
  ];
}

function depthInput(value) {
  const overrides = app.draft && app.draft.pages_per_keyword && typeof app.draft.pages_per_keyword === 'object'
    ? app.draft.pages_per_keyword : null;
  const current = overrides ? overrides[value] : undefined;
  const input = h('input', {
    type: 'number', class: 'term-depth', min: '1', max: '30', step: '1', id: `terms-depth-${termKey(value) || 'x'}`,
    'aria-label': `「${value}」每词页深`,
  });
  input.value = current === undefined || current === null ? '' : String(current);
  input.placeholder = '默认';
  input.addEventListener('change', () => {
    if (!app.draft) return;
    if (!app.draft.pages_per_keyword || typeof app.draft.pages_per_keyword !== 'object') app.draft.pages_per_keyword = {};
    const raw = input.value.trim();
    if (raw === '') {
      delete app.draft.pages_per_keyword[value];
      input.setAttribute('aria-invalid', 'false');
      markDirty();
      return;
    }
    const parsed = Number(raw);
    if (!Number.isInteger(parsed) || parsed < 1 || parsed > 30) {
      input.setAttribute('aria-invalid', 'true');
      toast(`「${value}」每词页深需为 1–30 的整数。`, 'error');
      return;
    }
    input.setAttribute('aria-invalid', 'false');
    app.draft.pages_per_keyword[value] = parsed;
    markDirty();
  });
  return input;
}

function buildGroups() {
  groups = [];

  const keywordList = listHandle(['keywords']);
  groups.push(termGroup({
    id: 'terms-keywords',
    title: '搜索词',
    note: '采集时按这些词逐个搜索。覆盖统计来自本机库，仅用于判断这个词采到了多少岗位。',
    get: keywordList.get,
    set: (next) => { keywordList.set(next); pruneOverrides(next); },
    onChange: markDirty,
    columns: [{ label: '岗位', num: true }, { label: '独有覆盖', num: true }, { label: '上轮末页', num: true }],
    stats: keywordStats,
    rowExtra: (value) => depthInput(value),
    maxItems: 5000,
    placeholder: '例如：大模型应用开发',
    emptyText: '尚未配置搜索词。',
    openByDefault: true,
    importNote: '每行一个搜索词；导入时按 Unicode 规范化去重，重复项会被跳过。',
  }));

  if (!app.draft) return;
  ensureCandidate();

  const simple = (path, title, note, maxItems, placeholder, openByDefault = false) => {
    const handle = listHandle(path);
    groups.push(termGroup({
      id: `terms-${path.join('-')}`,
      title,
      note,
      get: handle.get,
      set: handle.set,
      onChange: markDirty,
      maxItems,
      placeholder,
      emptyText: '列表为空。',
      openByDefault,
    }));
  };

  simple(['candidate', 'roles'], '目标角色 / 方向', '用于判断岗位方向是否对口；参与画像评分。', 60, '例如：Agent 开发', true);
  simple(['candidate', 'skills'], '技能关键词', '命中越多，技术栈得分越高。', 100, '例如：Python', true);
  simple(['include_title_keywords'], '标题必含词', '标题命中任一即保留；留空 = 不做标题限制。', 500, '例如：工程师');
  simple(['exclude_title_keywords'], '标题排除词', '标题命中任一即跳过该岗位。', 500, '例如：销售');
  simple(['exclude_company_keywords'], '公司排除词', '公司名命中任一即跳过该岗位。', 500, '例如：某某人力');
  simple(['candidate', 'excluded_companies'], '公司黑名单', '命中后该岗位在复核排序中降级；原始记录保留。', 100, '例如：某某科技');
  simple(['candidate', 'excluded_titles'], '岗位排除词', '命中后该岗位在复核排序中降级。', 100, '例如：外包');
}

/** 搜索词删除后同步清理它的每词页深覆盖，避免后端 422。 */
function pruneOverrides(keywords) {
  if (!app.draft || !app.draft.pages_per_keyword || typeof app.draft.pages_per_keyword !== 'object') return;
  const known = new Set((Array.isArray(keywords) ? keywords : []).map((item) => termKey(item)));
  for (const key of Object.keys(app.draft.pages_per_keyword)) {
    if (!known.has(termKey(key))) delete app.draft.pages_per_keyword[key];
  }
}

export function renderTerms(force = false) {
  const host = document.getElementById('terms-body');
  if (!host) return;

  if (!app.draft) {
    host.textContent = '';
    host.appendChild(app.stateError
      ? errorBlock(`配置读取失败：${app.stateError}`)
      : loadingBlock('正在载入配置…'));
    built = false;
    updateSaveBar();
    return;
  }

  const signature = `${app.draft.keywords ? app.draft.keywords.length : 0}|${app.state ? app.state.revision : ''}`;
  if (built && !force && host.dataset.signature === signature) {
    refreshGroups();
    updateSaveBar();
    return;
  }

  host.textContent = '';
  built = true;
  host.dataset.signature = signature;

  host.appendChild(h('p', {
    class: 'callout callout-info',
    role: 'note',
    text: '词表与「配置」页共用同一份配置；这里改动后需要在页面底部点保存才会写入本地配置文件。',
  }));

  if (termStats.error) {
    host.appendChild(errorBlock(`搜索词覆盖统计读取失败：${termStats.error}`, () => { void loadTermStats(true); }));
  } else {
    host.appendChild(h('div', { class: 'terms-stats-line' },
      h('button', {
        class: 'btn btn-sm btn-quiet', type: 'button', text: '刷新覆盖统计',
        onclick: () => { void loadTermStats(true); },
      }),
      h('span', { class: 'hint', text: '覆盖统计基于已采集的岗位记录。' })));
  }

  buildGroups();
  for (const group of groups) host.appendChild(group.node);

  if (!groups.length) host.appendChild(emptyBlock('没有可编辑的词表。'));
  updateSaveBar();
}
