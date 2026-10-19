/**
 * store.js —— 页面状态、配置草稿与状态轮询。
 *
 * 关键约束：
 *  - 配置草稿（app.draft）只在「载入 / 保存成功 / 主动放弃」时被替换。后台轮询在
 *    app.dirty 为真时不碰草稿，只记录远端修订已变化（staleRemote），因此不会覆盖未保存编辑。
 *  - 保存只提交 draft + revision（+ clear_secrets）；空密钥字段原样留空 = 保留服务端已有值。
 */
import { API, ApiError, setCsrfToken } from './api.js';
import { bus, deepClone, h, num, setText, str, termKey, valueText } from './core.js';
import { fillMessageList, getSaveBar, toast } from './ui.js';
import { reconcileConfig, draftPayload, pollStillCurrent } from './config-state.js';

export const SECRET_PATHS = ['llm.api_key', 'browser.proxy_password', 'refresh.proxy_password'];
export const BUCKETS = ['优先核实', '需核实门槛', '信息不足', '暂不优先'];
export const TASK_KINDS = [
  { value: 'collect', label: '采集岗位列表' },
  { value: 'details', label: '补全岗位详情' },
  { value: 'refresh', label: '刷新岗位状态' },
  { value: 'tag', label: 'LLM 打标' },
];

/** SearchFilters 的字段名与界面标签（顺序即渲染顺序）。 */
export const FILTER_KEYS = ['salary', 'experience', 'degree', 'scale', 'stage', 'jobType', 'industry'];
export const FILTER_LABELS = {
  salary: '薪资',
  experience: '经验',
  degree: '学历',
  scale: '公司规模',
  stage: '融资阶段',
  jobType: '求职类型',
  industry: '行业',
};

/** 通用 CandidateProfile 默认值：必须与 candidate.py 的字段默认一致。
 *  新项目还没有画像时不要套用旧的「本科 / 0 年 / 排除实习」模板 —— 那会把
 *  个人求职假设写进通用配置；这里的默认表示「不做门槛」。 */
export const CANDIDATE_DEFAULTS = Object.freeze({
  cities: [], preferred_cities: [], graduation_year: null,
  education: '不限', max_experience_years: 40,
  salary_min_k: 0, salary_max_k: 300,
  roles: [], skills: [], exclude_internships: false, exclude_staffing: false,
  excluded_companies: [], excluded_titles: [],
});

export function candidateDefaults() {
  return deepClone(CANDIDATE_DEFAULTS);
}

export const app = {
  state: null,
  stateError: null,
  /** 配置草稿 */
  draft: null,
  dirty: false,
  conflict: false,
  saving: false,
  /** 本页有未保存修改时，远端修订发生了变化 */
  staleRemote: false,
  /** 用户勾选要清空的密钥路径 */
  clearSecrets: new Set(),
  saveProblems: [],
  saveWarnings: [],
  options: null,
  optionsError: null,
  optionsLoading: false,
  overview: null,
  overviewError: null,
  overviewLoading: false,
  view: 'stats',
  jobs: {
    q: '', city: '', tier: '', hiring: '', page: 1, page_size: 30,
    data: null, loading: false, error: null,
    cursor: -1, selectedId: null, detail: null,
    detailLoading: false, detailError: null,
  },
  tasks: {
    data: null, error: null, loading: false,
    selectedId: null, detail: null, detailError: null, detailLoading: false,
    events: [], nextCursor: 0, logPaused: false, logError: null,
  },
};

export function isNarrow() {
  return window.matchMedia('(max-width: 1119px)').matches;
}

// ============================== 归一化 ==============================

export function normalizeState(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const safety = data.safety && typeof data.safety === 'object' ? data.safety : {};
  const secrets = data.secrets && typeof data.secrets === 'object' ? data.secrets : {};
  return {
    config: data.config && typeof data.config === 'object' ? data.config : {},
    revision: data.revision === undefined ? null : data.revision,
    csrf_token: str(data.csrf_token),
    refresh_safety: data.refresh_safety && typeof data.refresh_safety === 'object' ? data.refresh_safety : null,
    safety: {
      mode: str(safety.mode),
      reason: str(safety.reason),
      resume_after: safety.resume_after === undefined || safety.resume_after === null ? '' : str(safety.resume_after),
      manual_release_required: safety.manual_release_required === true,
      updated_at: str(safety.updated_at),
      source: str(safety.source),
      corrupt: safety.corrupt === true,
    },
    secrets: Object.fromEntries(SECRET_PATHS.map((path) => [path, secrets[path] === true])),
    apply_policy: data.apply_policy && typeof data.apply_policy === 'object' ? data.apply_policy : null,
    version: str(data.version),
  };
}

export function normalizeOptions(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const optionList = (value) => (Array.isArray(value) ? value.map((item) => {
    if (item && typeof item === 'object') {
      const value_ = item.value === undefined || item.value === null ? '' : String(item.value);
      return { value: value_, label: str(item.label) || value_ || '（空）' };
    }
    return { value: str(item), label: str(item) };
  }).filter((item) => item.value !== '') : []);
  const filters = {};
  if (data.filters && typeof data.filters === 'object') {
    for (const key of Object.keys(data.filters)) filters[key] = optionList(data.filters[key]);
  }
  return {
    cities: Array.isArray(data.cities) ? data.cities.map((item) => ({
      name: str(item && item.name),
      boss_code: item && item.boss_code !== undefined && item.boss_code !== null ? String(item.boss_code) : '',
    })).filter((item) => item.name) : [],
    filters,
    task_kinds: optionList(data.task_kinds).length ? optionList(data.task_kinds) : TASK_KINDS,
    apply_policy: data.apply_policy && typeof data.apply_policy === 'object' ? data.apply_policy : null,
  };
}

// ============================== 状态读取 ==============================

/**
 * 读取 /api/state。
 * @param {{preserveDraft?:boolean}} options preserveDraft=true 时不覆盖未保存的配置草稿。
 */
export async function loadState(options = {}) {
  try {
    const payload = await API.state();
    const next = normalizeState(payload);
    applyState(next, { preserveDraft: options.preserveDraft === true });
    app.stateError = null;
    return true;
  } catch (error) {
    app.stateError = error.message;
    return false;
  } finally {
    bus.emit('state');
    bus.emit('status');
  }
}

function applyState(next, { preserveDraft }) {
  setCsrfToken(next.csrf_token);
  if (reconcileConfig(app, next, preserveDraft)) bus.emit('draft-replaced');
}

export async function reloadStateWithConfirm() {
  if (app.dirty && !window.confirm('重新载入会丢弃未保存的配置修改，继续？')) return;
  await loadState();
  bus.emit('draft-replaced');
  if (app.state) toast('已重新载入本地状态。', 'ok');
}

export async function loadOptions(force = false) {
  if (app.optionsLoading) return;
  if (app.options && !force) return;
  app.optionsLoading = true;
  try {
    app.options = normalizeOptions(await API.options());
    app.optionsError = null;
  } catch (error) {
    app.optionsError = error.message;
  } finally {
    app.optionsLoading = false;
    bus.emit('options');
  }
}

/** 后台轮询：只更新安全状态与（无未保存修改时的）配置。 */
export async function pollState() {
  if (app.saving || document.hidden) return;
  const epoch = app.draftEpoch;
  try {
    const next = normalizeState(await API.state());
    if (!pollStillCurrent(app, epoch)) return;
    applyState(next, { preserveDraft: true });
    app.stateError = null;
  } catch (error) {
    app.stateError = error.message;
  } finally {
    bus.emit('state');
    bus.emit('status');
  }
}

let pollTimer = null;

export function startStatePolling(intervalMs = 5000) {
  if (pollTimer) return;
  pollTimer = window.setInterval(() => { void pollState(); }, intervalMs);
}

// ============================== 配置草稿 ==============================

export function markDirty() {
  const wasDirty = app.dirty;
  app.dirty = true;
  if (!wasDirty && app.conflict) app.conflict = false;
  const { problems, warnings } = validateConfig();
  app.saveWarnings = warnings;
  if (app.saveProblems.length) app.saveProblems = problems;
  updateSaveBar();
  bus.emit('status');
}

/** 生效时点：state.apply_policy 优先，其次用内置映射。
 *  后端 APPLY_POLICY 的键是配置字段名（如 service.batch_size），这里给出中文标签。 */
export const EFFECT_LABELS = {
  llm: 'LLM 端点、模型、密钥与 prompt',
  'service.batch_size': '批大小',
  request_delay_seconds: '列表请求间隔',
  detail_delay_seconds: '详情请求间隔',
  network_enabled: '在线访问总开关',
  browser: '浏览器与连接',
  refresh: '刷新与出口',
  keywords: '搜索词',
    cities: '城市',
    list_source: '列表采集方式',
  filters: '筛选组合',
  candidate: '候选人画像',
  database_path: '数据库路径',
};

export const EFFECT_ORDER = [
  'network_enabled', 'browser', 'refresh', 'llm', 'service.batch_size',
    'request_delay_seconds', 'detail_delay_seconds', 'cities', 'keywords', 'filters', 'list_source',
  'candidate', 'database_path',
];

export const EFFECT_FALLBACK = {
  network_enabled: '下一次请求前检查',
  browser: '新任务生效',
  refresh: '新任务生效',
  llm: '下一批生效',
  'service.batch_size': '下一批生效',
  request_delay_seconds: '下一批生效',
  detail_delay_seconds: '下一批生效',
  cities: '下一次采集任务生效',
  keywords: '下一次采集任务生效',
  filters: '下一次采集任务生效',
  candidate: '保存后更新本地复核；采集选行在新任务生效',
  database_path: '运行中固定，需在本机配置文件中修改后重启服务',
};

export function effectText(key) {
  const policy = (app.state && app.state.apply_policy) || (app.options && app.options.apply_policy) || null;
  if (policy && key in policy) return valueText(policy[key]);
  return EFFECT_FALLBACK[key] || '';
}

export function applyPolicyRows() {
  const policy = (app.state && app.state.apply_policy) || (app.options && app.options.apply_policy) || null;
  if (!policy || !Object.keys(policy).length) {
    return EFFECT_ORDER.map((key) => [EFFECT_LABELS[key] || key, EFFECT_FALLBACK[key]]);
  }
  const keys = Object.keys(policy).sort((a, b) => {
    const ia = EFFECT_ORDER.indexOf(a);
    const ib = EFFECT_ORDER.indexOf(b);
    return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib);
  });
  return keys.map((key) => [EFFECT_LABELS[key] || key, valueText(policy[key])]);
}

/** 校验配置草稿：返回 {problems, warnings}。problems 阻断保存。 */
export function validateConfig() {
  const problems = [];
  const warnings = [];
  const config = app.draft;
  if (!config) return { problems: [{ message: '配置尚未载入。' }], warnings };

  const cities = Array.isArray(config.cities) ? config.cities : [];
  if (!cities.length) problems.push({ message: '采集城市：至少需要一个城市。', focus: 'cfg-city-select' });
  if (cities.length > 500) problems.push({ message: '采集城市：最多 500 个。', focus: 'cfg-city-select' });
  const seenCities = new Set();
  cities.forEach((city, index) => {
    const name = str(city && city.name).trim();
    const key = termKey(name);
    if (!name) problems.push({ message: `采集城市：第 ${index + 1} 行为空名。` });
    else if (seenCities.has(key)) problems.push({ message: `采集城市：「${name}」重复。` });
    if (name) seenCities.add(key);
    const code = city && city.boss_code;
    if (code !== undefined && code !== null && String(code).trim() !== ''
      && !/^\d{4,12}$/.test(String(code).trim())) {
      problems.push({ message: `采集城市：「${name || `第 ${index + 1} 行`}」的城市码应为 4–12 位数字，留空则按城市名自动补全。` });
    }
  });

  const listChecks = [
    ['keywords', '搜索词', 5000],
    ['include_title_keywords', '标题必含词', 500],
    ['exclude_title_keywords', '标题排除词', 500],
    ['exclude_company_keywords', '公司排除词', 500],
  ];
  for (const [key, label, max] of listChecks) {
    const values = Array.isArray(config[key]) ? config[key] : [];
    if (values.length > max) problems.push({ message: `${label}：最多 ${max} 条（当前 ${values.length}）。` });
    const seen = new Set();
    const duplicates = new Set();
    for (const item of values) {
      const text = str(item).trim();
      if (!text) problems.push({ message: `${label}：存在空词条，请删除或补全。` });
      else if (text.length > 100) problems.push({ message: `${label}：词条「${text.slice(0, 20)}…」超过 100 字。` });
      const itemKey = termKey(text);
      if (seen.has(itemKey)) duplicates.add(text); else seen.add(itemKey);
    }
    if (duplicates.size) problems.push({ message: `${label}：存在重复项（${[...duplicates].slice(0, 5).join('、')}）。` });
  }
  if (!(Array.isArray(config.keywords) ? config.keywords.length : 0)) {
    warnings.push('搜索词为空：下次采集需要至少一个搜索词。');
  }

  const filters = Array.isArray(config.filters) ? config.filters : [];
  if (!filters.length) problems.push({ message: '筛选组合：至少保留一组。' });
  const unknownFilterValues = [];
  filters.forEach((group, index) => {
    if (!group || typeof group !== 'object') {
      problems.push({ message: `筛选组合：第 ${index + 1} 组结构无效。` });
      return;
    }
    for (const key of Object.keys(group)) {
      if (!FILTER_KEYS.includes(key)) {
        problems.push({ message: `筛选组合：第 ${index + 1} 组含未识别字段 ${key}。` });
        continue;
      }
      const value = group[key];
      const text = value === null || value === undefined ? '' : String(value);
      if (text === '') continue;
      const options = (app.options && app.options.filters && app.options.filters[key]) || null;
      if (!options || !options.length) continue;
      // SearchFilters 里写的是中文标签，/api/options 的 value 是平台接口码：两种都算已识别。
      for (const part of text.split(',').map((item) => item.trim()).filter(Boolean)) {
        const known = options.some((option) => option.value === part || option.label === part);
        if (!known) unknownFilterValues.push(`${FILTER_LABELS[key] || key}=「${part}」`);
      }
    }
  });
  if (unknownFilterValues.length) {
    warnings.push(`筛选组合里有 ${unknownFilterValues.length} 个值未收录在本地码表`
      + `（${unknownFilterValues.slice(0, 3).join('、')}），将按原值提交。`);
  }

  const overrides = config.pages_per_keyword && typeof config.pages_per_keyword === 'object' ? config.pages_per_keyword : {};
  const keywords = Array.isArray(config.keywords) ? config.keywords : [];
  const keywordKeys = new Set(keywords.map((item) => termKey(item)));
  for (const key of Object.keys(overrides)) {
    if (!keywordKeys.has(termKey(key))) {
      delete overrides[key];
      warnings.push(`已移除多余的每词页深：${key}（该词已从搜索词里删掉）。`);
      continue;
    }
    const value = num(overrides[key]);
    if (value === null || !Number.isInteger(value) || value < 1 || value > 30) {
      problems.push({ message: `每词页深「${key}」：需为 1–30 的整数。`, focus: 'terms-depth' });
    }
  }

  const delayPairs = [
    ['request_delay_seconds', '列表请求间隔', 'cfg-req-min', 'cfg-req-max'],
    ['detail_delay_seconds', '详情请求间隔', 'cfg-detail-min', 'cfg-detail-max'],
    ['detail_cooldown_seconds', '详情批间冷却', 'cfg-cooldown-min', 'cfg-cooldown-max'],
    ['burst_penalty_seconds', '突发冷却', 'cfg-burst-min', 'cfg-burst-max'],
  ];
  for (const [key, label, minId, maxId] of delayPairs) {
    const values = Array.isArray(config[key]) ? config[key] : [];
    const min = num(values[0]);
    const max = num(values[1]);
    if (min === null || max === null) continue;
    if (max < min) problems.push({ message: `${label}：上限需不小于下限。`, focus: maxId });
  }

  const candidate = config.candidate && typeof config.candidate === 'object' ? config.candidate : null;
  if (!candidate) {
    problems.push({ message: '候选人画像：缺少 candidate 对象。', focus: 'cfg-education' });
  } else {
    const salaryMin = num(candidate.salary_min_k);
    const salaryMax = num(candidate.salary_max_k);
    if (salaryMin !== null && salaryMax !== null && salaryMax < salaryMin) {
      problems.push({ message: '候选人画像：期望薪资上限需不小于下限。', focus: 'cfg-salary-max' });
    }
    if (!str(candidate.education).trim()) problems.push({ message: '候选人画像：学历要求为必填项。', focus: 'cfg-education' });
    for (const [key, label, max] of [['roles', '目标角色', 60], ['skills', '技能关键词', 100],
      ['excluded_companies', '公司黑名单', 100], ['excluded_titles', '岗位排除词', 100],
      ['cities', '目标城市', 200], ['preferred_cities', '优先城市', 20]]) {
      const values = Array.isArray(candidate[key]) ? candidate[key] : [];
      if (values.length > max) problems.push({ message: `候选人画像 · ${label}：最多 ${max} 条。` });
    }
  }

  const llm = config.llm && typeof config.llm === 'object' ? config.llm : null;
  if (llm) {
    const baseUrl = str(llm.base_url).trim();
    if (!/^https?:\/\/[^\s]+$/i.test(baseUrl)) problems.push({ message: 'LLM 端点：需为 http(s) 地址。', focus: 'cfg-llm-base-url' });
    if (llm.enabled === true && !str(llm.model).trim()) {
      warnings.push('LLM 已启用但模型名为空：打标任务可能无法执行。');
    }
  }

  const refresh = config.refresh && typeof config.refresh === 'object' ? config.refresh : null;
  if (refresh && str(refresh.route) === 'proxy' && !str(refresh.proxy_server).trim()) {
    problems.push({ message: '刷新出口：选择代理路线时必须填写代理地址。', focus: 'cfg-refresh-proxy' });
  }

  return { problems, warnings };
}

// ============================== 保存 / 放弃 ==============================

function buildPayload() {
  return draftPayload(app);
}

export function secretsCleared(path) {
  return app.clearSecrets.has(path);
}

export function toggleClearSecret(path, cleared) {
  if (cleared) app.clearSecrets.add(path);
  else app.clearSecrets.delete(path);
  markDirty();
}

export async function saveConfig(button) {
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

  app.saving = true;
  if (button) button.disabled = true;
  updateSaveBar();
  bus.emit('status');
  try {
    const payload = await API.saveConfig(buildPayload());
    const next = normalizeState(payload);
    applyState(next, {preserveDraft: false});
    app.stateError = null;
    bus.emit('draft-replaced');
    bus.emit('state');
    toast('配置已保存。', 'ok');
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
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
  } finally {
    app.saving = false;
    if (button && button.isConnected) button.disabled = false;
    updateSaveBar();
    bus.emit('status');
  }
}

export function discardDraft() {
  if (!app.state) return;
  if (app.dirty && !window.confirm('放弃未保存的配置修改？')) return;
  reconcileConfig(app, app.state, false);
  bus.emit('draft-replaced');
  bus.emit('status');
  toast('已放弃未保存的修改。');
}

export function focusFirstProblem() {
  const withFocus = app.saveProblems.find((problem) => problem && problem.focus);
  const target = withFocus ? document.getElementById(withFocus.focus) : null;
  const first = target || document.querySelector('.view:not([hidden]) [aria-invalid="true"]');
  if (first) {
    first.focus();
    if (typeof first.scrollIntoView === 'function') first.scrollIntoView({ block: 'center', behavior: 'auto' });
  }
}

export function updateSaveBar() {
  for (const id of ['config-savebar', 'terms-savebar']) {
    const root = document.getElementById(id);
    if (!root) continue;
    const refs = getSaveBar(root);
    if (!refs.wired) {
      refs.saveButton.addEventListener('click', (event) => { void saveConfig(event.currentTarget); });
      refs.discardButton.addEventListener('click', () => discardDraft());
      refs.reloadButton.addEventListener('click', () => { void reloadStateWithConfirm(); });
      refs.wired = true;
    }
    const loaded = Boolean(app.state && app.draft);
    const dirty = loaded && app.dirty;
    if (!loaded) {
      setText(refs.status, app.stateError ? `配置读取失败：${app.stateError}` : '配置尚未载入。');
    } else if (app.conflict) {
      refs.status.textContent = '';
      refs.status.appendChild(h('b', { text: '配置在别处已被修改（409），本页改动没有保存。' }));
    } else if (dirty) {
      refs.status.textContent = '';
      refs.status.appendChild(h('b', { text: '有未保存的修改' }));
      refs.status.appendChild(document.createTextNode('（保存只写本地配置文件）'));
      if (app.staleRemote) {
        refs.status.appendChild(h('span', {
          class: 'savebar-flag',
          text: ' · 远端配置同时有更新：保存会返回 409，可先重新载入',
        }));
      }
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

/** 判断服务端 config 是否认识某个键（extra="forbid" 的模型不能凭空加字段）。 */
export function configHas(path) {
  if (!app.draft) return false;
  let node = app.draft;
  for (const key of path.split('.')) {
    if (node === null || typeof node !== 'object' || !(key in node)) return false;
    node = node[key];
  }
  return true;
}
