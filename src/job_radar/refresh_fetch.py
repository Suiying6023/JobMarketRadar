"""匿名同源 fetch 读岗位页 —— 刷新链路的取值与判定核心。

**为什么不再用整页加载**（2026-08-30 实测，`data/calibration/` 有原始记录）：

| 取法 | 每条请求数 | 说明 |
|---|---:|---|
| `page.goto` 整页 | **102** | 图片 62、xhr 17、script 14…（`REQ_PER_JOB=14` 严重低估） |
| 整页 + 拦静态资源 | 39 | 但**风控域 `www.zhipin.com` 只从 21 降到 20** |
| **同源 fetch HTML** | **1** | `fetch()` 不加载子资源，只发一个文档请求 |

关键点：拦静态资源省下的 63 次全在 `img.bosszhipin.com`/`static.zhipin.com`,
**风控主体域几乎没变** —— 省的是带宽不是风险。真正的降幅来自不加载子资源：
整页那 20 次自家域请求里，17 次是 SPA 自己的壳（`city.json`、`industry.json`、
banner、popupguides、agreement/tip、resume/parser、smsSwitch…），读 JD 一个都不需要。

**配额窗口与采集链路同构**：一个 `__zp_stoken__` 撑约 **5.5 条** fetch
（实测 [6,5,6,5]），之后每条回 41KB 挑战页；重算 stoken 约 **3 次**请求。
摊薄后 **1.58 次/条**（实测 38 次读 24 条），与采集链路详情的 1.51 次/条同量级。

⚠️ **判据必须看 `.job-status` 的值，不能看节点空不空**。这个坑我踩过：
在招岗位该节点是「招聘中」/「最新」，关闭是「职位已关闭」——
按"有文本即关闭"判会把**全部**岗位判成关闭。当时的样本恰好一半真关闭，
单看那一半是 10/10 满分，是双向对照（平台 SEO 在招页 8 条）才暴露的。

⚠️ 整篇文本找子串（旧 `_is_closed(body.innerText)`）在**整页加载**下是对的，
但在 fetch 到的**原始 HTML** 上不行：没有 CSS，登录浮层与「更多职位」推荐位的
文案全混进纯文本（2196 字 → 58993 字）。所以这里一律按容器取值。
"""
from __future__ import annotations

from .normalization import CLOSED_MARKERS

# 一条 fetch 的请求数，以及窗口耗尽后重算 stoken 的开销。
# 用于账本折算——**别再用一个写死的 14**，那个数是从 verify 页数出来的。
def quota_factor(used: int, cap: int,
                 ladder: list[tuple[float, float]] | None = None) -> float:
    """越过日线后的延迟乘数。`ladder` 是 (用量倍数, 延迟乘数) 的升序列表。

    **唯一实现**：采集链路（boss.py）与刷新链路（refresh_status_loop.py）
    都调这一个，避免两份档位表各自漂移。

    档位而非连续函数：连续函数会让减速在越线那一刻几乎无感（1.0x → 1.01x），
    档位让每一级都真的看得出来，日志里也能一眼认出当前在哪一级。

    `ladder=None` 或空表示不降速（返回 1.0）—— 由调用方决定要不要降速，
    这里不替它选。
    """
    if not ladder or cap <= 0 or used < cap:
        return 1.0
    over = used / cap
    factor = 1.0
    for threshold, mult in sorted(ladder):
        if over >= threshold:
            factor = mult
    return factor


REQ_PER_FETCH = 1
REQ_PER_RECOVER = 3
FETCH_WINDOW = 5          # 保守取 5（实测 [6,5,6,5]，取下界免得反复撞挑战页）
# 摊薄后的单条成本，给日线换算用。
REQ_PER_JOB_AMORTIZED = REQ_PER_FETCH + REQ_PER_RECOVER / FETCH_WINDOW  # 1.6

# 窗口默认落点 `x,y`：主屏之外，跑批时不抢焦点。
DEFAULT_WINDOW_POSITION = "2400,1400"


def launch_args(window_position: str = DEFAULT_WINDOW_POSITION) -> list[str]:
    """浏览器启动参数。刷新链路各脚本共用，别在调用处另拼一份。

    `--disable-blink-features=AutomationControlled` 是反检测必需项，
    同时也是 `cleanup_browsers.py` 认「这个浏览器是我们起的」的特征之一。

    ⚠️ **`window_position` 的坐标必须为正**。挑战 JS 把窗口几何当环境指纹：
    负的大数被判为异常环境，请求会被推去登录页（`/web/user/?seed=...`）而
    **不给可自解的挑战、也不给可点的验证页**——那种窗口人是点不动的。
    实测 `-32000,-32000` 得 0/4，正坐标 `2400,1400` 挑战率 7%（n=14），
    优于在屏基线 14-16%。最小化同样不行（Windows 把坐标写成 -21333）。
    传空字符串 = 不指定位置，窗口开在屏幕中央会抢焦点。
    """
    args = ["--disable-blink-features=AutomationControlled"]
    if window_position:
        if window_position.startswith("-") or ",-" in window_position:
            raise ValueError(
                f"窗口坐标必须为正，收到 {window_position!r}。"
                "负坐标会让挑战 JS 判为异常环境，请求被推去登录页（实测 0/4）。")
        args.append(f"--window-position={window_position}")
    return args

# 挑战页约 41KB，正常岗位页 ≥ 60KB（实测 138-147KB）。用长度做**第一道**判据，
# 因为挑战页解析出来的 DOM 里什么容器都没有，再往下抠只会得到一串 None。
MIN_JOB_HTML_LEN = 60_000

# 页面内取值脚本。三件事一次做完：
#   ① fetch HTML（credentials:'include' —— stoken 在 cookie 里）
#   ② 用 DOMParser 解析成真 DOM，按容器精确取值
#   ③ 回 h1 做身份校验：确认拿到的是请求的那条岗位，不是被导到了别处
EXTRACT_JS = """async ({url, minLen}) => {
    let html, status;
    try {
        const r = await fetch(url, {credentials: 'include'});
        status = r.status;
        html = await r.text();
    } catch (e) {
        return {ok: false, reason: 'fetch_error', error: String(e)};
    }
    if (!html || html.length < minLen) {
        return {ok: false, reason: 'challenged', status, len: (html || '').length};
    }
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const txt = (sel) => {
        const el = doc.querySelector(sel);
        return el ? (el.textContent || '').trim() : null;
    };
    return {
        ok: true, status, len: html.length,
        h1: txt('.info-primary .name h1') || txt('h1'),
        job_status: txt('.job-banner .job-status'),
        active_time: txt('.boss-active-time'),
        boss_line: txt('.job-boss-info h2.name'),
        jd_len: (txt('.job-sec-text') || '').length,
    };
}"""

# `.job-status` 的已知取值（2026-08-30 实测）：
#   在招   —— 「招聘中」「最新」
#   关闭   —— 「职位已关闭」
# 只把**明确的关闭文案**当关闭，其余一律按在招处理。
# 方向刻意如此：误判关闭会**单向降级**一个在招岗位（不可逆），
# 而误判在招只是下一轮再刷一次（可逆且便宜）。
# 关闭文案的真源在 `normalization`，这里只引用 —— 两份各自维护必然漂移，
# 而漂移的代价是误降一个在招岗位（不可逆）。
CLOSED_VALUES = CLOSED_MARKERS
OPEN_VALUES = ("招聘中", "最新")


def is_closed_status(job_status: str | None) -> bool | None:
    """按 `.job-status` 的值判关闭。None = 判不了（缺容器，别乱猜）。"""
    s = (job_status or "").strip()
    if not s:
        return None
    if any(v in s for v in CLOSED_VALUES):
        return True
    if any(v in s for v in OPEN_VALUES):
        return False
    # 平台加了新文案。**不猜**：返回 None 让调用方记未判定，
    # 猜错的方向要么白刷一轮（便宜），要么误降一个在招岗位（不可逆）。
    return None


def same_job(h1: str | None, db_title: str | None) -> bool:
    """身份校验：fetch 回来的页面是不是请求的那条岗位。

    存在理由：一旦被重定向（挑战页/推荐页/换页），判定就会张冠李戴 ——
    把 A 岗位的「已关闭」写到 B 岗位上，而降级是单向的。
    比较用前 8 字：标题里常有全角括号与后缀差异，全等太脆。
    """
    a = (h1 or "").strip()
    b = (db_title or "").strip()
    if not a or not b:
        return False
    return a[:8] in b or b[:8] in a
