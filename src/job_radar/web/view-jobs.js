/**
 * view-jobs.js —— 岗位池：检索、分页、JD 原文阅读与本地复核标记（/api/jobs）。
 *
 * 本地标记（关注 / 已投 / 备注）只写本机复核库，不向平台发送任何内容。
 */
import { API } from './api.js';
import {
  clear, deepClone, externalLink, fmtInt, fmtTime, h, num, relTime, safeJobUrl, setText, str, strList, valueText,
} from './core.js';
import { BUCKETS, app, isNarrow } from './store.js';
import { badge, errorBlock, loadingBlock, setPending, toast } from './ui.js';
import { pager } from './components.js';

const BUCKET_BADGE = {
  '优先核实': 'badge-priority',
  '需核实门槛': 'badge-gate',
  '信息不足': 'badge-unknown',
  '暂不优先': 'badge-later',
};

/**
 * 「是否在招」的五档。判定在服务端 `analysis/market.py::hiring_of` 里做一次，
 * 这里只负责显示——前端不做第二套判断，避免两处口径漂移。
 */
const HIRING_BADGE = {
  open: 'badge-ok',
  warm: 'badge-run',
  cold: 'badge-warn',
  stale: 'badge-later',
  closed: 'badge-bad',
  unknown: 'badge-neutral',
};

const HIRING_FILTERS = [
  { value: '', label: '全部在招状态' },
  { value: 'open', label: '在招（HR 近期活跃）' },
  { value: 'warm', label: '近期活跃' },
  { value: 'cold', label: '一月内动过' },
  { value: 'stale', label: '长期未动' },
  { value: 'closed', label: '平台标已关闭' },
  { value: 'unknown', label: '无法判断' },
];

export function hiringFilterChoices() {
  return HIRING_FILTERS.slice();
}

let searchTimer = null;
let jobsToken = 0;

export function normalizeItem(raw) {
  const item = raw && typeof raw === 'object' ? raw : {};
  const decision = item.decision && typeof item.decision === 'object' ? item.decision : {};
  return {
    id: item.id === undefined || item.id === null ? '' : String(item.id),
    title: str(item.title) || '（无标题）',
    company: str(item.company),
    city: str(item.city),
    salary: str(item.salary),
    experience: str(item.experience),
    education: str(item.education),
    industry: str(item.industry),
    company_size: str(item.company_size),
    url: str(item.url),
    detail_fetched_at: str(item.detail_fetched_at),
    stale: item.stale === true,
    snapshot_age_days: num(item.snapshot_age_days),
    snapshot_status: str(item.snapshot_status),
    duplicate_count: num(item.duplicate_count),
    decision: {
      bucket: str(decision.bucket) || '信息不足',
      score: num(decision.score),
      reasons: strList(decision.reasons),
      concerns: strList(decision.concerns),
      evidence: strList(decision.evidence),
    },
    skills: strList(item.skills),
    bookmarked: item.bookmarked === true,
    applied: item.applied === true,
    note: str(item.note),
    /** 模型标注（/api/jobs 附带的最近一次打标结果） */
    annotation: item.annotation && typeof item.annotation === 'object' ? {
      payload: item.annotation.payload && typeof item.annotation.payload === 'object' ? item.annotation.payload : {},
      model: str(item.annotation.model),
      created_at: str(item.annotation.created_at),
      stale: item.annotation.stale === true,
    } : null,
    job_description: str(item.job_description),
    matched_queries: strList(item.matched_queries),
    legacy_match: item.legacy_match === undefined || item.legacy_match === null ? '' : str(item.legacy_match),
    legacy_match_reason: str(item.legacy_match_reason),
    legacy_retention: str(item.legacy_retention),
    first_seen_at: str(item.first_seen_at),
    /** 在招信号：服务端已判定好的五档 + 原始证据，前端只显示不重算。 */
    hiring: (() => {
      const raw = item.hiring && typeof item.hiring === 'object' ? item.hiring : {};
      const level = str(raw.level) || 'unknown';
      return {
        level: HIRING_BADGE[level] ? level : 'unknown',
        label: str(raw.label) || '在招状态未知',
        hr_text: str(raw.hr_text),
        job_state: str(raw.job_state),
        age_days: num(raw.age_days),
        seen_count: num(raw.seen_count),
      };
    })(),
  };
}

function normalizeJobsPayload(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  return {
    total: num(data.total) ?? 0,
    page: num(data.page) ?? 1,
    page_size: num(data.page_size) ?? 30,
    items: Array.isArray(data.items) ? data.items.map(normalizeItem) : [],
  };
}

function currentParams(extra = {}) {
  const jobs = app.jobs;
  return {
    q: jobs.q, city: jobs.city, tier: jobs.tier, hiring: jobs.hiring,
    page: jobs.page, page_size: jobs.page_size, ...extra,
  };
}

export async function loadJobs() {
  const token = ++jobsToken;
  app.jobs.loading = true;
  app.jobs.error = null;
  renderJobs();
  try {
    const payload = normalizeJobsPayload(await API.jobs(currentParams()));
    if (token !== jobsToken) return;
    app.jobs.data = payload;
  } catch (error) {
    if (token !== jobsToken) return;
    app.jobs.error = error.message;
  } finally {
    if (token === jobsToken) {
      app.jobs.loading = false;
      renderJobs();
    }
  }
}

export function cityChoices() {
  const counts = new Map();
  if (app.overview) {
    for (const row of app.overview.cities) counts.set(row.name, row.jobs);
  }
  for (const city of (app.options ? app.options.cities : [])) {
    if (!counts.has(city.name)) counts.set(city.name, null);
  }
  return [...counts.entries()]
    .filter(([name]) => name && name !== '未提供')
    .sort((a, b) => (b[1] ?? -1) - (a[1] ?? -1) || a[0].localeCompare(b[0], 'zh-CN'))
    .map(([name, jobs]) => ({ name, jobs }));
}

export function populateJobFilters() {
  const citySelect = document.getElementById('f-city');
  if (citySelect) {
    const previous = app.jobs.city;
    clear(citySelect);
    citySelect.appendChild(h('option', { value: '', text: '全部城市' }));
    for (const city of cityChoices()) {
      citySelect.appendChild(h('option', {
        value: city.name,
        text: city.jobs === null ? city.name : `${city.name}（${fmtInt(city.jobs)}）`,
      }));
    }
    citySelect.value = previous;
    if (citySelect.value !== previous) {
      citySelect.value = '';
      app.jobs.city = '';
    }
  }
  const tierSelect = document.getElementById('f-tier');
  if (tierSelect) {
    const previous = app.jobs.tier;
    clear(tierSelect);
    tierSelect.appendChild(h('option', { value: '', text: '全部优先级' }));
    const buckets = (app.overview && app.overview.candidate_funnel.length)
      ? app.overview.candidate_funnel.map((row) => row.label)
      : BUCKETS;
    for (const bucket of buckets) tierSelect.appendChild(h('option', { value: bucket, text: bucket }));
    tierSelect.value = previous;
    if (tierSelect.value !== previous) { tierSelect.value = ''; app.jobs.tier = ''; }
  }
  const hiringSelect = document.getElementById('f-hiring');
  if (hiringSelect) {
    const previous = app.jobs.hiring;
    clear(hiringSelect);
    for (const choice of HIRING_FILTERS) {
      hiringSelect.appendChild(h('option', { value: choice.value, text: choice.label }));
    }
    hiringSelect.value = previous;
    if (hiringSelect.value !== previous) { hiringSelect.value = ''; app.jobs.hiring = ''; }
  }
}

function hiringChip(item) {
  return badge(item.hiring.label, HIRING_BADGE[item.hiring.level] || 'badge-neutral');
}

/** 「挂了多久 / HR 多久没动」这一行：在招判断里最直观的两个数。 */
function hiringLine(item) {
  const parts = [];
  if (item.hiring.hr_text) parts.push(`HR ${item.hiring.hr_text}`);
  const age = item.hiring.age_days;
  if (age !== null && age !== undefined) {
    parts.push(age < 1 ? '今天首次见到' : `已挂 ${Math.round(age)} 天`);
  }
  if (item.hiring.seen_count) parts.push(`见过 ${item.hiring.seen_count} 次`);
  return parts.join(' · ');
}

function jobCard(item, index) {
  const meta = [
    item.company || '未提供公司',
    item.city || '未提供城市',
    item.salary || '未提供薪资',
    item.experience || '经验未提供',
    item.education || '学历未提供',
  ];
  const hiringText = hiringLine(item);
  return h('li', {
    class: `job${app.jobs.selectedId === item.id ? ' is-selected' : ''}`,
    dataset: { id: item.id, index: String(index) },
  },
  h('div', { class: 'job-main' },
    h('div', { class: 'job-head' },
      h('button', {
        class: 'job-title', type: 'button', 'data-act': 'detail', 'data-id': item.id,
        onclick: () => openDetail(item.id),
      }, item.title)),
    h('p', { class: 'job-meta', text: meta.join(' · ') }),
    h('p', { class: 'job-snapshot' },
      item.detail_fetched_at
        ? `详情快照 ${fmtTime(item.detail_fetched_at)}（${relTime(item.detail_fetched_at)}）${item.stale ? ' · 偏旧' : ''}`
        : '没有详情快照时间'),
    hiringText ? h('p', { class: 'job-hiring' }, hiringText) : null),
  h('div', { class: 'job-badges', 'aria-label': '岗位标签' },
    hiringChip(item),
    badge(item.decision.bucket, BUCKET_BADGE[item.decision.bucket] || 'badge-neutral'),
    item.decision.score === null ? null : badge(`排序分 ${item.decision.score}`, 'badge-quiet'),
    item.duplicate_count && item.duplicate_count > 1 ? badge(`疑似重复 ×${item.duplicate_count}`, 'badge-quiet') : null),
  h('div', { class: 'job-actions' },
    h('button', {
      class: 'btn btn-sm btn-quiet', type: 'button', 'data-act': 'detail', 'data-id': item.id,
      onclick: () => openDetail(item.id),
    }, '阅读原文'),
    h('button', {
      class: `btn btn-sm${item.bookmarked ? ' is-on' : ''}`, type: 'button', 'data-act': 'bookmark', 'data-id': item.id,
      onclick: (event) => { void runJobAction(item.id, 'bookmark', !item.bookmarked, event.currentTarget); },
    }, item.bookmarked ? '已关注' : '关注'),
    h('button', {
      class: `btn btn-sm${item.applied ? ' is-on' : ''}`, type: 'button', 'data-act': 'applied', 'data-id': item.id,
      onclick: (event) => { void runJobAction(item.id, 'applied', !item.applied, event.currentTarget); },
    }, item.applied ? '已投递（本地）' : '标记已投递')));
}

export function renderJobs() {
  const list = document.getElementById('jobs-list');
  const summary = document.getElementById('jobs-summary');
  const emptyNode = document.getElementById('jobs-empty');
  const pagerHost = document.getElementById('jobs-pager');
  if (!list) return;

  clear(list);
  clear(pagerHost);
  populateJobFilters();

  const jobs = app.jobs;
  if (jobs.loading && !jobs.data) {
    list.appendChild(h('li', {}, loadingBlock('正在读取岗位池…')));
    setText(summary, '正在读取…');
    if (emptyNode) emptyNode.hidden = true;
    return;
  }
  if (jobs.error && !jobs.data) {
    list.appendChild(h('li', {}, errorBlock(`无法读取岗位池：${jobs.error}`, () => { void loadJobs(); })));
    setText(summary, '读取失败。');
    if (emptyNode) emptyNode.hidden = true;
    return;
  }
  const data = jobs.data;
  if (!data) return;

  if (!data.items.length) {
    if (emptyNode) emptyNode.hidden = false;
    setText(summary, '没有匹配的岗位。');
    return;
  }
  if (emptyNode) emptyNode.hidden = true;

  data.items.forEach((item, index) => list.appendChild(jobCard(item, index)));

  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  setText(summary, `匹配 ${fmtInt(data.total)} 条，按复核优先级排序；第 ${data.page} / ${pages} 页。`);
  pagerHost.appendChild(pager(data.page, pages, data.total, (next) => goToPage(next)));
}

export function goToPage(page) {
  const data = app.jobs.data;
  if (!data) return;
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  app.jobs.page = Math.min(Math.max(1, page), pages);
  app.jobs.cursor = -1;
  void loadJobs();
}

export function findJob(id) {
  const data = app.jobs.data;
  if (data) {
    const found = data.items.find((item) => item.id === id);
    if (found) return found;
  }
  if (app.jobs.detail && app.jobs.detail.id === id) return app.jobs.detail;
  return null;
}

export async function loadDetail(id) {
  app.jobs.detailLoading = true;
  app.jobs.detailError = null;
  if (app.jobs.selectedId !== id) {
    app.jobs.detail = null;
    app.jobs.selectedId = id;
  }
  renderDetail();
  try {
    app.jobs.detail = normalizeItem(await API.job(id));
    app.jobs.detailError = null;
  } catch (error) {
    app.jobs.detailError = error.message;
  } finally {
    app.jobs.detailLoading = false;
    renderDetail();
  }
}

export function openDetail(id) {
  app.jobs.selectedId = id;
  app.jobs.detail = findJob(id);
  syncDetailInert();
  renderDetail();
  renderJobs();
  if (isNarrow()) {
    document.body.classList.add('detail-open');
    const backdrop = document.getElementById('detail-backdrop');
    if (backdrop) backdrop.hidden = false;
  }
  void loadDetail(id);
}

export function closeDetail() {
  app.jobs.selectedId = null;
  app.jobs.detail = null;
  app.jobs.detailError = null;
  document.body.classList.remove('detail-open');
  const backdrop = document.getElementById('detail-backdrop');
  if (backdrop) backdrop.hidden = true;
  syncDetailInert();
  renderDetail();
  renderJobs();
}

export function syncDetailInert() {
  const panel = document.getElementById('job-detail');
  if (!panel) return;
  if (isNarrow() && !app.jobs.selectedId) panel.setAttribute('inert', '');
  else panel.removeAttribute('inert');
}

/** 详情刷新时不覆盖用户正在编辑、尚未保存的备注。 */
function pendingNoteDraft(id) {
  const textarea = document.getElementById('detail-note');
  if (!textarea || app.jobs.selectedId !== id) return null;
  if (app.jobs.renderedNote !== undefined && textarea.value !== app.jobs.renderedNote) return textarea.value;
  return null;
}

function decisionBlock(item) {
  const decision = item.decision;
  const rows = [
    ['优先级', decision.bucket],
    ['排序分', decision.score === null ? '—' : String(decision.score)],
  ];
  const node = h('div', { class: 'detail-block' },
    h('h4', { text: '复核依据' }),
    h('dl', { class: 'kv' }, rows.map(([key, value]) => h('div', { class: 'kv-row' },
      h('dt', { text: key }), h('dd', { text: value })))),
    decision.reasons.length
      ? h('div', {}, h('p', { class: 'detail-label', text: '加分依据' }),
        h('ul', { class: 'detail-list' }, decision.reasons.map((text) => h('li', { text }))))
      : null,
    decision.concerns.length
      ? h('div', {}, h('p', { class: 'detail-label', text: '待核实项' }),
        h('ul', { class: 'detail-list' }, decision.concerns.map((text) => h('li', { text }))))
      : null,
    decision.evidence.length
      ? h('div', {}, h('p', { class: 'detail-label', text: '原文证据' }),
        h('ul', { class: 'detail-list evidence' }, decision.evidence.map((text) => h('li', { text }))))
      : null);
  return node;
}

export function renderDetail() {
  const body = document.getElementById('detail-body');
  const title = document.getElementById('detail-h');
  if (!body) return;
  clear(body);

  if (!app.jobs.selectedId) {
    setText(title, '岗位详情');
    body.appendChild(h('p', { class: 'empty', text: '从左侧列表选择一条岗位，这里显示本机缓存的 JD 原文与本地复核记录。' }));
    return;
  }
  const item = app.jobs.detail;
  if (!item) {
    setText(title, '岗位详情');
    body.appendChild(app.jobs.detailError
      ? errorBlock(`无法读取岗位详情：${app.jobs.detailError}`, () => { void loadDetail(app.jobs.selectedId); })
      : loadingBlock('正在读取岗位详情…'));
    return;
  }

  setText(title, item.title);
  const url = safeJobUrl(item.url);

  body.appendChild(h('div', { class: 'detail-block' },
    h('p', { class: 'detail-meta', text: [item.company || '未提供公司', item.city || '未提供城市',
      item.salary || '未提供薪资', item.experience || '经验未提供', item.education || '学历未提供',
      item.company_size || null, item.industry || null].filter(Boolean).join(' · ') }),
    h('div', { class: 'job-badges' },
      hiringChip(item),
      badge(item.decision.bucket, BUCKET_BADGE[item.decision.bucket] || 'badge-neutral'),
      item.duplicate_count && item.duplicate_count > 1 ? badge(`疑似重复 ×${item.duplicate_count}`, 'badge-quiet') : null),
    h('p', { class: 'detail-meta' },
      item.detail_fetched_at
        ? `详情快照 ${fmtTime(item.detail_fetched_at)}（${relTime(item.detail_fetched_at)}）`
        : '没有详情快照时间'),
    h('p', { class: 'detail-meta', text: `在招判断：${item.hiring.label}`
      + (item.hiring.job_state ? ` · 平台状态「${item.hiring.job_state}」` : '')
      + (hiringLine(item) ? ` · ${hiringLine(item)}` : '') }),
    url ? externalLink(url, '在原站打开该岗位', 'btn btn-sm') : h('p', { class: 'hint', text: '该记录没有可用的站内链接。' })));

  const noteDraft = pendingNoteDraft(item.id);
  const noteArea = h('textarea', { id: 'detail-note', rows: '4', placeholder: '记录核实情况，最多 3000 字' });
  noteArea.value = noteDraft === null ? item.note : noteDraft;
  app.jobs.renderedNote = item.note;

  body.appendChild(h('div', { class: 'detail-block' },
    h('h4', { text: '本地复核记录' }),
    h('p', { class: 'hint', text: '仅写入本机复核库。' }),
    h('div', { class: 'detail-actions' },
      h('button', {
        class: `btn btn-sm${item.bookmarked ? ' is-on' : ''}`, type: 'button',
        onclick: (event) => { void runJobAction(item.id, 'bookmark', !item.bookmarked, event.currentTarget); },
      }, item.bookmarked ? '已关注' : '关注'),
      h('button', {
        class: `btn btn-sm${item.applied ? ' is-on' : ''}`, type: 'button',
        onclick: (event) => { void runJobAction(item.id, 'applied', !item.applied, event.currentTarget); },
      }, item.applied ? '已投递（本地）' : '标记已投递'),
      h('button', {
        class: 'btn btn-sm btn-quiet', type: 'button',
        onclick: (event) => {
          const ok = window.confirm('重置这条岗位的本地记录（关注 / 已投 / 备注）？只改本机复核库。');
          if (ok) void runJobAction(item.id, 'reset', true, event.currentTarget);
        },
      }, '重置本地记录')),
    h('div', { class: 'field' },
      h('label', { for: 'detail-note', text: '复核备注' }),
      noteArea,
      h('button', {
        class: 'btn btn-sm', type: 'button',
        onclick: (event) => {
          const textarea = document.getElementById('detail-note');
          void runJobAction(item.id, 'note', textarea ? textarea.value : '', event.currentTarget);
        },
      }, '保存备注'))));

  body.appendChild(decisionBlock(item));

  if (item.annotation && Object.keys(item.annotation.payload).length) {
    const annotationLabels = {
      role: '岗位方向', skills: '技术导航', education: '学历要求', experience: '经验要求',
      employment_type: '岗位性质', responsibilities: '职责', evidence: '原文依据',
      required_skills: '要求相关技术', preferred_skills: '优先相关技术',
      requirement_notes: '择一、优先与放宽条件', fresh_graduate: '对应届生的明确表态',
      graduation_years: '明确届别', school_requirement: '院校条件', work_schedule: '工作制度',
    };
    const rows = Object.entries(item.annotation.payload)
      .filter(([key]) => !['evidence_ids', 'requirement_note_ids'].includes(key))
      .map(([key, value]) => [annotationLabels[key] || key, valueText(value)]);
    body.appendChild(h('div', { class: 'detail-block' },
      h('h4', { text: '模型标注' }),
      h('p', { class: 'hint', text: '技术名称用于定位要求；是否必须同时掌握、能否放宽，请结合条件原句核实。' }),
      item.annotation.stale ? h('p', {class:'callout callout-info',text:'岗位原文或标注配置已更新，可重新打标生成当前版本。'}) : null,
      h('p', { class: 'hint', text: [item.annotation.model || '未记录模型',
        item.annotation.created_at ? fmtTime(item.annotation.created_at) : null].filter(Boolean).join(' · ') }),
      h('dl', { class: 'kv' }, rows.map(([key, value]) => h('div', { class: 'kv-row' },
        h('dt', { text: key }), h('dd', { text: value }))))));
  }

  const extra = [];
  if (item.matched_queries.length) extra.push(['命中的搜索词', item.matched_queries.join('、')]);
  if (item.skills.length) extra.push(['列表与历史累计技能标签', item.skills.join('、')]);
  if (item.legacy_match) extra.push(['历史模型判定', `${item.legacy_match}${item.legacy_match_reason ? ` · ${item.legacy_match_reason}` : ''}`]);
  if (item.legacy_retention) extra.push(['历史保留分层', item.legacy_retention]);
  if (item.first_seen_at) extra.push(['首次入库', fmtTime(item.first_seen_at)]);
  if (extra.length) {
    body.appendChild(h('div', { class: 'detail-block' },
      h('h4', { text: '入库与标签' }),
      h('dl', { class: 'kv' }, extra.map(([key, value]) => h('div', { class: 'kv-row' },
        h('dt', { text: key }), h('dd', { text: value }))))));
  }

  body.appendChild(h('div', { class: 'detail-block' },
    h('h4', { text: 'JD 原文' }),
    item.job_description
      ? h('pre', { class: 'jd-text', text: item.job_description })
      : h('p', { class: 'empty', text: '本机没有这条岗位的 JD 正文；可在任务页发起「补全岗位详情」。' })));
}

export async function runJobAction(id, action, value, button) {
  setPending(button, true, '写入中…');
  try {
    await API.jobAction(id, { action, value });
    applyLocalAction(id, action, value);
    toast('本地记录已更新。', 'ok');
  } catch (error) {
    toast(`本地记录未更新：${error.message}`, 'error');
  } finally {
    setPending(button, false);
  }
}

function applyLocalAction(id, action, value) {
  const apply = (item) => {
    if (!item || item.id !== id) return item;
    if (action === 'bookmark') return { ...item, bookmarked: value === true };
    if (action === 'applied') return { ...item, applied: value === true };
    if (action === 'note') return { ...item, note: str(value) };
    if (action === 'reset') return { ...item, bookmarked: false, applied: false, note: '' };
    return item;
  };
  if (app.jobs.data) {
    app.jobs.data = { ...app.jobs.data, items: app.jobs.data.items.map(apply) };
  }
  if (app.jobs.detail && app.jobs.detail.id === id) app.jobs.detail = apply(deepClone(app.jobs.detail));
  renderJobs();
  renderDetail();
}

export function resetJobFilters() {
  app.jobs.q = '';
  app.jobs.city = '';
  app.jobs.tier = '';
  app.jobs.hiring = '';
  app.jobs.page = 1;
  app.jobs.cursor = -1;
  const q = document.getElementById('f-q');
  if (q) q.value = '';
  populateJobFilters();
  void loadJobs();
}

export function updateExportHref() {
  const link = document.getElementById('export-csv');
  if (!link) return;
  link.href = API.exportJobsHref(currentParams({ page: '', page_size: '' }));
}

export function wireJobsEvents() {
  const q = document.getElementById('f-q');
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

  const city = document.getElementById('f-city');
  if (city) {
    city.addEventListener('change', () => {
      app.jobs.city = city.value;
      app.jobs.page = 1;
      app.jobs.cursor = -1;
      void loadJobs();
    });
  }

  const tier = document.getElementById('f-tier');
  if (tier) {
    tier.addEventListener('change', () => {
      app.jobs.tier = tier.value;
      app.jobs.page = 1;
      app.jobs.cursor = -1;
      void loadJobs();
    });
  }

  const hiring = document.getElementById('f-hiring');
  if (hiring) {
    hiring.addEventListener('change', () => {
      app.jobs.hiring = hiring.value;
      app.jobs.page = 1;
      app.jobs.cursor = -1;
      void loadJobs();
    });
  }

  const size = document.getElementById('f-size');
  if (size) {
    size.addEventListener('change', () => {
      app.jobs.page_size = num(size.value) || 30;
      app.jobs.page = 1;
      void loadJobs();
    });
  }

  const reset = document.getElementById('f-reset');
  if (reset) reset.addEventListener('click', () => resetJobFilters());

  const filters = document.getElementById('jobs-filters');
  if (filters) filters.addEventListener('submit', (event) => event.preventDefault());

  const close = document.getElementById('detail-close');
  if (close) close.addEventListener('click', () => closeDetail());

  const backdrop = document.getElementById('detail-backdrop');
  if (backdrop) backdrop.addEventListener('click', () => closeDetail());

  window.addEventListener('resize', () => {
    if (!isNarrow()) {
      document.body.classList.remove('detail-open');
      const node = document.getElementById('detail-backdrop');
      if (node) node.hidden = true;
    }
    syncDetailInert();
  });
}

/** 岗位池快捷键：仅在本视图内生效。 */
export function handleJobsKey(event) {
  if (app.view !== 'jobs') return;
  const items = app.jobs.data ? app.jobs.data.items : [];
  if (event.key === '/') {
    event.preventDefault();
    const search = document.getElementById('f-q');
    if (search) search.focus();
    return;
  }
  if (event.key === 'j' || event.key === 'ArrowDown') {
    if (!items.length) return;
    event.preventDefault();
    app.jobs.cursor = Math.min(app.jobs.cursor + 1, items.length - 1);
    renderJobs();
    highlightCursor();
    return;
  }
  if (event.key === 'k' || event.key === 'ArrowUp') {
    if (!items.length) return;
    event.preventDefault();
    app.jobs.cursor = Math.max(app.jobs.cursor - 1, 0);
    renderJobs();
    highlightCursor();
    return;
  }
  if (event.key === 'Enter' && app.jobs.cursor >= 0 && items[app.jobs.cursor]) {
    event.preventDefault();
    openDetail(items[app.jobs.cursor].id);
  }
}

function highlightCursor() {
  const node = document.querySelector(`#jobs-list .job[data-index="${app.jobs.cursor}"]`);
  if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' });
}
