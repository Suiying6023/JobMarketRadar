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

/* ---- 卡片：三列固定网格 = 岗位信息 | LLM 标签 | 操作 ----
   列宽写死（不用 auto/fr 让内容决定），因为一旦某条标签长一点，
   整列就变宽、上下行对不齐，纵向扫标签的能力就没了 —— 而那正是分列的目的。
   标签本身也在导出侧压成了短词（门槛 ≤9 字、技术栈 ≤16 字），
   两侧一起保证列宽稳定。 */
/* 不设 max-width：卡片自己靠 grid 定宽（见 .card），容器铺满窗口。
   原先给容器设 1076px 会让卡片居中、左右各留 250px 空白，
   而顶栏是全宽的 —— 两者对不齐，页面看起来像没铺开。 */
main{padding:10px 16px 64px}
/* 高度构成（实测）：两行内容 44px + 上下内边距 + 外边距。
   标签列只占 21px 一行，不是撑高的原因。压内边距是唯一还能省的地方。
   ⚠️ 左列用 `minmax(0,560px)` 而**不是 1fr**：实测 1fr 会把剩余宽度全吃掉
   （分到 1014px 而真实内容最宽只有 481px、中位仅 302px），在岗位列和标签列
   之间留出 533px 空白，眼睛要横跨半个屏幕才能把一条岗位和它的标签对上。
   上限 560 = 实测最宽内容 481 + 余量；minmax 让窄屏时还能压缩。
   第四列（JD 摘要）用 1fr 吸收富余宽度：实测 1366-1920px 下富余 265-819px，
   与其空着不如放 JD 开头 —— 那是判断"这岗位到底做什么"最快的信息源。
   ⚠️ 它必须单行不换行（见 .peek），否则会把卡片撑高、抵消掉密度收益。 */
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:6px 11px;margin-bottom:4px;transition:.12s;
  display:grid;
  grid-template-columns:minmax(0,520px) 340px minmax(0,1fr) 118px;
  gap:14px;align-items:center}
.card:hover{border-color:#3b4252}
.card.sel{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.card.done{opacity:.4}
/* 左侧色条标注**有**门槛的（不写「无门槛」——那会在每张干净的卡上重复噪音）。
   红=硬挡（明确不招应届/公司有风险），黄=需留意（年限/名校/硕博）。 */
.card.gate{border-left:3px solid var(--warn);padding-left:9px}
.card.block{border-left:3px solid var(--bad);padding-left:9px}

/* ---- 左列：岗位信息 ---- */
.mid{min-width:0}
.r1{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.sc{font-variant-numeric:tabular-nums;font-weight:700;color:var(--accent);font-size:15px}
.ti{font-size:14.5px;font-weight:600;color:var(--fg)}
.co{color:var(--dim);font-size:13px}
.sa{color:var(--ok);font-weight:600;font-variant-numeric:tabular-nums;font-size:13.5px}
.r2{margin-top:3px;display:flex;gap:9px;flex-wrap:wrap;align-items:center;
  color:var(--dim2);font-size:11.5px}

/* ---- 中列：LLM 提取的标签。门槛一行、技术栈一行 ----
   两组分行是因为它们回答不同问题（能不能投 / 对不对口）。
   固定 340px 且标签 nowrap：宁可换行也不让单个标签拉宽整列。 ---- */
.side{display:flex;flex-direction:column;gap:3px;align-items:flex-start;min-width:0}
.tgroup{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-start}
.tag{background:var(--panel2);border:1px solid var(--line);border-radius:4px;
  padding:1px 6px;font-size:11px;color:var(--dim);white-space:nowrap;
  max-width:160px;overflow:hidden;text-overflow:ellipsis}
.tag.g{color:var(--warn);border-color:#4a3f22;background:#241f0f}  /* 门槛：挡人的 */
.tag.t{color:#7dd3fc;border-color:#1e3a4a;background:#0e1f2a}      /* 技术栈：对口度 */
.tag.r{color:var(--bad);border-color:#4a2626;background:#2a1414}   /* 公司风险 */
.tag.ok{color:var(--ok);border-color:#1e4032;background:#0d2019}   /* 正面：双休等 */
.age{font-variant-numeric:tabular-nums}
.age.w{color:var(--warn)} .age.b{color:var(--bad)}
.stale{text-decoration:line-through;opacity:.55}    /* 过期的活跃度 */

/* ---- 第三列：JD 摘要（滑动窗口）----
   原先这块宽度是空的。放 JD 开头能直接回答"这岗位到底做什么"，
   而标签只给技术栈和门槛，答不了这个。
   `white-space:nowrap` + `overflow-x:auto` 是关键：单行滚动而非换行，
   卡片高度完全不受它影响（实测仍是 58px）。鼠标横向滚可以往后读，
   要读全文点 JD 按钮展开。 */
.peek{min-width:0;color:var(--dim2);font-size:11.5px;line-height:1.5;
  white-space:nowrap;overflow-x:auto;overflow-y:hidden;
  scrollbar-width:none;-ms-overflow-style:none;
  mask-image:linear-gradient(to right,#000 calc(100% - 24px),transparent);
  -webkit-mask-image:linear-gradient(to right,#000 calc(100% - 24px),transparent)}
.peek::-webkit-scrollbar{height:0}
.card:hover .peek{color:var(--dim)}

/* ---- 末列：操作。**横排**——竖排会把每张卡撑到 76px 高，一屏只剩 8 条，
   而卡片内容本身只有两行。 ---- */
.ops{display:flex;gap:4px;align-items:flex-start;justify-content:flex-end}
.ops .btn{padding:2px 7px;font-size:11px}
.btn.on{color:var(--ok);border-color:#1e4032;background:#0d2019}
.btn.warn:hover{color:var(--bad);border-color:#4a2626;background:#2a1414}
/* 窄屏时左列改成可压缩（minmax），否则 560px 定宽会溢出横向滚动 */
/* 窄屏优先牺牲 JD 摘要：它是"顺便看到"的信息，而标签和操作是必需的。 */
@media(max-width:1400px){
  .card{grid-template-columns:minmax(0,1fr) 320px 118px;gap:12px}
  .peek{display:none}
  .tag{max-width:130px}
}
@media(max-width:860px){
  .card{grid-template-columns:minmax(0,1fr) 118px}
  .side{grid-column:1;margin-top:5px}
}
.why{margin-top:6px;color:var(--dim2);font-size:12px;border-left:2px solid var(--warn);
  padding-left:8px;display:none}
.card.open .why{display:block}
.jd{margin-top:7px;background:var(--panel2);border-radius:6px;padding:9px 11px;
  white-space:pre-wrap;font-size:12.5px;color:#c3c9d4;max-height:420px;
  overflow:auto;display:none;line-height:1.6}
.card.open .jd{display:block}
.acts{display:flex;gap:5px;margin-left:4px}
.btn{background:var(--panel2);border:1px solid var(--line);color:var(--dim);
  border-radius:5px;padding:1px 8px;font-size:11.5px;cursor:pointer}
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
  <span><kbd>a</kbd> 收藏</span><span><kbd>x</kbd> 降级</span><span><kbd>Esc</kbd> 清搜索</span>
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
// `undo` 传入时 toast 带一个撤回按钮。降级去掉了确认框，撤回入口就是
// 它的安全网 —— 所以这里必须能承载动作，不能只是一条消息。
function toast(msg,bad,undo){
  let el=document.getElementById('toast');
  if(!el){ el=document.createElement('div'); el.id='toast'; document.body.appendChild(el); }
  el.innerHTML=esc(msg)+(undo?' <button id="tundo">撤回</button>':'');
  el.style.cssText='position:fixed;left:50%;bottom:34px;transform:translateX(-50%);'+
    'background:'+(bad?'#7f1d1d':'#14532d')+';color:#fff;padding:7px 14px;'+
    'border-radius:6px;font-size:13px;z-index:99;box-shadow:0 4px 14px rgba(0,0,0,.4);'+
    'display:flex;gap:10px;align-items:center';
  if(undo){
    const btn=el.querySelector('#tundo');
    btn.style.cssText='background:rgba(255,255,255,.16);border:0;color:#fff;'+
      'border-radius:4px;padding:2px 9px;font-size:12px;cursor:pointer';
    btn.onclick=()=>{ undo(); el.remove(); };
  }
  clearTimeout(el._t); el._t=setTimeout(()=>el.remove(),undo?6000:3200);
}

// JD 摘要：压成单行。JD 开头常是「岗位职责：」「一、工作内容」这类小标题，
// 直接截会得到一屏的标题而没有内容 —— 所以跳过纯标题行，从第一句实质内容开始。
const _HDR=/^[\s\d一二三四五六七八九十、.,，：:（）()【】\[\]-]*(岗位职责|工作职责|职位描述|岗位描述|工作内容|职责描述|岗位要求|任职要求|任职资格|职位要求|我们希望你|你将负责|工作地点)?[\s：:，,、]*$/;
function peek(jd){
  if(!jd) return '';
  const lines=jd.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
  const body=lines.filter(s=>!_HDR.test(s));
  // 全是标题行时退回原文，别返回空白（那样这一列就白占了）
  return ((body.length?body:lines).join(' · ')).slice(0,600);
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
    if(HIDDEN.has(r.id)) return false;           // 已手工降级的
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
  // 活跃度过期就划掉：19 天前记的"在线"既不能证明在线也不能证明不在线
  const staleAct=r.age!=null&&r.age>=T.staleFull;
  const act=r.act?`<span class="${staleAct?'stale':''}">${esc(r.act)}</span>`:'';
  // 门槛组：挡人的东西。r.gates 已在导出侧滤掉"本科及以上""经验不限"这类
  // 求职者已满足的值和"未提及"，所以这里出现的每一条都值得看一眼。
  const blockers=[
    ...r.gates.map(g=>`<span class="tag g" title="${esc(g)}">${esc(g)}</span>`),
    ...(r.fresh?[`<span class="tag g">${esc(r.fresh)}</span>`]:[]),
    ...r.risk.map(x=>`<span class="tag r">${esc(x)}</span>`),
  ];
  const perks=r.sched?[`<span class="tag ok">${esc(r.sched)}</span>`]:[];
  const techs=r.tech.map(t=>`<span class="tag t" title="${esc(t)}">${esc(t)}</span>`);
  // 左侧色条分两档：硬挡（明确不招应届 / 公司有风险）比"需留意"更该先看到
  const hard=r.fresh==='不接受应届'||r.risk.length>0;
  const cls=hard?'block':(r.gates.length||r.fresh?'gate':'');
  return `<div class="card ${cls} ${i===sel?'sel':''} ${isApplied(r)?'done':''}" data-i="${i}">
    <div class="mid">
      <div class="r1">
        <span class="sc" title="综合分">${r.score}</span>
        <a class="ti" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.t)}</a>
        <span class="sa">${esc(r.sal)}</span>
        <span class="co">${esc(r.co)}</span>
      </div>
      <div class="r2">
        <span>${esc(r.city)}${r.dist?'·'+esc(r.dist):''}</span>
        <span>${esc(r.jt)}</span>
        ${act}
        <span class="age ${ageCls(r.age)}">核实 ${ageTxt(r.age)}</span>
        ${r.size?`<span>${esc(r.size)}</span>`:''}
        ${r.stage?`<span>${esc(r.stage)}</span>`:''}
      </div>
      ${r.why?`<div class="why">${esc(r.why)}</div>`:''}
      <div class="jd">${esc(r.jd)}</div>
    </div>
    <div class="side">
      ${blockers.length||perks.length
        ? `<div class="tgroup">${blockers.join('')}${perks.join('')}</div>`:''}
      ${techs.length?`<div class="tgroup">${techs.join('')}</div>`:''}
    </div>
    <div class="peek" title="横向滚动可继续阅读；点 JD 看全文">${esc(peek(r.jd))}</div>
    <div class="ops">
      <button class="btn" data-act="jd">JD</button>
      <button class="btn ${isApplied(r)?'on':''}" data-act="ap">${isApplied(r)?'★ 已收':'收藏'}</button>
      <button class="btn warn" data-act="dm">降级</button>
    </div>
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

// 本地隐藏集：降级后立刻从视图消失。跑服务时数据库也真的改了，
// 但页面不会自己重载，所以要有前端侧的即时反馈。
const HIDDEN=new Set(JSON.parse(localStorage.getItem('pool.demoted.v1')||'[]'));
// 不弹确认框：降级是高频动作（筛池子时一路往下刷），每次确认反而让人
// 养成盲点「确定」的习惯，那时确认框就只剩摩擦、不剩保护。
// 安全网改成事后可撤：toast 给撤回入口 + 顶栏常驻「恢复本页隐藏的 N 条」。
async function demote(r){
  HIDDEN.add(r.id);
  localStorage.setItem('pool.demoted.v1',JSON.stringify([...HIDDEN]));
  // 必须重绘 chips：「恢复本页隐藏的 N 条」那个入口是按 HIDDEN.size 生成的，
  // 只 render() 不 drawChips() 会让误点降级后**没有撤回入口**。
  sel=-1; drawChips(); render();
  // 撤回：本页恢复显示，并让服务把它撤回 active（/restore 会同时打 bookmarked，
  // 使后续清理脚本不再自动降它 —— 手工判断优先于脚本判断）
  const undo=async()=>{
    HIDDEN.delete(r.id);
    localStorage.setItem('pool.demoted.v1',JSON.stringify([...HIDDEN]));
    drawChips(); render();
    if(LIVE) try{ await fetch('/restore',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({id:r.id})}); }catch(e){}
  };
  if(!LIVE){ toast(`已隐藏「${r.t.slice(0,14)}」（未跑服务，库未改）`,false,undo); return; }
  try{
    const res=await fetch('/demote',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({id:r.id})});
    const j=await res.json();
    if(j.ok) toast(`已降级「${r.t.slice(0,14)}」`,false,undo);
    else toast('写库失败: '+(j.err||'未知'),true,undo);
  }catch(e){ toast('写库失败（服务未运行？）本次仅本页隐藏',true,undo); }
}

function toggleCard(i,what){
  const r=window.__rows[i];
  if(!r) return;
  if(what==='ap'){ markApplied(r,!isApplied(r)); render(); return; }
  if(what==='dm'){ demote(r); return; }
  document.querySelector(`.card[data-i="${i}"]`)?.classList.toggle('open');
}

document.addEventListener('click',e=>{
  const tab=e.target.closest('.tab');
  if(tab){ view=tab.dataset.v; sel=-1; render(); return; }
  if(e.target.closest('#unhide')){
    HIDDEN.clear(); localStorage.removeItem('pool.demoted.v1');
    toast('已恢复本页隐藏的条目（数据库里已降级的仍需重跑导出）');
    drawChips(); render(); return;
  }
  const chip=e.target.closest('.chip');
  if(chip&&chip.dataset.ex){ EX[chip.dataset.ex]=!EX[chip.dataset.ex]; drawChips(); render(); return; }
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
  else if(e.key==='x'&&rows[sel]){toggleCard(sel,'dm');return;}
  else return;
  render();
  document.querySelector('.card.sel')?.scrollIntoView({block:'nearest'});
});

function drawChips(){
  const defs=[['risk','隐藏风险公司'],['applied','隐藏已收藏'],['stale',`隐藏超${T.staleFull}天未核实`]];
  let h=defs.map(([k,n])=>
    `<span class="chip ${EX[k]?'on':'off'}" data-ex="${k}">${EX[k]?'✓':'○'} ${n}</span>`).join('');
  // 误点降级要能撤回。只清本页隐藏——数据库那边的 retention_status 需要
  // 用页面上的「撤销」或重跑脚本恢复，这里说清楚，不要让人以为全撤了。
  if(HIDDEN.size) h+=`<span class="chip" id="unhide" style="border-color:#4a3f22;color:var(--warn)">`
    +`↺ 恢复本页隐藏的 ${HIDDEN.size} 条</span>`;
  h+=`<span class="chip" style="cursor:default;border:0">城市 ${T.cities.join('/')}`
    +` · 薪资上限均值 ${T.salaryDropMid}K · 硬过滤 ${S.dropped} 条 · 去重 ${S.dup} 条</span>`;
  $('#chips').innerHTML=h;
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



