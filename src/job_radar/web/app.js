/**
 * app.js —— 启动、路由、顶栏状态与全局事件。
 *
 * 安全边界：所有写操作带 X-CSRF-Token；数据字符串只经 textContent 进入 DOM；
 * 页面上没有任何内部脚本、内联事件属性或外部资源（CSP: default-src 'self'）。
 */
import { $, append, bus, fmtTime, h, relTime, setText, str } from './core.js';
import { app, loadOptions, loadState, reloadStateWithConfirm, startStatePolling, updateSaveBar } from './store.js';
import { busy, moreBlock, safetyLabel, safetySummary, sanitizeTechnical, toast } from './ui.js';
import { loadOverview, renderStats } from './view-stats.js';
import {
  handleJobsKey, loadJobs, populateJobFilters, renderDetail, renderJobs, syncDetailInert,
  updateExportHref, wireJobsEvents,
} from './view-jobs.js';
import { loadTermStats, renderTerms } from './view-terms.js';
import { renderConfig } from './view-config.js';
import {
  loadTasks, onVisibilityChange, renderAccessPanel, renderTasks, stopTaskPolling, wireTasksEvents,
} from './view-tasks.js';

const VIEWS = ['stats', 'jobs', 'terms', 'tasks', 'config'];

/** 旧地址兼容：#/overview → 岗位池总览，#/keywords → 关键词，#/notes → 岗位池总览。 */
const VIEW_ALIASES = { overview: 'stats', keywords: 'terms', notes: 'stats', home: 'stats' };

const FROZEN_RE = /frozen|offline|paused|stop|halt|hold|suspend|cooldown/i;

// ============================== 顶栏 ==============================

export function renderBootStatus() {
  const node = $('#boot-status');
  if (!node) return;
  if (busy.count > 0) {
    node.className = 'pill pill-busy';
    setText(node, busy.text || '同步中…');
  } else if (app.stateError && !app.state) {
    node.className = 'pill pill-bad';
    setText(node, '状态读取失败');
  } else if (app.dirty) {
    node.className = 'pill pill-dirty';
    setText(node, '配置有未保存修改');
  } else {
    node.className = 'pill';
    setText(node, '就绪');
  }
}

export function renderStatus() {
  const box = $('#safety');
  if (!box) return;
  box.textContent = '';
  box.className = 'safety';
  box.appendChild(h('span', { class: 'safety-dot', 'aria-hidden': 'true' }));
  const text = h('span', { class: 'safety-text' });
  box.appendChild(text);

  if (app.stateError) {
    box.classList.add('safety-error');
    text.appendChild(h('b', { text: '无法读取访问状态' }));
    text.appendChild(document.createTextNode(`：${app.stateError}`));
    box.appendChild(h('button', {
      class: 'btn btn-sm', type: 'button', text: '重试',
      onclick: () => { void reloadStateWithConfirm(); },
    }));
    return;
  }
  if (!app.state) {
    box.classList.add('safety-loading');
    setText(text, '正在读取访问策略…');
    return;
  }

  const safety = app.state.safety;
  const frozen = !safety.mode || FROZEN_RE.test(safety.mode);
  box.classList.add(frozen ? 'safety-frozen' : 'safety-warn');
  append(text, [
    '在线访问：', h('b', { text: safetyLabel(safety.mode) }),
    ` · ${safetySummary(safety.mode)}`,
  ]);
  if (safety.resume_after) {
    text.appendChild(document.createTextNode(` · 最早可人工恢复：${fmtTime(safety.resume_after)}（${relTime(safety.resume_after)}）`));
  }
  if (safety.manual_release_required) text.appendChild(document.createTextNode(' · 需要人工恢复'));

  box.appendChild(h('button', {
    class: 'btn btn-sm btn-quiet', type: 'button', text: '访问控制',
    onclick: () => { goToAccessPanel(); },
  }));

  const more = moreBlock('技术细节', [
    ['状态来源', safety.source ? sanitizeTechnical(safety.source) : ''],
    ['原因', sanitizeTechnical(safety.reason)],
    ['状态更新时间', safety.updated_at ? fmtTime(safety.updated_at) : ''],
    ['策略文件', safety.corrupt ? '内容损坏，已按关闭处理' : ''],
  ]);
  if (more) box.appendChild(more);
}

export function renderMeta() {
  const pill = $('#meta-pill');
  const footer = $('#footer-meta');
  if (!app.state) {
    setText(pill, '—');
    setText(footer, '版本 —');
    return;
  }
  const version = app.state.version || '—';
  const revision = str(app.state.revision);
  setText(pill, `v${version} · 修订 ${revision.slice(0, 8) || '—'}`);
  pill.title = `配置修订：${revision || '—'}`;
  setText(footer, `版本 ${version} · 配置修订 ${revision.slice(0, 8) || '—'}`);
  footer.title = `配置修订：${revision || '—'}`;
}

function setActiveNav(view) {
  for (const link of document.querySelectorAll('.nav-link')) {
    if (link.dataset.view === view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
}

// ============================== 路由 ==============================

export function viewFromHash() {
  const raw = (window.location.hash || '').replace(/^#\/?/, '').split(/[?&]/)[0].trim();
  if (!raw) return 'stats';
  const name = VIEW_ALIASES[raw] || raw;
  return VIEWS.includes(name) ? name : 'stats';
}

export async function showView(name, options = {}) {
  const view = VIEWS.includes(name) ? name : 'stats';
  app.view = view;
  for (const candidate of VIEWS) {
    const section = document.getElementById(`view-${candidate}`);
    if (section) section.hidden = candidate !== view;
  }
  setActiveNav(view);

  if (view !== 'tasks') stopTaskPolling();

  if (view === 'stats') {
    renderStats();
    await loadOverview(options.force === true);
  } else if (view === 'jobs') {
    renderJobs();
    renderDetail();
    updateExportHref();
    if (options.force === true || !app.jobs.data) await loadJobs();
  } else if (view === 'terms') {
    renderTerms(options.force === true);
    await loadTermStats(options.force === true);
  } else if (view === 'tasks') {
    renderTasks();
    await loadTasks();
  } else if (view === 'config') {
    renderConfig();
  }
  bus.emit('view', view);
}

export function goToAccessPanel() {
  if (window.location.hash !== '#/tasks') {
    window.location.hash = '#/tasks';
  }
  window.setTimeout(() => {
    const panel = document.getElementById('access-panel');
    if (panel) {
      panel.scrollIntoView({ block: 'start', behavior: 'auto' });
      const button = panel.querySelector('button');
      if (button) button.focus();
    }
  }, 60);
}

// ============================== 装配 ==============================

function wireEvents() {
  const reload = $('#reload');
  if (reload) reload.addEventListener('click', () => { void reloadStateWithConfirm(); });

  const accessButton = $('#access-btn');
  if (accessButton) accessButton.addEventListener('click', () => goToAccessPanel());

  window.addEventListener('hashchange', () => { void showView(viewFromHash()); });

  const configForm = $('#config-form');
  if (configForm) configForm.addEventListener('submit', (event) => event.preventDefault());

  document.addEventListener('visibilitychange', () => { onVisibilityChange(); });

  document.addEventListener('keydown', (event) => {
    const target = event.target;
    const tag = target && target.tagName ? target.tagName : '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
      if (event.key === 'Escape' && app.jobs.selectedId) closeDetailIfOpen();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === 'Escape') {
      closeDetailIfOpen();
      return;
    }
    handleJobsKey(event);
  });

  wireJobsEvents();
  wireTasksEvents();

  window.addEventListener('resize', () => { syncDetailInert(); });
}

function closeDetailIfOpen() {
  const link = document.getElementById('detail-close');
  if (app.jobs.selectedId && link) link.click();
}

function wireStore() {
  bus.on('busy', () => renderBootStatus());
  bus.on('status', () => { renderBootStatus(); updateSaveBar(); });
  bus.on('state', () => {
    renderStatus();
    renderMeta();
    updateSaveBar();
    if (app.view === 'tasks') renderAccessPanel();
  });
  bus.on('options', () => {
    populateJobFilters();
    if (app.view === 'config') renderConfig();
    if (app.view === 'tasks') renderTasks();
  });
  bus.on('draft-replaced', () => {
    if (app.view === 'config') renderConfig();
    if (app.view === 'terms') renderTerms(true);
    populateJobFilters();
    updateSaveBar();
    renderBootStatus();
  });
}

// ============================== 启动 ==============================

async function boot() {
  wireEvents();
  wireStore();

  const view = viewFromHash();
  app.view = view;
  setActiveNav(view);
  renderStatus();
  renderMeta();
  renderBootStatus();
  updateSaveBar();
  syncDetailInert();

  const ok = await loadState();
  await loadOptions(false);
  await showView(view, { force: false });

  if (ok) {
    startStatePolling(5000);
    if (!app.overview) void loadOverview(false);
  } else {
    toast('无法连接本地服务：请确认管理服务已在本机运行。', 'error', {
      label: '重试',
      run: () => { void reloadStateWithConfirm(); },
    });
  }
}

void boot();
