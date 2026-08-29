"""投递池单页模板：CSS + HTML 骨架 + 前端逻辑。

单独成文件是因为它有 400+ 行且几乎全是字符串常量，混在导出脚本里会
把真正的数据逻辑埋掉。`export_pool.py` 只负责取数与分桶。

设计取向（都是针对旧七文件方案的具体问题）：
  · 一份数据、一个页面 —— 桶是筛选条件，不是文件边界
  · 门槛信息（经验年限/学历/名校/工作制）直接铺在卡片上，
    因为那是决定"投不投"的东西，旧版把它折叠在展开区里
  · 每条都显示"最后核实距今几天"，且过期的活跃度标记为不可信
  · 键盘可用：/ 聚焦搜索、j/k 移动、Enter 打开岗位、a 标记已投
"""

CSS = """
:root{
  --bg:#0f1115; --panel:#171a21; --panel2:#1d212a; --line:#272c37;
  --fg:#e6e8ee; --dim:#9aa3b2; --dim2:#6b7280;
  --accent:#4f9cf9; --ok:#34d399; --warn:#fbbf24; --bad:#f87171;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--fg);
  font:14px/1.55 -apple-system,"Segoe UI",system-ui,"Microsoft YaHei",sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* ---- 顶栏：始终可见，因为筛选状态必须随时可查 ---- */
header{position:sticky;top:0;z-index:20;background:rgba(15,17,21,.96);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:10px 16px 8px}
.hrow{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{margin:0;font-size:16px;font-weight:600;letter-spacing:.2px}
.meta{color:var(--dim);font-size:12px}
.meta b{color:var(--fg);font-weight:600}
.grow{flex:1}
input[type=search],select{background:var(--panel2);color:var(--fg);
  border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:13px;outline:none}
input[type=search]{min-width:240px}
input[type=search]:focus,select:focus{border-color:var(--accent)}
kbd{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
  padding:1px 5px;font-size:11px;color:var(--dim);font-family:ui-monospace,monospace}

/* ---- 视图切换：原先是七个文件，现在是七个按钮 ---- */
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.tab{background:var(--panel);border:1px solid var(--line);color:var(--dim);
  border-radius:999px;padding:4px 12px;font-size:12.5px;cursor:pointer;
  display:flex;gap:6px;align-items:center;transition:.12s}
.tab:hover{border-color:var(--accent);color:var(--fg)}
.tab.on{background:var(--accent);border-color:var(--accent);color:#0b1220;font-weight:600}
.tab .n{opacity:.75;font-variant-numeric:tabular-nums}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px;align-items:center}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:6px;
  padding:3px 9px;font-size:12px;color:var(--dim);cursor:pointer}
.chip.on{border-color:var(--ok);color:var(--ok)}
.chip.off{opacity:.5;text-decoration:line-through}

/* ---- 卡片列表 ---- */
main{padding:12px 16px 60px;max-width:1180px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:11px 13px;margin-bottom:9px;transition:.12s}
.card:hover{border-color:#3b4252}
.card.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.card.done{opacity:.45}
.r1{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.sc{font-variant-numeric:tabular-nums;font-weight:700;color:var(--accent);
  min-width:34px;font-size:15px}
.ti{font-size:15px;font-weight:600}
.co{color:var(--dim)}
.sa{color:var(--ok);font-weight:600;font-variant-numeric:tabular-nums}
.r2{margin-top:5px;display:flex;gap:7px;flex-wrap:wrap;align-items:center;
  color:var(--dim);font-size:12.5px}
.tag{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
  padding:1px 7px;font-size:11.5px;color:var(--dim)}
.tag.g{color:var(--warn);border-color:#4a3f22}      /* 门槛：要注意的 */
.tag.t{color:#7dd3fc;border-color:#1e3a4a}          /* 技术栈 */
.tag.r{color:var(--bad);border-color:#4a2626}       /* 风险 */
.dot{width:6px;height:6px;border-radius:50%;display:inline-block}
.age{font-variant-numeric:tabular-nums}
.age.w{color:var(--warn)} .age.b{color:var(--bad)}
.stale{text-decoration:line-through;opacity:.6}     /* 过期的活跃度 */
.why{margin-top:6px;color:var(--dim2);font-size:12px;border-left:2px solid var(--line);
  padding-left:8px;display:none}
.card.open .why{display:block}
.jd{margin-top:8px;background:var(--panel2);border-radius:6px;padding:9px 11px;
  white-space:pre-wrap;font-size:12.5px;color:#c3c9d4;max-height:420px;
  overflow:auto;display:none}
.card.open .jd{display:block}
.acts{margin-left:auto;display:flex;gap:6px}
.btn{background:var(--panel2);border:1px solid var(--line);color:var(--dim);
  border-radius:5px;padding:2px 9px;font-size:12px;cursor:pointer}
.btn:hover{color:var(--fg);border-color:var(--accent)}
.empty{color:var(--dim);text-align:center;padding:48px 0}
footer{position:fixed;bottom:0;left:0;right:0;background:rgba(15,17,21,.96);
  border-top:1px solid var(--line);padding:5px 16px;font-size:11.5px;color:var(--dim2);
  display:flex;gap:14px;flex-wrap:wrap}
"""

BODY = """
<header>
  <div class="hrow">
    <h1>投递池</h1>
    <span class="meta" id="sum"></span>
    <span class="grow"></span>
    <input type="search" id="q" placeholder="搜索岗位 / 公司 / 技术栈 / JD 正文…">
    <select id="sort">
      <option value="score">综合分</option>
      <option value="mid">薪资</option>
      <option value="age">最后核实</option>
      <option value="actDays">活跃度</option>
      <option value="co">公司</option>
    </select>
  </div>
  <div class="tabs" id="tabs"></div>
  <div class="chips" id="chips"></div>
</header>
<main><div id="list"></div></main>
<footer>
  <span><kbd>/</kbd> 搜索</span><span><kbd>j</kbd>/<kbd>k</kbd> 移动</span>
  <span><kbd>Enter</kbd> 打开岗位</span><span><kbd>Space</kbd> 展开 JD</span>
  <span><kbd>a</kbd> 标记已投</span><span><kbd>Esc</kbd> 清搜索</span>
  <span class="grow"></span><span id="thr"></span>
</footer>
"""

JS = r"""
// 视图 = 原先的七个文件。定义放在一处，标签和筛选都从它派生。
const VIEWS = [
  {k:'p0',   n:'P0 优先',  f:r=>r.bucket==='core'&&r.tier==='p0'},
  {k:'p1',   n:'P1 待确认',f:r=>r.bucket==='core'&&r.tier==='p1'},
  {k:'p2',   n:'P2 冷池',  f:r=>r.bucket==='core'&&r.tier==='p2'},
  {k:'high', n:'高薪',     f:r=>r.bucket==='high'},
  {k:'low',  n:'低薪',     f:r=>r.bucket==='low'},
  {k:'over', n:'超纲留档', f:r=>r.bucket==='over'},
  {k:'risk', n:'风险公司', f:r=>r.risk.length>0},
  {k:'all',  n:'全部',     f:_=>true},
];
let view='p0', sortKey='score', dir=-1, sel=-1;
// 排除项默认开着（=过滤掉）。风险公司在主视图里藏起来，但一键可见。
const EX = {risk:true, applied:false, stale:false};
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// 已投状态：serve_pool.py 在跑时写数据库（POST /apply，同时打 bookmarked
// 让它豁免自动降级）；直接双击打开 file:// 时退化成 localStorage。
// 两条路都要有 —— 页面既要能当静态文件用，跑服务时又不该丢状态。
const KEY='pool.applied.v1';
const applied=new Set(JSON.parse(localStorage.getItem(KEY)||'[]'));
const LIVE=location.protocol==='http:'||location.protocol==='https:';
const isApplied=r=>applied.has(r.id)||!!r.applied;
async function markApplied(r,on){
  if(applied.has(r.id)) applied.delete(r.id); else applied.add(r.id);
  localStorage.setItem(KEY,JSON.stringify([...applied]));
  if(!LIVE) return;                       // file:// 下没有接口可调
  try{
    const res=await fetch('/apply',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:r.id,message:on?'':'撤销'})});
    const j=await res.json();
    // 接口失败要说出来。静默失败的后果是用户以为已入库，
    // 下次重导出发现标记全没了，却无从判断是哪一步丢的。
    if(!j.ok) toast('写库失败: '+(j.err||'未知'),true);
  }catch(e){ toast('写库失败（服务未运行？）本次仅存本地',true); }
}
function toast(msg,bad){
  let el=document.getElementById('toast');
  if(!el){ el=document.createElement('div'); el.id='toast'; document.body.appendChild(el); }
  el.textContent=msg;
  el.style.cssText='position:fixed;left:50%;bottom:34px;transform:translateX(-50%);'+
    'background:'+(bad?'#7f1d1d':'#14532d')+';color:#fff;padding:7px 14px;'+
    'border-radius:6px;font-size:13px;z-index:99;box-shadow:0 4px 14px rgba(0,0,0,.4)';
  clearTimeout(el._t); el._t=setTimeout(()=>el.remove(),3200);
}

function ageCls(a){ if(a==null) return 'b'; if(a<=T.staleFresh) return ''; return a>=T.staleFull?'b':'w'; }
function ageTxt(a){ return a==null?'未核实':(a===0?'今天':a+'天前'); }

// 排除项。`vk` 是目标视图 —— 必须传，因为「风险公司」和「全部」视图
// 本身就是要看风险的，在那里应用 EX.risk 会让视图永远为空。
// ⚠️ 标签计数也必须按各自的视图算（而不是按当前视图），否则
// 「风险公司」标签会显示 0（它数的是已经把风险过滤掉的集合），
// 而且「全部」的计数会和切过去后实际看到的条数不一致。
function pool(vk){
  const seesRisk = (vk==='risk' || vk==='all');
  return DATA.filter(r=>{
    if(EX.risk && r.risk.length && !seesRisk) return false;
    if(EX.applied && isApplied(r)) return false;
    if(EX.stale && r.age!=null && r.age>=T.staleFull) return false;
    return true;
  });
}
function current(){
  const v=VIEWS.find(v=>v.k===view);
  let rows=pool(view).filter(v.f);
  const q=$('#q').value.trim().toLowerCase();
  if(q){
    const ws=q.split(/\s+/);
    rows=rows.filter(r=>{
      const hay=(r.t+' '+r.co+' '+r.tech.join(' ')+' '+r.gates.join(' ')+' '+
                 r.city+r.dist+' '+r.jt+' '+r.jd).toLowerCase();
      return ws.every(w=>hay.includes(w));   // 多词 AND，比单串匹配好用
    });
  }
  rows.sort((a,b)=>{
    const x=a[sortKey], y=b[sortKey];
    if(typeof x==='string'||typeof y==='string')
      return String(x||'').localeCompare(String(y||''),'zh')*dir;
    // null 一律排最后，不参与大小比较（否则"未核实"会冒到顶部）
    if(x==null) return 1; if(y==null) return -1;
    return (x-y)*dir;
  });
  return rows;
}

function card(r,i){
  const ac=ageCls(r.age);
  // 活跃度过期就划掉：19 天前记的"在线"既不能证明在线也不能证明不在线
  const staleAct=r.age!=null&&r.age>=T.staleFull;
  const act=r.act?`<span class="${staleAct?'stale':''}">${esc(r.act)}</span>`:'';
  const tags=[
    ...r.gates.map(g=>`<span class="tag g">${esc(g.slice(0,26))}</span>`),
    ...r.tech.slice(0,4).map(t=>`<span class="tag t">${esc(t.slice(0,22))}</span>`),
    ...r.risk.map(x=>`<span class="tag r">${esc(x)}</span>`),
  ].join('');
  return `<div class="card ${i===sel?'sel':''} ${isApplied(r)?'done':''}" data-i="${i}">
    <div class="r1">
      <span class="sc">${r.score}</span>
      <a class="ti" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.t)}</a>
      <span class="co">${esc(r.co)}</span>
      <span class="sa">${esc(r.sal)}</span>
      <span class="acts">
        <button class="btn" data-act="jd">JD</button>
        <button class="btn" data-act="ap">${isApplied(r)?'撤销':'已投'}</button>
      </span>
    </div>
    <div class="r2">
      <span>${esc(r.city)}${r.dist?'·'+esc(r.dist):''}</span>
      <span class="tag">${esc(r.jt)}</span>
      ${act}
      <span class="age ${ac}">核实 ${ageTxt(r.age)}</span>
      ${r.size?`<span>${esc(r.size)}</span>`:''}
      ${r.stage?`<span>${esc(r.stage)}</span>`:''}
      ${r.sched?`<span class="tag">${esc(r.sched)}</span>`:''}
      ${tags}
    </div>
    ${r.why?`<div class="why">${esc(r.why)}</div>`:''}
    <div class="jd">${esc(r.jd)}</div>
  </div>`;
}

function render(){
  const rows=current();
  $('#list').innerHTML = rows.length
    ? rows.map(card).join('')
    : '<div class="empty">没有匹配的岗位。试试清空搜索或切到「全部」。</div>';
  // 标签计数按**各自视图**的排除规则算，点进去看到的就是这个数。
  $('#tabs').innerHTML=VIEWS.map(v=>
    `<button class="tab ${v.k===view?'on':''}" data-v="${v.k}">${v.n}
       <span class="n">${pool(v.k).filter(v.f).length}</span></button>`).join('');
  const fresh=rows.filter(r=>r.age!=null&&r.age<=T.staleFresh).length;
  const old=rows.filter(r=>r.age==null||r.age>=T.staleFull).length;
  $('#sum').innerHTML=`<b>${rows.length}</b> 条 · ${T.staleFresh}天内核实 <b>${fresh}</b>`+
    ` · 超${T.staleFull}天或未核实 <b>${old}</b> · 数据 ${S.date}`;
  window.__rows=rows;
}

function toggleCard(i,what){
  const el=document.querySelector(`.card[data-i="${i}"]`);
  if(!el) return;
  if(what==='ap'){
    const r=window.__rows[i];
    const on=!isApplied(r);
    markApplied(r,on);
    render();
    return;
  }
  el.classList.toggle('open');
}

document.addEventListener('click',e=>{
  const tab=e.target.closest('.tab');
  if(tab){ view=tab.dataset.v; sel=-1; render(); return; }
  const chip=e.target.closest('.chip');
  if(chip){ EX[chip.dataset.ex]=!EX[chip.dataset.ex]; drawChips(); render(); return; }
  const btn=e.target.closest('.btn');
  if(btn){ toggleCard(+btn.closest('.card').dataset.i, btn.dataset.act); return; }
});
$('#q').addEventListener('input',()=>{sel=-1;render()});
$('#sort').addEventListener('change',e=>{
  sortKey=e.target.value;
  // 分数/薪资降序直觉上是"从好到差"；核实天数与活跃天数是越小越好，故升序
  dir=(sortKey==='age'||sortKey==='actDays'||sortKey==='co')?1:-1;
  render();
});
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'){
    if(e.key==='Escape'){e.target.value='';e.target.blur();render();}
    return;
  }
  const rows=window.__rows||[];
  if(e.key==='/'){e.preventDefault();$('#q').focus();return;}
  if(e.key==='j'||e.key==='ArrowDown'){e.preventDefault();sel=Math.min(sel+1,rows.length-1);}
  else if(e.key==='k'||e.key==='ArrowUp'){e.preventDefault();sel=Math.max(sel-1,0);}
  else if(e.key==='Enter'&&rows[sel]){window.open(rows[sel].url,'_blank');return;}
  else if(e.key===' '&&sel>=0){e.preventDefault();toggleCard(sel,'jd');return;}
  else if(e.key==='a'&&rows[sel]){toggleCard(sel,'ap');return;}
  else return;
  render();
  document.querySelector('.card.sel')?.scrollIntoView({block:'nearest'});
});

function drawChips(){
  const defs=[['risk','隐藏风险公司'],['applied','隐藏已投'],['stale',`隐藏超${T.staleFull}天未核实`]];
  $('#chips').innerHTML=defs.map(([k,n])=>
    `<span class="chip ${EX[k]?'on':'off'}" data-ex="${k}">${EX[k]?'✓':'○'} ${n}</span>`).join('')
    + `<span class="chip" style="cursor:default;border:0">城市 ${T.cities.join('/')}`
    + ` · 薪资上限均值 ${T.salaryDropMid}K · 硬过滤 ${S.dropped} 条 · 去重 ${S.dup} 条</span>`;
}
$('#thr').textContent=`P0≤${T.p0Days}天活跃 · 核心档 ${T.lowMax}–${T.highMin}K · 超纲留档 ${S.overArchive} 条`;
drawChips(); render();
"""


def build(records_json: str, stats_json: str, thresholds_json: str) -> str:
    """拼出完整单页。数据用 JSON 内嵌 —— 页面要能直接双击打开（file://），
    那样 fetch 外部 JSON 会被 CORS 挡掉。"""
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>投递池</title><style>" + CSS + "</style></head><body>"
        + BODY
        + "<script>\n"
        + f"const DATA={records_json};\n"
        + f"const S={stats_json};\n"
        + f"const T={thresholds_json};\n"
        + JS
        + "\n</script></body></html>"
    )



