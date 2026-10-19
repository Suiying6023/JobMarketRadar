"""刷新链路的核心：取一条岗位页，判定它现在是「在招 / 已关闭 / 已下架 / 被拦」。

## 为什么单独成模块

原来的实现把「fetch + DOM 取值 + 挑战页识别 + 重铸恢复」全塞在任务编排里，
三者互相纠缠：改一个判据要动任务代码，而任务代码又依赖账本、节流、验证窗口。
抽出来之后，判定逻辑是**纯函数**（`classify`），可以脱网测试；
取值动作是**一个函数**（`refresh_one`），只有它需要浏览器；
批量编排是**一个生成器**（`run_batch`），不碰数据库、不碰账本。

## 为什么解析在 Python 侧

`fetch` 只把字节拿回来，`htmlpage.query` 在 Python 里按容器取值。
这样 `HTML → 取值 → 判定 → 落库` 整条链路都能用真实 HTML 脱网测试
（`tests/e2e/test_refresh_e2e.py`）。代价是每条多传约 140KB 过 CDP。

⚠️ 原始 HTML **不进 `JobStatus.raw`**：`raw` 会整段落进 `job_status_checks`，
一条 140KB × 数千条等于把库撑爆。只留长度与摘要。

## 为什么不再重铸

2026-10-18 实测：岗位页 HTML 的响应**同样下发** `__zp_sseed__` / `__zp_sname__` /
`__zp_sts__`，所以每取完一条就能就地算出新 token，成本 **0 个请求**。

原先是等窗口耗尽（约 5.5 条）后做一次「普通页面导航」——那一次会把整个 SPA
拉起来，实测几十到上百个请求，摊薄到每条 +0.6。现在降到 0。

挑战页仍然可能因为别的原因出现（会话漂走、平台主动拦截），所以**判定保留**，
只是不再把它当成"该重铸了"的信号。

## 三种短页面必须分开

| 短页面 | 长度 | 标志 | 正确动作 |
|---|---:|---|---|
| 岗位已下架 | 约 11KB | 含「页面不存在」/ 404 | 标记关闭，**别重试** |
| JS 挑战页 | 约 41KB | 含 `security-check` / 请稍候 | 交给上层验证 |
| 验证页 | 约 44.5KB | 含「安全验证」/ `verify.html` | 开新页面让它自解 |

混成一类会让脚本对着已下架的岗位反复重试——重试一万次也变不出内容。
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
from dataclasses import dataclass, field

from .htmlpage import query
from .models import CLOSED_MARKERS

# 正常岗位页实测 138–147KB；60KB 是保守下界。
MIN_JOB_HTML_LEN = 60_000

GONE_MARKERS = ("您访问的页面不存在", "页面不存在", "404 Not Found")
CHALLENGE_MARKERS = ("security-check", "请稍候", "security.html")
# ⚠️ `verify.html` 是**另一种**拦截页，和 JS 挑战页不是一回事：
#   实测 HTTP 200、44.5KB、被 302 到 /web/passport/zp/verify.html、
#   标题「安全验证 - BOSS直聘」、正文「您的账号可能存在异常访问行为」。
#   它不含 `security-check` 字样，所以第一版漏判成 `short → unknown`，
#   批量任务因此不会停，继续对着验证页打了几百次请求。
VERIFY_MARKERS = ("安全验证", "verify.html", "passport/zp/verify")

OPEN_VALUES = ("招聘中", "最新")

# 页面里那几个容器。**一次取值，别在调用处另写一份选择器**。
PAGE_SELECTORS = {
    "h1": ".info-primary .name h1",
    "h1_fallback": ".job-banner h1",
    "job_status": ".job-banner .job-status",
    "active_time": ".boss-active-time",
    "captcha": ".verify-wrap,.captcha,.slider-verify,#nc_1_n1z",
}

_RESTRICTION = re.compile(
    r"账户存在异常|IP地址存在异常|IP 地址存在异常|访问频繁|操作过于频繁|操作太频繁")

# 只取字节，不解析。解析在 Python 侧（见模块头）。
FETCH_JOB_JS = """async (url) => {
  let response;
  try {
    response = await fetch(url, {credentials: 'include'});
  } catch (e) {
    return {ok: false, reason: 'fetch_error', error: String(e)};
  }
  let text = '';
  try {
    text = await response.text();
  } catch (e) {
    return {ok: false, reason: 'fetch_error', error: String(e),
            status: response.status, url: response.url || ''};
  }
  return {ok: true, status: response.status, url: response.url || '', text};
}"""


@dataclass(slots=True)
class JobStatus:
    """一条岗位的观测结果。`state` 是唯一判据，其余都是证据。"""

    state: str                      # open | closed | gone | challenged | unknown
    h1: str | None = None
    job_status: str | None = None
    active_time: str | None = None
    http_status: int | None = None
    length: int = 0
    url: str = ""
    reason: str = ""
    # 这一条是不是**自愈后**才拿到的（撞过 verify 页，导航一次后恢复）。
    recovered: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def closed(self) -> bool | None:
        """True=已关闭，False=在招，None=判不了。"""
        if self.state == "closed":
            return True
        if self.state == "open":
            return False
        return None

    @property
    def invalid(self) -> bool | None:
        """落库用的三态：True=失效，False=有效，None=未知（**不改库**）。"""
        if self.state in ("closed", "gone"):
            return True
        if self.state == "open":
            return False
        return None


def parse_job_page(html: str) -> dict:
    """从岗位页 HTML 里取容器。纯函数。

    找不到 `.info-primary .name h1` 时退回 `.job-banner h1`：平台改过一次结构，
    两份选择器都在实测里出现过，留一条退路比在判定处猜更便宜。
    """
    got = query(html, PAGE_SELECTORS)
    h1 = got["h1"]["text"] or got["h1_fallback"]["text"]
    return {
        "h1": h1,
        "job_status": got["job_status"]["text"],
        "active_time": got["active_time"]["text"],
        "captcha_present": bool(got["captcha"]["present"]),
        "restriction_notice": not h1 and bool(_RESTRICTION.search(html or "")),
    }


def read_job_page(fetched: dict) -> dict:
    """把 `FETCH_JOB_JS` 的返回变成 `classify` 吃的证据。纯函数。

    短页面的三种形态在这里分开（下架 / 挑战 / 验证），因为它们的处置完全相反。
    """
    if not fetched:
        return {"ok": False, "reason": "fetch_error", "error": "空返回"}
    if fetched.get("error") or fetched.get("reason") == "fetch_error":
        return {"ok": False, "reason": "fetch_error",
                "error": fetched.get("error") or "fetch_error",
                "status": fetched.get("status")}

    text = fetched.get("text") or ""
    status = fetched.get("status")
    final_url = fetched.get("url") or ""
    length = len(text)
    # `text` 一路带着：落库前由 `evidence_for_store` 换成摘要。
    # 摘要有用——同一份内容重复出现（平台给推荐页、缓存页）能一眼看出来。
    evidence: dict = {"status": status, "url": final_url, "len": length, "text": text}

    # 验证页优先判：它也是短页面，但处置与挑战页相同（必须停，等人过验证）。
    if any(mark in final_url for mark in VERIFY_MARKERS) or \
            any(mark in text for mark in VERIFY_MARKERS):
        return {**evidence, "ok": False, "reason": "verify", "challenged": True}
    if length < MIN_JOB_HTML_LEN:
        if status == 404 or any(mark in text for mark in GONE_MARKERS):
            return {**evidence, "ok": False, "reason": "gone", "gone": True}
        if any(mark in text for mark in CHALLENGE_MARKERS):
            return {**evidence, "ok": False, "reason": "challenged", "challenged": True}
        return {**evidence, "ok": False, "reason": "short"}

    evidence.update(parse_job_page(text))
    evidence["ok"] = True
    # `gone` / `challenged` 两个布尔是给 `service_jobs.status_verdict` 用的：
    # 它按旧证据契约判（`evidence["gone"]`），而旧取值脚本**从来没给过**这个键，
    # 于是下架岗位在任务链路里一直被记成「判不出来」。这里一次补齐。
    evidence["challenged"] = bool(
        evidence.get("captcha_present") or evidence.get("restriction_notice")
        or status in (401, 403, 429))
    return evidence


def classify(evidence: dict, *, title: str | None = None) -> JobStatus:
    """把取值结果判成状态。纯函数，不碰网络。

    ⚠️ 判据是 `.job-status` 的**值**，不是节点空不空：在招是「招聘中」/「最新」，
    关闭是「职位已关闭」。按"有文本即关闭"判会把全部岗位判成关闭。
    """
    if not evidence:
        return JobStatus(state="unknown", reason="空证据")

    if evidence.get("error"):
        return JobStatus(state="unknown", reason=f"fetch_error: {evidence['error']}"[:120],
                         http_status=evidence.get("status"))

    reason = evidence.get("reason")
    if reason == "gone":
        return JobStatus(state="gone", reason="页面不存在/404",
                         http_status=evidence.get("status"), length=evidence.get("len") or 0)
    if reason == "verify":
        return JobStatus(state="challenged", reason="要求人工验证（verify.html）",
                         http_status=evidence.get("status"), length=evidence.get("len") or 0,
                         url=evidence.get("url") or "")
    if reason == "challenged":
        return JobStatus(state="challenged", reason="JS 挑战页",
                         http_status=evidence.get("status"), length=evidence.get("len") or 0)
    if reason == "short":
        return JobStatus(state="unknown", reason="短页面且无已知标志",
                         http_status=evidence.get("status"), length=evidence.get("len") or 0)
    if reason == "fetch_error":
        return JobStatus(state="unknown", reason="fetch 异常")

    status = evidence.get("status")
    if status in (401, 403, 429):
        return JobStatus(state="challenged", reason=f"HTTP {status}", http_status=status)
    if evidence.get("captcha_present"):
        return JobStatus(state="challenged", reason="出现验证控件", http_status=status)
    if evidence.get("restriction_notice"):
        return JobStatus(state="challenged", reason="限制提示文案", http_status=status)

    h1 = evidence.get("h1")
    # 身份校验：拿回来的必须是我们请求的那条岗位，否则判定会张冠李戴，
    # 而降级是单向的（误判关闭不可逆）。
    if title and h1 and not same_job(h1, title):
        return JobStatus(state="unknown", reason=f"身份不符: {h1[:20]}",
                         h1=h1, http_status=status, length=evidence.get("len") or 0)

    job_status = (evidence.get("job_status") or "").strip()
    common = dict(h1=h1, job_status=job_status or None,
                  active_time=evidence.get("active_time"),
                  http_status=status, length=evidence.get("len") or 0,
                  url=evidence.get("url") or "")
    if not job_status:
        return JobStatus(state="unknown", reason="缺 job-status 容器", **common)
    if any(value in job_status for value in CLOSED_MARKERS):
        return JobStatus(state="closed", **common)
    if any(value in job_status for value in OPEN_VALUES):
        return JobStatus(state="open", **common)
    # 平台加了新文案：不猜。猜错要么白刷一轮（便宜），要么误降一个在招岗位（不可逆）。
    return JobStatus(state="unknown", reason=f"未知状态文案: {job_status[:20]}", **common)


def same_job(h1: str | None, db_title: str | None) -> bool:
    """比较前 8 字：标题常有全角括号与后缀差异，全等太脆。"""
    left = (h1 or "").strip()
    right = (db_title or "").strip()
    if not left or not right:
        return False
    return left[:8] in right or right[:8] in left


def evidence_for_store(evidence: dict) -> dict:
    """落库前的证据：丢掉原始 HTML，只留能复算判定的东西。"""
    if not evidence:
        return {}
    trimmed = {key: value for key, value in evidence.items() if key != "text"}
    text = evidence.get("text")
    if isinstance(text, str):
        trimmed["text_len"] = len(text)
        trimmed["text_digest"] = hashlib.sha1(
            text.encode("utf-8", "replace")).hexdigest()[:12]
    return trimmed


async def recover_session(context, url: str, *, timeout_s: float = 75.0,
                          poll_s: float = 1.0, before_navigate=None) -> bool:
    """撞到验证页时，**开一个新页面**把它当文档加载，让站点自己的 JS 完成校验。

    ## 为什么必须新页面（实测 2026-10-18）

    复用那个已经跑了几十次 fetch 的页面做 `goto`，恢复不了；新开一个页面 `goto`
    同一个 URL，约 12 秒后站点自己跳到 `job_detail/xxx.html?_security_check=1_<ts>`，
    随后普通 fetch 恢复正常（实测恢复后连取 5 条，147–149KB 正常页，零失败）。

    区别在 `fetch()` 只拿到验证页的 HTML、**不执行它的 JS**，而校验逻辑就在那段 JS 里。
    所以恢复动作必须是导航，且要一个干净页面 —— 这也是原先「复用忙碌页面自愈」
    在 180 条记录里 `recovered` 全为 False 的原因。

    ## 为什么启动参数里必须有那三项（踩过的坑）

    Chrome 对**被遮挡/后台**的页面会节流定时器（`setTimeout` 被压到约每分钟一次），
    挑战就跑不完。实测：屏内窗口 10–16 秒自愈，屏外窗口一直不跳转。
    所以靠 `--disable-background-timer-throttling` 等三项，不靠把窗口开到屏内
    （那会在无人值守时抢用户焦点）。

    返回 True 表示已离开验证页。

    `before_navigate` 是**记账钩子**：这次 `goto` 是一次真实的平台请求，而
    `service_jobs` 那边每发一个请求都要先在账本里占一行、并在发之前查额度。
    不传它就等于让一条请求绕过账本。
    """
    page = None
    try:
        page = await context.new_page()
        if before_navigate is not None:
            await before_navigate()
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            await page.bring_to_front()
        except Exception:  # noqa: BLE001 - 某些环境没有这个方法
            pass
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            current = page.url or ""
            if not any(hint in current for hint in VERIFY_MARKERS):
                return True
        return False
    except Exception:  # noqa: BLE001 - 恢复失败就走停机路径
        return False
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass


async def refresh_one(page, url: str, *, title: str | None = None,
                      roller=None, recover: bool = True,
                      recover_timeout_s: float = 75.0,
                      recover_poll_s: float = 1.0,
                      before_recover=None) -> JobStatus:
    """取一条岗位并判定。成功取值后**就地滚 token**，让窗口不会耗尽。

    `roller` 可以是 `boss_scrape.stoken.StokenRoller`，也可以传 None（不滚动）。
    滚动失败不影响本次判定——挑战页仍会被 `classify` 识别出来交给上层。

    `recover=True` 时，撞到验证页会开一个新页面自愈，成功就重取一次。
    自愈要等站点自己跳回来，实测 10–16 秒，所以超时给到 75 秒。
    `before_recover` 是那次导航的记账钩子（见 `recover_session`）。
    """
    evidence = read_job_page(await page.evaluate(FETCH_JOB_JS, url))
    status = classify(evidence, title=title)
    # 原始证据始终挂上：调用方要用 url/status 判会话，这些字段在"短页面"分支里
    # 没有被 classify 搬进结构体。
    status.raw = evidence_for_store(evidence)

    if recover and status.state == "challenged" and evidence.get("reason") == "verify":
        context = getattr(page, "context", None)
        if context is not None and await recover_session(
                context, url, timeout_s=recover_timeout_s, poll_s=recover_poll_s,
                before_navigate=before_recover):
            evidence = read_job_page(await page.evaluate(FETCH_JOB_JS, url))
            status = classify(evidence, title=title)
            status.raw = evidence_for_store(evidence)
            status.recovered = True

    # 只有正常页面才值得滚：挑战页/下架页不下发种子，滚了也是白跑一趟。
    if roller is not None and evidence.get("ok"):
        try:
            await roller.roll()
        except Exception:  # noqa: BLE001 - 滚动是优化项，失败不影响判定
            pass
    return status


# ---------------------------------------------------------------- 批量编排

@dataclass(slots=True)
class BatchPolicy:
    """批量的节奏与停机条件。**全部可关**，测试里关掉就等于纯判定。"""

    pace: tuple[float, float] = (8.0, 15.0)
    pause_every: int = 20
    pause_seconds: tuple[float, float] = (60.0, 90.0)
    # 连续几条判不出来就停。不设这道闸，页面退化后会一路打下去，白烧配额。
    stop_after_unknown: int = 3
    # 撞到验证页且新页面也恢复不了 → 立刻停。无人值守时重试只会加压。
    stop_on_unrecovered: bool = True
    # 自愈等多久。实测站点自己跳回来要 10–16 秒，75 秒是留足余量的上界。
    recover_timeout_s: float = 75.0
    recover_poll_s: float = 1.0


@dataclass(slots=True)
class BatchSummary:
    """一批跑完的计数。**不存逐条结果** —— 那是 `on_result` 回调的事。"""

    attempted: int = 0
    states: dict = field(default_factory=dict)
    recovered: int = 0
    rolls: int = 0
    roll_failures: int = 0
    stopped: str = ""

    def count(self, state: str) -> None:
        self.states[state] = self.states.get(state, 0) + 1


async def run_batch(page, targets, *, roller=None, policy: BatchPolicy | None = None,
                    on_result=None, sleep=asyncio.sleep) -> BatchSummary:
    """按顺序刷一批岗位。**不碰数据库**：每条结果交给 `on_result`。

    `on_result(item)` 收到一个 dict（含 canonical_id/title/job_url/state/…），
    落库、写 JSONL、只打日志都由调用方决定。这样测试可以只数结果条数，
    生产可以顺手写库，实验可以只落文件 —— 三者共用同一段编排。

    停机条件都在 `BatchPolicy` 里，触发后**保留已得结果**再返回。
    """
    policy = policy or BatchPolicy()
    summary = BatchSummary()
    consecutive_unknown = 0

    for index, item in enumerate(targets, 1):
        # 长停顿：打断连续请求串，模拟真人节奏。
        # 86 条那道墙是在 2–4 秒的节奏下撞到的；加长间隔 + 周期性停顿是为了
        # **不撞墙**，而不是撞了再恢复（历史封禁的前兆正是"连续触发 + 高频重算"）。
        if policy.pause_every and index > 1 and (index - 1) % policy.pause_every == 0:
            low, high = policy.pause_seconds
            await sleep(random.uniform(low, high))

        try:
            status = await refresh_one(page, item["job_url"],
                                       title=item.get("title"), roller=roller,
                                       recover_timeout_s=policy.recover_timeout_s,
                                       recover_poll_s=policy.recover_poll_s)
        except Exception as exc:  # noqa: BLE001 - 单条异常不该带走整批
            summary.stopped = f"异常 {type(exc).__name__}: {exc}"[:140]
            break

        summary.attempted += 1
        summary.count(status.state)
        if status.recovered:
            summary.recovered += 1
        if on_result is not None:
            on_result({
                "canonical_id": item.get("canonical_id"),
                "title": item.get("title"),
                "job_url": item["job_url"],
                "state": status.state,
                "job_status": status.job_status,
                "h1": status.h1,
                "active_time": status.active_time,
                "reason": status.reason,
                "len": status.length,
                "http_status": status.http_status,
                "recovered": status.recovered,
                "invalid": status.invalid,
                "evidence": status.raw,
            })

        if status.state == "challenged":
            if policy.stop_on_unrecovered:
                summary.stopped = f"第 {index} 条撞验证页：{status.reason}"
                break

        if status.state == "unknown":
            consecutive_unknown += 1
            if consecutive_unknown >= policy.stop_after_unknown:
                summary.stopped = (
                    f"连续 {consecutive_unknown} 条无法判读"
                    f"（最后原因：{status.reason[:60]}），已停止")
                break
        else:
            consecutive_unknown = 0

        if index < len(targets):
            await sleep(random.uniform(*policy.pace))

    if roller is not None:
        summary.rolls = getattr(roller, "roll_count", 0)
        summary.roll_failures = getattr(roller, "fail_count", 0)
    return summary
