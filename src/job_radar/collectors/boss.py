from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from patchright.async_api import BrowserContext, Page
from patchright.async_api import Error as PlaywrightError

from boss_scrape.errors import AUTH_EXPIRED_CODES, RISK_CODES, RISK_TEXTS
from boss_scrape.htmlpage import query as html_query

from ..models import CityConfig, CollectionConfig, JobRecord, Platform, SearchFilters
from ..profile import is_staffing_company
from ..safety import AccessBlocked, AccessGate
from ..throttle import gaussian
from .base import Collector
from .browser import BrowserSession
from .boss_payload import apply_detail, authenticated_user_response, parse_list_job, response_condition, response_message
from .stoken import StokenRoller, roll_quietly


ORIGIN = "https://www.zhipin.com"
API_PATH = "/wapi/zpgeek/search/joblist.json"
DETAIL_PATH = "/wapi/zpgeek/job/detail.json"
DETAIL_PAGE = "/job_detail/{}.html"
USER_INFO_PATH = "/wapi/zpuser/wap/getUserInfo.json"

# SSR 详情页的选择器。2026-10-19 在两条已知正文长度的岗位上核对过：
# `.job-sec-text` 与 detail.json 的 postDescription 归一化后逐字相等。
SSR_SELECTORS = {
    "jd": ".job-sec-text",
    "job_status": ".job-status",
    "boss_name": ".job-boss-info .name",
    "boss_title": ".job-boss-info .boss-info-attr",
    "boss_active": ".job-boss-info .boss-active-time",
    "address": ".location-address",
    "skills": ".job-keyword-list li",
    # ---- 工商信息：**就在详情页里，零额外请求** ----
    #
    # 2026-10-19 发现：详情页的 `.business-info-box` 直接带着 GSXT 照面字段。
    # 之前一直以为要单独请求 `/gongsi/{id}.html`，其实不用 —— 只是当时按
    # 「注册资本」去搜，而页面用的词是「**注册资金**」，所以一直搜到 0 次。
    #
    # 这块数据的价值：注册资本能直接筛掉皮包公司（实测某岗位对应公司仅 10 万）。
    # 按公司去重算，抓 `/gongsi/` 是每家 1 个请求；而这里跟着 JD 一起白拿。
    "reg_name": ".business-info-box .company-name",
    "reg_legal": ".business-info-box .company-user",
    "reg_founded": ".business-info-box .res-time",
    "reg_type": ".business-info-box .company-type",
    "reg_state": ".business-info-box .manage-state",
    "reg_capital": ".business-info-box .company-fund",
}

# 详情页 HTML 里内联的 `var _jobInfo = {...}`：job_id / user_id / job_name /
# job_salary / company / securityId。页面自己铸的 securityId 也在这里——
# 所以「securityId 被剪掉了」并不意味着这条路走不通。
_INLINE_JOBINFO_RE = re.compile(r"var\s+_jobInfo\s*=\s*\{(.*?)\}\s*;", re.S)
_INLINE_PAIR_RE = re.compile(r"(\w+)\s*:\s*'([^']*)'")
_INLINE_ISLOGIN_RE = re.compile(r"var\s+_userInfo\s*=\s*\{[^}]*?isLogin\s*:\s*(true|false)", re.S)


def _inline_jobinfo(html: str) -> dict:
    """解析内联 `_jobInfo` 的键值。只取单引号字符串，其它类型忽略。"""
    match = _INLINE_JOBINFO_RE.search(html or "")
    if not match:
        return {}
    return {key: value for key, value in _INLINE_PAIR_RE.findall(match.group(1))}


def _inline_is_login(html: str) -> bool | None:
    """详情页 HTML 里内联的 `_userInfo.isLogin`。读不到返回 None（不当作未登录）。"""
    match = _INLINE_ISLOGIN_RE.search(html or "")
    if not match:
        return None
    return match.group(1) == "true"


#: 详情页工商块（`.business-info-box`）取出来的文本带标签前缀，
#: 例如 `注册资金100万人民币`、`成立日期2019-10-30`。这里按标签剥掉。
_REG_LABELS = (
    ("reg_name", "公司名称", "公司名称"),
    ("reg_legal", "法定代表人", "法定代表人"),
    ("reg_founded", "成立日期", "成立时间"),
    ("reg_type", "企业类型", "企业类型"),
    ("reg_state", "经营状态", "经营状态"),
    # 页面写的是「注册资金」，而 GSXT 照面叫「注册资本」—— 统一成后者，
    # 与 `company_reg_json` 已有的 727 条保持同一套键名。
    ("reg_capital", "注册资金", "注册资本"),
)


def _company_reg_from_html(fields: dict) -> dict:
    """从详情页的工商块拼出 `company_reg_json` 结构。

    **零额外请求**：这些字段就在抓 JD 的同一个 SSR 文档里。
    2026-10-19 之前一直以为要单独请求 `/gongsi/{id}.html`，其实不用 ——
    当时按「注册资本」去搜而页面写的是「注册资金」，所以搜到 0 次、误以为没有。
    """
    reg: dict[str, str] = {}
    for key, label, canonical in _REG_LABELS:
        text = ((fields.get(key) or {}).get("text") or "").strip()
        if not text:
            continue
        # 只剥开头的标签，正文里出现同样的字不受影响。
        if text.startswith(label):
            text = text[len(label):].strip()
        if text:
            reg[canonical] = text
    # 至少要有注册资本或统一社会信用代码才算有效工商数据，避免把空块写进库。
    return reg if "注册资本" in reg else {}


# 风控页上的可见文案里，这些词说明「平台在说什么」。
_PROMPT_MARKS = ("异常", "验证", "登录", "限制", "封禁", "封", "频繁", "稍后",
                 "重试", "拒绝", "无权", "禁止", "安全", "解锁")
_TAG_RE = re.compile(r"<[^>]+>")
_DROP_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.S | re.I)
_SPACE_RE = re.compile(r"[\s\u3000]+")


def _page_prompt(html: str, limit: int = 120) -> str:
    """从风控 / 验证 / 封禁页里抠出**平台自己给的那句话**。

    为什么要读它：只看 URL 和字节数只能判出「大概是哪一类」，判不出
    「这次到底为什么」。而页面本来就写着原因 ——
    `您的IP地址存在异常行为`、`您的账号可能存在异常访问行为，完成验证后即可正常使用`、
    `访问过于频繁，请稍后再试`。这些原句比任何分类标签都更精确，也更能指导
    下一步该做什么（换出口？过滑块？重新登录？还是等冷却）。

    实现上刻意**不用选择器**：风控页每次改版都可能换 class，但文案几乎不变。
    剥掉 script/style 后取可见文本，挑第一句含风控词的返回。
    """
    text = _DROP_RE.sub(" ", html or "")
    text = _TAG_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if not text:
        return ""
    # 按中英文句末切句，挑含风控词的。
    for sentence in re.split(r"[。！？!?\n]", text):
        sentence = sentence.strip()
        if len(sentence) < 4:
            continue
        if any(mark in sentence for mark in _PROMPT_MARKS):
            # 没有标点的长串（压缩过的 HTML 文本）截到第一处分句边界，
            # 否则会把整页文字塞进异常消息。
            return sentence[:limit]
    # **没命中风控词就返回空串**：正常岗位页的首句是 JD 正文，把它当成
    # 「平台提示」写进异常消息只会误导。语义上 `prompt` = 平台在说哪里不对。
    return ""


#: 页面文案 → 停机类别。**这是最精确的一层判据** —— 平台自己写的原因
#: 比 URL 模式、字节数区间都更可靠，也直接告诉人下一步该做什么。
#:
#: ⚠️ 匹配前会把**空白全部去掉**再看。平台的实际文案是
#: 「您的 IP 存在异常行为」（IP 后有空格、且没有「地址」二字），
#: 而最初写成 `ip地址存在异常` 就永远匹配不上 —— 结果一个真封禁被判成了
#: `js_challenge`，白白多发了十几个请求。模式一律按「去掉空白后的子串」写。
_PROMPT_CATEGORY = (
    (("ip存在异常", "ip地址存在异常"), "network_restriction", 35),
    # 「账户存在异常」在不同页面配**不同的 code**：`verify.html` 上是 36
    # （要求滑块验证），`403.html` 上是 32（直接限制访问）。纯文本判不出是哪个，
    # 所以 code 留空 —— 由 URL 或 `code-block` 结构补。猜一个数只会误导。
    (("账户存在异常", "账号存在异常", "账号可能存在异常", "账户异常"),
     "account_restriction", None),
    (("环境存在异常", "环境异常"), "environment_restriction", 37),
    (("访问过于频繁", "操作过于频繁", "操作太频繁"), "rate_limited", None),
    (("重新登录", "请先登录", "登录已失效", "未登录"), "authentication_required", 7),
)


def _prompt_verdict(prompt: str) -> tuple[str, int | None] | None:
    """把页面文案翻成 (category, code)。认不出返回 None，交给其他判据。"""
    # 去掉所有空白再比：平台文案里 `IP 存在异常`、`IP地址存在异常` 都出现过。
    lowered = _SPACE_RE.sub("", (prompt or "").casefold())
    if not lowered:
        return None
    for marks, category, code in _PROMPT_CATEGORY:
        if any(mark in lowered for mark in marks):
            return category, code
    return None


#: 封禁页 `/web/passport/zp/403.html?code=N` 的结构：同一页里**内联了多个
#: `code-block`**，只有带 `show` 的那个生效。
#:
#: 2026-10-20 实测抓到的页面同时含 `id="code31"`（IP 受限）和
#: `id="code32" class="code-block show"`（账户受限）—— 只看文案会挑中第一个
#: 命中的句子，把「IP 异常」当成主因，实际生效的是**账户限制**。
_BLOCK_ID_RE = re.compile(r'id="code(\d+)"', re.I)
_EXPIRE_RE = re.compile(r'class="[^"]*\bexpire-time\b[^"]*"[^>]*>([^<]*)<', re.I)

#: code → (类别, 给人看的说明)。实测 `403.html` 里：31 是 IP 受限、32 是账户受限。
#: **这张表只是兜底** —— 优先按生效块自己的文案判（见 `_parse_block_page`），
#: 因为平台可能新增 code，而文案口径比编号稳定。
_BLOCK_CODES = {
    31: ("network_restriction", "IP 地址存在异常"),
    32: ("account_restriction", "账户存在异常行为，已被限制访问"),
    33: ("account_restriction", "账号访问受限"),
    34: ("account_restriction", "账号访问受限"),
    35: ("network_restriction", "IP 地址存在异常"),
    36: ("verification_required", "要求人工验证"),
    37: ("environment_restriction", "环境存在异常"),
}

_BLOCK_LABELS = {
    "network_restriction": "IP 地址存在异常",
    "account_restriction": "账户存在异常行为，已被限制访问",
    "environment_restriction": "环境存在异常",
    "rate_limited": "访问过于频繁",
    "authentication_required": "需要登录",
}


def _expire_to_iso(value: str | None) -> str:
    """把封禁页上的 `2026-10-21 00:01` 转成闸门能用的 ISO 串。

    解析不出来就返回空串（当成「没有明确期限」），**不猜时间** ——
    `normalise_resume_after` 遇到不可解析的值会抛错，那会让停机路径本身崩掉。
    """
    text = (value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).astimezone().isoformat(timespec="seconds")
        except ValueError:
            continue
    return ""


def _dump_abnormal_page(html: str, diag: dict, visible: str) -> str:
    """把异常页面的 HTML + 可见文本落盘，供人事后复核。

    为什么值得留：规则判错的时候（2026-10-20 那次封禁被判成 JS 挑战），
    光看 `request_log` 里的 `js_challenge` 根本查不出真相 ——
    真相在页面文字里。留着原件才能复盘，也才能给模型当样本。

    写盘失败只警告，不影响停机结论。
    """
    try:
        root = Path(__file__).resolve().parents[3] / "local" / "abnormal"
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        kind = str(diag.get("kind") or "unknown")
        stem = f"{stamp}-{kind}"
        (root / f"{stem}.html").write_text(html or "", encoding="utf-8")
        lines = [
            f"kind      : {kind}",
            f"category  : {diag.get('category')}",
            f"code      : {diag.get('code')}",
            f"evidence  : {diag.get('evidence')}",
            f"http      : {diag.get('status')}",
            f"bytes     : {diag.get('length')}",
            f"expire_at : {diag.get('expire_at')}",
            f"url       : {diag.get('url')}",
            f"prompt    : {diag.get('prompt')}",
            "",
            "---- 可见文本 ----",
            visible,
        ]
        (root / f"{stem}.txt").write_text("\n".join(lines), encoding="utf-8")
        return str(root / f"{stem}.txt")
    except Exception as exc:  # noqa: BLE001 - 落盘失败不该掩盖风控结论
        print(f"[PAGE-DUMP-FAIL] {type(exc).__name__}: {exc}", flush=True)
        return ""


def _parse_block_page(html: str) -> dict | None:
    """解析限制 / 封禁页，取出**生效的那个 code**、平台原话和恢复时间。

    没有 `id="codeN"` 结构时返回 None，交给其他判据 —— **不猜**。
    """
    text = html or ""
    marks = list(_BLOCK_ID_RE.finditer(text))
    if not marks:
        return None

    segments = []
    for index, match in enumerate(marks):
        tag_start = text.rfind("<div", 0, match.start())
        if tag_start < 0:
            continue
        tag_end = text.find(">", match.start())
        tag = text[tag_start:tag_end + 1] if tag_end > 0 else ""
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        segments.append({
            "code": int(match.group(1)),
            "active": bool(re.search(r'class="[^"]*\bshow\b', tag)),
            "html": text[tag_start:end],
        })
    if not segments:
        return None

    chosen = next((s for s in segments if s["active"]), segments[0])
    raw = chosen["html"]
    expire = _EXPIRE_RE.search(raw)
    visible = _SPACE_RE.sub(" ", _TAG_RE.sub(" ", _DROP_RE.sub(" ", raw))).strip()
    # **优先用生效块自己的文案判类别**，编号只作兜底：平台新增 code 时，
    # 文案口径（「IP 存在异常」/「账户存在异常」）仍然可信。
    by_text = _prompt_verdict(visible)
    if by_text:
        category = by_text[0]
        label = _BLOCK_LABELS.get(category, "访问受限")
    else:
        category, label = _BLOCK_CODES.get(
            chosen["code"], ("account_restriction", "访问受限"))
    return {
        "code": chosen["code"],
        "category": category,
        "label": label,
        "expire_at": (expire.group(1).strip() if expire else ""),
        "message": visible[:200],
        "all_codes": [s["code"] for s in segments],
    }



# 详情页读到空正文时的归因标记。
_CLOSED_MARKS = ("职位已关闭", "该职位已下线", "岗位已下线", "已停止招聘",
                 "职位不存在", "该职位不存在", "页面不存在", "该页面不存在",
                 "已结束招聘", "职位已失效")
_VERIFY_MARKS = ("拖动滑块", "请完成验证", "安全验证", "验证后继续")


def _is_challenge_url(url: str) -> bool:
    """当前地址是不是风控校验页（JS 安全挑战 / 滑块验证）。"""
    lowered = (url or "").casefold()
    return ("/web/passport/zp/security.html" in lowered
            or "/web/passport/zp/verify.html" in lowered
            or "safe-validate" in lowered)


def _ssr_blank_reason(html: str, final_url: str, status: int, judge=None) -> dict:
    """详情页没有 `.job-sec-text` 时，**说清是哪一种空**。

    为什么值得单独做：2026-10-19 一轮校准有 5 条失败，原来的错误信息一律是
    「岗位可能已下线或页面改版」—— 把四件完全不同的事糊成一句：

      · 平台要人工验证（风控信号，必须停）
      · 岗位真的下线了（正常，不算问题）
      · 未登录只能看预览（账号问题，必须停）
      · 我们的选择器失效了（我们自己的 bug）

    糊在一起的代价是双向的：真下线被当成风控而白白停轮，风控被当成下线而继续
    加压，选择器失效则长期无声地丢正文。所以这里把证据带回异常消息里。

    ## 两套判据，规则在前、模型兜底

    规则（本函数下半部分）处理**结构性、零成本**的判据：URL 模式、字节数分档、
    `code-block` 结构。它们对已知页面又快又准。

    `judge` 是一个可选回调（签名 `judge(text, url, size, status) -> dict`），
    由 `job_analysis.page_state.classify_page` 提供。**当规则拿不准时调用它** ——
    平台改文案、换编号、上新页面，规则一定会漏，而模型读得懂新写法。

    2026-10-20 那次事故就是规则漏判的典型：封禁页 `403.html?code=32` 不在
    `_is_challenge_url()` 里，字节数又落进 JS 挑战页区间，于是被判成
    「读早了，等 2 秒会自愈」，对一个已封禁的账号又发了十几个请求。

    ⚠️ 归因顺序是刻意的：**地址栏优先于正文关键词**。`verify` 出现在 URL 上是
    平台把我们送去了验证页，这是硬证据；而"安全验证"四个字可能只是页面脚本里的
    一句文案。反过来判会把正常岗位误判成风控 —— 而误判风控要人工解冻，代价很高。
    """
    text = html or ""
    lowered = text.casefold()
    url_l = (final_url or "").casefold()
    # ⚠️ 必须用**字节数**：历史标定的四档（下架 ~11KB / JS 挑战 ~41KB /
    # 验证页 ~44.5KB / 正常页 138–147KB）都是响应体字节数。中文页面
    # `len(str)` 只有字节数的三分之一左右，拿字符数去比阈值会把 41KB 的
    # 挑战页判成 14K 字符 → 落进 `selector_miss`（当成我们自己的 bug）。
    size = len(text.encode("utf-8", "ignore"))
    # 页面自己给的文案 —— 比任何分类标签都精确，也直接指导下一步该做什么。
    # 空正文归因时一并带出去，异常消息里就能看到「平台原话」。
    base = {"status": status, "length": size, "url": final_url or "",
            "prompt": _page_prompt(html)}

    # **最高优先级：限制页 / 封禁页。**
    #
    # `/web/passport/zp/403.html?code=32` 这类页面里内联了多个 `code-block`，
    # 只有带 `show` 的那个生效。它既不在 `_is_challenge_url()` 里（那三个是
    # security/verify/safe-validate），字节数又恰好落在 JS 挑战页的区间，
    # 于是**一个真封禁被按字节数判成了 `js_challenge`** —— 2026-10-20 实测：
    # 页面明写「您的账户存在异常行为，已暂时被限制访问！将于 2026-10-21 00:01
    # 恢复正常」，我们却当成"读早了，等 2 秒会自愈"，继续发了十几个请求。
    #
    # 这条必须放在 `_prompt_verdict` 之前：`_page_prompt` 只取「第一句含风控词的
    # 话」，而这一页第一句是 code31 的「IP 存在异常」，会把主因判成 IP 限制，
    # 掩盖真正的账号限制。
    block = _parse_block_page(html)
    if block:
        kind = {"account_restriction": "account_restricted",
                "network_restriction": "ip_restricted",
                "environment_restriction": "environment_restricted",
                "verification_required": "verification",
                }.get(block["category"], "banned")
        return {**base, "kind": kind, "category": block["category"],
                "code": block["code"], "evidence": f"block:{block['code']}",
                "prompt": block["message"], "expire_at": block["expire_at"],
                "label": block["label"], "all_codes": block["all_codes"]}

    # 其次看页面自己说什么。`IP存在异常` 就是网络限制，`账户存在异常` 就是
    # 账号限制 —— 比按 URL 或字节数猜准得多。只改判据来源，不改「撞墙即停」的语义。
    verdict = _prompt_verdict(base.get("prompt", ""))
    if verdict:
        category, code = verdict
        kind = {"network_restriction": "ip_restricted",
                "account_restriction": "account_restricted",
                "environment_restriction": "environment_restricted",
                "rate_limited": "rate_limited",
                "authentication_required": "login_wall"}.get(category, "risk")
        return {**base, "kind": kind, "category": category, "code": code,
                "evidence": "page_prompt"}

    if any(token in url_l for token in ("verify", "safe-validate", "security-check")):
        return {**base, "kind": "verification", "evidence": "url"}
    # `/web/passport/zp/security.html` 是**风控阶梯的第二级**：JS 安全挑战，
    # 不是硬拦。要和"滑块验证"分开 —— 混为一谈会让我们要么对它过度反应
    # （白停一轮），要么把它当成自己的 bug（继续加压）。
    # 判据沿用 `service_jobs.page_session_check`：有验证控件才算要人，
    # 没有就是可自愈的 JS 挑战。
    if "/web/passport/zp/security.html" in url_l or "/zp/security.html" in url_l:
        hit = next((m for m in _VERIFY_MARKS if m in lowered), None)
        return {**base, "kind": "verification" if hit else "js_challenge",
                "evidence": hit or "security.html"}
    # 正文里出现验证控件**且整页很短**才认：长页面里的"安全验证"多半是页脚文案。
    if size < 8000:
        hit = next((m for m in _VERIFY_MARKS if m in lowered), None)
        if hit:
            return {**base, "kind": "verification", "evidence": hit}
    # ⚠️ **不要在这里判登录墙**。曾经有过一版扫 `登录/注册`、`请先登录` 这类文案，
    # 结果误报：页头本来就有「登录/注册」入口，任何空页都能命中 —— 而误判登录墙
    # 会触发 `PlatformRiskStop` → 持久熔断 → 要人工解冻，代价极高。
    # 真正的登录判定是 `_inline_is_login()` 读页面内联的 `_userInfo.isLogin`，
    # 它在 `_collect_ssr_detail` 里**更早**就已经执行过，是结构化的硬信号。
    hit = next((m for m in _CLOSED_MARKS if m in lowered), None)
    if hit:
        return {**base, "kind": "closed", "evidence": hit}
    if "job-sec-text" not in text:
        # 规则到这里都认不出来 —— **这正是让模型插手的时刻**。
        # 字节数分档只是历史的经验区间，平台换一次版式就失效；而模型读得懂
        # 「您的账户存在异常行为，已暂时被限制访问！」这种没见过的新写法。
        #
        # 只在「没有正文容器」这条路上调用：真有 `job-sec-text` 就不必花模型调用，
        # 而正常下线的岗位会先在 `_CLOSED_MARKS` 那步被拦掉。
        if judge is not None:
            judged = _judge_page(judge, text, final_url, size, status)
            if judged:
                return {**base, **judged}
        # 按**字节数**兜一层。历史标定（boss-scrape-layer 记忆）给了四档：
        #   下架 ~11KB / JS 挑战 ~41KB / 验证页 ~44.5KB / 正常岗位页 138–147KB
        # 2026-10-19 实测的 security.html 是 42,825 字节，正落在挑战页区间。
        #
        # 这一层是必要的：挑战页有时**直接回在 job_detail 的 URL 上**，地址栏看不
        # 出来，正文里也不一定有「拖动滑块」，只看 URL 会把它误判成我们自己的
        # 选择器失效（`selector_miss`）——那会让一个真实的风控信号被记成 bug。
        if 25000 <= size <= 62000:
            return {**base, "kind": "js_challenge", "evidence": f"size:{size}"}
        # 连容器都没有：页面很长说明结构变了，很短说明压根不是详情页。
        kind = "selector_miss" if size > 4000 else "empty_page"
        return {**base, "kind": kind, "evidence": "no-container"}
    # 容器在、里面是空的：岗位在线但正文为空（少数岗位确实不写 JD）。
    return {**base, "kind": "blank_jd", "evidence": "empty-container"}


#: 模型判出的页面状态 → `_ssr_blank_reason` 的 kind。
_JUDGE_KIND = {
    "normal_content": "selector_miss",
    "job_closed": "closed",
    "job_not_found": "closed",
    "login_required": "login_wall",
    "verification_required": "verification",
    "rate_limited": "rate_limited",
    "account_restricted": "account_restricted",
    "ip_restricted": "ip_restricted",
    "environment_flagged": "environment_restricted",
    "page_changed": "selector_miss",
    "unknown": "unknown_page",
}


def _judge_page(judge, text: str, url: str, size: int, status: int) -> dict | None:
    """调用模型回调判页，返回可并入归因字典的字段。

    **模型失败一律返回 None，退回规则判据。** 不能把「模型没答上来」
    当成「页面没问题」—— 那等于给一个可能已封禁的账号开了继续请求的口子。

    `judge` 的签名是 `judge(text, url, size, status) -> {state, action, category, ...}`，
    由 `job_analysis.page_state.classify_page` 适配而来（见 `build_collector`）。
    """
    try:
        result = judge(text, url, size, status) or {}
    except Exception as exc:  # noqa: BLE001 - 判页失败必须退回规则而不是放行
        print(f"[JUDGE-FAIL] {type(exc).__name__}: {str(exc)[:160]}；退回规则判据",
              flush=True)
        return None
    state = str(result.get("state") or "").strip()
    if state not in _JUDGE_KIND:
        return None
    kind = _JUDGE_KIND[state]
    verdict = {
        "kind": kind,
        "evidence": f"llm:{state}",
        "llm_state": state,
        "llm_action": result.get("action") or "stop",
        "llm_confidence": result.get("confidence"),
    }
    category = result.get("category")
    if category:
        verdict["category"] = category
    return verdict


# 必须是 jobs（带 s）。`/web/geek/job` 会 302 到 `/web/geek/jobs`，多一跳；
# 2026-08-10 标定实测：code=37 重算后回搜索页那次导航正好撞上这个 302 而超时
# （Navigation to ".../web/geek/job?..." interrupted），把重算后本该成功的详情
# 重试白白废掉一条配额。直接写终态 URL，省一跳也不再被 302 打断。
SEARCH_BASE = f"{ORIGIN}/web/geek/jobs"

# 同源请求使用绝对地址，发送前核对页面来源。持续空白或关闭是浏览器异常，
# 不能据此认定账号风控，也不能通过反复导航将它当作已恢复。
# 31/37 来自公开项目实测；36「您的账户存在异常行为」是本地实测命中的账号级风控；
# 风控码表与关键字兜底的**唯一真源**在 `boss_scrape.errors`（见那里的注释）：
# 抓取层按它决定抛哪种异常，采集链路按它决定熔断与账本 outcome。
# 两边各写一份时已经漂过（一边把 36 当限速退避、另一边当封禁），所以别再复制。


RISK_DUMP_DIR = Path("data/risk_dumps")

# 一次 code=37 重算折算多少次请求。**不是 1**（2026-08-30 实测推翻）。
#
# 重算发两次导航（`security-check.html` + 回一个同域页），逐步优化的实测轨迹：
#   · 无拦截 + 回搜索页        ：发起 226 次
#   · 加资源拦截 + 回搜索页    ：真实上网 **139** 次（拦截只砍 39%）
#   · 加资源拦截 + 回 /about   ：真实上网 **37** 次  ← 现状，再省 73%
#
# 关键在**回哪个页**，不在拦什么：回搜索页要加载整个 SPA（107 次），
# 而 `_evaluate_fetch` 只要求页面停在 zhipin 域，不依赖搜索页的 DOM。
# 换成 `/about` 后那 107 次变成 1 次。
#
# 剩下的 37 次里 script 30 是 `security-check.html` 自己的挑战脚本 ——
# **这部分不可压缩**，stoken 就是靠它算出来的。所以 37 已接近这条路的下限；
# 想再低只能不走导航，而 stoken 必须由真实浏览器 JS 生成（本地算已证死路）。
#
# **改动拦截规则或回法就要重新量。**
# 探针 `scripts/probe_recover_cost.py` + `probe_recover_callback.py`，
# 数据 `data/calibration/recover-cost*.json`。
REQ_PER_RECOVER = 37


class RiskControlStop(RuntimeError):
    """A collection operation stopped by a local limit or platform response."""


class PlatformRiskStop(RiskControlStop):
    """An actual platform response that can require interactive verification."""

    def __init__(self, message: str, *, code: int | None = None,
                 http_status: int | None = None, category: str | None = None,
                 request_url: str = ""):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.category = category or response_condition(code, http_status, message)
        self.request_url = request_url


class BrowserPageUnavailable(RiskControlStop):
    """The browser page cannot support collection; this is not an API risk code."""


def is_boss_origin(url: str) -> bool:
    """Check the actual origin, without accepting a matching query or lookalike host."""
    try:
        location = urlsplit(url)
        return location.scheme == "https" and location.netloc == "www.zhipin.com"
    except ValueError:
        return False


# 浏览器对 DNS / 连接 / TLS 失败的统一报法。**与账号状态无关**，重试即可自愈。
# 风控走的是另一条路（HTTP 200 + 验证页正文 → _dump_risk_body → RiskControlStop），
# 永远不会命中这里，所以在这里重试不会掩盖风控信号。
_TRANSIENT_FETCH_ERRORS = (
    "Failed to fetch",        # Chromium：连接失败的通用报法
    "NetworkError",           # 部分场景下的等价报法
    "Load failed",            # WebKit 同义
    "ERR_NETWORK_CHANGED",    # 切网/IP 变化
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_NAME_NOT_RESOLVED",  # DNS 抖动（TUN 环境下常见）
    "ERR_INTERNET_DISCONNECTED",
    "ERR_TIMED_OUT",
)


def _is_transient_fetch_error(result: dict) -> bool:
    """页面内 fetch 是否因**瞬时网络问题**失败（可重试），而非风控或业务错误。

    判据只看 `error` 字段：有 error 说明 fetch 抛了异常、连响应都没拿到。
    风控与业务错误都是正常响应（有 status 和 text），不会走到这里。
    """
    err = str(result.get("error") or "")
    return bool(err) and any(sig in err for sig in _TRANSIENT_FETCH_ERRORS)


FILTER_CODES = {
    "salary": {
        "3K以下": "402", "3-5K": "403", "5-10K": "404", "10-20K": "405",
        "20-50K": "406", "50K以上": "407",
    },
    "experience": {
        "在校生": "108", "应届生": "102", "经验不限": "101", "1年以内": "103",
        "1-3年": "104", "3-5年": "105", "5-10年": "106", "10年以上": "107",
    },
    "degree": {"初中及以下": "209", "中专/中技": "208", "高中": "206", "大专": "202", "本科": "203", "硕士": "204", "博士": "205"},
    "scale": {"0-20人": "301", "20-99人": "302", "100-499人": "303", "500-999人": "304", "1000-9999人": "305", "10000人以上": "306"},
    "stage": {"未融资": "801", "天使轮": "802", "A轮": "803", "B轮": "804", "C轮": "805", "D轮及以上": "806", "已上市": "807", "不需要融资": "808"},
    # 求职类型（接口参数 jobType）。1901 是「全职」，从 URL 实测确认。
    # 2026-10-01 normal-page filter/conditions response: 1902=实习, 1903=兼职.
    "jobType": {"全职": "1901", "兼职": "1903", "实习": "1902"},
}


class BossCollector(Collector):
    def __init__(self, config: CollectionConfig):
        self.config = config
        self.browser_session = BrowserSession(config.browser)
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.request_count = 0
        # 重算次数。⚠️ 历史注释里"高频重算会升级到 32 封禁"这个机制**从未被验证**
        # （反例：156 条 1 次重算就被封、108 条 21 次重算零封禁），所以它现在
        # 只用于统计与日志，不参与任何放行/停止判断。
        self.code37_count = 0
        # code=37 熔断阈值。2026-08-10/11 标定：code=37 是每约 6 条一次的配额窗口
        # 信号（间距实测全程稳定 6.0），所以抓 N 条详情正常会触发约 N/6 次。
        #
        # ⚠️ 这个阈值**不是封禁预警**：code=37 密度不随累计量上升（无记忆滑动窗口），
        # 而封禁与 code=37 频率无关——08-03 实测「156 条仅 1 次 code=37 就直接
        # code=32」，另一次「108 条 21 次 code=37 零封禁」。它只是"跑了多少量"的
        # 代理指标，作用是防止一轮跑太久。
        # 从 config 读，激进探测时可放开（config.aggressive.yaml 设到 2000）。
        self.code37_limit = getattr(config, "code37_limit", 35)
        self.last_lid = ""
        self.owns_page = False
        # 登录态观察窗口。`start()` 会重新置空，但**必须在 __init__ 里先定义**：
        # `_require_detail_login()` 直接读这两个属性，如果调用方没走 start()
        # （比如单测直接调详情解析，或将来新增的入口），拿到的是 AttributeError，
        # 而不是「没有观察记录 → 重新问一次服务端」这个本来正确的分支。
        # 这不是为了测试而加的：属性在方法里被读，就应该在构造时存在。
        self._session_response = None
        self._session_observed_at = ""
        self._detail_access_evidence: dict = {}
        # 本轮是否已经完成过一次搜索页导航。一次运行有 18 个「城市×关键词」查询，
        # 导航的真正作用是重铸 stoken（实测一个窗口约 5 个请求），不是"打开搜索页"。
        self._search_ready = False
        # 距上次重铸发过几个接口请求。窗口按**请求数**算，不按查询数算。
        self._requests_since_remint = 0
        # stoken 滚动器。开着它时"续窗口"靠就地重算（0 个请求），
        # 导航只在页面不可用时才做；关掉则退回旧的"每 N 个请求导航一次"。
        self._stoken_roller: StokenRoller | None = None
        # 滚动最近一次是否**真的成功**。只有它成立，窗口才算"已续上"；
        # 滚动器存在但一直失败（种子缺失、签名脚本拉不到）时必须退回按请求数导航，
        # 否则窗口会悄悄耗尽，下一个请求就撞 37。这是有意的降级路径。
        self._stoken_roll_ok = False
        self.last_roll = None
        # 已有 JD 的 canonical_id：详情接口是最贵资源（封禁红线 150-200 条），
        # 由 runner 从库里灌进来，命中即跳过详情，配额只花在真正缺 JD 的岗位上。
        self.skip_detail_ids: set[str] = set()
        self.detail_skipped = 0
        # 请求账本。**可选**：注入后每次请求落一行，用于回答"今天一共发了多少"；
        # 不注入（或写失败）只影响观测，不影响采集是否继续（见 `_log_request`）。
        self.ledger = None  # type: ignore[var-annotated]
        self.ledger_script = ""
        #: 账本回填失败次数（账本没有 mark_outcome / 回填抛异常）。
        #: 仅作观测计数与 `[LEDGER-FAIL]` 日志，**不再拦请求**。
        self._ledger_failures = 0
        # 访问策略闸门（持久化的"能不能联网"）。构造它只是记下状态文件路径，
        # 真正读盘发生在 require_online()。允许调用方在 start() 之前注入自己的
        # 闸门（脚本用自己那个库的目录，测试用 tmp_path）。
        self.gate: AccessGate | None = None
        #: 仅作历史/观测字段：`start()` 走完即置 True。
        #: ⚠️ **不参与任何判断** —— 每次请求都会无条件复查策略（见 `_require_online`），
        #: 因为"直接把 page 塞进来"的使用者可能是真实脚本（探针/手工脚本都这么干），
        #: 给替身留一条跳过路径就等于给生产留了一条。
        self._gate_armed = False
        # 本轮各类请求计数（标定用：区分 list / detail / security_check 的风险差异）
        self.kind_counts: dict[str, int] = {}

    def _policy_gate(self) -> AccessGate:
        """**读**策略用的闸门：显式注入的优先，其次账本库所在目录，最后配置里的库路径。

        读操作只碰 `access-policy.json`，猜错目录最多是"读到另一套策略"；
        写操作不能用这个（见 `_pause_gate`）。
        """
        if self.gate is not None:
            return self.gate
        base = getattr(self.ledger, "path", None) or getattr(self.config, "database_path", "")
        return AccessGate(base or ".")

    def _pause_gate(self) -> AccessGate | None:
        """**写**熔断用的闸门：认不出这是哪个库时返回 None，**不猜目录**。

        猜错的后果不是"多写一个文件"，而是把冻结写到**别的库**的策略上
        （离线测试就是最典型的情形：一个没注入账本的替身会把 paused 写进真实
        `data/access-policy.json`，于是真环境在下一次运行时被凭空拦住）。
        认不出来时的正确动作是：照样停（RiskControlStop/直接抛），但不落盘。
        """
        if self.gate is not None:
            return self.gate
        base = getattr(self.ledger, "path", None)
        if base is None:
            return None
        self.gate = AccessGate(base)
        return self.gate

    def _online_enabled(self) -> bool:
        """`CollectionConfig.network_enabled`，缺字段按**关闭**处理（默认拒绝）。"""
        check = getattr(self, "online_check", None)
        if check is not None:
            return bool(check())
        return bool(getattr(self.config, "network_enabled", False))

    def _require_online(self) -> None:
        """联网前置检查：`network_enabled` 为真**且**持久策略是 ready，否则抛 AccessBlocked。

        **每次请求、每次导航之前都要调**（`_evaluate_fetch` / `_goto_search` /
        `start`）。没有"离线替身就跳过"的开关：直接注入 page 的调用也可能是真实
        脚本（探针、手工脚本都这么干），给测试留一条跳过路径就是给生产留一条 ——
        冻结期唯一可信的判据是策略文件本身。测试要离线跑，就注入一个 tmp 目录的
        `AccessGate`（见 `tests/test_strict_budget.py` 的 `make_collector`）。
        """
        self._policy_gate().require_online(self._online_enabled())

    async def start(self) -> None:
        # ⚠️ 顺序是硬要求：持久策略放行（network_enabled + access-policy.json=ready）
        # 必须在**起浏览器之前**。冻结期"只想打开看一眼"也不行 —— 浏览器一起来，
        # 前端自己就会发请求，那些请求不经过 `_log_request`，也就绕过了观测。
        self._require_online()
        self.context = await self.browser_session.start()
        # 优先复用浏览器里已经登录并加载好的 zhipin 标签页。
        # 实测直接打开 https://www.zhipin.com/ 会经 /{城市}/?seoRefer=index 最终跳到
        # about:blank，新开的空白页反而进不去；复用已有页签最稳。
        for existing in self.context.pages:
            if is_boss_origin(existing.url or ""):
                self.page = existing
                self.owns_page = False
                break
        if self.page is None:
            self.page = await self.context.new_page()
            self.owns_page = True
        self.page.set_default_timeout(self.config.browser.navigation_timeout_ms)
        self._session_response = None
        self._session_observed_at = ""
        self._detail_access_evidence = {}
        self.page.on("response", self._remember_session_response)
        self._native_detail_responses = []
        if self.config.list_source == "native":
            self.page.on("response", self._remember_native_detail)
        # 从这里开始，每次 fetch 前复查闸门：长跑期间策略可能被人工改成 paused
        # （有人在巡检时发现了风控，或账号状态变了），本轮要当场停下。
        self._gate_armed = True
        # stoken 滚动器：宿主页是 /robots.txt（同源、极小、不发接口请求）。
        # 起不来不致命——roll() 会降级，采集退回"按请求数导航重铸"。
        if getattr(self.config, "stoken_roll", False):
            self._stoken_roller = StokenRoller(self.context, origin=ORIGIN)
            await self._stoken_roller.start()
            if self._stoken_roller.last_error:
                print(f"[STOKEN] 宿主页未就绪，滚动将降级：{self._stoken_roller.last_error}",
                      flush=True)

    async def close(self) -> None:
        # 只关闭自己开的页签，不动用户原有的登录页。
        try:
            if self.page:
                self.page.remove_listener("response", self._remember_session_response)
            if self.page and self.config.list_source == "native":
                self.page.remove_listener("response", self._remember_native_detail)
            if self.page and self.owns_page:
                await self.page.close()
            if self._stoken_roller is not None:
                roller = self._stoken_roller
                # 让操作者能一眼看出"续窗口"到底靠的是滚动还是导航。
                # 滚动次数 ≈ 成功的接口请求数；失败次数 > 0 说明退回了导航重铸。
                note = (f"，最后一次失败：{roller.last_error}"
                        if roller.fail_count else "")
                print(f"[STOKEN] 滚动 {roller.roll_count} 次，失败 "
                      f"{roller.fail_count} 次{note}", flush=True)
                await roller.close()
        finally:
            await self.browser_session.close()

    async def _goto_search(self, url: str, attempts: int = 1, *, already_paced: bool = False) -> None:
        """Navigate once, then wait for the page rather than repeatedly reopening it.

        ``attempts`` remains accepted for existing callers. A persistently blank or
        closed page stops collection independently of platform response codes.
        """
        assert self.page is not None
        self._require_online()
        if self.page.is_closed():
            raise BrowserPageUnavailable("浏览器岗位页已关闭，任务停止；请检查当前浏览器会话")
        pace = getattr(self, "pace", None)
        if pace is not None and not already_paced:
            await pace("list")
        self._require_online()
        rowid = self._log_request(url)
        self.request_count += 1
        self._session_response = None
        self._session_observed_at = ""
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded")
            status = response.status if response else None
        except BaseException as exc:
            self._finish_request(rowid, "error")
            if isinstance(exc, PlaywrightError) and self.page.is_closed():
                raise BrowserPageUnavailable("导航期间岗位页已关闭，任务停止；请检查浏览器兼容性") from exc
            raise
        if status in (401, 403, 429):
            self._trip_risk(rowid, f"搜索页返回 HTTP {status}，需人工核对", code=None, outcome="risk", http_status=status)
        try:
            # Allow the normal page initialization to complete before checking its origin.
            await asyncio.sleep(random.uniform(2.5, 4.0))
            await self._settle_on_origin()
        except BaseException:
            self._finish_request(rowid, "error", status)
            raise
        self._finish_request(rowid, "navigation", status)

    @staticmethod
    def _filter_value(name: str, value: str | None) -> str | None:
        """把筛选中文标签映射成接口码；逗号分隔的多选逐个映射后拼接。

        BOSS 多选筛选在 URL 里是逗号分隔（实测 experience=101,104 生效），
        配置里写「经验不限,1-3年」即可。找不到映射的原始值原样透传，
        方便直接写码。
        """
        if not value:
            return None
        mapping = FILTER_CODES.get(name, {})
        if "," in value:
            return ",".join(mapping.get(part.strip(), part.strip()) for part in value.split(","))
        return mapping.get(value, value)

    def _params(self, city: CityConfig, keyword: str, filters: SearchFilters, page: int) -> dict[str, str | int]:
        if not city.boss_code:
            raise ValueError(f"城市 {city.name} 缺少 boss_code")
        params: dict[str, str | int] = {
            "scene": "1",
            "query": keyword,
            "city": city.boss_code,
            "page": page,
            "pageSize": self.config.page_size,
        }
        for name, value in filters.model_dump().items():
            code = self._filter_value(name, value)
            if code:
                params[name] = code
        return params

    async def _settle_on_origin(self, timeout_ms: int = 15_000) -> None:
        """Wait for a usable origin; persistent blank/closed pages stop the task."""
        assert self.page is not None
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.page.is_closed():
                raise BrowserPageUnavailable("浏览器岗位页已关闭，任务停止；请检查当前浏览器会话")
            if is_boss_origin(self.page.url or ""):
                return
            await asyncio.sleep(0.25)
        location = (self.page.url or "").split("?")[0].split("#")[0]
        raise BrowserPageUnavailable(
            f"浏览器岗位页持续空白或跳转，当前地址：{location}；任务停止，请检查浏览器兼容性")

    @staticmethod
    def _kind_of(url: str) -> str:
        """按 URL 判断请求类型。风险差异很大：详情接口几乎承担了全部风控。"""
        # SSR 详情页（`/job_detail/{id}.html`）也是详情，必须归到 `detail`。
        # 漏掉它的后果是**观测失真**：2026-10-19 实测，SSR 路径的详情请求
        # 全被记成 `list`，于是"详情占多少请求""code=37 浪费在哪类请求上"
        # 这两类分析对 SSR 路径全部失效。只影响记账归类，不影响 pacing
        # （pacing 由调用点显式传 "detail"）。
        if DETAIL_PATH in url or "/job_detail/" in url:
            return "detail"
        if "security-check" in url:
            return "security_check"
        if "/recommend/" in url:
            return "recommend"
        return "list"

    def _count_request(self, url: str, outcome: str = "ok", code: int | None = None) -> None:
        """内存计数 + 记一行账（观测路径，只给标定/既有测试直接调用）。

        正常请求路径不要用它：`_evaluate_fetch` / `_native_list` 走 `_log_request()`。
        账本是**可选观测**：写失败只打 `[LEDGER-FAIL]` 并计入 `_ledger_failures`，
        不会拦下一次请求。
        """
        kind = self._kind_of(url)
        self.kind_counts[kind] = self.kind_counts.get(kind, 0) + 1
        if self.ledger is None:
            return
        try:
            self.ledger.log_request(
                kind, outcome=outcome, code=code, script=self.ledger_script
            )
        except Exception as exc:  # noqa: BLE001
            self._ledger_failures += 1
            print(f"[LEDGER-FAIL] 记账失败（{type(exc).__name__}: {exc}）；"
                  f"仅影响观测，采集继续。", flush=True)

    def _pace_signal(self, name: str, value=None) -> None:
        """把观察到的信号回传给 pacer（内容长度 / 挑战命中 / 成功）。

        约定：`pace_signal` 是**可选**注入点（和 `pace` 一样用 getattr 取）。
        没有注入时静默跳过 —— 它只影响节奏的"形状"，不该影响采集能否进行。
        """
        hook = getattr(self, "pace_signal", None)
        if hook is None:
            return
        try:
            hook(name, value)
        except Exception:  # noqa: BLE001 - 信号失败不影响采集
            pass

    def _log_request(self, url: str) -> int:
        """**记账**：先记内存计数，再往账本落一行；返回该行 id（无账本时 0）。

        只观测，不拦：没有账本返回 0，写失败也只是计数 + 日志，
        请求照发。真正会停的只有持久策略闸门与平台风控响应。
        """
        kind = self._kind_of(url)
        self.kind_counts[kind] = self.kind_counts.get(kind, 0) + 1
        if self.ledger is None:
            return 0
        try:
            return int(self.ledger.log_request(kind, script=self.ledger_script))
        except Exception as exc:  # noqa: BLE001
            self._ledger_failures += 1
            print(f"[LEDGER-FAIL] 记账失败（{type(exc).__name__}: {exc}）；"
                  f"仅影响观测，采集继续。", flush=True)
            return 0

    def _finish_request(self, rowid: int | None, outcome: str,
                        code: int | None = None) -> bool:
        """回填该行的最终结果，返回是否**成功**记上。**从不抛异常。**

        只改 `rowid` 那一行（不是 `MAX(id)`）：并发时最后一行可能是别人的，
        把风控结论写到无关请求上，等于真封禁在账上被记成 ok。

        账本是**可选观测**：没有 `mark_outcome`、或回填抛异常 → 计数 +
        `[LEDGER-FAIL]` 日志，仅此而已，**不会影响后续请求**。
        ⚠️ 本方法刻意不抛：风控分支要先落地持久熔断，若这里因回填失败抛出去，
        `gate.pause()` 就永远不会被执行 —— 冻结丢失比一行账没写上严重得多。
        """
        if not rowid:
            return True
        if self.ledger is None:
            return False
        mark = getattr(self.ledger, "mark_outcome", None)
        if mark is None:
            self._ledger_failures += 1
            print(f"[LEDGER-FAIL] 账本 {type(self.ledger).__name__} 没有 mark_outcome，"
                  f"第 {rowid} 行的结论（{outcome}）无法回填；仅影响观测，采集继续。",
                  flush=True)
            return False
        try:
            ok = bool(mark(rowid, outcome, code))
        except Exception as exc:
            ok = False
            print(f"[LEDGER-FAIL] 回填第 {rowid} 行失败：{type(exc).__name__}: {exc}；"
                  f"仅影响观测，采集继续。", flush=True)
        if not ok:
            self._ledger_failures += 1
        return ok

    async def _evaluate_fetch(self, url: str, attempts: int = 3) -> dict:
        """在页面里发同源请求。

        BOSS 的安全校验会在加载后触发一次跳转，正好撞上 evaluate 时 Playwright 会抛
        "Execution context was destroyed"。这不是风控，等页面稳定后重试即可。
        token 在 Python 侧持有（会话内稳定），避免页面刷新把 _PAGE.token 重置。
        """
        assert self.page is not None
        # BOSS 对不带 token 头的请求给极低配额（实测约 12-13 次/窗口），页面自己的
        # 请求带齐 token/zp_token/traceId 所以能持续跑。token 的合法来源是
        # window._PAGE.token（登录后由 __zp_stoken__ 派生）。zp_token 来自 cookie bst，
        # fetch credentials:'include' 会自动带。traceId 每次请求重新生成，这里暂不构造。
        # ⚠️ 2026-08-10 两处修复：
        #
        # ① headers 必须嵌在 init 的 `headers` 键里。原先写成 `fetch(url, headers)`
        #    （headers 当 init 顶层字段）—— fetch 只认 method/headers/body/credentials
        #    等白名单键，Accept/token/X-Requested-With 全被静默丢弃。`credentials`
        #    恰好是合法键，所以 cookie 一直带上、请求照样成功，把 bug 完全掩盖。
        #    本地 http server 实测：旧写法服务端收到 token=undefined、accept=*/*。
        #
        # ② 被动监听前端真实请求后改用正确的头集合。**前端一个 `token` 头都不发**
        #    （8 个 wapi 请求全部 token=<无>），它发的是 `zp_token`（= cookie bst 的
        #    值，前端手动复制到头里）+ `x-requested-with` + `traceid`。所以旧代码执着
        #    的 window._PAGE.token 方向本身就是错的——而且 _PAGE 在当前前端版本里
        #    根本不存在（hasPage: false）。
        #    bst 不是 HttpOnly，JS 可直接从 document.cookie 读。
        destination = urlsplit(url)
        if destination.scheme != "https" or destination.netloc != "www.zhipin.com":
            raise ValueError("采集器只允许向 BOSS 同源 HTTPS 接口发送请求")
        script = """async ({url, timeout}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeout);
            try {
                const readCookie = (name) => {
                    const m = document.cookie.match(
                        new RegExp('(?:^|; )' + name + '=([^;]*)'));
                    return m ? decodeURIComponent(m[1]) : '';
                };
                const headers = {
                    'Accept': 'application/json, text/plain, */*',
                    'X-Requested-With': 'XMLHttpRequest',
                };
                const bst = readCookie('bst');
                if (bst) headers['zp_token'] = bst;
                const response = await fetch(url, {credentials: 'include', headers, signal: controller.signal});
                const text = await response.text();
                return {status: response.status, text};
            } catch (err) {
                return {status: -1, text: '', error: String(err)};
            } finally {
                clearTimeout(timer);
            }
        }"""
        # zp_token 在页面内从 document.cookie 现读，不需要 Python 侧传 token。
        # （旧代码走 window._PAGE.token，该对象在当前前端版本已不存在，
        # 且前端真实请求从不发 token 头——见上方注释②。）
        for attempt in range(1, attempts + 1):
            # 每次请求都复查持久策略 —— 没有"替身就跳过"的分支。
            # 长跑期间策略可能被改成 paused（人工巡检发现风控 / 账号状态变化），
            # 必须当场停。
            self._require_online()
            pace = getattr(self, "pace", None)
            if pace is not None:
                await pace(self._kind_of(url))
                self._require_online()
            # 记账（观测，不拦）：无账本返回 0，写失败只打日志。
            rowid = self._log_request(url)
            self.request_count += 1
            try:
                result = await self.page.evaluate(script, {"url": url, "timeout": self.config.browser.navigation_timeout_ms})
            except PlaywrightError as exc:
                # 这次尝试已经真实发出（或至少进入了浏览器栈），按"已尝试"结账；
                # 上下文销毁这类瞬时故障会重试，重试会新记一行。
                self._finish_request(rowid, "retry")
                if "Execution context was destroyed" not in str(exc) or attempt == attempts:
                    raise
                await self._settle_on_origin()
                await asyncio.sleep(random.uniform(1.5, 3.0))
                continue
            # 页面内 fetch 抛异常时 evaluate 本身是**成功**的（回一个带 error 的对象），
            # 所以这类失败不会走上面的 except —— 必须在这里单独判，否则网络抖一下
            # 就永久放弃这条岗位。2026-08-29 实测：一次波动连废 12 条，
            # 每条都已记账、JD 没拿到、还留在待补队列里等下一轮再花一次配额。
            if _is_transient_fetch_error(result) and attempt < attempts:
                self._finish_request(rowid, "retry")
                await self._settle_on_origin()
                await asyncio.sleep(random.uniform(2.0, 4.0))
                continue
            # `_rowid` 交给 `_same_origin_fetch` 回填最终结论（ok / risk / error）。
            # 用私有键而不是 `self._last_rowid`：同一个 collector 上的重试与并发
            # 调用会把"最后一次"搞混，而"这一行的 id"必须跟着这次响应走。
            return {**result, "_rowid": rowid}
        raise RuntimeError("同源请求重试耗尽")  # pragma: no cover

    @staticmethod
    def _dump_risk_body(path: str, status: int, raw_text: str) -> None:
        """把风控响应正文原样落盘，用于事后判断风控形态。

        风控响应同样是 HTTP 200，正文可能是验证页 HTML 而不是 JSON。只有留下
        原文，才能区分「可人工过验证」和「硬封」。落盘失败不能影响主流程——
        这里已经在抛 RiskControlStop 的路上了，不要用 IO 错误盖掉真实原因。
        """
        try:
            RISK_DUMP_DIR.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^A-Za-z0-9]+", "_", path)[:60].strip("_")
            target = RISK_DUMP_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{status}-{name}.txt"
            target.write_text(raw_text, encoding="utf-8")
        except OSError:
            pass

    async def _same_origin_fetch(self, path: str, recover_code37: bool = False) -> dict:
        """发一次同源请求并判定结果。

        ⚠️ 判定顺序是**风控优先**：先认 HTTP 401/403/429 与 31/32/35/36/37、
        验证页 HTML，再谈别的。旧实现把「环境存在异常（code=37）→ 自动重算
        stoken → 重试」放在最前面，于是**风控响应先被当成技术故障处理了一次**：
        在一次已经触发风控的会话上又补发导航，等于给惩罚加码。
        需要重算一次（成本 0 个请求，见 `collectors/stoken.py`），但那是**主动**
        做的，不是"撞了再补"。`recover_code37` 参数保留仅为兼容旧调用点
        （`_collect_detail` 与若干探针），**不再有任何效果**：任何风控响应都会
        持久熔断并停机。
        """
        assert self.page is not None
        url = f"{ORIGIN}{path}" if path.startswith("/") else path
        destination = urlsplit(url)
        if destination.scheme != "https" or destination.netloc != "www.zhipin.com":
            raise ValueError("采集器只允许向 BOSS 同源 HTTPS 接口发送请求")
        self._require_online()
        await self._settle_on_origin()
        result = await self._evaluate_fetch(url)
        # 每发一个接口请求就记一笔，`collect()` 据此决定下一次要不要重铸。
        # 计数放在这里而不是 collect 里：详情、登录校验等所有同源请求都走这条路，
        # 窗口消耗必须按**真实请求数**算，不能只算列表请求。
        self._requests_since_remint += 1
        body = self._decode_response(path, result)
        # 成功的响应已经把新种子（`__zp_sseed__`/`__zp_sname__`/`__zp_sts__`）
        # 放进 cookie jar。就地把它算成下一个请求要用的 `__zp_stoken__`，
        # 成本 0 个请求 —— 这就是站点自己前端的做法。
        #
        # 放在 decode **之后**：风控/技术故障走的是 raise，那时不该再滚动。
        # 滚动失败不致命（37 的兜底逻辑还在），只是退回按请求数导航。
        if (self._stoken_roller is not None and isinstance(body, dict)
                and body.get("code") == 0):
            self.last_roll = await roll_quietly(self._stoken_roller)
            self._stoken_roll_ok = bool(self.last_roll)
            if self._stoken_roll_ok:
                self._requests_since_remint = 0
        return body

    def _decode_response(self, path: str, result: dict) -> dict:
        """Apply the same business and access checks to API and native page responses."""
        # 预留行 id 跟着这次响应走；pop 掉，别让它混进调用方看到的响应体。
        rowid = int(result.pop("_rowid", 0) or 0)
        status = int(result.get("status", 0))
        if result.get("error"):
            self._finish_request(rowid, "error")
            raise RuntimeError(f"同源请求失败: {result['error']}")
        raw_text = result.get("text") or ""
        if status in (401, 403, 429):
            self._dump_risk_body(path, status, raw_text)
            self._trip_risk(rowid, f"接口返回 HTTP {status}，需人工核对", code=None, outcome="risk", http_status=status)
        try:
            body = json.loads(raw_text or "{}")
        except json.JSONDecodeError:
            # 非 JSON 正文 = 验证页 HTML（滑块/验证码），按风控停机。只在解析失败时扫全文，
            # 避免 JSON 里的 JD 正文误报（如爬虫岗 JD 会提到「滑块」）。
            if any(token in raw_text for token in (*RISK_TEXTS, "安全验证", "请完成验证", "verify.html")):
                self._dump_risk_body(path, status, raw_text)
                self._trip_risk(rowid, f"响应命中风控关键字（验证页 HTML），HTTP {status}",
                                code=None, outcome="risk", http_status=status)
            self._finish_request(rowid, "error")
            raise RuntimeError(f"BOSS 接口返回非 JSON，HTTP {status}")
        if not isinstance(body, dict):
            self._finish_request(rowid, "error")
            raise RuntimeError("BOSS 接口返回了非对象 JSON")
        code = body.get("code")
        if isinstance(code, str) and code.isdecimal():
            code = int(code)
        message = response_message(body)
        # 2026-07-30 实测：节流响应是 HTTP 200 + {"code": 0, "message": "访问频繁，请稍后再试"}。
        # 注意 code 是 0，所以下面所有基于 code 的判断都会放过它，这段 message 关键字检查
        # 是唯一的防线。只查 message 字段不查全文——JD 正文里的「滑块」「安全校验」等是
        # 正常内容（尤其爬虫/安全岗），扫全文会误报丢弃好数据。仍然落盘，因为平台改文案时
        # 这里是唯一能看到原始正文的地方。
        if any(token in message for token in RISK_TEXTS):
            self._dump_risk_body(path, status, raw_text)
            # 文案里带风控码（如 code=32 + "账户存在异常"）时按真风控记，
            # 只有 code=0 的纯节流文案才记 throttled —— 两者严重度不同，
            # 混成一个 outcome 会让"今天只是被限速"和"账号被禁"看起来一样。
            risk_code = code if isinstance(code, int) and code in RISK_CODES else 0
            self._trip_risk(rowid, f"响应命中风控关键字（{message[:40]}）code={code}",
                            code=risk_code,
                            outcome="risk" if risk_code else "throttled", http_status=status,
                            category=response_condition(risk_code, status, message))
        if status in (401, 403, 429) or code in RISK_CODES:
            self._trip_risk(rowid, f"命中平台风控 HTTP {status} code={code}: {message}",
                            code=code if isinstance(code, int) else None,
                            outcome="risk", http_status=status,
                            category=response_condition(code, status, message))
        if isinstance(code, int) and code in AUTH_EXPIRED_CODES:
            # 登录过期：停止但**不冻结**策略。冻结会让「重新登录后继续」也被拦住，
            # 而这里唯一该做的动作就是去登录。
            self._finish_request(rowid, "authentication", code)
            raise PlatformRiskStop(
                f"登录状态已失效（code={code}）：请在浏览器里重新登录后再跑；"
                f"这不是平台风控，访问策略未冻结。",
                code=code, http_status=status, category="authentication_required")
        if code not in (None, 0):
            self._finish_request(rowid, "error", code if isinstance(code, int) else None)
            raise RuntimeError(f"BOSS 接口错误 code={code}: {message}")
        self._finish_request(rowid, "ok")
        return body

    def _remember_session_response(self, response) -> None:
        location = urlsplit(response.url)
        if location.scheme == "https" and location.netloc == "www.zhipin.com" and location.path == USER_INFO_PATH:
            self._session_response = response
            self._session_observed_at = datetime.now(timezone.utc).isoformat()

    def _remember_native_detail(self, response) -> None:
        location = urlsplit(response.url)
        if location.scheme == "https" and location.netloc == "www.zhipin.com" and location.path == DETAIL_PATH:
            # Capture the automatically selected first job as well as card clicks.
            # Response handles stay inside this query; no credential/body dump is needed.
            self._native_detail_responses.append(response)
            self._native_detail_responses = self._native_detail_responses[-30:]

    @staticmethod
    def _native_request_parameters(request):
        values = parse_qs(urlsplit(request.url).query, keep_blank_values=True)
        if request.post_data:
            try:
                posted = json.loads(request.post_data)
            except ValueError:
                posted = parse_qs(request.post_data, keep_blank_values=True)
            if not isinstance(posted, dict):
                raise RuntimeError("网页列表请求参数不是对象，无法核对查询来源")
            values.update(posted)
        return values

    @staticmethod
    def _native_response_reader(path: str, *, expected_params: dict | None = None):
        """Read only this operation's response; old in-flight requests are not its data."""
        expected = urlsplit(ORIGIN + path)
        issued_after_ms = time.time() * 1000
        captured = {}

        async def read(response):
            location = urlsplit(response.url)
            if (location.scheme, location.netloc, location.path) != (expected.scheme, expected.netloc, expected.path):
                return False
            if response.status in (401, 403, 429):
                # Preserve explicit restrictions even on a prior operation's response.
                # None records that no body was read; callers stop on the actual status.
                captured[response] = None
                return True
            started = response.request.timing.get("startTime", 0)
            if started <= 0:
                raise RuntimeError("网页列表请求缺少开始时点，无法确认本次响应来源")
            if started < issued_after_ms:
                return False
            parameters = BossCollector._native_request_parameters(response.request)
            for name in ("city", "query", "page"):
                if name not in (expected_params or {}):
                    continue
                value = parameters.get(name)
                if isinstance(value, list):
                    if len(value) != 1:
                        return False
                    value = value[0]
                if value is None or str(value) != str(expected_params[name]):
                    return False
            try:
                captured[response] = await response.text()
            except PlaywrightError as exc:
                safe = {"request_start_ms": started, "capture_start_ms": issued_after_ms}
                for name in ("city", "query", "page"):
                    value = parameters.get(name)
                    safe[name] = value[0] if isinstance(value, list) and len(value) == 1 else value
                kind = "Network.getResponseBody资源失效" if "No resource with given identifier" in str(exc) else "响应正文读取异常"
                raise RuntimeError(f"{kind}；已核对响应来源：{json.dumps(safe, ensure_ascii=False)}") from exc
            return True

        return read, captured

    async def _native_list(self, *, search_url: str | None = None, expected_params: dict | None = None) -> dict:
        """Read the site's own list response for navigation or the next normal scroll."""
        assert self.page is not None
        self._require_online()
        if search_url:
            self._native_detail_responses = []
            pace = getattr(self, "pace", None)
            if pace is not None:
                # The response timeout starts only after configured pacing is complete.
                await pace("list")
                self._require_online()
        if not search_url:
            await self._settle_on_origin()
            pace = getattr(self, "pace", None)
            if pace is not None:
                await pace("list")
            self._require_online()
        rowid = self._log_request(ORIGIN + API_PATH)
        self.request_count += 1

        is_list, captured = self._native_response_reader(API_PATH, expected_params=expected_params)

        try:
            async with self.page.expect_response(is_list, timeout=self.config.browser.navigation_timeout_ms) as pending:
                if search_url:
                    await self._goto_search(search_url, already_paced=True)
                else:
                    await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            response = await pending.value
            text = captured.pop(response)
        except BaseException:
            self._finish_request(rowid, "error")
            raise
        if response.status in (401, 403, 429):
            self._trip_risk(rowid, f"原生列表收到 HTTP {response.status}，需要人工复检", code=None,
                            outcome="risk", http_status=response.status,
                            category=response_condition(None, response.status, ""))
        body = self._decode_response(API_PATH, {"_rowid": rowid, "status": response.status, "text": text})
        request = response.request
        request_params = self._native_request_parameters(request)
        safe_params = {}
        for name in ("city", "query", "page", "pageSize", "scene", "experience", "degree", "salary", "scale", "stage", "industry", "jobType", "position"):
            value = request_params.get(name)
            if isinstance(value, list):
                if len(value) != 1:
                    raise RuntimeError(f"网页列表请求有重复参数：{name}，无法核对查询来源")
                value = value[0]
            if value is not None:
                safe_params[name] = str(value)
        # pageSize is chosen by the website (currently 15), independently of legacy GET.
        for name, expected in (expected_params or {}).items():
            if name in safe_params or name in ("city", "query", "page"):
                if name not in ("pageSize", "scene") and safe_params.get(name) != str(expected):
                    raise RuntimeError(f"网页列表实际请求与计划的{name}不一致，查询保持未完成")
            elif name not in ("pageSize", "scene", "lid"):
                raise RuntimeError(f"网页列表实际请求缺少计划的{name}条件，查询保持未完成")
        body["_list_source"] = {"transport": "native", "method": request.method, "parameters": safe_params}
        self.last_native_list_source = dict(body["_list_source"])
        return body

    def _trip_risk(self, rowid: int | None, detail: str, *,
                   code: int | None, outcome: str = "risk",
                   http_status: int | None = None, category: str | None = None,
                   resume_after: str = "") -> None:
        """风控响应统一出口：落账本 → **持久熔断** → 抛 RiskControlStop。

        顺序是有意的：账本是事后取证的来源，策略文件是**活过本次进程**的那一份。
        写策略失败不改判结论（照样抛 RiskControlStop），但会打一条显眼的
        `[SAFETY-FAIL]` —— 那意味着"重启后拦不住"，必须让人看见。
        """
        self._finish_request(rowid, outcome, code)
        gate = self._pause_gate()
        persisted = False
        if gate is None:
            print("[SAFETY-FAIL] 没有可写的访问策略位置（未注入 gate/ledger）："
                  "本次停机不会被持久化 —— 重启后仍可能发起请求。", flush=True)
        else:
            try:
                gate.pause(
                    f"{detail}（script={self.ledger_script or 'collector'}）",
                    resume_after=resume_after or None,
                    source=getattr(self, "gate_source", f"boss:{self.ledger_script or 'collector'}"))
                persisted = True
            except Exception as exc:  # noqa: BLE001 - 写盘失败必须显形但不掩盖风控
                # 策略锁被占用 / 磁盘错误：**拒绝无锁写**（详见 AccessPolicyLocked）。
                # 冻结没落地 → 由下面这行停止本进程，并把这件事写进异常消息。
                print(f"[SAFETY-FAIL] 访问策略未写入（{type(exc).__name__}: {exc}）；"
                      f"冻结未持久化，本进程停止，需人工确认账号状态。", flush=True)
        note = "" if persisted else "（访问策略未写入，见 [SAFETY-FAIL]）"
        # code=36 和 code=37 是两套完全不同的东西，停机信息必须说清楚，
        # 否则操作者会照着"等一会儿再试"去做，而实测等待 12 分钟无效。
        if code == 36:
            detail = (f"{detail}｜这是平台要求**人工过验证**（岗位页会被 302 到 "
                      f"verify.html），等待和重载都无效；请运行 `job-radar verify` "
                      f"在弹出的窗口里完成验证，再手动解除冻结")
        elif code in (31, 32, 33, 34):
            # 账号限制。和「验证」完全不同 —— 拖滑块和重登都没用，只能等。
            detail = (f"{detail}｜这是**账号访问限制**（不是滑块验证、也不是 IP 问题）："
                      f"拖滑块、重新登录、重载页面都无效，只能在平台给出的时间之后"
                      f"再确认。期间每多发一个请求都可能延长限制期限。")
        # 置前窗口**不在这里做**：本方法是同步的，`browser.show()` 是协程，
        # 调度出去的任务很可能在进程退出前没跑到。真正的置前在
        # `worker.TaskContext.verified()` 里（`await browser.show(page)`），
        # 所有采集路径的 PlatformRiskStop 都要经过它。
        raise PlatformRiskStop(f"{detail}{note}", code=code, http_status=http_status, category=category)

    async def _recover_code37(self, seed: str, name: str, ts: str) -> None:
        """**已停用**：自动 stoken 重算一律不执行。

        code=37（「您的环境存在异常」）是**待人工判断的验证信号**：
        它既不等同于封禁（历史上有 1 次重算就被封、也有 21 次重算零封禁），
        也不能被当成"账号安全"（它确实出现在封禁那一轮里）。既然判断不了，
        正确动作是停下来交给人，而不是自动补发导航 —— 那会在一个已经被平台
        注意到的会话上继续加压（实测一次重算约 37 次真实请求，`REQ_PER_RECOVER`）。

        保留这个方法名并让它抛异常，是为了让任何遗留调用点（以及直接调用者）
        都拿到明确的停止信号，而不是"静默什么都没做"继续往下跑。
        """
        try:
            gate = self._pause_gate()
            if gate is None:
                print("[SAFETY-FAIL] 没有可写的访问策略位置，code=37 停机未持久化。",
                      flush=True)
            else:
                gate.pause(
                    f"code=37（环境存在异常；待人工判断的验证信号）触发停机，"
                    f"自动重算已停用（script={self.ledger_script or 'collector'}）",
                    source=f"boss:code37-disabled:{self.ledger_script or 'collector'}")
        except Exception as exc:  # noqa: BLE001 - 写失败不得掩盖下面的停机
            print(f"[SAFETY-FAIL] 访问策略未写入（{type(exc).__name__}: {exc}）；"
                  f"本进程停止，需人工确认账号状态。", flush=True)
        raise RiskControlStop(
            "code=37（环境存在异常）是需要人工判断的验证信号：自动重算已停用，"
            "本轮在此停止并写入持久熔断。它既不能当成封禁结论，也不能当成账号安全 —— "
            "请人工复核账号与出口状态后再决定是否解除。")

    def _parse_api_job(self, raw: dict, keyword: str, rank: int) -> JobRecord:
        return parse_list_job(raw, keyword, rank)

    def _should_keep(self, job: JobRecord) -> bool:
        """Apply explicit collection rules and optional configured candidate facts."""
        title, company = job.title.casefold(), job.company_name.casefold()
        # 岗位类型先判：这是**平台自己标的**字段，比标题里的字眼可靠。
        # 2026-10-18 实测语义：0=全职、4=实习、5=校招/应届、6=兼职。
        # ⚠️ 5 必须保留——「应届生」「27届校招」正是目标岗；只有 4/6 该排除。
        job_type = (job.raw or {}).get("jobType")
        if isinstance(job_type, int) and job_type in self.config.exclude_job_types:
            return False
        if self.config.include_title_keywords and not any(word.casefold() in title for word in self.config.include_title_keywords):
            return False
        if any(word.casefold() in title for word in self.config.exclude_title_keywords):
            return False
        if any(word.casefold() in company for word in self.config.exclude_company_keywords):
            return False
        # 外包/人力服务公司名单：命中即降级为 sample（**仍然入库**，只是不进投递池，
        # 下次增量采集照样能识别为已见）。放在采集侧而不是只放分析侧，是因为它
        # 决定要不要为这个岗位花一次详情请求 —— 详情是最贵、最容易触发风控的一环。
        if is_staffing_company(job.company_name):
            return False
        if self.config.service.candidate_filtering and self.config.candidate is not None:
            from ..candidate import passes_list_facts
            data = job.model_dump()
            data["anonymous"] = job.raw.get("anonymous", 0)
            return passes_list_facts(data, self.config.candidate)
        return bool(job.title and job.job_url)

    async def _require_detail_login(self) -> None:
        if self.page.is_closed():
            raise BrowserPageUnavailable("浏览器岗位页已关闭，详情任务停止；请检查当前浏览器会话")
        await self._settle_on_origin()
        login_visible = await self.page.evaluate("""() => [...document.querySelectorAll('a,button')].some(e =>
          e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden' &&
          /^(登录|登录\\/注册|注册\\/登录|登录注册)$/.test((e.innerText || '').trim()))""")
        if login_visible:
            raise PlatformRiskStop("本机详情会话当前显示登录入口，请登录后复检；不将匿名预览视为完整正文")
        response = self._session_response
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(self._session_observed_at)).total_seconds()
        except (ValueError, TypeError):
            age = float("inf")
        if response is None or not 0 <= age <= 60:
            # Existing attached pages may have loaded before the observer was registered.
            # Expired observations also need a fresh ordinary read, so a long-running
            # task cannot use its startup login result to bless later anonymous previews.
            body = await self._same_origin_fetch(USER_INFO_PATH)
            observed_at = datetime.now(timezone.utc).isoformat()
        else:
            if response.status != 200:
                raise PlatformRiskStop("登录状态响应未通过，请在浏览器中复检登录", http_status=response.status)
            body = await response.json()
            observed_at = self._session_observed_at
            if isinstance(body, dict) and body.get("code") not in (None, 0):
                code = body.get("code") if type(body.get("code")) is int else None
                raise PlatformRiskStop("登录状态业务响应未通过，请在浏览器中复检", code=code,
                    http_status=response.status, category=response_condition(code, response.status, response_message(body)))
        if not authenticated_user_response(body):
            raise PlatformRiskStop("服务端未确认已登录的账号，请登录后继续；不将预览正文保存为核验全文",
                                   category="authentication_required")
        self._detail_access_evidence = {"kind": "get_user_info_response", "path": USER_INFO_PATH,
                                        "authenticated": True, "observed_at": observed_at}

    async def _collect_native_detail(self, job: JobRecord) -> JobRecord:
        """Click the unique card identified by its job link and verify the returned ID."""
        self._require_online()
        await self._require_detail_login()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job.source_job_id):
            raise ValueError("岗位 ID 字符格式无效，无法定位网页卡片")
        link = self.page.locator(f'a[href^="/job_detail/{job.source_job_id}.html"]')
        card = self.page.locator("li.job-card-box, .job-card-wrapper").filter(has=link)
        await card.first.wait_for(state="visible", timeout=self.config.browser.navigation_timeout_ms)
        if await card.count() != 1:
            raise RuntimeError("网页岗位卡片无法按岗位 ID 唯一定位，不按顺序猜测对应岗位")
        pace = getattr(self, "pace", None)
        if pace is not None:
            await pace("detail")
        self._require_online()
        response = None
        for observed in list(self._native_detail_responses):
            try:
                captured = await observed.json()
            except (PlaywrightError, ValueError):
                continue
            if not isinstance(captured, dict):
                continue
            data = captured.get("zpData")
            info = data.get("jobInfo") if isinstance(data, dict) else None
            if isinstance(info, dict) and info.get("encryptId") == job.source_job_id:
                response = observed
                break
            if captured.get("code") not in (None, 0) or observed.status in (401, 403, 429) or any(
                token in response_message(captured) for token in RISK_TEXTS):
                response = observed
                break
        rowid = self._log_request(ORIGIN + DETAIL_PATH)
        self.request_count += 1

        def is_detail(response):
            location = urlsplit(response.url)
            return location.scheme == "https" and location.netloc == "www.zhipin.com" and location.path == DETAIL_PATH

        try:
            if response is None:
                async with self.page.expect_response(is_detail, timeout=self.config.browser.navigation_timeout_ms) as pending:
                    await card.click()
                response = await pending.value
            text = await response.text()
        except BaseException:
            self._finish_request(rowid, "error")
            raise
        body = self._decode_response(DETAIL_PATH, {"_rowid": rowid, "status": response.status, "text": text})
        zp = body.get("zpData") or {}
        if response in self._native_detail_responses:
            self._native_detail_responses.remove(response)
        await self._require_detail_login()
        detailed = apply_detail(job, zp, access_verified=True)
        # Verify that this response's body actually reached the current visible panel.
        await self.page.wait_for_function(
            "text => document.body.innerText.replace(/\\s/g,'').includes(text)",
            arg="".join(detailed.job_description.split()), timeout=self.config.browser.navigation_timeout_ms)
        detailed.raw["detail_source"].update({"transport": "native", "method": response.request.method,
            "request_query_keys": sorted(parse_qs(urlsplit(response.request.url).query)),
            "access_evidence": dict(self._detail_access_evidence)})
        if pace is None:
            low, high = self.config.detail_delay_seconds
            await asyncio.sleep(gaussian(low, high))
        return detailed

    async def _collect_detail(self, job: JobRecord) -> JobRecord:
        """从详情接口补全 JD 与招聘者活跃度。

        实测（2026-07-29）搜索列表接口不返回 activeTimeDesc，只有详情接口的
        bossInfo 里有；JD 正文在 jobInfo.postDescription，是干净的纯文本，
        不需要解析 DOM，也就不受前端 class 名改版影响。
        详情接口必须带列表响应里的 securityId 和 lid，否则会被打回空白页。

        ⚠️ 这条路依赖列表响应里的 `securityId`。`securityId` 会在每次详情响应后轮换，
        也会被历史剪枝删掉（2026-10-18 删了 11,329 行），所以它对"库里已有但没正文"
        的岗位不可用。那种岗位走 `_collect_ssr_detail`。
        """
        await self._require_detail_login()
        raw = job.raw
        security_id = str(raw.get("securityId") or "")
        if not security_id:
            raise RuntimeError("列表记录缺少 securityId，无法请求详情接口")
        params = {"securityId": security_id, "lid": str(raw.get("lid") or self.last_lid or "")}
        body = await self._same_origin_fetch(f"{DETAIL_PATH}?{urlencode(params)}")
        zp = body.get("zpData") or {}
        job = apply_detail(job, zp, access_verified=True)
        job.raw["detail_source"].update({"transport": "api", "method": "GET", "access_evidence": dict(self._detail_access_evidence)})
        # 条内延迟用高斯分布（比 uniform 更像真人：有时快有时慢），2026-08-03 拟人化
        lo, hi = self.config.detail_delay_seconds
        if getattr(self, "pace", None) is None:
            await asyncio.sleep(gaussian(lo, hi))
        return job

    async def _settle_challenge(self, html: str, timeout_s: float = 20.0) -> str:
        """详情页落在安全校验页时，**等它自己跳走**再重新取 HTML。

        为什么需要：`wait_until="domcontentloaded"` 只保证 DOM 就绪，不保证站点
        那段安全校验 JS 跑完。挑战页的地址会自己变成
        `job_detail/...?_security_check=1_<ts>`，页面才真正是岗位页。

        代价：**零额外主动请求**。`page.url` 与 `page.content()` 都是本地状态；
        唯一的网络流量是站点自己发起的那次跳转（它本来就会发生，我们只是不再
        提前把挑战页当结果）。

        2026-10-19 屏外窗口实测：43,417 字节挑战页 → 2 秒后 467,056 字节岗位页，
        `.job-sec-text` 出现。**这条也说明「屏外窗口一直不跳转」不成立于这个场景**
        —— 那句话描述的是 `refresh.recover_session` 里"新开页面 goto"的流程。
        """
        if not _is_challenge_url(self.page.url or ""):
            return html
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            if not _is_challenge_url(self.page.url or ""):
                try:
                    return await self.page.content()
                except PlaywrightError:
                    return html
        # 等不到自愈时把**平台原话**记下来。挑战页上通常写着为什么被拦
        # （「访问过于频繁」「您的IP地址存在异常行为」），比只知道"超时"有用。
        try:
            stuck = await self.page.content()
            prompt = _page_prompt(stuck)
            if prompt:
                print(f"[RISK-PAGE] {prompt}", flush=True)
        except PlaywrightError:
            pass
        return html

    async def _collect_ssr_detail(self, job: JobRecord) -> JobRecord:
        """打开岗位详情页，只放行 document，从 SSR HTML 里取正文。

        为什么有这条路：详情页是 **SSR + hydration**。实测（2026-10-19）导航到
        `/job_detail/{id}.html` 后，页面**一次都不请求 `detail.json`**（等 40 秒超时，
        `detail_requests: []`）；它只发 `jobdetail/popupguides/query.json` 和
        `zpitem/web/competitive/jobDetail.json`，都不是正文。

        正文在 `.job-sec-text` 里，和 `detail.json` 的 `postDescription` **逐字相等**：
        两条已知长度的岗位归一化后 661/661、6111/6111，差 0。

        代价与对策：不 abort 子资源时，一次详情页导航会发 24 个 `/wapi/` 引导请求，
        比直接 fetch 还贵。所以这里**只放行 document**，其余一律 abort——服务端只看到
        1 个文档请求，接口调用 0 次。

        这条路不依赖 `securityId`，也不碰 stoken，所以能覆盖"库里已有 job_url、
        但没有 securityId、也不在搜索结果里"的存量岗位。
        """
        self._require_online()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job.source_job_id):
            raise ValueError("岗位 ID 字符格式无效，无法打开详情页")

        # 节奏：这条路径是 `page.goto` 直接发文档请求，不经过 `_same_origin_fetch`，
        # 所以必须自己调 pace。**漏了这一句的后果不是"稍快一点"**：末尾那个
        # `if pace is None` 的兜底 sleep 在 pace 已注入时会整段跳过，详情就变成
        # 零间隔连发——而详情正是最贵、最容易触发封禁的一环。
        # （2026-10-19 实测：未接 pace 时 2 条详情 5 秒跑完，pacer_requests=1。）
        pace = getattr(self, "pace", None)
        if pace is not None:
            await pace("detail")
            self._require_online()

        url = f"{ORIGIN}{DETAIL_PAGE.format(job.source_job_id)}"
        rowid = self._log_request(url)
        self.request_count += 1

        async def only_document(route):
            if route.request.resource_type == "document":
                await route.continue_()
            else:
                await route.abort()

        await self.page.route("**/*", only_document)
        try:
            response = await self.page.goto(
                url, wait_until="domcontentloaded",
                timeout=self.config.browser.navigation_timeout_ms)
            if response is None:
                raise RuntimeError("详情页没有返回响应，不写入正文")
            if response.status in (401, 403, 429):
                # 错误页的正文里通常有平台的原始说明（「访问过于频繁」等），
                # 读出来比只报状态码有用得多。
                try:
                    body_text = await response.text()
                except PlaywrightError:
                    body_text = ""
                prompt = _page_prompt(body_text)
                detail = (f"详情页收到 HTTP {response.status}"
                          + (f"｜平台提示「{prompt}」" if prompt else "") + "，需要人工复检")
                self._trip_risk(rowid, detail,
                                code=None, outcome="risk", http_status=response.status,
                                category=response_condition(None, response.status, prompt))
            html = await response.text()
            # **挑战页是读早了，不是被拦了。** 详情页的 SSR 文档拿到后，站点自己的
            # JS 还会跑一段安全校验，通过后把地址跳回 `job_detail/...?_security_check=1`。
            # `wait_until="domcontentloaded"` 之后立刻 `response.text()` 读到的是
            # 校验**中途**的那一页（约 41–43KB，没有 `.job-sec-text`）。
            #
            # 2026-10-19 屏外窗口实测：导航落在 `security.html`（43,417 字节），
            # 被动等 2 秒后地址自己跳回岗位页，页面变成 467,056 字节、正文容器在了。
            # 所以这里**只等、不重发请求** —— `page.url` 和 `page.content()` 都是
            # 本地状态，零额外请求。等不到就照原样返回，由归因函数判成 js_challenge。
            html = await self._settle_challenge(html)
        except BaseException:
            self._finish_request(rowid, "error")
            raise
        finally:
            await self.page.unroute("**/*", only_document)

        # 登录态从页面内联的 `_userInfo` 读，**零额外请求**——这条路径不需要
        # `getUserInfo.json`。读不到（None）不算未登录：页面改版少一个字段时，
        # 宁可放行一次让正文进库，也不要因此把整轮停掉。
        is_login = _inline_is_login(html)
        if is_login is False:
            self._finish_request(rowid, "authentication")
            raise PlatformRiskStop("详情页显示未登录，不把匿名预览当作完整正文",
                                   category="authentication_required")
        self._detail_access_evidence = {
            "kind": "ssr_inline_user_info", "is_login": is_login,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

        fields = html_query(html, SSR_SELECTORS)
        description = (fields["jd"]["text"] or "").strip()
        if not description:
            diag = _ssr_blank_reason(html, self.page.url or url, response.status,
                                     judge=getattr(self, "page_judge", None))
            # 认不出来的页面留个原件：规则判错时光看 request_log 查不出真相
            # （2026-10-20 那次封禁被判成 js_challenge 就是这样藏了一夜）。
            if diag["kind"] in ("unknown_page", "selector_miss", "js_challenge",
                                "verification", "empty_page"):
                saved = _dump_abnormal_page(html, diag, _page_prompt(html))
                if saved:
                    diag["page_dump"] = saved
                    print(f"[PAGE-DUMP] {diag['kind']} → {saved}", flush=True)
            # 验证页与登录墙是**平台状态问题**，必须停；其余是岗位/选择器问题，
            # 记一条分类后继续跑下一条。
            # 页面文案直接判出风险类别时，**立刻熔断** —— 这是最硬的证据。
            if diag.get("category") and diag["kind"] in (
                    "ip_restricted", "account_restricted", "environment_restricted",
                    "rate_limited", "login_wall", "banned"):
                self._finish_request(rowid, diag["kind"], diag.get("code"))
                # 封禁页会明写恢复时间（`expire-time`），带上它 —— 闸门的
                # `resume_after` 就是靠这个挡住「到点前提前 release」。
                extra = ""
                if diag.get("expire_at"):
                    extra = f"，平台称将于 {diag['expire_at']} 恢复"
                label = diag.get("label") or "详情页提示"
                self._trip_risk(
                    rowid,
                    f"{label}（code={diag.get('code')}）｜平台原话「{diag['prompt'][:120]}」{extra}",
                    code=diag.get("code"), outcome="risk",
                    http_status=response.status, category=diag["category"],
                    resume_after=_expire_to_iso(diag.get("expire_at")))
            if diag["kind"] == "verification":
                # 平台原话优先：它直接说明「为什么要验证」。
                why = (f"平台提示「{diag['prompt']}」" if diag.get("prompt")
                       else f"证据：{diag['evidence']}")
                self._trip_risk(rowid, f"详情页被导向验证（{why}）",
                                code=36, outcome="risk", http_status=response.status,
                                category="verification_required")
            if diag["kind"] == "js_challenge":
                # 挑战页会**污染页面上下文**：留在上面时页面内的 `fetch()` 直接失败
                # （2026-10-19 实测：连续 3 次挑战之后下一个列表请求报
                # `TypeError: Failed to fetch`）。标脏后 `collect()` 会重新导航回
                # 搜索页，把干净页面拿回来。不在这里导航，是因为详情调用点没有
                # 搜索 URL 的上下文。
                self._search_ready = False
                # 同时告诉 pacer 放慢 —— 被站点推回时要让路。
                self._pace_signal("challenge")
            self._finish_request(rowid, diag["kind"])
            prompt = f"｜平台提示「{diag['prompt']}」" if diag.get("prompt") else ""
            raise RuntimeError(
                f"详情页没有正文（{diag['kind']}｜HTTP {diag['status']}｜"
                f"{diag['length']} 字节｜证据 {diag['evidence']}{prompt}｜{diag['url'][:90]}），"
                f"不把空正文写库")

        inline = _inline_jobinfo(html)
        encrypt_id = inline.get("job_id") or job.source_job_id
        status_text = (fields["job_status"]["text"] or "").strip()
        active = (fields["boss_active"]["text"] or "").strip()
        boss_name = (fields["boss_name"]["text"] or "").strip()
        if active and boss_name.endswith(active):
            # `.name` 里带活跃文案（"李女士 刚刚活跃"），切掉才和 detail.json 的
            # bossInfo.name 对齐。
            boss_name = boss_name[: -len(active)].strip()

        zp = {
            "jobInfo": {
                "encryptId": encrypt_id,
                "encryptUserId": inline.get("user_id") or "",
                "postDescription": description,
                "jobStatusDesc": status_text,
                "address": (fields["address"]["text"] or "").strip(),
                "showSkills": [s.strip() for s in (fields["skills"]["text"] or "").split() if s.strip()],
            },
            "bossInfo": {
                "name": boss_name,
                "title": (fields["boss_title"]["text"] or "").strip(),
                "activeTimeDesc": active,
            },
            "brandComInfo": {},
        }

        was_invalid = job.job_invalid
        job = apply_detail(job, zp, access_verified=True)
        # 关闭只降不升：页面明确写了关闭才置 True，没写就保留原标记，绝不清掉。
        # 和刷新链路同一条判据（见 boss_scrape/refresh.py 的 classify）。
        if "关闭" in status_text or "下线" in status_text:
            job.job_invalid = True
        else:
            job.job_invalid = was_invalid
        job.raw["detail_source"].update({
            "transport": "ssr",
            "method": "GET",
            "resource_scope": "document_only",
            "inline_security_id_present": bool(inline.get("securityId")),
            "access_evidence": dict(self._detail_access_evidence),
        })
        # 工商信息**跟着 JD 一起白拿**，不额外发请求。
        # 只在这条岗位还没有工商数据时写，避免用页面上的简版覆盖
        # `/gongsi/` 抓到的完整版（后者还有统一社会信用代码、注册地址等）。
        reg = _company_reg_from_html(fields)
        if reg and not (job.company_reg or {}).get("注册资本"):
            job.company_reg = {**(job.company_reg or {}), **reg}
            job.company_reg_updated_at = datetime.now(timezone.utc).isoformat()
        self._finish_request(rowid, "ok")

        # 把"这条读完了、多长"回传给 pacer：下一次间隔按字数微调（只算 20%，
        # 见 `pacing.py` 的说明），并逐步解除挑战倍数。
        self._pace_signal("ok")
        self._pace_signal("content", len(description))

        # **导航之后必须补滚一次 stoken。**
        #
        # 整页导航会轮换 cookie 里的种子（`__zp_sseed__` / `__zp_sname__` /
        # `__zp_sts__`），但**不会重算 `__zp_stoken__`**；而滚动器只在
        # 「接口响应成功之后」才滚动（见 `_same_origin_fetch`）。
        #
        # 2026-10-19 实测：一次导航之后列表接口仍然 `code=0`（种子变了、stoken
        # 没变，服务端容忍一次）；但 `sweep` 的形态是「1 个列表 + 8 条详情导航」，
        # 8 次种子轮换中间一次都不重算 —— 下一个列表请求就拿到 `code=37`
        # 「您的环境存在异常」。**这不是封禁**：浏览器里页面完全正常，人工确认过。
        #
        # 成本 0 个请求：滚动是纯 JS 计算，种子已经在 cookie jar 里。
        roller = self._stoken_roller
        if roller is not None:
            try:
                self.last_roll = await roll_quietly(roller)
                self._stoken_roll_ok = bool(self.last_roll)
                if self._stoken_roll_ok:
                    self._requests_since_remint = 0
            except Exception:  # noqa: BLE001 - 滚动失败只降级，不影响已经拿到的正文
                self._stoken_roll_ok = False

        lo, hi = self.config.detail_delay_seconds
        if getattr(self, "pace", None) is None:
            await asyncio.sleep(gaussian(lo, hi))
        return job


    async def collect(
        self,
        city: CityConfig,
        keyword: str,
        filters: SearchFilters,
        pages: int,
        detail_limit: int,
    ) -> AsyncIterator[JobRecord]:
        assert self.page is not None
        self.last_lid = ""
        params = self._params(city, keyword, filters, 1)
        visible = {k: v for k, v in params.items() if k not in ("pageSize", "scene")}
        # Navigate once and confirm the page is available before requesting list data.
        native = self.config.list_source == "native"
        search_url = f"{SEARCH_BASE}?{urlencode(visible)}"
        first_body = await self._native_list(search_url=search_url, expected_params=params) if native else None
        if not native:
            # 导航在这里有两个作用：① 打开搜索页；② 冷启动时建立有效 stoken
            # （种子是会话 cookie，进程重启后必须靠一次整页加载 bootstrap）。
            #
            # 2026-10-18 实测（已登录会话，间隔 6 秒）：同一个 stoken 窗口内
            # **第 5 个请求成功、第 6 个返回 code=37**。所以窗口确实是按请求数算的。
            #
            # 但续窗口不必靠导航：每个 code=0 响应都会下发新种子，就地能算出新
            # stoken，成本 0 个请求（实测连续 20/20 全过）。滚动**确实生效**时，
            # 导航只在"还没打开过"或"页面漂走了"时才做。
            # 滚动关掉或降级时，退回旧的按请求数导航——注意那时一次导航是
            # 92~993 个真实请求，代价很高。
            window_exhausted = (
                not self._stoken_roll_ok
                and self._requests_since_remint >= self.config.remint_after_requests
            )
            # 页面是否已被风控页**污染**。挑战页和验证页都在 `www.zhipin.com` 上，
            # `is_boss_origin()` 判不出来 —— 但留在那上面时，页面内的 `fetch()`
            # 会直接失败（2026-10-19 实测：连续 3 次 `js_challenge` 之后，下一个
            # 列表请求报 `TypeError: Failed to fetch`，整轮扫描就此中断）。
            # 所以必须重新导航回搜索页，把干净页面拿回来。
            page_url = self.page.url or ""
            poisoned = _is_challenge_url(page_url)
            need_remint = (
                not self._search_ready
                or not is_boss_origin(page_url)
                or poisoned
                or window_exhausted
            )
            if need_remint:
                await self._goto_search(search_url)
                self._search_ready = True
                self._requests_since_remint = 0

        detailed = 0
        observed_page_ids = set()
        for page_index in range(1, pages + 1):
            api_params = self._params(city, keyword, filters, page_index)
            # 平台用上一页响应里的 lid 串联同一次搜索会话，带上它更接近真实翻页。
            if self.last_lid:
                api_params["lid"] = self.last_lid
            body = (first_body if page_index == 1 else await self._native_list(expected_params=api_params)) if native else \
                await self._same_origin_fetch(f"{API_PATH}?{urlencode(api_params)}")
            zp_data = body.get("zpData")
            if body.get("code") != 0 or not isinstance(zp_data, dict) or not isinstance(zp_data.get("jobList"), list):
                raise RuntimeError("列表响应结构不完整，不能判定为无岗位")
            if zp_data.get("lid"):
                self.last_lid = str(zp_data["lid"])
            raw_jobs = zp_data.get("jobList") or []
            if not raw_jobs:
                break
            page_ids = {str(raw.get("encryptJobId") or "") for raw in raw_jobs if isinstance(raw, dict)}
            if native and page_index > 1 and not (page_ids - observed_page_ids):
                raise RuntimeError("网页翻页没有产生新岗位，查询保持未完成；不把重复页面算作新一页")
            observed_page_ids.update(page_ids)
            for rank, raw in enumerate(raw_jobs, start=1):
                if not isinstance(raw, dict) or not raw.get("encryptJobId"):
                    raise ValueError("列表包含缺少岗位 ID 的记录，查询保持未完成，不写入无主键岗位")
                job = self._parse_api_job(raw, keyword, rank)
                job.raw["list_source"] = {**body.get("_list_source", {"transport": "api", "method": "GET"}),
                                          "page": page_index}
                # 全保留分层：无效岗位跳过；画像外岗位也入库标 sample（去重样本），
                # 不再过滤丢弃，保证下次增量采集能识别为已见而 UPDATE 而非 INSERT。
                if not job.title or not job.job_url:
                    continue
                job.retention_status = "active" if self._should_keep(job) else "sample"
                # 先落列表数据。详情触发验证或停止时，这页已经搜到的岗位仍可恢复。
                on_list = getattr(self, "on_list", None)
                if on_list is not None:
                    on_list(job)
                # 去重前置：库里已有 JD 的岗位不再抓详情。13 个关键词的搜索结果
                # 大量交叉重叠，原先同一岗位每被搜到一次就重抓一次详情，白烧配额。
                already = job.canonical_key() in self.skip_detail_ids
                if already:
                    self.detail_skipped += 1
                if (
                    self.config.collect_details
                    and detailed < detail_limit
                    and job.retention_status == "active"
                    and not already
                ):
                    # 熔断（2026-08-10 补齐）：backfill 路径一直有 code37_limit=35，
                    # collect 路径完全没有——同一个 collector 类两条调用路径安全等级
                    # 不对称。code=37 本身可恢复，但每次重算都消耗信用，累积会升到
                    # code=32 封禁（实测：156 条仅 1 次 code37 就触发过 32）。
                    if self.code37_count >= self.code37_limit:
                        raise RiskControlStop(
                            f"code=37 重算 {self.code37_count} 次超阈值 "
                            f"{self.code37_limit}，主动停止防封禁"
                        )
                    try:
                        detailed += 1
                        transport = self.config.detail_transport
                        if transport == "ssr":
                            job = await self._collect_ssr_detail(job)
                        elif transport == "native":
                            job = await self._collect_native_detail(job)
                        else:
                            job = await self._collect_detail(job)
                        # 本轮内同一岗位也可能再出现（换关键词），记下避免重复
                        self.skip_detail_ids.add(job.canonical_key())
                    except RiskControlStop:
                        # 必须原样上抛。RiskControlStop 继承 RuntimeError，原先被
                        # 下面的 `except Exception` 吞掉后只写进 detail_error 字段，
                        # 循环继续往下抓——真封禁时反而会持续撞墙加重惩罚。
                        raise
                    except AccessBlocked:
                        # 同一条理由：策略闸门说不能联网时，绝不能降级成"这条岗位
                        # 详情没拿到"然后继续循环（下一次 fetch 还会被拦，
                        # 但中间会白写一批 detail_error，看起来像业务失败）。
                        raise
                    except Exception as exc:
                        job.raw["detail_error"] = str(exc)
                yield job
            if page_index < pages:
                if native and zp_data.get("hasMore") is False:
                    break
                low, high = self.config.request_delay_seconds
                if getattr(self, "pace", None) is None:
                    await asyncio.sleep(random.uniform(low, high))
