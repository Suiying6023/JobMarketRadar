"""BOSS payload normalization, independent of browser control and HTTP transport."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from datetime import datetime, timezone

from ..models import JobRecord, Platform
from ..normalization import active_days_from_desc, founded_year_from_intro, parse_salary, split_location


def response_message(body: dict) -> str:
    """Read envelope notices without scanning a successful job description."""
    for key in ("message", "msg", "error", "zpData"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def response_condition(code: int | None, status: int | None, message: str) -> str:
    """Describe the observed failure for review; this never authorizes a retry."""
    message = message.casefold()
    if code in (32, 36) or "账户存在异常" in message:
        return "account_restriction"
    if code == 35 or "ip地址存在异常" in message or "ip 地址存在异常" in message:
        return "network_restriction"
    if "环境存在异常" in message or "环境异常" in message:
        return "environment_restriction"
    auth = any(word in message for word in ("token", "登录", "凭证", "认证"))
    expired = any(word in message for word in ("过期", "失效", "expired"))
    if status == 401 or any(word in message for word in ("登录入口", "未登录", "请先登录", "需要登录")) or (auth and expired):
        return "authentication_required"
    if status == 429 or any(word in message for word in ("访问频繁", "操作过于频繁", "操作太频繁")):
        return "rate_limited"
    return "verification_required"


def authenticated_user_response(body: dict) -> bool:
    """Observed getUserInfo contract: success plus a positive numeric user ID.

    The caller checks response origin/status and provides the session observation.
    Never returns or stores account identifiers or token values.
    """
    if not isinstance(body, dict) or body.get("code") != 0:
        return False
    data = body.get("zpData")
    user_id = data.get("userId") if isinstance(data, dict) else None
    return type(user_id) is int and user_id > 0


def parse_list_job(raw: dict, keyword: str, rank: int) -> JobRecord:
    if not isinstance(raw, dict):
        raise ValueError("列表记录需要 JSON 对象")
    encrypt_job_id = str(raw.get("encryptJobId") or "").strip()
    if not encrypt_job_id:
        raise ValueError("列表记录缺少岗位 ID，不能创建稳定岗位主键")
    job_url = f"https://www.zhipin.com/job_detail/{encrypt_job_id}.html"
    city = str(raw.get("cityName") or "")
    district = str(raw.get("areaDistrict") or "")
    business = str(raw.get("businessDistrict") or "")
    salary = str(raw.get("salaryDesc") or "")
    salary_min, salary_max, salary_months = parse_salary(salary)
    return JobRecord(
        platform=Platform.BOSS,
        source_job_id=encrypt_job_id,
        job_url=job_url,
        company_url=(
            f"https://www.zhipin.com/gongsi/{raw.get('encryptBrandId')}.html"
            if raw.get("encryptBrandId") else ""
        ),
        title=str(raw.get("jobName") or ""),
        company_name=str(raw.get("brandName") or ""),
        city=city,
        district=district,
        business_district=business,
        location_text=split_location(city, district, business),
        salary_text=salary,
        salary_min_k=salary_min,
        salary_max_k=salary_max,
        salary_months=salary_months,
        experience=str(raw.get("jobExperience") or ""),
        education=str(raw.get("jobDegree") or ""),
        company_size=str(raw.get("brandScaleName") or ""),
        financing_stage=str(raw.get("brandStageName") or ""),
        industry=str(raw.get("brandIndustry") or ""),
        recruiter_name=str(raw.get("bossName") or ""),
        recruiter_title=str(raw.get("bossTitle") or ""),
        recruiter_active_status=str(raw.get("activeTimeDesc") or ("在线" if raw.get("bossOnline") else "")),
        recruiter_online=bool(raw.get("bossOnline")),
        brand_id=str(raw.get("encryptBrandId") or ""),
        skills=[str(v) for v in (raw.get("skills") or []) if v],
        labels=[str(v) for v in (raw.get("jobLabels") or []) if v],
        welfare=[str(v) for v in (raw.get("welfareList") or []) if v],
        matched_queries=[keyword],
        source_rank=rank,
        raw=deepcopy(raw),
    )


def apply_detail(job: JobRecord, zp: dict, *, access_verified: bool | None = None, fetched_at: str = "") -> JobRecord:
    """Normalize a captured detail response; the caller supplies access evidence.

    Job identity and nonempty body are required. Parsing itself does not establish
    login status, page accessibility or whether an anonymous body is complete.
    """
    job = job.model_copy(deep=True)
    if not isinstance(zp, dict):
        raise ValueError("详情数据需要 JSON 对象")
    job_info = zp.get("jobInfo")
    if not isinstance(job_info, dict) or not job_info:
        raise ValueError("详情响应缺少有效 jobInfo，不写库")
    if str(job_info.get("encryptId") or "") != job.source_job_id:
        raise RuntimeError("详情响应岗位 ID 与请求岗位不一致，不写库")
    boss_info = zp.get("bossInfo") or {}
    brand_info = zp.get("brandComInfo") or {}
    if not isinstance(boss_info, dict) or not isinstance(brand_info, dict):
        raise ValueError("详情中的招聘者或公司数据格式无效，不写库")

    description = job_info.get("postDescription")
    if not isinstance(description, str):
        raise ValueError("详情正文需要文本，不写入非文本内容")
    description = description.strip()
    if not description:
        raise RuntimeError("详情响应正文为空，不更新时间或将旧正文视为新正文")
    job.job_description = re.sub(r"\n{3,}", "\n\n", description)

    # activeTimeDesc 只有详情接口给：2026-07-30 核对 897 条纯列表记录，
    # 列表响应连这个键都没有（bossOnline 有，但 535/1001 为 true，明显是
    # 「近期在线」的宽松口径，不能当活跃度用）。

    active_desc = str(boss_info.get("activeTimeDesc") or "")
    if active_desc:
        job.recruiter_active_status = active_desc
        job.recruiter_active_days = active_days_from_desc(active_desc)
    job.recruiter_online = bool(boss_info.get("bossOnline"))
    job.recruiter_certificated = bool(boss_info.get("certificated"))
    if boss_info.get("name"):
        job.recruiter_name = str(boss_info["name"])
    if boss_info.get("title"):
        job.recruiter_title = str(boss_info["title"])
    # 招聘者唯一 ID：同一个 ID 挂大量岗位是僵尸/中介信号，公司名不可靠时靠它聚合。
    if job_info.get("encryptUserId"):
        job.recruiter_id = str(job_info["encryptUserId"])
    job.job_status_desc = str(job_info.get("jobStatusDesc") or "")
    job.position_name = str(job_info.get("positionName") or "")
    job.recruitment_count = str(job_info.get("recruitmentCountDesc") or "")
    if job_info.get("latitude") is not None:
        job.latitude = float(job_info["latitude"])
    if job_info.get("longitude") is not None:
        job.longitude = float(job_info["longitude"])

    # proxyJob/proxyType 非 0 表示代招（猎头、外包、人力公司）。
    job.is_proxy_job = bool(job_info.get("proxyJob")) or bool(job_info.get("proxyType"))
    job.job_invalid = bool(job_info.get("invalidStatus"))
    if job_info.get("address"):
        job.address = str(job_info["address"])
    for skill in job_info.get("showSkills") or []:
        text = str(skill or "").strip()
        if text and text not in job.skills:
            job.skills.append(text)
    if brand_info.get("scaleName"):
        job.company_size = str(brand_info["scaleName"])
    if brand_info.get("stageName"):
        job.financing_stage = str(brand_info["stageName"])
    if brand_info.get("industryName"):
        job.industry = str(brand_info["industryName"])
    # brandId 比公司名可靠：BOSS 上有大量「某大型互联网公司」这类匿名马甲，
    # 只有 ID 能把它们聚合成同一家。
    if brand_info.get("encryptBrandId"):
        job.brand_id = str(brand_info["encryptBrandId"])
    intro = str(brand_info.get("introduce") or "").strip()
    if intro:
        job.company_intro = intro
        # 注册资本 BOSS 完全不给；成立年份只能从简介文本里碰运气提取。
        job.company_founded_year = founded_year_from_intro(intro)
    for label in brand_info.get("labels") or []:
        text = str(label or "").strip()
        if text and text not in job.welfare:
            job.welfare.append(text)
    job.raw["detail"] = deepcopy(zp)
    job.raw["detail_access_verified"] = access_verified
    # 详情响应会回一个轮换后的新 securityId。存回去，让下次重试用新鲜 token，
    # 免得存量记录里的旧 token 越放越久。lid 同理。
    if zp.get("securityId"):
        job.raw["securityId"] = str(zp["securityId"])
    if zp.get("lid"):
        job.raw["lid"] = str(zp["lid"])
    # 记录详情成功获取的时间戳（活跃度是时效字段，抓完 30 天后就无意义了）
    job.detail_fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()

    # Detail metadata belongs to this body observation and can supersede list facts.
    for field, key in (("title", "jobName"), ("education", "degreeName"),
                       ("experience", "experienceName"), ("city", "locationName")):
        if job_info.get(key):
            setattr(job, field, str(job_info[key]))
    if job_info.get("salaryDesc"):
        job.salary_text = str(job_info["salaryDesc"])
        job.salary_min_k, job.salary_max_k, job.salary_months = parse_salary(job.salary_text)
    job.location_text = split_location(job.city, job.district, job.business_district)
    job.raw["detail_source"] = {
        "kind": "boss_detail_response", "source_job_id": job.source_job_id,
        "fetched_at": job.detail_fetched_at, "access_verified": access_verified,
        "body_sha256": hashlib.sha256(job.job_description.encode("utf8")).hexdigest(),
    }
    return job
