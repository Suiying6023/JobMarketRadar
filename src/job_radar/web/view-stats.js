/**
 * view-stats.js —— 岗位池总览（/api/overview）。
 *
 * 口径摆放：JD 覆盖单独成块；城市 / 行业 / 经验 / 学历 / 规模 / 融资 六张分布表
 * 只列岗位数量与占比，不混入 JD 覆盖列。
 */
import { API } from './api.js';
import {
  clear, fmtDecimal, fmtInt, fmtTime, h, num, ratioOf, ratioText, relTime, str, strList,
} from './core.js';
import { app, loadOptions } from './store.js';
import { barCell, collapsiblePanel, emptyBlock, errorBlock, kvList, limitNote, loadingBlock, panel, statCard, tableScaffold } from './ui.js';

const TABLE_ROW_LIMIT = 15;

const TOTALS = [
  ['jobs', '岗位记录', '本地库去重后的条数'],
  ['with_jd', '有 JD 正文', '详情正文已落库的条数'],
  ['open_snapshot', '快照未标关闭', '抓详情时平台未标关闭的条数'],
  ['closed', '快照已关闭', '抓详情时平台已标关闭或失效'],
  ['tagged', '已打标（模型标注）', '模型标注已落库的条数'],
  ['stale', '详情快照偏旧', '详情超过 7 天'],
  ['unknown_status', '状态未知', '没有 JD 正文，或正文缺时间戳'],
  ['duplicate_groups', '疑似重复组', '公司标识 + 规范化标题 + 城市相同'],
  ['companies', '公司（按名称去重）', '按公司名称去重后的家数'],
];

export function normalizeOverview(raw) {
  const data = raw && typeof raw === 'object' ? raw : {};
  const list = (value, mapper) => (Array.isArray(value) ? value.map(mapper) : []);
  const groups = (value) => list(value, (row) => ({
    name: str(row && row.name) || '未提供',
    jobs: num(row && row.jobs) ?? 0,
    jd: num(row && row.jd) ?? 0,
    salary_n: num(row && row.salary_n),
    median_salary_k: num(row && row.median_salary_k),
    companies: num(row && row.companies),
  }));
  const observation = data.observation_window && typeof data.observation_window === 'object' ? data.observation_window : {};
  return {
    scope: str(data.scope),
    as_of: str(data.as_of),
    window: {
      first_seen: str(observation.first_seen),
      last_seen: str(observation.last_seen),
      latest_detail_or_status: str(observation.latest_detail_or_status),
    },
    totals: data.totals && typeof data.totals === 'object' ? data.totals : {},
    cities: groups(data.cities),
    industries: groups(data.industries),
    experience: groups(data.experience),
    education: groups(data.education),
    company_sizes: groups(data.company_sizes),
    financing: groups(data.financing),
    skills: list(data.skills, (row) => ({
      name: str(row && row.name) || '未知技能',
      count: num(row && row.count) ?? 0,
      denominator: num(row && row.denominator),
      percent: num(row && row.percent),
    })),
    quality: list(data.quality, (row) => ({
      label: str(row && row.label) || '未命名指标',
      value: row ? row.value : null,
      detail: str(row && row.detail),
    })),
    candidate_funnel: list(data.candidate_funnel, (row) => ({
      label: str(row && row.label) || '未命名阶段',
      count: num(row && row.count) ?? 0,
    })),
    methodology: strList(data.methodology),
  };
}

export async function loadOverview(force = false) {
  if (app.overviewLoading) return;
  if (app.overview && !force) return;
  app.overviewLoading = true;
  renderStats();
  try {
    app.overview = normalizeOverview(await API.overview());
    app.overviewError = null;
  } catch (error) {
    app.overviewError = error.message;
  } finally {
    app.overviewLoading = false;
    renderStats();
  }
}

function distributionTable(rows, firstHeader, caption, total) {
  if (!rows.length) return emptyBlock('当前没有这一维度的统计。');
  const sorted = rows.slice().sort((a, b) => b.jobs - a.jobs);
  const bodyRows = sorted.slice(0, TABLE_ROW_LIMIT).map((row) => h('tr', {},
    h('td', { class: 'kw-name', text: row.name }),
    h('td', { class: 'num', text: fmtInt(row.jobs) }),
    h('td', { class: 'num', text: ratioText(row.jobs, total) }),
    h('td', {}, barCell('', ratioOf(row.jobs, total), 'sea')),
    h('td', { class: 'num', text: row.median_salary_k === null ? '—' : `${fmtDecimal(row.median_salary_k)}K` })));
  return h('div', {},
    tableScaffold([{ label: firstHeader }, { label: '岗位', num: true }, { label: '占比', num: true },
      { label: '占比图示' }, { label: '薪资中位数', num: true }], bodyRows, caption),
    limitNote(sorted.length, TABLE_ROW_LIMIT));
}

function jdCoverage(overview) {
  const totals = overview.totals;
  const jobs = num(totals.jobs) ?? 0;
  const withJd = num(totals.with_jd) ?? 0;
  const missing = Math.max(0, jobs - withJd);
  const ratio = ratioOf(withJd, jobs);
  const rows = [
    ['有 JD 正文', withJd, ratio],
    ['没有 JD 正文', missing, ratioOf(missing, jobs)],
  ];
  return panel('JD 覆盖', '正文落库比例，分母是本地岗位记录数。',
    h('div', { class: 'coverage' }, rows.map(([label, count, r]) => h('div', { class: 'coverage-row' },
      h('div', { class: 'coverage-top' },
        h('span', { text: label }),
        h('span', { class: 'val', text: `${fmtInt(count)} / ${fmtInt(jobs)} · ${ratioText(count, jobs)}` })),
      barCell('', r, label.startsWith('有') ? 'sea' : 'slate')))),
    kvList([
      ['JD 覆盖', ratioText(withJd, jobs)],
      ['已打标（模型标注）', fmtInt(totals.tagged)],
      ['详情快照偏旧（>7 天）', fmtInt(totals.stale)],
      ['状态未知（无正文或缺时间）', fmtInt(totals.unknown_status)],
    ]));
}

export function renderStats() {
  const root = document.getElementById('stats-body');
  if (!root) return;
  clear(root);

  if (!app.overview) {
    if (app.overviewError) {
      root.appendChild(errorBlock(`无法读取岗位池统计：${app.overviewError}`, () => { void loadOverview(true); }));
    } else {
      root.appendChild(loadingBlock('正在读取本地岗位库…'));
    }
    return;
  }

  const overview = app.overview;
  const totals = overview.totals;
  const jobs = num(totals.jobs) ?? 0;

  const windowBits = [];
  if (overview.window.first_seen) windowBits.push(`最早采集 ${fmtTime(overview.window.first_seen)}`);
  if (overview.window.last_seen) windowBits.push(`最近采集 ${fmtTime(overview.window.last_seen)}`);
  if (overview.window.latest_detail_or_status) windowBits.push(`最近详情/状态 ${fmtTime(overview.window.latest_detail_or_status)}`);

  root.appendChild(h('div', { class: 'meta-line' },
    h('span', {}, '统计范围：', h('b', { text: overview.scope || '未提供' })),
    h('span', {}, '数据截至：', h('b', { text: fmtTime(overview.as_of) }), overview.as_of ? `（${relTime(overview.as_of)}）` : ''),
    h('span', {}, '配置修订：', h('b', { text: app.state ? String(app.state.revision || '').slice(0, 8) : '—' })),
    windowBits.length ? h('span', { text: windowBits.join(' · ') }) : null,
    h('button', {
      class: 'btn btn-sm', type: 'button', text: '刷新统计',
      onclick: () => { void loadOverview(true); },
    })));

  root.appendChild(h('ul', { class: 'stat-grid' }, TOTALS.map(([key, label, note]) => {
    const value = num(totals[key]);
    if (value === null) return null;
    if (key === 'companies' || key === 'duplicate_groups') return statCard(label, fmtInt(value), note);
    return statCard(label, fmtInt(value), note);
  }).filter(Boolean)));

  const median = num(totals.median_salary_k);
  if (median !== null) {
    root.appendChild(h('ul', { class: 'stat-grid stat-grid-slim' },
      statCard('薪资中位数', `${fmtDecimal(median)}K`, '挂牌月薪区间中点的中位数'),
      statCard('薪资样本数', fmtInt(totals.salary_n), '有可比较月薪区间的岗位数'),
      statCard('重复多余记录', fmtInt(totals.duplicate_excess), '疑似重复组多出的记录数'),
      statCard('历史 active 分层', fmtInt(totals.active_legacy), '沿用历史保留分层的条数')));
  }

  root.appendChild(jdCoverage(overview));

  root.appendChild(panel('城市分布', '按岗位所在城市；占比分母是全库岗位记录数。',
    distributionTable(overview.cities, '城市', '城市分布的岗位数与占比', jobs)));

  root.appendChild(panel('行业分布', '行业来自平台给公司打的标签；缺失值记作「未提供」。',
    distributionTable(overview.industries, '行业', '行业分布的岗位数与占比', jobs)));

  root.appendChild(panel('经验与学历', '分母是全库岗位记录数。',
    distributionTable(overview.experience, '经验要求', '经验要求分布', jobs),
    h('div', { class: 'panel-sub' }, distributionTable(overview.education, '学历要求', '学历要求分布', jobs))));

  // 两个维度都为空时隐藏整块，避免只显示两行「没有统计」。
  if (overview.company_sizes.length || overview.financing.length) {
    root.appendChild(collapsiblePanel('公司规模与融资阶段', '来自平台的公司标签，分母是全库。', false,
      distributionTable(overview.company_sizes, '公司规模', '公司规模分布', jobs),
      h('div', { class: 'panel-sub' }, distributionTable(overview.financing, '融资阶段', '融资阶段分布', jobs))));
  }

  const skills = overview.skills.slice().sort((a, b) => b.count - a.count);
  const skillRows = skills.slice(0, TABLE_ROW_LIMIT).map((row) => {
    const denominator = num(row.denominator);
    const percent = num(row.percent);
    const ratio = denominator && denominator > 0 ? row.count / denominator
      : (percent === null ? null : (percent <= 1 ? percent : percent / 100));
    return h('tr', {},
      h('td', { class: 'kw-name', text: row.name }),
      h('td', { class: 'num', text: fmtInt(row.count) }),
      h('td', { class: 'num', text: denominator === null ? '—' : fmtInt(denominator) }),
      h('td', {}, barCell(ratio === null ? '—' : `${fmtDecimal(ratio * 100)}%`, ratio, 'teal')));
  });
  root.appendChild(panel('技能命中（基于 JD 正文）', '分母是有 JD 正文的岗位数；同一条正文可命中多个技能，占比为命中次数 ÷ 分母。',
    skills.length
      ? h('div', {}, tableScaffold([{ label: '技能' }, { label: '命中', num: true },
        { label: '分母', num: true }, { label: '占比' }], skillRows, '技能在本库 JD 正文中的命中数与占比'),
        limitNote(skills.length, TABLE_ROW_LIMIT))
      : emptyBlock('本地库没有技能统计。')));

  // 分桶数据来自 /api/overview 的 candidate_funnel；没有数据时整块隐藏，不留空卡。
  const buckets = overview.candidate_funnel;
  if (buckets.length) {
    const bucketMax = buckets.reduce((max, row) => Math.max(max, num(row.count) ?? 0), 0);
    root.appendChild(panel('候选画像复核分桶', '每条岗位只落进一个桶；用于排序复核顺序。',
      h('div', { class: 'funnel' }, buckets.map((row) => h('div', { class: 'funnel-row' },
        h('div', { class: 'funnel-top' },
          h('span', { text: row.label }),
          h('span', { class: 'val', text: `${fmtInt(row.count)} · 占全库 ${ratioText(row.count, jobs)}` })),
        barCell('', bucketMax > 0 ? (num(row.count) ?? 0) / bucketMax : null, 'teal'))))));
  }

  const qualityRows = overview.quality.map((row) => h('div', { class: 'quality-row' },
    h('span', { class: 'q-label', text: row.label }),
    h('span', { class: 'q-value', text: row.value === null || row.value === undefined ? '—' : String(row.value) }),
    h('span', { class: 'q-detail', text: row.detail || '—' })));
  root.appendChild(collapsiblePanel('数据质量项', '本地库自检结果。', false,
    qualityRows.length ? h('div', { class: 'quality-list' }, qualityRows) : emptyBlock('当前没有质量检查项。')));

  if (overview.methodology.length) {
    root.appendChild(collapsiblePanel('统计口径', '这些数字的计算方式。每条一句。', false,
      h('ul', { class: 'detail-list' }, overview.methodology.map((text) => h('li', { text })))));
  }

  void loadOptions(false);
}
