"""GLM 批量提取岗位 JD 标签，写入 DB 的 glm_tags_json 列（断点续跑）。

用 step-3.7-flash + reasoning_effort=low（信息抽取场景）从 JD 正文提取：
exp_req / edu_req / school_req / tech_req / other_req / bonus / job_type。
并发批量 + 429 退避重试，只处理 active 且已补 JD 的岗位。

用法:
  python scripts/extract_glm_tags.py --concurrency 20 --limit 0
  --limit 0 表示全部；指定条数用于小批测试。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlreq

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SYSTEM_PROMPT = """你是岗位画像提取与投递匹配判断器。从岗位 JD 正文提取硬性要求与加分项，并综合判断该岗位是否符合给定的求职者画像。只输出 JSON，不要任何解释或前后缀。

求职者画像（26届应届本科·双非，求职杭州 AI/Agent/大模型 编程开发岗，要求双休）：
可投：AI/Agent 开发、大模型应用、算法、AI全栈/后端/前端/客户端等编程岗；AI 产品经理。
必须排除（命中任一即 match=demote）：
  1. 不招应届：JD 写"暂未开放在校生/应届生计划""仅实习经历请勿投递""必须有实际工作经验""实习经历不计"或硬性要求N年以上正式工作经验
  2. 名校要求：985/211/一本/双一流/重点院校等硬性院校门槛
  3. 硕士及以上学历
  4. 仅限27届 / 仅招实习生
  5. 硬性英语四级及以上（"优先/加分"不算硬性）
  6. 非双休：大小周/单休/996/上六休一/六天工作制等
  7. 电销/电话销售/电话开发客户
  8. 外包/外派/驻场/劳务派遣
  9. 纯设计岗/内容岗/运营岗/市场销售BD岗
  10. 非编程业务岗：商品开发/辅料开发/面料/饰品/采购/商务/货代/跟单/企划/渠道/外贸/出纳/会计等（即使标题带"开发"）
  11. 普通产品岗（文创/电商/服饰/五金/供应链等）；AI/Agent 产品岗可留
  12. 最高薪资低于 7K（无薪资信息不算）

输出格式（必须严格是这个结构，字段顺序也必须一致）：
{"exp_req":[],"edu_req":[],"school_req":[],"tech_req":[],"other_req":[],"bonus":[],"job_type":"开发岗/算法岗/测试岗/设计岗/运营岗/市场岗/产品岗/内容岗/其他","grad_year":"26届可投/仅27届/仅实习/不限","work_schedule":"双休/大小周/单休/其他非双休/未提及","fresh_grad":"接受应届/不接受应届/优秀应届可放宽","match_evidence":"","match_reason":"","match":"keep/demote"}

规则：
- exp_req：硬性经验要求（如"经验不限""3年以上XX经验""实习经历不计"）
- edu_req：硬性学历要求，以 JD 正文表述为准（如"本科及以上""硕士""博士""大专"），不要编造
- school_req：院校要求（如"985/211""双一流""一本""重点高校"），没有则空
- tech_req：硬性技术栈/工具要求（如 Python、大模型、RAG、LangChain）
- other_req：其他硬性门槛（专业、英语、出差、驻场、证书、到岗时间等）
- bonus：明确写"优先/加分/更佳"的加分项（"英语四级优先"属加分，不算硬性）
- job_type：判断岗位实际类型，务必区分编程岗与业务岗：
  - 开发岗/算法岗/测试岗：仅指 软件/前端/后端/客户端/AI/Agent/大模型 等编程或算法类岗位
  - 产品岗：产品经理/产品策划等；技术或 AI 产品岗才算产品岗
  - 市场岗：销售/BD/商务/货代/市场拓展/电销/面销 等营销类
  - 其他：实体商品/材料开发（辅料开发、面料开发、饰品开发、商品开发、采购开发）、企划、跟单、出纳、财务、行政、人事等非编程非产品业务岗
  - 反例警示：**不要因为标题含"开发"就标开发岗**。"辅料开发""商品开发""采购开发""饰品开发""市场开发"等开发的是实体商品或业务，不是软件，标其他或市场岗；只有开发"软件/系统/算法/AI"才算开发岗
  - 设计岗/内容岗：美工、平面、视频剪辑、文案、新媒体等
- grad_year：毕业届别限制（重要）：
  - "仅27届"：JD 明确只面向 27 届（毕业时间在 2026年9月/11月之后，如"2026年11月-2027年10月毕业""2027届""27届""2027年毕业在校生"）
  - "仅实习"：JD 明确只招实习（"实习岗位""仅限实习生"）
  - "26届可投"：JD 写 26届/25-26届/2026届/含应届且未排除 26 届（如"面向25、26届学生""2026届应届生"）
  - "不限"：无届别/实习限制
- work_schedule：JD 明确写的工作制。"双休"；"大小周"；"单休"；
  **"其他非双休"用于确实写了工作制、但不属于上面三类的写法**（如"月休4天/月休5天""做六休一""排班制"）；
  完全没写工作制才填"未提及"。⚠️ 只能填这五个值之一，不要原样照抄 JD 用词（如不要填"月休5天"）
- fresh_grad：JD 是否接受应届（"不接受应届"：未开放应届计划/必须工作经验/实习不计；"优秀应届可放宽"：明确写可放宽；"接受应届"：未排除应届）
- **match_evidence / match_reason / match 三个字段必须按此顺序输出，先给证据再下结论**：
  - match_evidence：**逐字摘录** JD 原文里与"是否命中排除项"最相关的片段（保留原文用词，可摘多句用「；」分隔）。
    若通读全文确实没有任何触及上述 12 条排除项的表述，填"无排除项相关表述"。**此字段不得为空**。
  - match_reason：基于 match_evidence 说明判定依据。判 demote 时**必须**指明命中第几条排除项及其原文
    （如"暂未开放在校生/应届生计划→命中1不招应届"）；判 keep 时说明为何 12 条都不命中
    （如"经验不限、本科、双休、AI开发岗，无排除项"）。**此字段不得为空**。
  - match：仅在 match_evidence 与 match_reason 都写完后给出 "keep" 或 "demote"。
  - ⚠️ 先写证据再判定，不要先决定 keep/demote 再补理由。**证据里没有的排除项，不得凭印象判 demote；
    但证据里明确写了的经验/学历/院校/工作制门槛，绝不能漏判成 keep**。
  - 命中任一排除项 → "demote"；否则 "keep"。无法从 JD 判断的条件默认不算命中。
所有标签必须有 JD 原文依据，不得凭空编造。"""


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


REQUIRED_FIELDS = ("exp_req", "edu_req", "tech_req", "job_type",
                   "grad_year", "work_schedule", "match")
ENUM_FIELDS = {
    "grad_year": {"26届可投", "仅27届", "仅实习", "不限"},
    # "其他非双休"：JD 写了工作制但不属前三类（月休4/5天、做六休一、排班制…）。
    # 2026-08-12 实测有一条 JD 写"月休5天"，模型如实照抄导致 BAD_ENUM 卡住 ——
    # 模型判断没错，是枚举太窄。这类同样命中「非双休」排除项，不能漏成"未提及"。
    "work_schedule": {"双休", "大小周", "单休", "其他非双休", "未提及"},
    "fresh_grad": {"接受应届", "不接受应届", "优秀应届可放宽"},
    "match": {"keep", "demote"},
}


def validate_tags(parsed: dict) -> str | None:
    """校验抽取结果，返回错误描述或 None（通过）。

    为什么要这层：阶跃只有 json_object，**不保证字段齐全或枚举值合法**，
    所以"能 json.loads"不等于"能用"。

    ★ 空 match_reason 是漏判的特征信号，不是无害的省略。
    2026-08-11 实测另一个模型（laguna-s-2.1-free）在 60 条对照里把 13 条该 demote 的
    岗位放成 keep，**这 13 条的 match_reason 全是空字符串** —— 模型没找依据就下了结论。
    人工核对 7 条，现役 stepfun 全对（那些 JD 明写"1年以上经验""1-3年工作经验"）。
    对 26 届应届生来说"不招应届"是最高频的排除项，放过一条的代价远高于误杀一条，
    所以这里把"理由为空"直接判为无效并触发重试，而不是存进库。
    """
    missing = [k for k in REQUIRED_FIELDS if k not in parsed]
    if missing:
        return f"MISSING {missing}"
    for key, allowed in ENUM_FIELDS.items():
        val = parsed.get(key)
        if val is not None and val not in allowed:
            return f"BAD_ENUM {key}={val!r}"
    # 证据与理由都不许空——两个字段是"先证据后结论"这套 prompt 的执行凭证，
    # 空值说明模型跳过了论证步骤，判定不可信（无论它给的是 keep 还是 demote）。
    for key in ("match_evidence", "match_reason"):
        if not str(parsed.get(key) or "").strip():
            return f"EMPTY {key}"
    return None


def call_glm(env: dict, jd: str, max_retries: int = 5) -> tuple[dict | None, str | None, int]:
    """调用 GLM，返回 (解析 JSON 或 None, 错误或 None, 尝试次数)。429 指数退避重试。"""
    body = json.dumps({
        "model": env["GLM_MODEL"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"岗位 JD：\n{jd[:2500]}"},
        ],
        "temperature": 0.1,
        "max_tokens": 4000,
        "reasoning_effort": "low",  # 信息抽取用低推理，省 token 且快
        # 阶跃只支持 json_object（不支持 OpenAI 那种 json_schema 强约束，2026-08-12 查官方文档确认）。
        # 收益是模型原生 JSON 模式、不再吐 ``` 围栏；但**不保证字段齐全或枚举值合法**，
        # 所以下面的 _validate 校验必须保留，剥围栏的兜底也保留（防它偶尔仍加围栏）。
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urlreq.Request(env["GLM_API_URL"], data=body, headers={
        "Authorization": f"Bearer {env['ZHIPU_API_KEY']}",
        "Content-Type": "application/json",
    })
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        try:
            with urlreq.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode("utf-8")
            data = json.loads(raw)
            content = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("非 JSON 对象")
            bad = validate_tags(parsed)
            if bad:
                # 校验不过就重试（换一次采样往往就补上了）；重试用尽才算失败，
                # 宁可这条不入库，也不要存一条判定依据缺失的标签。
                if attempt < max_retries:
                    time.sleep(1)
                    continue
                return None, bad, attempt
            return parsed, None, attempt
        except urlerror.HTTPError as exc:
            if exc.code == 429 and attempt < max_retries:
                time.sleep(attempt * 4)  # 4s, 8s, 12s...
                continue
            try:
                detail = exc.read().decode("utf-8")[:200]
            except Exception:
                detail = ""
            return None, f"HTTP {exc.code} {detail}", attempt
        except (urlerror.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            if attempt < max_retries:
                time.sleep(2)
                continue
            return None, str(exc)[:80], attempt
    return None, "retries exhausted", max_retries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=7)
    parser.add_argument("--limit", type=int, default=0, help="处理条数上限，0=全部")
    parser.add_argument("--force", action="store_true", help="重跑已提取过的岗位（换新 prompt 后重新提取）")
    parser.add_argument("--db", default="data/hangzhou_agent.sqlite3")
    args = parser.parse_args()

    env = load_env()
    if not env.get("ZHIPU_API_KEY") or not env.get("GLM_API_URL"):
        raise SystemExit("缺少 ZHIPU_API_KEY / GLM_API_URL（.env）")
    model = env.get("GLM_MODEL", "step-3.7-flash")

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    where = "WHERE retention_status = 'active' AND job_description != ''"
    if not args.force:
        where += " AND glm_tags_updated_at = ''"
    rows = con.execute(f"SELECT canonical_id, title, company_name, job_description FROM jobs {where}").fetchall()
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)
    print(f"待提取: {total} 条（模型 {model}，并发 {args.concurrency}，reasoning_effort=low）")

    done = 0
    ok_count = 0
    fail_count = 0
    fail_detail: dict[str, int] = {}
    start_all = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        # 一次提交全部 futures
        futures = {pool.submit(call_glm, env, r["job_description"]): r for r in rows}
        for idx, fut in enumerate(as_completed(futures), 1):
            r = futures[fut]
            parsed, err, attempts = fut.result()
            done += 1
            if parsed is not None:
                con.execute(
                    "UPDATE jobs SET glm_tags_json = ?, glm_tags_updated_at = ? WHERE canonical_id = ?",
                    (json.dumps(parsed, ensure_ascii=False), time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     r["canonical_id"]),
                )
                con.commit()
                ok_count += 1
            else:
                fail_count += 1
                fail_detail[err or "?"] = fail_detail.get(err or "?", 0) + 1
            if done % 10 == 0 or done == total:
                el = time.monotonic() - start_all
                rate = done / el if el > 0 else 0
                eta = (total - done) / rate / 60 if rate > 0 else 0
                print(f"[{done}/{total}] 成功 {ok_count} 失败 {fail_count} | "
                      f"{rate:.1f}条/分 | 预计剩余 {eta:.0f} 分钟", flush=True)
    print(f"\n完成: 成功 {ok_count}，失败 {fail_count}，总耗时 {(time.monotonic()-start_all)/60:.1f} 分钟")
    if fail_detail:
        print("失败分布:", fail_detail)
    con.close()


if __name__ == "__main__":
    main()
