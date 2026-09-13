/**
 * components.js —— 可复用控件：可搜索下拉（单选/多选）与词表分组编辑器。
 *
 * 设计取舍：
 *  - 选项菜单是文档流内的可滚动区域，不做绝对定位浮层：窄屏可用、不会被其它面板遮挡。
 *  - 词表一律用「表格 + 分页/折叠 + 批量导入」，不把小标签铺满页面。
 */
import { clear, h, num, setText, termKey } from './core.js';
import { fieldBlock, importDialog, toast } from './ui.js';

function defaultKey(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return termKey(value);
  if (typeof value === 'object') {
    const name = value.name !== undefined ? value.name : value.value;
    const code = value.boss_code !== undefined ? value.boss_code : '';
    return `${termKey(name)}|${String(code || '')}`;
  }
  return termKey(String(value));
}

function defaultSearch(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'object') {
    return [value.name, value.boss_code, value.label, value.value].filter(Boolean).join(' ');
  }
  return String(value);
}

function defaultLabel(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'object') {
    const name = value.name !== undefined ? value.name : value.label;
    const code = value.boss_code !== undefined ? value.boss_code : '';
    return code ? `${name}（${code}）` : String(name === undefined ? '' : name);
  }
  return String(value);
}

/**
 * 可搜索下拉。mode='multi' 时值为数组，mode='single' 时为字符串。
 * @returns {{node: HTMLElement, refresh: Function}}
 */
export function searchSelect(options) {
  const {
    id, label, hint,
    mode = 'multi',
    options: choices = [],
    get, set, onChange,
    keyOf = defaultKey,
    searchOf = defaultSearch,
    labelOf = defaultLabel,
    placeholder = '输入关键字筛选选项…',
    emptyText = '（未选择）',
    maxItems = Number.MAX_SAFE_INTEGER,
    allowEmpty = true,
    emptyLabel = '不限定',
    allowCustom = false,
    customPlaceholder = '自定义名称',
    customCode = null,
    makeValue = (name, code) => (code ? { name, boss_code: code } : { name }),
  } = options;

  const chosen = h('div', { class: 'ms-chosen', role: 'list' });
  const menu = h('div', { class: 'ms-menu', role: 'listbox' });
  const search = h('input', { type: 'search', class: 'ms-search', autocomplete: 'off', placeholder });
  const picker = h('div', { class: 'ms-picker', hidden: true });
  const note = h('p', { class: 'hint ms-note' });
  const count = h('span', { class: 'ms-count' });
  const toggle = h('button', { class: 'btn btn-sm', type: 'button', text: '展开选项', 'aria-expanded': 'false' });
  const clearButton = mode === 'multi'
    ? h('button', { class: 'btn btn-sm btn-quiet', type: 'button', text: '清空' })
    : null;

  const current = () => {
    const value = get();
    if (mode === 'multi') return Array.isArray(value) ? value.slice() : [];
    return value === null || value === undefined ? '' : String(value);
  };

  const commit = (next) => {
    set(next);
    if (typeof onChange === 'function') onChange();
    refresh();
  };

  // ---- 自定义项（例如自定义城市与城市码） ----
  let customRow = null;
  if (allowCustom) {
    const nameInput = h('input', {
      type: 'text', class: 'ms-custom-name', autocomplete: 'off', placeholder: customPlaceholder,
      'aria-label': customPlaceholder,
    });
    const codeInput = customCode
      ? h('input', {
        type: 'text', class: 'ms-custom-code', autocomplete: 'off', inputmode: 'numeric',
        placeholder: customCode.placeholder || '城市码（可留空）', 'aria-label': customCode.label || '编码',
      })
      : null;
    const addCustom = () => {
      const name = nameInput.value.trim();
      if (!name) { setText(note, '请先填写名称。'); nameInput.focus(); return; }
      const code = codeInput ? codeInput.value.trim() : '';
      if (code && !/^\d{4,12}$/.test(code)) { setText(note, '编码应为 4–12 位数字，或留空。'); codeInput.focus(); return; }
      const value = makeValue(name, code);
      const values = current();
      if (mode === 'multi') {
        if (values.some((item) => keyOf(item) === keyOf(value))) { setText(note, `「${name}」已在列表中。`); return; }
        if (values.length >= maxItems) { setText(note, `最多 ${maxItems} 项。`); return; }
        commit(values.concat([value]));
      } else {
        commit(value);
      }
      nameInput.value = '';
      if (codeInput) codeInput.value = '';
      setText(note, `已添加「${labelOf(value)}」（尚未保存）。`);
    };
    if (codeInput) {
      codeInput.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); addCustom(); } });
    }
    nameInput.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); addCustom(); } });
    customRow = h('div', { class: 'ms-custom' },
      nameInput, codeInput,
      h('button', { class: 'btn btn-sm', type: 'button', text: '添加', onclick: addCustom }),
      customCode ? h('span', { class: 'hint', text: customCode.hint || '' }) : null);
  }

  function renderChosen() {
    clear(chosen);
    const values = current();
    if (mode === 'single') {
      chosen.appendChild(values === ''
        ? h('p', { class: 'ms-empty', text: emptyText })
        : h('div', { class: 'ms-row' },
          h('span', { class: 'ms-row-label', text: labelOf(values) }),
          h('button', {
            class: 'ms-remove', type: 'button', text: '×', 'aria-label': '清除选择',
            onclick: () => commit(''),
          })));
    } else if (!values.length) {
      chosen.appendChild(h('p', { class: 'ms-empty', text: emptyText }));
    } else {
      values.forEach((value, index) => {
        chosen.appendChild(h('div', { class: 'ms-row' },
          h('span', { class: 'ms-row-label', text: labelOf(value) }),
          h('button', {
            class: 'ms-remove', type: 'button', text: '×',
            'aria-label': `移除 ${labelOf(value)}`,
            onclick: () => {
              const next = current();
              next.splice(index, 1);
              commit(next);
            },
          })));
      });
    }
    const values_ = current();
    setText(count, mode === 'multi' ? `已选 ${values_.length} 项` : (values_ ? '已选 1 项' : '未选择'));
  }

  function renderMenu() {
    clear(menu);
    const query = termKey(search.value);
    const values = current();
    const selectedKeys = new Set((mode === 'multi' ? values : [values]).filter((item) => item !== '').map(keyOf));
    const filtered = choices.filter((choice) => {
      if (!query) return true;
      return termKey(searchOf(choice.value)).includes(query);
    });
    const shown = filtered.slice(0, 300);

    if (mode === 'single' && allowEmpty) {
      const active = current() === '';
      menu.appendChild(h('button', {
        class: `ms-option${active ? ' is-selected' : ''}`, type: 'button', role: 'option',
        'aria-selected': active ? 'true' : 'false', text: emptyLabel,
        onclick: () => commit(''),
      }));
    }

    for (const choice of shown) {
      const key = keyOf(choice.value);
      const active = selectedKeys.has(key);
      menu.appendChild(h('button', {
        class: `ms-option${active ? ' is-selected' : ''}`,
        type: 'button', role: 'option', 'aria-selected': active ? 'true' : 'false',
        onclick: () => {
          if (mode === 'single') {
            commit(choice.value);
            return;
          }
          const next = current();
          const index = next.findIndex((item) => keyOf(item) === key);
          if (index >= 0) next.splice(index, 1);
          else {
            if (next.length >= maxItems) { toast(`「${label}」最多 ${maxItems} 项。`, 'error'); return; }
            next.push(choice.value);
          }
          commit(next);
        },
      },
      h('span', { class: 'ms-option-label', text: choice.label === undefined ? labelOf(choice.value) : String(choice.label) }),
      choice.note ? h('span', { class: 'ms-option-note', text: String(choice.note) }) : null));
    }

    if (!shown.length) {
      menu.appendChild(h('p', { class: 'ms-empty', text: allowCustom ? '没有匹配项；可在下方添加自定义项。' : '没有匹配项。' }));
    } else if (filtered.length > shown.length) {
      menu.appendChild(h('p', { class: 'ms-empty', text: `匹配 ${filtered.length} 项，仅显示前 ${shown.length} 项；继续输入以缩小范围。` }));
    }
  }

  function refresh() {
    renderChosen();
    if (!picker.hidden) renderMenu();
  }

  search.addEventListener('input', () => {
    if (picker.hidden) togglePicker(true);
    renderMenu();
  });
  search.addEventListener('focus', () => togglePicker(true));

  function togglePicker(open) {
    picker.hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    setText(toggle, open ? '收起选项' : '展开选项');
    if (open) renderMenu();
  }

  toggle.addEventListener('click', () => togglePicker(picker.hidden));
  if (clearButton) {
    clearButton.addEventListener('click', () => {
      if (mode === 'multi') commit([]);
      else commit('');
    });
  }

  picker.appendChild(h('div', { class: 'ms-picker-head' }, search,
    h('span', { class: 'hint', text: `可选 ${choices.length} 项` })));
  picker.appendChild(menu);
  if (customRow) picker.appendChild(customRow);

  const control = h('div', { class: 'ms' },
    h('div', { class: 'ms-head' }, count, toggle, clearButton),
    chosen,
    picker,
    note);

  const block = fieldBlock(id, label, control, hint);
  refresh();
  return { node: block.node, refresh, block };
}

// ============================== 词表分组 ==============================

const SPLIT_RE = /[\n\r,，、;；\t]+/;

export function parseTerms(text) {
  return String(text || '').split(SPLIT_RE).map((item) => item.trim()).filter((item) => item !== '');
}

/**
 * 合并新词条：跳过空项、超过 maxLength 的项、已存在的项（按 Unicode 规范化 + 大小写折叠去重），
 * 并在达到 maxItems 后停止追加。纯函数，便于单独验证。
 */
export function dedupeTerms(existing, incoming, { maxItems = Number.MAX_SAFE_INTEGER, maxLength = 100 } = {}) {
  const values = Array.isArray(existing) ? existing.slice() : [];
  const seen = new Set(values.map((item) => termKey(item)));
  let added = 0;
  let duplicated = 0;
  let tooLong = 0;
  let overflow = 0;
  for (const raw of incoming) {
    const item = String(raw === null || raw === undefined ? '' : raw).trim();
    if (!item) continue;
    if (item.length > maxLength) { tooLong += 1; continue; }
    const key = termKey(item);
    if (seen.has(key)) { duplicated += 1; continue; }
    if (values.length >= maxItems) { overflow += 1; continue; }
    seen.add(key);
    values.push(item);
    added += 1;
  }
  return { values, added, duplicated, tooLong, overflow };
}

/**
 * 词表编辑器：搜索 + 分页 + 折叠 + 批量多行导入（去重）。
 * @returns {{node: HTMLElement, refresh: Function}}
 */
export function termGroup(config) {
  const {
    id, title, note,
    get, set, onChange,
    columns = [],
    placeholder = '输入词条后回车或点添加',
    maxItems = Number.MAX_SAFE_INTEGER,
    maxLength = 100,
    pageSize = 40,
    rowExtra = null,
    stats = null,
    emptyText = '列表为空。',
    allowAdd = true,
    openByDefault = true,
    importNote = '每行一个词条；也支持用逗号、顿号、分号分隔。导入时自动去重并跳过重复项。',
    usedElsewhere = null,
  } = config;

  const state = { query: '', page: 1, open: openByDefault };

  const countBadge = h('span', { class: 'badge badge-neutral' });
  const message = h('p', { class: 'hint', role: 'status', 'aria-live': 'polite' });
  const body = h('div', { class: 'term-body' });
  const tableHost = h('div', { class: 'table-wrap' });
  const pagerHost = h('nav', { class: 'pager', 'aria-label': `${title}分页` });
  const search = h('input', {
    type: 'search', class: 'term-search', autocomplete: 'off',
    placeholder: '搜索词条…', 'aria-label': `搜索${title}`,
  });
  const newInput = h('input', {
    type: 'text', id, class: 'term-input', autocomplete: 'off', placeholder,
  });
  const importButton = h('button', { class: 'btn btn-sm', type: 'button', text: '批量导入' });
  const toggle = h('button', {
    class: 'collapse-toggle', type: 'button',
    'aria-expanded': openByDefault ? 'true' : 'false', text: openByDefault ? '收起' : '展开',
  });

  const valuesOf = () => {
    const value = get();
    return Array.isArray(value) ? value.slice() : [];
  };

  const commit = (next, message_) => {
    set(next);
    if (typeof onChange === 'function') onChange();
    draw();
    if (message_) setText(message, message_);
  };

  function addValues(raw, mode = 'append') {
    const parsed = parseTerms(raw);
    const existing = mode === 'replace' ? [] : valuesOf();
    const { values, added, duplicated, tooLong, overflow } = dedupeTerms(existing, parsed, { maxItems, maxLength });
    const bits = [`新增 ${added} 条`];
    if (mode === 'replace') bits.unshift('已覆盖替换');
    if (duplicated) bits.push(`跳过重复 ${duplicated} 条`);
    if (tooLong) bits.push(`超过 ${maxLength} 字被忽略 ${tooLong} 条`);
    if (overflow) bits.push(`超出上限 ${maxItems} 被忽略 ${overflow} 条`);
    commit(values, bits.join('；') + '（尚未保存）。');
    return { added, duplicated, tooLong, overflow };
  }

  function removeValue(index) {
    const next = valuesOf();
    const [removed] = next.splice(index, 1);
    commit(next, `已移除「${removed}」（尚未保存）。`);
  }

  function filtered() {
    const values = valuesOf();
    const query = termKey(state.query);
    if (!query) return values.map((value, index) => ({ value, index }));
    return values
      .map((value, index) => ({ value, index }))
      .filter((row) => termKey(String(row.value)).includes(query));
  }

  function draw() {
    const values = valuesOf();
    setText(countBadge, `${values.length} 条`);
    const rows = filtered();
    const pages = Math.max(1, Math.ceil(rows.length / pageSize));
    if (state.page > pages) state.page = pages;

    const head = columns.map((column) => h('th', { scope: 'col', class: column.num ? 'num' : '', text: column.label }));
    const pageRows = rows.slice((state.page - 1) * pageSize, state.page * pageSize);
    const bodyRows = pageRows.map((row) => h('tr', {},
      h('td', { class: 'term-cell' }, h('span', { class: 'term-text', text: String(row.value) })),
      ...(stats ? stats(String(row.value)).map((cell) => h('td', {
        class: cell && cell.num ? 'num' : '',
      }, cell && cell.node ? cell.node : h('span', { text: cell ? String(cell.text) : '—' }))) : []),
      rowExtra ? h('td', {}, rowExtra(String(row.value), row.index)) : null,
      h('td', { class: 'term-actions' },
        h('button', {
          class: 'btn btn-sm btn-quiet', type: 'button', text: '移除',
          'aria-label': `移除 ${row.value}`,
          onclick: () => removeValue(row.index),
        }))));

    if (!values.length) {
      clear(tableHost);
      tableHost.appendChild(h('p', { class: 'empty', text: emptyText }));
    } else if (!rows.length) {
      clear(tableHost);
      tableHost.appendChild(h('p', { class: 'empty', text: `没有匹配「${state.query}」的词条。` }));
    } else {
      clear(tableHost);
      tableHost.appendChild(h('table', { class: 'table term-table' },
        h('thead', {}, h('tr', {}, h('th', { scope: 'col', text: '词条' }), ...head,
          rowExtra ? h('th', { scope: 'col', text: '设置' }) : null,
          h('th', { scope: 'col', text: '操作' }))),
        h('tbody', {}, bodyRows)));
    }

    clear(pagerHost);
    if (pages > 1) {
      pagerHost.appendChild(h('button', {
        class: 'btn btn-sm btn-quiet', type: 'button', text: '上一页', disabled: state.page <= 1,
        onclick: () => { state.page -= 1; draw(); },
      }));
      pagerHost.appendChild(h('span', { class: 'pager-info', text: `第 ${state.page} / ${pages} 页 · 共 ${rows.length} 条` }));
      pagerHost.appendChild(h('button', {
        class: 'btn btn-sm btn-quiet', type: 'button', text: '下一页', disabled: state.page >= pages,
        onclick: () => { state.page += 1; draw(); },
      }));
    } else if (rows.length) {
      pagerHost.appendChild(h('span', { class: 'pager-info', text: `共 ${rows.length} 条` }));
    }
  }

  const addCurrent = () => {
    const raw = newInput.value.trim();
    if (!raw) { setText(message, '请先输入词条。'); newInput.focus(); return; }
    const result = addValues(raw, 'append');
    if (result.added) newInput.value = '';
    newInput.focus();
  };

  newInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); addCurrent(); }
  });
  const addButton = h('button', { class: 'btn btn-primary', type: 'button', text: '添加', onclick: addCurrent });
  search.addEventListener('input', () => { state.query = search.value; state.page = 1; draw(); });
  importButton.addEventListener('click', async () => {
    const result = await importDialog({
      title: `${title} · 批量导入`,
      note: importNote,
      placeholder: '每行一个词条',
    });
    if (result) addValues(result.text, result.mode);
  });
  toggle.addEventListener('click', () => {
    state.open = !state.open;
    body.hidden = !state.open;
    toggle.setAttribute('aria-expanded', state.open ? 'true' : 'false');
    setText(toggle, state.open ? '收起' : '展开');
  });

  body.hidden = !openByDefault;
  body.appendChild(h('div', { class: 'term-toolbar' }, search, newInput, addButton, importButton));
  body.appendChild(tableHost);
  body.appendChild(pagerHost);
  body.appendChild(message);
  if (usedElsewhere) body.appendChild(h('p', { class: 'hint', text: usedElsewhere }));

  const node = h('section', { class: 'panel term-group' },
    h('div', { class: 'panel-head' },
      h('h3', { class: 'panel-title', text: title }),
      countBadge,
      toggle),
    note ? h('p', { class: 'panel-note', text: note }) : null,
    body);

  draw();
  return { node, refresh: draw };
}

/** 简单分页条。 */
export function pager(page, pages, total, onGo) {
  const host = h('nav', { class: 'pager', 'aria-label': '分页' });
  if (pages <= 1) {
    if (total) host.appendChild(h('span', { class: 'pager-info', text: `共 ${total} 条` }));
    return host;
  }
  host.appendChild(h('button', {
    class: 'btn btn-sm btn-quiet', type: 'button', text: '上一页', disabled: page <= 1,
    onclick: () => onGo(page - 1),
  }));
  host.appendChild(h('span', { class: 'pager-info', text: `第 ${page} / ${pages} 页 · 共 ${total} 条` }));
  host.appendChild(h('button', {
    class: 'btn btn-sm btn-quiet', type: 'button', text: '下一页', disabled: page >= pages,
    onclick: () => onGo(page + 1),
  }));
  return host;
}

/** 进度条（任务进度用）。 */
export function progressBar(processed, total, failed) {
  const done = num(processed) || 0;
  const all = num(total);
  const ratio = all && all > 0 ? Math.min(1, done / all) : null;
  return h('div', { class: 'progress' },
    h('div', { class: 'progress-track', role: 'progressbar', 'aria-valuenow': String(done), 'aria-valuemax': all === null ? '' : String(all) },
      h('div', { class: `progress-fill${failed ? ' has-failed' : ''}`, style: { width: `${ratio === null ? 0 : Math.max(2, ratio * 100)}%` } })),
    h('span', { class: 'progress-text', text: all === null ? `已处理 ${done}` : `已处理 ${done} / ${all}` }));
}
