/**
 * ui.js —— 反馈组件（忙碌状态、提示、确认对话框）与通用表单/展示构件。
 *
 * 所有交互都通过 addEventListener 绑定，不生成内联事件属性（CSP: script-src 'self'）。
 */
import { $, bus, clear, h, setText, valueText } from './core.js';

// ============================== 忙碌与提示 ==============================

export const busy = { count: 0, text: '' };

export function busyPush(text) {
  busy.count += 1;
  busy.text = text || '同步中…';
  bus.emit('busy');
}

export function busyPop() {
  busy.count = Math.max(0, busy.count - 1);
  if (busy.count === 0) busy.text = '';
  bus.emit('busy');
}

export function toast(message, kind = 'info', action = null) {
  const box = $('#toasts');
  if (!box) return;
  const node = h('div', { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    h('span', { class: 'toast-text', text: message }));
  if (action && typeof action.run === 'function') {
    node.appendChild(h('button', {
      class: 'btn btn-sm', type: 'button', text: action.label || '重试',
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

export function setPending(button, pending, pendingText) {
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

// ============================== 状态块 ==============================

export function loadingBlock(text = '正在读取…') {
  return h('p', { class: 'empty', role: 'status', text });
}

export function errorBlock(message, onRetry) {
  const node = h('div', { class: 'callout callout-bad', role: 'alert' },
    h('p', { text: message }));
  if (typeof onRetry === 'function') {
    node.appendChild(h('p', {}, h('button', {
      class: 'btn btn-sm', type: 'button', text: '重试', onclick: () => onRetry(),
    })));
  }
  return node;
}

export function emptyBlock(text = '暂无数据。') {
  return h('p', { class: 'empty', text });
}

export function badge(text, cls = 'badge-neutral') {
  return h('span', { class: `badge ${cls}`, text });
}

// ============================== 访问状态文案 ==============================

/** 状态名用肯定句，不带内部术语。 */
export const SAFETY_LABELS = {
  frozen: '已冻结',
  offline: '在线访问关闭',
  paused: '已暂停',
  ready: '在线访问已允许',
  stopped: '已停止',
  cooldown: '冷却中',
  manual: '等待人工处理',
  timeout: '临时冻结',
};

/** 一句话结论：给用户「现在处于什么状态、怎么继续」。 */
export const SAFETY_SUMMARY = {
  frozen: '联网任务保持停止，人工恢复后继续。',
  offline: '可编辑配置、预览任务并查看数据。',
  paused: '联网任务保持停止，恢复访问后继续。',
  ready: '联网任务按当前配置执行。',
  stopped: '当前没有正在执行的联网任务。',
  cooldown: '冷却结束后可以再次执行联网任务。',
  manual: '等待人工处理后继续。',
  timeout: '临时冻结中，可人工恢复。',
};

const SOURCE_LABELS = {
  manual: '人工操作',
  'manual-release': '人工恢复',
  default: '本机还没有访问策略文件',
  unreadable: '访问策略文件不可读',
  corrupt: '访问策略文件损坏',
};

export function safetyLabel(mode) {
  const key = String(mode || '').trim();
  if (!key) return '未知';
  return SAFETY_LABELS[key.toLowerCase()] || key;
}

export function safetySummary(mode) {
  const key = String(mode || '').trim().toLowerCase();
  return SAFETY_SUMMARY[key] || '当前访问状态以本页访问面板为准。';
}

export function safetySource(source) {
  const key = String(source || '').trim();
  if (!key) return '未记录';
  return SOURCE_LABELS[key] || key;
}

/** 折叠区内展示技术说明：去掉本机路径与内部函数名。 */
export function sanitizeTechnical(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/[A-Za-z]:\\[^\s，。；、）)]*/g, '（本机策略文件）')
    .replace(/\/(?:[^\s，。；、）)]*\/)+[^\s，。；、）)]*\.json/g, '（本机策略文件）')
    .replace(/release\s*\([^)]*\)/gi, '人工恢复')
    .replace(/\brelease\b/gi, '人工恢复')
    .replace(/\s+/g, ' ')
    .trim();
}

/** 「技术细节」折叠块：没有内容时返回 null。 */
export function moreBlock(title, rows) {
  const filled = rows.filter(([, value]) => value);
  if (!filled.length) return null;
  return h('details', { class: 'more' },
    h('summary', { text: title || '技术细节' }),
    h('div', { class: 'more-body' }, filled.map(([label, value]) => h('p', {},
      h('span', { text: `${label}：` }), h('span', { class: 'raw', text: value })))));
}

// ============================== 对话框 ==============================

/**
 * 通用模态框。返回 {ok, text, checkbox}：ok 为 false 表示取消。
 * Esc 与「取消」都关闭并返回 ok=false。
 * @param {{title:string, note?:string, body?:Node|Node[], confirmLabel?:string,
 *          cancelLabel?:string, danger?:boolean, requireText?:string, textLabel?:string,
 *          textPlaceholder?:string, checkboxLabel?:string}} options
 * @returns {Promise<{ok:boolean, text:string, checkbox:boolean}>}
 */
export function openDialog(options) {
  return new Promise((resolve) => {
    const dialog = h('dialog', { class: 'modal' });
    const message = h('p', { class: 'field-msg', role: 'alert' });
    const textInput = options.requireText
      ? h('input', { type: 'text', class: 'modal-text', autocomplete: 'off', placeholder: options.textPlaceholder || '' })
      : null;
    const checkboxInput = h('input', { type: 'checkbox' });
    const checkbox = options.checkboxLabel
      ? h('label', { class: 'switch' }, checkboxInput, h('span', { text: options.checkboxLabel }))
      : null;

    const confirmButton = h('button', {
      class: `btn ${options.danger ? 'btn-danger' : 'btn-primary'}`, type: 'button',
      text: options.confirmLabel || '确定',
    });
    const cancelButton = h('button', {
      class: 'btn btn-quiet', type: 'button', text: options.cancelLabel || '取消',
    });

    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      const result = {
        ok,
        text: textInput ? textInput.value.trim() : '',
        checkbox: checkbox ? checkboxInput.checked : false,
      };
      if (dialog.open) dialog.close();
      dialog.remove();
      resolve(result);
    };

    const tryConfirm = () => {
      if (options.requireText && !textInput.value.trim()) {
        setText(message, options.requireText);
        textInput.focus();
        return;
      }
      if (checkbox && !checkboxInput.checked) {
        setText(message, '请先勾选上面的确认项。');
        return;
      }
      finish(true);
    };

    confirmButton.addEventListener('click', tryConfirm);
    cancelButton.addEventListener('click', () => finish(false));
    if (textInput) {
      textInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); tryConfirm(); }
      });
    }
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); finish(false); });

    const bodyChildren = options.body === undefined || options.body === null
      ? []
      : (Array.isArray(options.body) ? options.body : [options.body]);
    dialog.appendChild(h('div', { class: 'modal-inner' },
      h('h3', { class: 'modal-title', text: options.title || '请确认' }),
      options.note ? h('p', { class: 'modal-note', text: options.note }) : null,
      ...bodyChildren,
      textInput ? h('div', { class: 'field' },
        h('label', { for: 'modal-text' }, options.textLabel || '确认信息'), textInput) : null,
      checkbox,
      message,
      h('div', { class: 'modal-actions' }, cancelButton, confirmButton)));

    document.body.appendChild(dialog);
    dialog.showModal();
    window.setTimeout(() => {
      const focusTarget = textInput || dialog.querySelector('.modal-actions .btn-primary, .modal-actions .btn-danger');
      if (focusTarget) focusTarget.focus();
    }, 0);
  });
}

export async function confirmDialog(title, note, confirmLabel = '确定', danger = false) {
  const result = await openDialog({ title, note, confirmLabel, danger });
  return result.ok;
}

/** 批量多行导入：返回 {mode:'append'|'replace', text} 或 null。 */
export function importDialog({ title, note, initial = '', placeholder = '每行一个词条' }) {
  return new Promise((resolve) => {
    const textarea = h('textarea', { class: 'modal-textarea', rows: '10', placeholder });
    textarea.value = initial;
    const dialog = h('dialog', { class: 'modal' });
    const finish = (value) => { if (dialog.open) dialog.close(); dialog.remove(); resolve(value); };
    dialog.appendChild(h('div', { class: 'modal-inner' },
      h('h3', { class: 'modal-title', text: title }),
      note ? h('p', { class: 'modal-note', text: note }) : null,
      textarea,
      h('div', { class: 'modal-actions' },
        h('button', {
          class: 'btn btn-quiet', type: 'button', text: '取消',
          onclick: () => finish(null),
        }),
        h('button', {
          class: 'btn', type: 'button', text: '覆盖替换',
          onclick: () => finish({ mode: 'replace', text: textarea.value }),
        }),
        h('button', {
          class: 'btn btn-primary', type: 'button', text: '追加导入',
          onclick: () => finish({ mode: 'append', text: textarea.value }),
        }))));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); finish(null); });
    document.body.appendChild(dialog);
    dialog.showModal();
    window.setTimeout(() => textarea.focus(), 0);
  });
}

// ============================== 展示构件 ==============================

export function panel(title, note, ...children) {
  return h('section', { class: 'panel' },
    h('h3', { class: 'panel-title', text: title }),
    note ? h('p', { class: 'panel-note', text: note }) : null,
    ...children);
}

/** 可折叠面板。默认展开状态由 open 决定。 */
export function collapsiblePanel(title, note, open, ...children) {
  const body = h('div', { class: 'panel-body' }, ...children);
  const toggle = h('button', {
    class: 'collapse-toggle', type: 'button',
    'aria-expanded': open ? 'true' : 'false', text: open ? '收起' : '展开',
  });
  body.hidden = !open;
  const section = h('section', { class: 'panel' },
    h('div', { class: 'panel-head' },
      h('h3', { class: 'panel-title', text: title }),
      toggle),
    note ? h('p', { class: 'panel-note', text: note }) : null,
    body);
  toggle.addEventListener('click', () => {
    body.hidden = !body.hidden;
    toggle.setAttribute('aria-expanded', body.hidden ? 'false' : 'true');
    setText(toggle, body.hidden ? '展开' : '收起');
  });
  return section;
}

export function tableScaffold(headers, rows, caption) {
  return h('div', { class: 'table-wrap' },
    h('table', { class: 'table' },
      caption ? h('caption', { text: caption }) : null,
      h('thead', {}, h('tr', {}, headers.map((head) => h('th', {
        scope: 'col', class: head.num ? 'num' : '', text: head.label,
      })))),
      h('tbody', {}, rows.length ? rows : h('tr', {}, h('td', {
        colspan: String(headers.length), class: 'empty', text: '没有数据。',
      })))));
}

export function barCell(text, ratio, cls = '') {
  const percent = ratio === null || ratio === undefined ? 0 : Math.max(0, Math.min(100, ratio * 100));
  const width = percent > 0 ? `${Math.max(2, percent)}%` : '0%';
  return h('div', { class: 'bar-cell' },
    h('span', { class: 'bar', 'aria-hidden': 'true' },
      h('span', { class: `bar-fill ${cls}`.trim(), style: { width } })),
    text ? h('span', { class: 'bar-num', text }) : null);
}

export function statCard(label, value, note) {
  return h('li', { class: 'stat' },
    h('span', { class: 'stat-value', text: value }),
    h('span', { class: 'stat-label', text: label }),
    note ? h('span', { class: 'stat-note', text: note }) : null);
}

export function kvList(rows) {
  return h('dl', { class: 'kv' }, rows.map(([key, value]) => h('div', { class: 'kv-row' },
    h('dt', { text: key }),
    h('dd', { text: valueText(value) }))));
}

export function limitNote(total, shown) {
  if (total <= shown) return null;
  return h('p', { class: 'hint', text: `仅显示前 ${shown} 行（共 ${total} 行）；可用搜索或导出查看其余。` });
}

// ============================== 表单字段 ==============================

export function fieldBlock(id, labelText, control, hintText) {
  const message = h('p', { class: 'field-msg', id: `${id}-msg` });
  const node = h('div', { class: 'field' },
    h('label', { for: id, text: labelText }),
    control,
    hintText ? h('p', { class: 'hint', id: `${id}-hint`, text: hintText }) : null,
    message);
  control.setAttribute('aria-describedby', hintText ? `${id}-hint ${id}-msg` : `${id}-msg`);
  return { node, message, control };
}

export function numberField(options) {
  const input = h('input', {
    type: 'number', id: options.id, inputmode: 'decimal',
    min: String(options.min), max: String(options.max), step: String(options.step ?? 1),
  });
  const initial = options.get();
  input.value = initial === null || initial === undefined ? '' : String(initial);
  if (options.disabled && options.disabled()) input.disabled = true;
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

  const handle = () => { validate(); if (typeof options.onChange === 'function') options.onChange(); };
  input.addEventListener('input', handle);
  input.addEventListener('change', handle);
  return { node: block.node, input, validate };
}

export function textField(options) {
  const input = h('input', {
    type: options.type || 'text', id: options.id, autocomplete: 'off',
    value: options.get() === null || options.get() === undefined ? '' : String(options.get()),
    placeholder: options.placeholder || '',
  });
  if (options.maxlength) input.setAttribute('maxlength', String(options.maxlength));
  if (options.inputmode) input.setAttribute('inputmode', options.inputmode);
  const block = fieldBlock(options.id, options.label, input, options.hint);
  input.addEventListener('input', () => {
    options.set(input.value);
    if (typeof options.onChange === 'function') options.onChange();
  });
  return { node: block.node, input };
}

export function textareaField(options) {
  const input = h('textarea', {
    id: options.id, rows: String(options.rows || 6),
    placeholder: options.placeholder || '',
  });
  input.value = options.get() === null || options.get() === undefined ? '' : String(options.get());
  if (options.maxlength) input.setAttribute('maxlength', String(options.maxlength));
  const block = fieldBlock(options.id, options.label, input, options.hint);
  input.addEventListener('input', () => {
    options.set(input.value);
    if (typeof options.onChange === 'function') options.onChange();
  });
  return { node: block.node, input };
}

export function selectField(options) {
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
  select.addEventListener('change', () => {
    options.set(select.value);
    if (typeof options.onChange === 'function') options.onChange();
  });
  return { node: block.node, select };
}

export function checkField(options) {
  const input = h('input', { type: 'checkbox', id: options.id });
  input.checked = options.checked === true;
  const node = h('div', { class: 'field field-check' },
    h('label', { class: 'switch', for: options.id }, input, h('span', { text: options.label })),
    options.hint ? h('p', { class: 'hint', text: options.hint }) : null);
  input.addEventListener('change', () => {
    options.onChange(input.checked);
  });
  return { node, input };
}

// ============================== 保存条 ==============================

const saveBarRefs = new Map();

/** 结构只建一次，之后原地更新：重建会把 mousedown 与 mouseup 之间的按钮换掉，click 不派发。 */
export function buildSaveBar(root) {
  const status = h('p', { class: 'savebar-status', role: 'status', 'aria-live': 'polite' });
  const saveButton = h('button', { class: 'btn btn-primary', type: 'button', text: '保存配置' });
  const discardButton = h('button', { class: 'btn btn-quiet', type: 'button', text: '放弃修改' });
  const reloadButton = h('button', { class: 'btn', type: 'button', text: '重新载入最新配置', hidden: true });
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

export function getSaveBar(root) {
  if (!root) return null;
  let refs = saveBarRefs.get(root.id);
  if (!refs || !refs.inner.isConnected) refs = buildSaveBar(root);
  return refs;
}

export function fillMessageList(list, items) {
  clear(list);
  for (const item of items) list.appendChild(h('li', { text: typeof item === 'string' ? item : item.message }));
  list.hidden = items.length === 0;
}
