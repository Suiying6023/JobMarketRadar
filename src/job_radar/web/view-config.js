/**
 * view-config.js —— 配置页：分组编辑 CollectionConfig。
 *
 * 规则：
 *  - 只写服务端已有的键（模型 extra="forbid"，凭空加键会让整份配置 422）。
 *  - 密钥字段永远以空值展示；留空保存 = 保留服务端已有值，勾选「清除」才进 clear_secrets。
 *  - 每组标注生效时点（state.apply_policy 优先）。
 */
import {
  clear, getPath, h, num, setPath, str, strList, termKey,
} from './core.js';
import {
  CANDIDATE_DEFAULTS, FILTER_KEYS, FILTER_LABELS, app, applyPolicyRows, candidateDefaults, configHas,
  effectText, markDirty, secretsCleared, toggleClearSecret, updateSaveBar,
} from './store.js';
import {
  checkField, collapsiblePanel, emptyBlock, fieldBlock, kvList, loadingBlock, numberField, openDialog, panel,
  selectField, textField, textareaField,
} from './ui.js';
import { searchSelect } from './components.js';

const PLATFORM_LABELS = {
  boss: 'BOSS 直聘',
  liepin: '猎聘',
  job51: '前程无忧',
  zhihu: '知乎',
  zhilian: '智联招聘',
};

function group(title, note, ...children) {
  return h('fieldset', { class: 'group' },
    h('legend', { text: title }),
    note ? h('p', { class: 'group-note', text: note }) : null,
    ...children);
}

function grid(...children) {
  return h('div', { class: 'grid' }, ...children);
}

function effectiveNote(key) {
  const text = effectText(key);
  return text ? `生效时点：${text}` : '';
}

// ---------------- 在线访问总开关 ----------------

function networkSection(config) {
  if (!configHas('network_enabled')) return null;
  const field = checkField({
    id: 'cfg-network-enabled',
    label: '允许联网任务访问平台',
    checked: config.network_enabled === true,
    hint: '与「访问策略」是两道独立闸门：两项都允许时联网任务才会执行。'
      + '访问暂停与恢复在任务页的访问面板操作。',
    onChange: (checked) => {
      if (!checked) {
        config.network_enabled = false;
        markDirty();
        return;
      }
      if (config.network_enabled === true) return;
      void openDialog({
        title: '允许联网任务访问平台',
        note: '打开后，联网类型的任务在访问策略也允许时才会真正发出请求。'
          + '当前访问策略是否允许，请以顶栏与任务页显示的为准。',
        confirmLabel: '确认打开',
        danger: true,
        checkboxLabel: '我确认现在允许联网任务访问平台',
      }).then((result) => {
        if (result.ok) {
          config.network_enabled = true;
          markDirty();
        } else {
          field.input.checked = false;
        }
      });
    },
  });
  return group('在线访问总开关', effectiveNote('network_enabled'), field.node);
}

// ---------------- 城市 ----------------

function cityChoices() {
  const cities = (app.options ? app.options.cities : []) || [];
  const seen = new Set();
  const choices = [];
  for (const city of cities) {
    const key = `${city.name}|${city.boss_code || ''}`;
    if (seen.has(key)) continue;
    seen.add(key);
    choices.push({
      value: city.boss_code ? { name: city.name, boss_code: city.boss_code } : { name: city.name },
      label: city.name,
      note: city.boss_code ? `码 ${city.boss_code}` : '未记录城市码',
    });
  }
  return choices;
}

function citySection(config) {
  const select = searchSelect({
    id: 'cfg-city-select',
    label: '采集城市',
    hint: '支持搜索和多选，也可添加城市名称与平台城市码。',
    mode: 'multi',
    options: cityChoices(),
    get: () => (Array.isArray(config.cities) ? config.cities : []),
    set: (next) => { config.cities = next; },
    onChange: markDirty,
    maxItems: 500,
    emptyText: '尚未选择城市。',
    placeholder: '搜索城市名或城市码…',
    allowCustom: true,
    customPlaceholder: '自定义城市名',
    customCode: {
      label: 'BOSS 城市码',
      placeholder: '城市码（可留空）',
      hint: '城市码为平台城市编号；留空时由服务端按城市名补全。',
    },
    makeValue: (name, code) => (code ? { name, boss_code: code } : { name }),
  });
  return group('采集城市', '采集按这里的城市逐个搜索。', select.node);
}

// ---------------- 筛选组合 ----------------

/** SearchFilters 存的是中文标签，采集器再映射成平台接口码（见 collectors/boss.py::_filter_value）。
 *  因此下拉里的「取值」用 label，`/api/options` 给的 value（接口码）只作为提示显示。
 *  多选在配置里以英文逗号连接 —— 这正是采集器支持的多选写法。 */
function enumChoices(key, currentValues) {
  const raw = (app.options && app.options.filters && app.options.filters[key]) || [];
  const choices = [];
  const seen = new Set();
  for (const item of raw) {
    const label = str(item.label) || str(item.value);
    if (!label || seen.has(termKey(label))) continue;
    seen.add(termKey(label));
    choices.push({
      value: label,
      label,
      note: item.value && item.value !== label ? `接口码 ${item.value}` : '',
    });
  }
  for (const value of currentValues) {
    if (seen.has(termKey(value))) continue;
    seen.add(termKey(value));
    choices.push({ value, label: `${value}（当前值）`, note: '本地码表未收录；保存时原样提交' });
  }
  return choices;
}

function filterControls(config, group_, index) {
  const controls = FILTER_KEYS.map((key) => {
    const id = `cfg-filter-${index}-${key}`;
    const label = FILTER_LABELS[key];
    // 每次渲染都从 group_ 重新读，保证「放弃修改 / 重新载入」之后界面与草稿一致。
    const read = () => String(group_[key] === null || group_[key] === undefined ? '' : group_[key])
      .split(',').map((part) => part.trim()).filter((part) => part !== '');
    const write = (next) => { group_[key] = next.length ? next.join(',') : null; };
    const choices = enumChoices(key, read());

    if (!choices.length) {
      return searchSelect({
        id, label, mode: 'multi', options: [],
        get: read, set: write, onChange: markDirty,
        allowCustom: true,
        customPlaceholder: `填写${label}原值`,
        makeValue: (name) => name,
        emptyText: '不限定',
        placeholder: `本地没有${label}码表，可自定义添加`,
        hint: '本地没有这一维度的码表：按平台原值填写，可添加多个（保存时以英文逗号连接）。',
      }).node;
    }

    const hasCode = choices.some((choice) => choice.note && choice.note.startsWith('接口码'));
    return searchSelect({
      id, label, mode: 'multi', options: choices,
      get: read, set: write, onChange: markDirty,
      emptyText: '不限定',
      placeholder: `搜索${label}选项…`,
      hint: hasCode
        ? '可多选；多项保存为英文逗号连接的标签，采集时逐个映射成平台接口码。'
        : '可多选；多项保存为英文逗号连接。',
    }).node;
  });
  return h('div', { class: 'filter-group' },
    h('div', { class: 'filter-group-head' },
      h('h4', { text: `筛选组合 ${index + 1}` }),
      h('button', {
        class: 'btn btn-sm btn-danger', type: 'button', text: '删除本组',
        onclick: () => {
          config.filters.splice(index, 1);
          markDirty();
          renderConfig();
        },
      })),
    grid(...controls));
}

function filtersSection(config) {
  if (!Array.isArray(config.filters)) config.filters = [];
  const host = h('div', {}, config.filters.map((item, index) => filterControls(config, item, index)));
  if (!config.filters.length) host.appendChild(emptyBlock('至少需要一组筛选组合。'));
  host.appendChild(h('div', { class: 'group-actions' },
    h('button', {
      class: 'btn btn-sm', type: 'button', text: '添加筛选组合',
      onclick: () => {
        config.filters.push({
          salary: null, experience: null, degree: null, scale: null, stage: null, industry: null, jobType: null,
        });
        markDirty();
        renderConfig();
      },
    })));
  return group('筛选组合',
    '每组是一个独立的搜索条件集合，采集时逐组使用；留空 = 该维度不限。'
    + '单个维度可多选，保存时以英文逗号连接（例如「应届生,1年以内」），采集时逐个映射成平台接口码。',
    host);
}

// ---------------- 通用数字/文本 ----------------

function numField(config, id, label, path, options = {}) {
  const current = getPath(config, path);
  return numberField({
    id, label,
    min: options.min, max: options.max, step: options.step, integer: options.integer, nullable: options.nullable,
    hint: options.hint,
    get: () => (current === undefined ? (options.fallback ?? null) : num(current)),
    set: (value) => {
      if (value === null && options.nullable) setPath(config, path, null);
      else if (value !== null) setPath(config, path, value);
    },
    onChange: markDirty,
  }).node;
}

function delayPair(config, label, key, minId, maxId, hint) {
  return h('div', { class: 'field field-wide' },
    h('span', { class: 'field-label', text: label }),
    h('div', { class: 'field-inline' },
      numField(config, minId, '下限（秒）', [key, 0], { min: 0, max: 86400, step: 0.5, fallback: 0 }),
      numField(config, maxId, '上限（秒）', [key, 1], { min: 0, max: 86400, step: 0.5, fallback: 0 })),
    h('p', { class: 'hint', text: hint }));
}

// ---------------- 密钥 ----------------

function secretField(config, id, label, path, hint) {
  const [groupName, key] = path.split('.');
  const bucket = config[groupName];
  const saved = Boolean(app.state && app.state.secrets && app.state.secrets[path]);
  const input = h('input', {
    type: 'password', id, autocomplete: 'new-password', value: '',
    placeholder: saved ? '已保存密钥；留空 = 沿用' : '尚未设置',
  });
  const clearBox = h('input', { type: 'checkbox', id: `${id}-clear` });
  clearBox.checked = secretsCleared(path);
  const clearLabel = h('label', { class: 'switch', for: `${id}-clear` },
    clearBox, h('span', { text: '清除已保存的密钥' }));

  input.addEventListener('input', () => {
    bucket[key] = input.value;
    if (input.value !== '') {
      if (clearBox.checked) { clearBox.checked = false; toggleClearSecret(path, false); }
      clearBox.disabled = true;
    } else {
      clearBox.disabled = false;
    }
    markDirty();
  });
  clearBox.addEventListener('change', () => {
    toggleClearSecret(path, clearBox.checked);
    input.value = '';
    bucket[key] = '';
  });

  const block = fieldBlock(id, label, input, hint);
  block.node.appendChild(clearLabel);
  block.node.appendChild(h('p', {
    class: 'hint',
    text: saved ? '当前状态：已保存密钥。' : '当前状态：未设置密钥。',
  }));
  return block.node;
}

// ---------------- 各分组 ----------------

function planSection(config) {
  return group('采集计划', '控制每轮采多少、翻多深。',
    grid(
      configHas('list_source') ? selectField({
        id: 'cfg-list-source', label: '列表采集方式',
        get: () => config.list_source,
        set: (value) => { config.list_source = value; },
        options: [{ value: 'native', label: '随网页搜索和翻页读取' }, { value: 'api', label: '直接读取列表接口' }],
        hint: '网页方式每页数量由网站决定，同时补详情时读取卡片响应。更换方式请使用新的批次编号。',
        onChange: markDirty,
      }).node : null,
      numField(config, 'cfg-pages-per-query', '默认每词翻页深度', ['pages_per_query'], { min: 1, max: 10, integer: true, fallback: 3 }),
      numField(config, 'cfg-max-page-number', '单关键词最大页数', ['max_page_number'], { min: 1, max: 30, integer: true, fallback: 10 }),
      numField(config, 'cfg-max-keywords', '单轮关键词数', ['max_keywords_per_run'], { min: 1, max: 50, integer: true, fallback: 3 }),
      numField(config, 'cfg-detail-limit', '每词详情补全上限', ['detail_limit_per_query'], { min: 0, max: 200, integer: true, fallback: 30 }),
      numField(config, 'cfg-detail-after', '正文更新间隔（小时，0 表示每轮更新）', ['service', 'detail_after_hours'], { min: 0, max: 87600, fallback: 168 }),
      numField(config, 'cfg-page-size', '直接读取接口时每页条数', ['page_size'], { min: 10, max: 50, integer: true, fallback: 30 }),
      checkField({
        id: 'cfg-collect-details', label: '采集时同时补全详情',
        checked: config.collect_details === true,
        hint: '采集列表时一并抓取详情；关闭时在任务页单独发起「补全岗位详情」。',
        onChange: (checked) => { config.collect_details = checked; markDirty(); },
      }).node),
    h('p', { class: 'hint', text: '每词页深与搜索词在「关键词」页维护。' }));
}

function budgetSection(config) {
  return group('请求节奏', '拟人化的间隔与冷却。没有日线拦截——出事由访问策略的持久熔断停下来。',
    grid(
      configHas('detail_batch')
        ? numField(config, 'cfg-detail-batch', '详情批大小', ['detail_batch'], { min: 1, max: 50, integer: true, fallback: 5 })
        : null,
      configHas('burst_threshold')
        ? numField(config, 'cfg-burst-threshold', '突发阈值（条）', ['burst_threshold'], { min: 1, max: 100, integer: true, fallback: 8 })
        : null),
    delayPair(config, '列表请求间隔', 'request_delay_seconds', 'cfg-req-min', 'cfg-req-max', '每个列表请求之间的随机等待区间。'),
    delayPair(config, '详情请求间隔', 'detail_delay_seconds', 'cfg-detail-min', 'cfg-detail-max', '每条详情之间的随机等待区间。'),
    configHas('detail_cooldown_seconds')
      ? delayPair(config, '详情批间冷却', 'detail_cooldown_seconds', 'cfg-cooldown-min', 'cfg-cooldown-max', '每批详情后的冷却区间。')
      : null,
    configHas('burst_penalty_seconds')
      ? delayPair(config, '突发冷却', 'burst_penalty_seconds', 'cfg-burst-min', 'cfg-burst-max', '连续请求达到突发阈值后的额外冷却。')
      : null);
}

function serviceSection(config) {
  if (!configHas('service')) {
    return group('服务与批次', '运行中的服务尚未提供 service 分组。',
      emptyBlock('该分组不可用：保存时保留原值。'));
  }
  const service = config.service;
  return group('服务与批次', '任务调度层的批量与保留设置。',
    grid(
      numField(service, 'cfg-batch-size', '批大小', ['batch_size'], { min: 1, max: 200, integer: true, fallback: 20 }),
      numField(service, 'cfg-event-retention', '事件保留条数', ['event_retention'], { min: 1000, max: 1000000, integer: true, fallback: 20000 }),
      numField(service, 'cfg-stop-timeout', '停止超时（秒）', ['stop_timeout_seconds'], { min: 1, max: 60, step: 1, fallback: 10 }),
      checkField({
        id: 'cfg-candidate-filtering', label: '按候选人事实条件预筛选行',
        checked: service.candidate_filtering === true,
        hint: '采集、补全详情、刷新状态与打标在选行时都按候选人事实条件过滤；改动在新任务生效。',
        onChange: (checked) => { service.candidate_filtering = checked; markDirty(); },
      }).node),
    h('p', { class: 'hint', text: effectText('service.batch_size') ? `批大小生效时点：${effectText('service.batch_size')}` : '' }));
}

function refreshSection(config) {
  if (!configHas('refresh')) {
    return group('刷新与出口', '运行中的服务尚未提供 refresh 分组。', emptyBlock('该分组不可用：保存时保留原值。'));
  }
  const refresh = config.refresh;
  return group('刷新与出口', '状态刷新默认本地直连；代理用于网络可达性。',
    grid(
      selectField({
        id: 'cfg-refresh-route', label: '刷新路线',
        options: [{ value: 'direct', label: '本地直连（默认）' }, { value: 'proxy', label: '经代理' }],
        get: () => str(refresh.route) || 'direct',
        set: (value) => { refresh.route = value; },
        onChange: markDirty,
        hint: '选择「经代理」时必须填写代理地址。',
      }).node,
      textField({
        id: 'cfg-refresh-proxy', label: '代理地址',
        get: () => str(refresh.proxy_server),
        set: (value) => { refresh.proxy_server = value; },
        onChange: markDirty,
        placeholder: '例如 socks5://127.0.0.1:1080',
      }).node,
      textField({
        id: 'cfg-refresh-proxy-user', label: '代理用户名',
        get: () => str(refresh.proxy_username),
        set: (value) => { refresh.proxy_username = value; },
        onChange: markDirty,
        placeholder: '可选',
      }).node,
      textField({
        id: 'cfg-refresh-profile', label: '刷新会话目录',
        get: () => str(refresh.profile_dir),
        set: (value) => { refresh.profile_dir = value; },
        onChange: markDirty,
      }).node,
      numField(refresh, 'cfg-refresh-after', '只刷新超过（小时）未更新的岗位', ['after_hours'], { min: 0, max: 87600, step: 1, fallback: 168 })),
    secretField(config, 'cfg-refresh-proxy-pass', '代理密码', 'refresh.proxy_password',
      '留空保存会保留已保存的密码。'),
    h('p', { class: 'hint', text: effectiveNote('refresh') }));
}

function browserSection(config) {
  if (!configHas('browser')) return null;
  const browser = config.browser;
  return collapsiblePanel('浏览器会话', '连接与浏览器设置在新任务生效。', false,
    grid(
      selectField({
        id: 'cfg-browser-mode', label: '浏览器模式',
        options: [{ value: 'persistent', label: '持久化配置目录（推荐）' }, { value: 'cdp', label: 'CDP 调试端口（兼容）' }],
        get: () => str(browser.mode) || 'persistent',
        set: (value) => { browser.mode = value; },
        onChange: markDirty,
      }).node,
      textField({
        id: 'cfg-browser-channel', label: '浏览器通道',
        get: () => str(browser.channel), set: (value) => { browser.channel = value; }, onChange: markDirty,
      }).node,
      textField({
        id: 'cfg-browser-dir', label: '用户数据目录',
        get: () => str(browser.user_data_dir), set: (value) => { browser.user_data_dir = value; }, onChange: markDirty,
      }).node,
      textField({
        id: 'cfg-browser-cdp', label: 'CDP 地址',
        get: () => str(browser.cdp_url), set: (value) => { browser.cdp_url = value; }, onChange: markDirty,
      }).node,
      numField(browser, 'cfg-browser-timeout', '导航超时（毫秒）', ['navigation_timeout_ms'], { min: 1000, max: 300000, integer: true, fallback: 30000 }),
      numField(browser, 'cfg-browser-slowmo', '操作慢速（毫秒）', ['slow_mo_ms'], { min: 0, max: 5000, integer: true, fallback: 0 }),
      textField({
        id: 'cfg-browser-window', label: '窗口位置 x,y',
        get: () => str(browser.window_position), set: (value) => { browser.window_position = value; }, onChange: markDirty,
        placeholder: '例如 2400,1400；留空由浏览器决定',
        hint: '坐标取正数；留空时由浏览器决定。',
      }).node,
      textField({
        id: 'cfg-browser-proxy', label: '浏览器代理地址',
        get: () => str(browser.proxy_server), set: (value) => { browser.proxy_server = value; }, onChange: markDirty,
        placeholder: '例如 socks5://127.0.0.1:1080',
      }).node,
      textField({
        id: 'cfg-browser-proxy-user', label: '代理用户名',
        get: () => str(browser.proxy_username), set: (value) => { browser.proxy_username = value; }, onChange: markDirty,
      }).node),
    checkField({
      id: 'cfg-browser-headless', label: '无头模式', checked: browser.headless === true,
      hint: '采集与验证通常需要可见窗口；无头模式会影响人工验证流程。',
      onChange: (checked) => { browser.headless = checked; markDirty(); },
    }).node,
    checkField({
      id: 'cfg-browser-block-heavy', label: '拦截重资源（图片 / 媒体）', checked: browser.block_heavy_resources === true,
      onChange: (checked) => { browser.block_heavy_resources = checked; markDirty(); },
    }).node,
    secretField(config, 'cfg-browser-proxy-pass', '代理密码', 'browser.proxy_password', '留空保存会保留已保存的密码。'),
    h('p', { class: 'hint', text: effectiveNote('browser') }));
}

function llmSection(config) {
  if (!configHas('llm')) {
    return group('LLM 打标', '运行中的服务尚未提供 llm 分组。', emptyBlock('该分组不可用：保存时保留原值。'));
  }
  const llm = config.llm;
  return group('LLM 打标', '端点、模型、密钥与提示词；改动在下一批次生效。',
    grid(
      checkField({
        id: 'cfg-llm-enabled', label: '启用 LLM 打标', checked: llm.enabled === true,
        onChange: (checked) => { llm.enabled = checked; markDirty(); },
      }).node,
      textField({
        id: 'cfg-llm-base-url', label: 'API 端点',
        get: () => str(llm.base_url), set: (value) => { llm.base_url = value; }, onChange: markDirty,
        placeholder: 'http://127.0.0.1:11434/v1',
        hint: '服务根路径；认证信息用独立字段。',
      }).node,
      textField({
        id: 'cfg-llm-model', label: '模型名',
        get: () => str(llm.model), set: (value) => { llm.model = value; }, onChange: markDirty,
        placeholder: '例如 qwen2.5:14b',
      }).node,
      textField({
        id: 'cfg-llm-env', label: '密钥环境变量名（回退）',
        get: () => str(llm.api_key_env), set: (value) => { llm.api_key_env = value; }, onChange: markDirty,
        placeholder: '例如 OPENAI_API_KEY（可选）',
        hint: '本机密钥为空时读取这个环境变量。',
      }).node,
      numField(llm, 'cfg-llm-timeout', '请求超时（秒）', ['timeout_seconds'], { min: 1, max: 600, step: 1, fallback: 60 }),
      numField(llm, 'cfg-llm-concurrency', '同时抽取数量', ['concurrency'], { min: 1, max: 64, integer: true, fallback: 1 }),
      ('reasoning_effort' in llm) ? selectField({
        id: 'cfg-llm-effort', label: '推理强度（由接口支持情况决定）',
        options: ['auto', 'none', 'minimal', 'low', 'medium', 'high'].map(value => ({value, label: value})),
        get: () => llm.reasoning_effort || 'auto',
        set: value => { llm.reasoning_effort = value; }, onChange: markDirty,
      }).node : null,
      numField(llm, 'cfg-llm-max-tokens', '最大输出 tokens', ['max_tokens'], { min: 64, max: 32768, integer: true, fallback: 2048 }),
      numField(llm, 'cfg-llm-max-input', '最大输入字符数', ['max_input_chars'], { min: 1000, max: 100000, integer: true, fallback: 20000 })),
    secretField(config, 'cfg-llm-api-key', 'API key（本机保存）', 'llm.api_key',
      '本机密钥优先使用，留空时回退到上面的环境变量。留空保存沿用已保存的密钥，勾选「清除」删除。'),
    textareaField({
      id: 'cfg-llm-prompt', label: '打标提示词', rows: 10,
      get: () => str(llm.system_prompt),
      set: (value) => { llm.system_prompt = value; },
      onChange: markDirty,
      maxlength: 30000,
      hint: '提示词里的岗位原文按数据处理；改动在下一批次生效。',
    }).node,
    h('p', { class: 'hint', text: effectiveNote('llm') }));
}

/** 学历：已知取值来自 /api/options.filters.degree（与 decisions.DEGREES 一致），
 *  当前配置里的未知取值由 selectField 原样保留。 */
function educationField(candidate) {
  const known = ((app.options && app.options.filters && app.options.filters.degree) || [])
    .map((item) => str(item.label) || str(item.value))
    .filter(Boolean);
  const options = [{ value: '不限', label: '不限（关闭学历门槛）' }];
  const seen = new Set(['不限']);
  for (const label of known) {
    if (seen.has(label)) continue;
    seen.add(label);
    options.push({ value: label, label });
  }
  for (const label of ['初中及以下', '中专/中技', '高中', '大专', '本科', '硕士', '博士']) {
    if (seen.has(label)) continue;
    seen.add(label);
    options.push({ value: label, label });
  }
  return selectField({
    id: 'cfg-education', label: '学历要求', options,
    get: () => str(candidate.education) || CANDIDATE_DEFAULTS.education,
    set: (value) => { candidate.education = value; },
    onChange: markDirty,
    hint: '按平台返回的学历字段匹配；「不限」= 关闭学历门槛。',
  }).node;
}

function candidateSection(config) {
  if (!config.candidate || typeof config.candidate !== 'object') {
    // 通用默认：不限学历 / 不限经验 / 薪资 0–300K / 不排除实习与外包。
    config.candidate = candidateDefaults();
  }
  const candidate = config.candidate;
  const cityNames = cityChoices().map((choice) => ({
    value: choice.value.name,
    label: choice.label,
    note: choice.note,
  }));

  const graduation = num(candidate.graduation_year);
  const gradToggle = checkField({
    id: 'cfg-grad-any', label: '不限届别', checked: graduation === null,
    hint: '勾选 = 届别不限。',
    onChange: (checked) => {
      const input = document.getElementById('cfg-grad-year');
      if (checked) {
        candidate.graduation_year = null;
        if (input) { input.value = ''; input.disabled = true; }
      } else {
        if (input) input.disabled = false;
        if (candidate.graduation_year === null) candidate.graduation_year = new Date().getFullYear();
        if (input) input.value = String(candidate.graduation_year);
      }
      markDirty();
    },
  });
  const gradField = numberField({
    id: 'cfg-grad-year', label: '毕业届别（年份）', min: 1990, max: 2100, integer: true, nullable: true,
    hint: '例如 2026 表示 26 届。',
    disabled: () => candidate.graduation_year === null,
    get: () => (candidate.graduation_year === undefined ? null : candidate.graduation_year),
    set: (value) => { candidate.graduation_year = value; },
    onChange: markDirty,
  });
  gradField.input.disabled = graduation === null;

  return group('候选人画像', '用于计算复核优先级；开启「按候选人事实条件预筛选行」后也决定哪些岗位进入采集、补全、刷新与打标。',
    grid(
      searchSelect({
        id: 'cfg-cand-cities', label: '目标城市', mode: 'multi',
        options: cityNames,
        get: () => (Array.isArray(candidate.cities) ? candidate.cities : []),
        set: (next) => { candidate.cities = next; },
        onChange: markDirty,
        maxItems: 200,
        emptyText: '空 = 不限城市',
        placeholder: '搜索城市…',
        hint: '留空表示不限城市。未列出的城市仍保留在库中，只是复核排序降级。',
      }).node,
      searchSelect({
        id: 'cfg-cand-preferred', label: '优先城市（同级加分）', mode: 'multi',
        options: cityNames,
        get: () => (Array.isArray(candidate.preferred_cities) ? candidate.preferred_cities : []),
        set: (next) => { candidate.preferred_cities = next; },
        onChange: markDirty,
        maxItems: 20,
        emptyText: '未设置',
        placeholder: '搜索城市…',
        hint: '列表内城市同级加分。',
      }).node,
      h('div', { class: 'field' },
        h('span', { class: 'field-label', text: '届别' },),
        gradToggle.node,
        gradField.node),
      educationField(candidate),
      numField(candidate, 'cfg-max-exp', '经验年限参考上限（年）', ['max_experience_years'],
        { min: 0, max: 40, integer: true, fallback: CANDIDATE_DEFAULTS.max_experience_years }),
      numField(candidate, 'cfg-salary-min', '期望薪资下限（K）', ['salary_min_k'],
        { min: 0, max: 300, step: 0.5, fallback: CANDIDATE_DEFAULTS.salary_min_k }),
      numField(candidate, 'cfg-salary-max', '期望薪资上限（K）', ['salary_max_k'],
        { min: 0.5, max: 300, step: 0.5, fallback: CANDIDATE_DEFAULTS.salary_max_k })),
    h('div', { class: 'grid' },
      checkField({
        id: 'cfg-exclude-internships', label: '排除实习 / 兼职 / 日结岗位',
        checked: candidate.exclude_internships === true,
        onChange: (checked) => { candidate.exclude_internships = checked; markDirty(); },
      }).node,
      checkField({
        id: 'cfg-exclude-staffing', label: '排除外包 / 人力派遣信号',
        checked: candidate.exclude_staffing === true,
        onChange: (checked) => { candidate.exclude_staffing = checked; markDirty(); },
      }).node),
    h('p', { class: 'hint', text: '目标角色、技能关键词与各类排除词在「关键词」页统一维护。' }));
}

function readonlySection(config) {
  const platforms = strList(config.platforms).map((item) => PLATFORM_LABELS[item] || item);
  const autoChat = config.auto_chat && typeof config.auto_chat === 'object' ? config.auto_chat : null;
  return collapsiblePanel('只读信息', '这些值由本机配置文件管理，保存时原样保留。', false,
    kvList([
      ['岗位数据库', str(config.database_path) || '—'],
      ['原始数据目录', str(config.raw_dir) || '—'],
      ['导出目录', str(config.output_dir) || '—'],
      ['采集平台', platforms.length ? platforms.join('、') : '—'],
      ['自动开聊策略', autoChat ? (autoChat.enabled === true ? '开启' : '关闭') : '未配置'],
    ]),
    h('p', { class: 'hint', text: '数据库路径在服务启动时确定，需要修改请重启服务；自动开聊策略在本机配置文件中管理。' }));
}

// ---------------- 渲染 ----------------

export function renderConfig() {
  const root = document.getElementById('config-fields');
  if (!root) return;
  clear(root);

  if (!app.draft) {
    root.appendChild(app.stateError
      ? emptyBlock(`配置读取失败：${app.stateError}`)
      : loadingBlock('正在载入配置…'));
    updateSaveBar();
    return;
  }

  const config = app.draft;
  if (app.stateError) {
    root.appendChild(h('p', { class: 'callout callout-bad', role: 'alert', text: `状态读取失败：${app.stateError}` }));
  }

  root.appendChild(panel('生效时点', '配置保存后按下面的时机生效。', kvList(applyPolicyRows())));

  const network = networkSection(config);
  if (network) root.appendChild(network);
  root.appendChild(citySection(config));
  root.appendChild(planSection(config));
  root.appendChild(filtersSection(config));
  root.appendChild(budgetSection(config));
  root.appendChild(serviceSection(config));
  root.appendChild(refreshSection(config));
  root.appendChild(llmSection(config));
  root.appendChild(candidateSection(config));
  const browser = browserSection(config);
  if (browser) root.appendChild(browser);
  root.appendChild(readonlySection(config));

  // 提示：当前服务端认识的键集合，避免误以为缺字段是页面 bug。
  const missing = [];
  if (!configHas('service')) missing.push('service');
  if (!configHas('refresh')) missing.push('refresh');
  if (!configHas('llm')) missing.push('llm');
  if (missing.length) {
    root.appendChild(h('p', {
      class: 'callout callout-info', role: 'note',
      text: `运行中的服务端配置没有这些分组：${missing.join('、')}。保存时会按原值保留。`,
    }));
  }

  updateSaveBar();
}
