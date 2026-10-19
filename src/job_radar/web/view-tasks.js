/**
 * view-tasks.js —— 一站式任务页：启动 / 停止 采集、详情、刷新、打标任务，
 * 查看状态、进度与每秒游标日志，处理 waiting_verification，以及访问策略的人工暂停 / 恢复（/api/access）。
 *
 * 轮询边界：任务列表每 2 秒、所选任务日志每 1 秒（游标）拉取一次，只更新列表与日志容器；
 * 启动表单是静态 DOM，不会被轮询覆盖，未提交的输入不会丢失。
 */
import { API } from './api.js';
import { clear, fmtInt, fmtTime, h, num, relTime, setText, str, valueText } from './core.js';
import { TASK_KINDS, app, loadState } from './store.js';
import {
  badge, emptyBlock, errorBlock, kvList, loadingBlock, moreBlock, openDialog, safetyLabel, safetySource,
  safetySummary, sanitizeTechnical, setPending, toast,
} from './ui.js';
import { progressBar } from './components.js';

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'interrupted']);
const STATUS_LABEL = {
  queued: '排队中',
  running: '运行中',
  waiting_verification: '等待人工验证',
  stopping: '停止中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
};
const STATUS_CLASS = {
  queued: 'badge-neutral',
  running: 'badge-run',
  waiting_verification: 'badge-warn',
  stopping: 'badge-neutral',
  succeeded: 'badge-ok',
  failed: 'badge-bad',
  cancelled: 'badge-later',
  interrupted: 'badge-bad',
};
const LEVEL_CLASS = { error: 'log-error', warning: 'log-warn', warn: 'log-warn', info: 'log-info', debug: 'log-debug' };

const MAX_LOG_EVENTS = 800;

let listTimer = null;
let logTimer = null;
let listToken = 0;

const state = app.tasks;

// ============================== 归一化 ==============================

export function normalizeTask(raw) {
  const task = raw && typeof raw === 'object' ? raw : {};
  const progress = task.progress && typeof task.progress === 'object' ? task.progress : {};
  const verification = task.verification && typeof task.verification === 'object' ? task.verification : null;
  const options = task.options && typeof task.options === 'object' ? task.options : {};
  return {
    id: task.id === undefined || task.id === null ? '' : String(task.id),
    kind: str(task.kind),
    status: str(task.status) || 'queued',
    created_at: str(task.created_at),
    started_at: str(task.started_at),
    finished_at: str(task.finished_at),
    config_revision: str(task.config_revision),
    applied_revision: str(task.applied_revision),
    options: {
      limit: num(options.limit),
      dry_run: options.dry_run === true,
      extra: Object.fromEntries(Object.entries(options).filter(([key]) => key !== 'limit' && key !== 'dry_run')),
    },
    progress: {
      processed: num(progress.processed) ?? 0,
      total: num(progress.total),
      failed: num(progress.failed) ?? 0,
      round_id: str(progress.round_id),
      round_jobs: num(progress.round_jobs),
      queries_done: num(progress.queries_done),
      queries_total: num(progress.queries_total),
      duplicates: num(progress.duplicates),
      requests: num(progress.requests),
      pending_configuration: progress.pending_configuration && typeof progress.pending_configuration === 'object'
        ? {fields: Array.isArray(progress.pending_configuration.fields) ? progress.pending_configuration.fields.map(String) : []} : null,
    },
    result: task.result === undefined ? null : task.result,
    verification: verification ? { url: str(verification.url), message: str(verification.message) } : null,
    error: str(task.error),
  };
}

function isTerminal(task) {
  return TERMINAL.has(task.status);
}

function kindLabel(kind) {
  const known = (app.options && app.options.task_kinds) || TASK_KINDS;
  const found = known.find((item) => item.value === kind);
  return found ? found.label : kind;
}

// ============================== 任务列表 ==============================

export async function loadTasks({ silent = false } = {}) {
  const token = ++listToken;
  if (!silent) state.loading = true;
  try {
    const payload = await API.tasks();
    if (token !== listToken) return;
    const data = payload && typeof payload === 'object' ? payload : {};
    state.data = {
      items: Array.isArray(data.items) ? data.items.map(normalizeTask) : [],
      active_count: num(data.active_count) ?? 0,
    };
    state.error = null;
  } catch (error) {
    if (token !== listToken) return;
    state.error = error.message;
  } finally {
    if (token === listToken) {
      state.loading = false;
      renderTaskList();
      syncSelection();
    }
  }
}

function taskActions(task) {
  const buttons = [h('button', {
    class: 'btn btn-sm', type: 'button', text: '查看日志', onclick: () => selectTask(task.id),
  })];
  if (!isTerminal(task) && task.status !== 'stopping') {
    buttons.push(h('button', {
      class: 'btn btn-sm btn-danger', type: 'button', text: '停止',
      onclick: (event) => { void stopTask(task.id, event.currentTarget); },
    }));
  }
  if (task.status === 'waiting_verification') {
    buttons.push(h('button', {
      class: 'btn btn-sm btn-primary', type: 'button', text: '打开真实验证窗口',
      onclick: (event) => { void focusVerificationWindow(task.id, event.currentTarget); },
    }));
    buttons.push(h('button', {
      class: 'btn btn-sm', type: 'button', text: '已完成验证，复检并继续',
      onclick: () => { void resumeTask(task.id); },
    }));
  }
  return h('div', { class: 'task-actions' }, ...buttons);
}

function taskCard(task) {
  return h('article', {
    class: `task-card${state.selectedId === task.id ? ' is-selected' : ''}`,
    dataset: { id: task.id },
  },
  h('div', { class: 'task-head' },
    h('button', { class: 'task-title', type: 'button', onclick: () => selectTask(task.id) },
      `${kindLabel(task.kind)} · ${task.id}`),
    h('div', { class: 'job-badges' },
      badge(STATUS_LABEL[task.status] || task.status, STATUS_CLASS[task.status] || 'badge-neutral'),
      task.options.dry_run ? badge('计划预览 dry_run', 'badge-preview') : null,
      task.verification ? badge('需要人工验证', 'badge-warn') : null)),
  h('p', { class: 'task-meta', text: [
    `创建 ${fmtTime(task.created_at)}`,
    task.started_at ? `开始 ${fmtTime(task.started_at)}` : null,
    task.finished_at ? `结束 ${fmtTime(task.finished_at)}` : null,
    task.progress.failed ? `失败 ${fmtInt(task.progress.failed)}` : null,
  ].filter(Boolean).join(' · ') }),
  (task.status === 'running' || task.progress.total !== null || task.progress.processed)
    ? progressBar(task.progress.processed, task.progress.total, task.progress.failed) : null,
  task.error ? h('p', { class: 'callout callout-bad', text: task.error }) : null,
  taskActions(task));
}

export function renderTaskList() {
  const host = document.getElementById('tasks-list');
  if (!host) return;
  clear(host);

  if (state.error && !state.data) {
    host.appendChild(errorBlock(`无法读取任务列表：${state.error}`, () => { void loadTasks(); }));
    return;
  }
  if (!state.data) {
    host.appendChild(loadingBlock('正在读取任务…'));
    return;
  }
  if (!state.data.items.length) {
    host.appendChild(emptyBlock('还没有任务。可在上方选择任务类型后启动。'));
    return;
  }
  host.appendChild(h('p', { class: 'hint', text: `共 ${state.data.items.length} 个任务，其中 ${state.data.active_count} 个未结束。` }));
  for (const task of state.data.items) host.appendChild(taskCard(task));
}

function taskById(id) {
  const found = state.data ? state.data.items.find((item) => item.id === id) : null;
  return found || (state.detail && state.detail.id === id ? state.detail : null);
}

function syncSelection() {
  if (state.selectedId) {
    const fresh = taskById(state.selectedId);
    if (!fresh) {
      state.selectedId = null;
      state.detail = null;
      state.detailBuiltId = null;
    } else {
      state.detail = fresh;
    }
  }
  renderTaskDetail();
  ensurePolling();
}

// ============================== 任务详情 ==============================

export function selectTask(id) {
  if (state.selectedId !== id) {
    state.selectedId = id;
    state.detail = taskById(id);
    state.events = [];
    state.nextCursor = 0;
    state.logError = null;
    state.detailBuiltId = null;
  }
  renderTaskList();
  renderTaskDetail();
  void refreshTaskDetail(id);
  void pollEvents(true);
  ensurePolling();
}

export async function refreshTaskDetail(id) {
  state.detailLoading = true;
  try {
    const fresh = normalizeTask(await API.task(id));
    if (state.selectedId === id) state.detail = fresh;
    state.detailError = null;
  } catch (error) {
    state.detailError = error.message;
  } finally {
    state.detailLoading = false;
    renderTaskDetail();
  }
}

function progressHost(task) {
  return h('div', { id: 'task-progress' }, progressBar(task.progress.processed, task.progress.total, task.progress.failed));
}

function metaHost(task) {
  return h('div', { id: 'task-meta-kv' }, kvList(taskMetaRows(task)));
}

function taskMetaRows(task) {
  return [
    ['任务类型', kindLabel(task.kind)],
    ['状态', STATUS_LABEL[task.status] || task.status],
    ['创建时间', fmtTime(task.created_at)],
    ['开始时间', task.started_at ? `${fmtTime(task.started_at)}（${relTime(task.started_at)}）` : '—'],
    ['结束时间', task.finished_at ? fmtTime(task.finished_at) : '—'],
    ['配置修订（创建时）', task.config_revision ? task.config_revision.slice(0, 8) : '—'],
    ['配置修订（生效）', task.applied_revision.startsWith('partial:') ? '部分应用 · '+task.applied_revision.slice(8,16) : task.applied_revision ? task.applied_revision.slice(0, 8) : '—'],
    ['等待新任务的配置', task.progress.pending_configuration?.fields.join('、') || '—'],
    ['数量上限', task.options.limit === null ? '—' : fmtInt(task.options.limit)],
    ['采集批次', task.progress.round_id || task.options.extra.round_id || '—'],
    ['批次岗位数', task.progress.round_jobs === null ? '—' : fmtInt(task.progress.round_jobs)],
    ['完成查询', task.progress.queries_total === null ? '—' : `${task.progress.queries_done || 0} / ${task.progress.queries_total}`],
    ['重复列表命中', task.progress.duplicates === null ? '—' : fmtInt(task.progress.duplicates)],
    ['业务请求数', task.progress.requests === null ? '—' : fmtInt(task.progress.requests)],
    ['其它选项', Object.keys(task.options.extra).length ? valueText(task.options.extra) : '—'],
    ['失败数', fmtInt(task.progress.failed)],
  ];
}

function resultHost(task) {
  const result = task.result;
  if (result === null || result === undefined) return h('div', { id: 'task-result' });
  if (typeof result !== 'object') {
    return h('div', { id: 'task-result' }, h('p', { class: 'result-line', text: String(result) }));
  }
  const rows = Object.entries(result).map(([key, value]) => [key, valueText(value)]);
  // 任务刚创建时 result 是 {}：不要渲染一个空标题。
  if (!rows.length) return h('div', { id: 'task-result' });
  return h('div', { id: 'task-result' },
    h('h4', { text: '结果' }),
    h('dl', { class: 'kv' }, rows.map(([key, value]) => h('div', { class: 'kv-row' },
      h('dt', { text: key }), h('dd', { text: value })))));
}

function verificationHost(task) {
  const host = h('div', { id: 'task-verification' });
  if (!task.verification) return host;
  host.appendChild(h('div', { class: 'callout callout-warn' },
    h('p', { text: task.verification.message || '平台侧需要一次人工验证才能继续。' }),
    task.verification.url ? h('p', { class: 'hint', text: `验证入口：${task.verification.url}` }) : null,
    h('p', { class: 'hint', text: '验证由你本人在真实浏览器窗口中完成；完成后点「已完成验证，复检并继续」：'
      + '该操作会把访问策略恢复为可联网，并复检实际业务响应。' }),
    h('div', { class: 'task-actions' },
      h('button', {
        class: 'btn btn-sm btn-primary', type: 'button', text: '打开真实验证窗口',
        onclick: (event) => { void focusVerificationWindow(task.id, event.currentTarget); },
      }),
      h('button', {
        class: 'btn btn-sm', type: 'button', text: '已完成验证，复检并继续',
        onclick: () => { void resumeTask(task.id); },
      }))));
  return host;
}

function buildDetail(host, task) {
  clear(host);
  host.appendChild(h('div', { class: 'task-detail-head' },
    h('div', { class: 'job-badges', id: 'task-detail-status' }),
    h('div', { class: 'task-actions', id: 'task-detail-actions' })));
  host.appendChild(h('div', { id: 'task-detail-preview' }));
  host.appendChild(progressHost(task));
  host.appendChild(metaHost(task));
  host.appendChild(h('div', { id: 'task-detail-error' }, task.error ? h('p', { class: 'callout callout-bad', text: task.error }) : null));
  host.appendChild(verificationHost(task));
  host.appendChild(resultHost(task));
  host.appendChild(h('div', { class: 'log-block' },
    h('div', { class: 'log-head' },
      h('h4', { text: '实时日志（游标轮询，每秒一次）' }),
      h('div', { class: 'task-actions' },
        h('button', {
          class: 'btn btn-sm btn-quiet', type: 'button', id: 'task-log-toggle',
          text: state.logPaused ? '继续轮询' : '暂停轮询',
          onclick: (event) => {
            state.logPaused = !state.logPaused;
            setText(event.currentTarget, state.logPaused ? '继续轮询' : '暂停轮询');
            ensurePolling();
          },
        }),
        h('button', {
          class: 'btn btn-sm btn-quiet', type: 'button', text: '清空显示',
          onclick: () => { state.events = []; renderLog(); },
        }))),
    h('p', { class: 'hint', id: 'task-log-meta' }),
    h('div', { class: 'log-view', id: 'task-log', role: 'log', 'aria-live': 'polite' })));
}

function updateDetail(task) {
  const status = document.getElementById('task-detail-status');
  if (status) {
    clear(status);
    status.appendChild(badge(STATUS_LABEL[task.status] || task.status, STATUS_CLASS[task.status] || 'badge-neutral'));
    if (task.options.dry_run) status.appendChild(badge('计划预览 dry_run', 'badge-preview'));
    if (task.verification) status.appendChild(badge('需要人工验证', 'badge-warn'));
  }
  const actions = document.getElementById('task-detail-actions');
  if (actions) {
    clear(actions);
    actions.appendChild(h('button', {
      class: 'btn btn-sm', type: 'button', text: '刷新状态',
      onclick: () => { void refreshTaskDetail(task.id); },
    }));
    if (!isTerminal(task) && task.status !== 'stopping') {
      actions.appendChild(h('button', {
        class: 'btn btn-sm btn-danger', type: 'button', text: '停止任务',
        onclick: (event) => { void stopTask(task.id, event.currentTarget); },
      }));
    }
  }
  const preview = document.getElementById('task-detail-preview');
  if (preview) {
    clear(preview);
    if (task.options.dry_run) {
      preview.appendChild(h('p', {
        class: 'callout callout-preview', role: 'note',
        text: '计划预览（dry_run）：生成执行计划；采集与打标在真实任务里执行。',
      }));
    }
  }
  const progress = document.getElementById('task-progress');
  if (progress) {
    clear(progress);
    progress.appendChild(progressBar(task.progress.processed, task.progress.total, task.progress.failed));
  }
  const meta = document.getElementById('task-meta-kv');
  if (meta) {
    clear(meta);
    meta.appendChild(kvList(taskMetaRows(task)));
  }
  const error = document.getElementById('task-detail-error');
  if (error) {
    clear(error);
    if (task.error) error.appendChild(h('p', { class: 'callout callout-bad', role: 'alert', text: task.error }));
  }
  const verification = document.getElementById('task-verification');
  if (verification) {
    const next = verificationHost(task);
    verification.textContent = '';
    while (next.firstChild) verification.appendChild(next.firstChild);
  }
  const result = document.getElementById('task-result');
  if (result) {
    const next = resultHost(task);
    result.textContent = '';
    while (next.firstChild) result.appendChild(next.firstChild);
  }
  const toggle = document.getElementById('task-log-toggle');
  if (toggle) setText(toggle, state.logPaused ? '继续轮询' : '暂停轮询');
  renderLog();
}

export function renderTaskDetail() {
  const host = document.getElementById('task-detail-body');
  const title = document.getElementById('task-detail-h');
  if (!host) return;

  if (!state.selectedId) {
    state.detailBuiltId = null;
    setText(title, '任务详情');
    clear(host);
    host.appendChild(emptyBlock('从左侧列表选择一个任务，这里显示它的进度、结果与实时日志。'));
    return;
  }
  const task = state.detail || taskById(state.selectedId);
  if (!task) {
    setText(title, '任务详情');
    clear(host);
    host.appendChild(state.detailError
      ? errorBlock(`无法读取任务：${state.detailError}`, () => { void refreshTaskDetail(state.selectedId); })
      : loadingBlock('正在读取任务…'));
    return;
  }

  setText(title, `${kindLabel(task.kind)} · ${task.id}`);
  if (state.detailBuiltId !== task.id) {
    buildDetail(host, task);
    state.detailBuiltId = task.id;
  }
  updateDetail(task);
}

// ============================== 日志 ==============================

export function renderLog() {
  const host = document.getElementById('task-log');
  const meta = document.getElementById('task-log-meta');
  if (meta) {
    setText(meta, `游标 ${state.nextCursor} · 已显示 ${state.events.length} 条${state.logError ? ` · 读取失败：${state.logError}` : ''}`);
  }
  if (!host) return;
  const nearBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 48;
  clear(host);
  if (!state.events.length) {
    host.appendChild(h('p', { class: 'empty', text: '还没有日志事件。' }));
    return;
  }
  for (const event of state.events.slice(-MAX_LOG_EVENTS)) {
    host.appendChild(h('div', { class: `log-line ${LEVEL_CLASS[event.level] || 'log-info'}` },
      h('span', { class: 'log-seq', text: `#${event.seq}` }),
      h('span', { class: 'log-time', text: fmtTime(event.created_at) }),
      h('span', { class: 'log-type', text: event.type }),
      h('span', { class: 'log-msg', text: event.message }),
      event.data === null || event.data === undefined ? null : h('span', { class: 'log-data', text: valueText(event.data) })));
  }
  if (nearBottom) host.scrollTop = host.scrollHeight;
}

export async function pollEvents(reset = false) {
  if (!state.selectedId) return;
  if (reset) {
    state.nextCursor = 0;
    state.events = [];
  }
  const taskId = state.selectedId;
  try {
    const payload = await API.taskEvents(taskId, state.nextCursor, 200);
    if (state.selectedId !== taskId) return;
    const data = payload && typeof payload === 'object' ? payload : {};
    const items = Array.isArray(data.items) ? data.items : [];
    for (const item of items) {
      const seq = num(item && item.seq);
      state.events.push({
        seq: seq === null ? state.events.length + 1 : seq,
        created_at: str(item && item.created_at),
        type: str(item && item.type),
        level: str(item && item.level) || 'info',
        message: str(item && item.message),
        data: item ? item.data : null,
      });
    }
    const next = num(data.next_cursor);
    if (next !== null) state.nextCursor = next;
    if (state.events.length > MAX_LOG_EVENTS * 2) state.events = state.events.slice(-MAX_LOG_EVENTS);
    state.logError = null;
  } catch (error) {
    state.logError = error.message;
  } finally {
    renderLog();
  }
}

// ============================== 轮询调度 ==============================

function ensurePolling() {
  const onTasksView = app.view === 'tasks';
  const task = state.selectedId ? taskById(state.selectedId) : null;
  const wantList = onTasksView && !document.hidden;
  const wantLog = wantList && Boolean(task) && !isTerminal(task) && !state.logPaused;

  if (wantList && !listTimer) {
    listTimer = window.setInterval(() => { void loadTasks({ silent: true }); }, 2000);
  } else if (!wantList && listTimer) {
    window.clearInterval(listTimer);
    listTimer = null;
  }
  if (wantLog && !logTimer) {
    logTimer = window.setInterval(() => { void pollEvents(false); }, 1000);
  } else if (!wantLog && logTimer) {
    window.clearInterval(logTimer);
    logTimer = null;
  }
}

export function stopTaskPolling() {
  if (listTimer) { window.clearInterval(listTimer); listTimer = null; }
  if (logTimer) { window.clearInterval(logTimer); logTimer = null; }
}

// ============================== 任务操作 ==============================

export async function startTask(button) {
  const kindSelect = document.getElementById('task-kind');
  const limitInput = document.getElementById('task-limit');
  const dryRunBox = document.getElementById('task-dry-run');
  const roundId = document.getElementById('task-round-id')?.value.trim() || '';
  const message = document.getElementById('task-start-msg');
  if (!kindSelect || !limitInput || !dryRunBox) return;

  const limit = Number(limitInput.value);
  if (!Number.isInteger(limit) || limit < 1 || limit > 100000) {
    if (message) { message.className = 'field-msg'; setText(message, '数量上限需为 1–100000 的整数。'); }
    limitInput.focus();
    return;
  }
  // 采集会话的额外选项：留空就交给后端的阶段默认值，填了就必须是合法整数，
  // 否则会出现「任务创建成功但一个查询都没跑」这种最难查的失败。
  const session = kindSelect.value === 'session' ? readSessionOptions() : { options: {}, problem: '' };
  if (session.problem) {
    if (message) { message.className = 'field-msg'; setText(message, session.problem); }
    return;
  }
  const dryRun = dryRunBox.checked === true;
  if (!dryRun) {
    const result = await openDialog({
      title: '启动真实任务',
      note: '真实任务：会按当前配置访问平台或调用模型。请确认访问策略已允许，再决定现在执行。',
      confirmLabel: '确认启动',
      danger: true,
      checkboxLabel: '我确认现在启动真实任务，并已核对该任务的配置与节奏',
    });
    if (!result.ok) return;
  }

  setPending(button, true, '启动中…');
  if (message) { message.className = 'hint'; setText(message, ''); }
  try {
    const task = normalizeTask(await API.createTask({
      kind: kindSelect.value,
      options: { limit, dry_run: dryRun, round_id: roundId, ...session.options },
    }));
    if (message) {
      message.className = 'hint';
      setText(message, `已创建任务 ${task.id}（${kindLabel(task.kind)}${dryRun ? '，计划预览' : ''}）。`);
    }
    toast(dryRun ? `已创建计划预览任务 ${task.id}。` : `已创建任务 ${task.id}。`, 'ok');
    await loadTasks();
    selectTask(task.id);
  } catch (error) {
    if (message) { message.className = 'field-msg'; setText(message, `创建失败：${error.message}`); }
    toast(`任务未创建：${error.message}`, 'error');
  } finally {
    setPending(button, false);
  }
}

export async function stopTask(id, button) {
  const result = await openDialog({
    title: '停止任务',
    note: `将请求停止任务 ${id}；已完成的进度保留。`,
    confirmLabel: '停止任务',
    danger: true,
  });
  if (!result.ok) return;
  setPending(button, true, '停止中…');
  try {
    const task = normalizeTask(await API.stopTask(id));
    if (state.selectedId === id) state.detail = task;
    toast(`已请求停止 ${id}。`, 'ok');
    await loadTasks();
    renderTaskDetail();
  } catch (error) {
    toast(`停止失败：${error.message}`, 'error');
  } finally {
    setPending(button, false);
  }
}

export async function focusVerificationWindow(id, button) {
  setPending(button, true, '唤起窗口…');
  try {
    await API.focusTaskWindow(id);
    toast('已请求把任务使用的浏览器窗口切到前台。', 'ok');
  } catch (error) {
    toast(`未能唤起验证窗口：${error.message}`, 'error');
  } finally {
    setPending(button, false);
  }
}

export async function resumeTask(id) {
  const result = await openDialog({
    title: '复检并继续',
    note: '确认你已经在真实浏览器窗口中完成平台要求的验证。任务会先复检实际业务响应，通过后继续执行。'
      + '该操作也会把访问策略写为可联网（ready）；如果你刚才手动暂停过访问，请知悉这一点。',
    confirmLabel: '复检并继续',
    danger: true,
    checkboxLabel: '我已在真实浏览器窗口完成验证，并确认现在复检（会同时解除访问暂停）',
  });
  if (!result.ok) return;
  try {
    const task = normalizeTask(await API.resumeTask(id));
    if (state.selectedId === id) state.detail = task;
    toast(`任务 ${id} 已请求复检。`, 'ok');
    await loadTasks();
    renderTaskDetail();
    ensurePolling();
  } catch (error) {
    toast(`复检请求失败：${error.message}`, 'error');
  }
}

// ============================== 访问策略 ==============================

function releaseBlocked(safety) {
  if (!safety || !safety.resume_after) return '';
  const deadline = new Date(safety.resume_after);
  if (Number.isNaN(deadline.getTime())) return '';
  if (deadline.getTime() > Date.now()) {
    return `策略允许恢复的时间为 ${fmtTime(safety.resume_after)}，到点后即可人工恢复。`;
  }
  return '';
}

export function renderAccessPanel() {
  const host = document.getElementById('access-panel');
  if (!host) return;
  clear(host);
  const stateInfo = app.state;
  if (!stateInfo) {
    host.appendChild(loadingBlock('正在读取访问策略…'));
    return;
  }
  const safety = stateInfo.safety;
  const blocked = releaseBlocked(safety);
  const mode = safety.mode || '';
  const modeClass = mode === 'ready' ? 'badge-ok' : (mode === 'paused' ? 'badge-warn' : 'badge-later');

  host.appendChild(h('div', { class: 'panel-head' },
    h('h3', { class: 'panel-title', text: '访问策略' }),
    badge(safetyLabel(mode), modeClass),
    h('span', { class: 'hint', text: safetySummary(mode) })));
  host.appendChild(h('div', { class: 'task-actions' },
    h('button', {
      class: 'btn btn-sm btn-danger', type: 'button', text: '暂停访问',
      onclick: () => { void pauseAccess(); },
    }),
    h('button', {
      class: 'btn btn-sm', type: 'button', text: '人工恢复访问',
      disabled: Boolean(blocked),
      title: blocked || '恢复前需要你确认已完成平台侧核验',
      onclick: () => { void releaseAccess(); },
    }),
    h('button', {
      class: 'btn btn-sm btn-quiet', type: 'button', text: '重新读取状态',
      onclick: () => { void loadState({ preserveDraft: true }).then(() => renderAccessPanel()); },
    })));
  host.appendChild(kvList([
    ['当前状态', safetyLabel(mode)],
    ['最早可人工恢复', safety.resume_after ? `${fmtTime(safety.resume_after)}（${relTime(safety.resume_after)}）` : '未设置期限'],
    ['需要人工恢复', safety.manual_release_required ? '是' : '否'],
    ['状态来源', safetySource(safety.source)],
  ]));
  if (blocked) host.appendChild(h('p', { class: 'hint', text: blocked }));
  host.appendChild(h('p', { class: 'hint', text: '访问状态只由你在这里的操作或平台侧结果改变。' }));
  const more = moreBlock('技术细节', [
    ['原因', sanitizeTechnical(safety.reason)],
    ['状态更新时间', safety.updated_at ? fmtTime(safety.updated_at) : ''],
    ['策略文件', safety.corrupt ? '内容损坏，已按关闭处理' : ''],
  ]);
  if (more) host.appendChild(more);
  const refresh = stateInfo.refresh_safety;
  if (refresh) {
    host.appendChild(h('div', {class: 'callout callout-info'},
      h('p', {text: `独立刷新状态：${safetyLabel(refresh.mode)}${refresh.reason ? ' · '+sanitizeTechnical(refresh.reason) : ''}`}),
      h('p', {class:'hint',text:'刷新暂停单独保留；全局暂停仍会停止所有平台任务。'}),
      refresh.mode !== 'ready' ? h('button', {class:'btn btn-sm',type:'button',text:'人工恢复刷新访问',
        onclick:() => { void releaseAccess('refresh'); }}) : null));
  }
}

export async function pauseAccess() {
  const result = await openDialog({
    title: '暂停访问',
    note: '写入访问策略：之后的联网任务在启动前会被拦住，直到人工恢复；'
      + '正在运行的联网任务会一并停止；打标任务与计划预览继续运行。',
    confirmLabel: '确认暂停',
    danger: true,
    requireText: '请填写暂停原因（会写入策略文件，供之后判断能否恢复）。',
    textLabel: '暂停原因',
    textPlaceholder: '例如：手动暂停',
  });
  if (!result.ok) return;
  try {
    await API.access({ action: 'pause', reason: result.text || '手动暂停' });
    await loadState({ preserveDraft: true });
    renderAccessPanel();
    toast('访问已暂停。', 'ok');
  } catch (error) {
    toast(`暂停失败：${error.message}`, 'error');
  }
}

export async function releaseAccess(scope = '') {
  const safety = app.state ? (scope === 'refresh' ? app.state.refresh_safety : app.state.safety) : null;
  const blocked = releaseBlocked(safety);
  if (blocked) { toast(blocked, 'error'); return; }
  const result = await openDialog({
    title: scope === 'refresh' ? '人工恢复刷新访问' : '人工恢复访问',
    note: '恢复后联网任务可以再次执行。请在你确认平台侧限制解除后再操作。',
    confirmLabel: '确认恢复',
    requireText: '请填写恢复依据（用于记录依据什么判断可以恢复）。',
    textLabel: '恢复依据',
    textPlaceholder: '例如：平台限制已解除，用户确认',
    checkboxLabel: '我确认现场风险已解除，并主动要求恢复访问',
  });
  if (!result.ok) return;
  try {
    // confirmation 会随请求发给后端并写入策略文件，作为「依据什么判断可以恢复」的痕迹。
    await API.access({ action: 'release', scope, confirmed: true, confirmation: result.text });
    await loadState({ preserveDraft: true });
    renderAccessPanel();
    toast('访问已恢复。', 'ok');
  } catch (error) {
    toast(`恢复失败：${error.message}`, 'error');
    renderAccessPanel();
  }
}

// ============================== 装配 ==============================

export function populateTaskForm() {
  const kindSelect = document.getElementById('task-kind');
  if (!kindSelect) return;
  const kinds = (app.options && app.options.task_kinds && app.options.task_kinds.length)
    ? app.options.task_kinds
    : TASK_KINDS;
  const previous = kindSelect.value;
  clear(kindSelect);
  for (const kind of kinds) kindSelect.appendChild(h('option', { value: kind.value, text: kind.label }));
  if (previous && kinds.some((kind) => kind.value === previous)) kindSelect.value = previous;
  // 重绘后会话选项的可见性要跟当前选中的种类一致（不重建节点，未提交的输入不丢）。
  syncSessionFields(kindSelect.value);
}

export function renderTasks() {
  populateTaskForm();
  renderAccessPanel();
  renderTaskList();
  renderTaskDetail();
  ensurePolling();
}

const KIND_HINTS = {
  collect: '按配置里的城市、搜索词与筛选组合采集岗位列表。',
  session: '采集会话：推荐流校准 → 关键词扫描 → 存量补正文，三个阶段选一个执行。',
  details: '为缺少 JD 正文的岗位补全详情。',
  refresh: '刷新已有岗位的在招状态（默认本地直连，可在配置页改为代理）。',
  tag: '按当前提示词对岗位原文做 LLM 打标。',
};

// ============================== 采集会话选项 ==============================
// `session` 是唯一需要额外选项的任务种类（阶段 / 页数 / 正文条数 / 关键词）。
// 这些字段按需插进启动表单的网格里，结构与 index.html 的静态字段相同（都是
// `.field`），所以不用改样式；其它任务种类看不到它们（`hidden`，样式表里有
// `[hidden]{display:none!important}` 兜底）。

const SESSION_PHASES = [
  { value: 'calibrate', label: '推荐流校准（含抓正文）' },
  { value: 'sweep', label: '关键词扫描（就地补详情）' },
  { value: 'backfill', label: '存量补正文' },
];

function ensureSessionFields() {
  const existing = Array.from(document.querySelectorAll('[data-session-field]'));
  if (existing.length) return existing;
  const grid = document.querySelector('#task-form .grid');
  if (!grid) return [];
  const nodes = [
    h('div', { class: 'field', dataset: { sessionField: '1' } },
      h('label', { for: 'task-session-phase', text: '会话阶段' }),
      h('select', { id: 'task-session-phase' },
        ...SESSION_PHASES.map((item) => h('option', { value: item.value, text: item.label }))),
      h('p', { class: 'hint', text: '校准=推荐流采样并抓正文；扫描=按关键词轮转采集并就地补详情；补正文=只补 active 且缺正文的存量岗位。' })),
    h('div', { class: 'field', dataset: { sessionField: '1' } },
      h('label', { for: 'task-session-pages', text: '翻页数（可留空）' }),
      h('input', {
        id: 'task-session-pages', type: 'number', min: '1', max: '30', step: '1',
        inputmode: 'numeric', placeholder: '校准 5 / 扫描取配置',
      }),
      h('p', { class: 'hint', text: '留空按阶段默认。' })),
    h('div', { class: 'field', dataset: { sessionField: '1' } },
      h('label', { for: 'task-session-detail-limit', text: '正文条数（可留空）' }),
      h('input', {
        id: 'task-session-detail-limit', type: 'number', min: '0', max: '500', step: '1',
        inputmode: 'numeric', placeholder: '校准 40 / 扫描 8 / 补正文取数量上限',
      }),
      h('p', { class: 'hint', text: '填 0 表示扫描时只取列表、不抓正文。' })),
    h('div', { class: 'field', dataset: { sessionField: '1' } },
      h('label', { for: 'task-session-keywords', text: '关键词（可留空）' }),
      h('input', {
        id: 'task-session-keywords', type: 'text', maxlength: '1000',
        placeholder: '例如 AI Agent,Python',
      }),
      h('p', { class: 'hint', text: '逗号分隔；留空用配置里的，并按「最久没采过」轮转。' })),
  ];
  for (const node of nodes) grid.appendChild(node);
  return nodes;
}

function syncSessionFields(kind) {
  const visible = kind === 'session';
  for (const node of ensureSessionFields()) node.hidden = !visible;
}

/** 读一个可留空的整数输入；留空返回 undefined（后端按阶段默认），非法则记一条问题。 */
function readOptionalInt(id, label, min, max, problems) {
  const raw = (document.getElementById(id)?.value || '').trim();
  if (!raw) return undefined;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < min || value > max) {
    problems.push(`${label}需为 ${min}–${max} 的整数（留空按默认）。`);
    return undefined;
  }
  return value;
}

/** 采集会话的额外选项。留空一律不发送，让后端的阶段默认值生效。 */
export function readSessionOptions() {
  const problems = [];
  const options = {};
  const phase = document.getElementById('task-session-phase')?.value || '';
  if (phase) options.phase = phase;
  const pages = readOptionalInt('task-session-pages', '翻页数', 1, 30, problems);
  if (pages !== undefined) options.pages = pages;
  // `detail_limit: 0` 是有效取值（只扫不抓），所以只判 `undefined`，不判真假。
  const detailLimit = readOptionalInt('task-session-detail-limit', '正文条数', 0, 500, problems);
  if (detailLimit !== undefined) options.detail_limit = detailLimit;
  const keywords = (document.getElementById('task-session-keywords')?.value || '').trim();
  if (keywords) options.keywords = keywords;
  return { options, problem: problems[0] || '' };
}

export function wireTasksEvents() {
  const start = document.getElementById('task-start');
  if (start) start.addEventListener('click', (event) => { void startTask(event.currentTarget); });
  const form = document.getElementById('task-form');
  if (form) form.addEventListener('submit', (event) => event.preventDefault());
  const kindSelect = document.getElementById('task-kind');
  const hintNode = document.getElementById('task-kind-hint');
  const syncHint = () => {
    if (hintNode && kindSelect) setText(hintNode, KIND_HINTS[kindSelect.value] || '');
    if (kindSelect) syncSessionFields(kindSelect.value);
  };
  if (kindSelect) { kindSelect.addEventListener('change', syncHint); syncHint(); }
}

export function onVisibilityChange() {
  ensurePolling();
}

export { isTerminal };
