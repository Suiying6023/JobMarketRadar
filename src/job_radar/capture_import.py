"""Offline import of recorded job payloads; no browser, credentials or HTTP client."""
from __future__ import annotations

import base64
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .collectors.boss_payload import apply_detail, parse_list_job, response_message
from .models import JobRecord
from .rounds import CollectionRound, encode, stamp
from .storage import JobStore


def timestamp(value):
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when > datetime.now(timezone.utc):
            return ""
        return when.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return ""


def read_events(path):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("响应文件超过64MB，请分批导入")
    raw_file = path.read_bytes()
    value = json.loads(raw_file.decode("utf-8-sig"))
    digest = hashlib.sha256(raw_file).hexdigest()
    if isinstance(value, dict) and "events" in value:
        events = value["events"]
        if not isinstance(events, list):
            raise ValueError("events需要数组")
        return events, "saved-events", {}, digest
    if isinstance(value, dict) and "zpData" in value:
        if str(value.get("code")) != "0" or not isinstance(value["zpData"], dict):
            raise ValueError("原生JSON不是成功的岗位响应，不导入")
        notice = response_message(value)
        if any(word in notice for word in ("访问频繁", "环境存在异常", "账户存在异常", "请完成验证")):
            raise ValueError("原生JSON包含访问限制文案，不作为成功数据导入")
        data = value["zpData"]
        if isinstance(data.get("jobList"), list):
            return [{"kind": "list_job", "payload": raw, "rank": rank,
                     "query": {"query_kind": "unspecified"}, "captured_at": ""}
                    for rank, raw in enumerate(data["jobList"], 1)], "api-json", {}, digest
        if isinstance(data.get("jobInfo"), dict):
            raise ValueError("单份详情JSON缺少列表样本范围，请提供包含列表与详情的HAR或事件JSON")
        raise ValueError("原生JSON缺少岗位列表数据")
    if not isinstance(value, dict) or not isinstance(value.get("log"), dict):
        raise ValueError("需要HAR、原生列表JSON或含events数组的JSON文件")
    events, skipped = [], Counter()
    for entry in value["log"].get("entries", []):
        request = entry.get("request") or {}
        location = urlsplit(request.get("url") or "")
        if location.hostname != "www.zhipin.com" or location.scheme != "https":
            continue
        kind = {"/wapi/zpgeek/search/joblist.json": "list",
                "/wapi/zpgeek/job/detail.json": "detail"}.get(location.path)
        if not kind:
            continue
        response = entry.get("response") or {}
        content = response.get("content") or {}
        text = content.get("text") or ""
        try:
            if content.get("encoding") == "base64":
                text = base64.b64decode(text, validate=True).decode("utf8")
            body = json.loads(text)
        except (ValueError, UnicodeError):
            skipped["missing_or_non_json_body"] += 1
            continue
        if not isinstance(body, dict) or str(body.get("code")) != "0" or response.get("status") != 200:
            skipped["non_success_response"] += 1
            continue
        notice = response_message(body)
        if any(word in notice for word in ("访问频繁", "环境存在异常", "账户存在异常", "请完成验证")):
            skipped["restriction_notice"] += 1
            continue
        data = body.get("zpData")
        if not isinstance(data, dict):
            skipped["missing_business_data"] += 1
            continue
        query = {key: values[-1] for key, values in parse_qs(location.query).items()
                 if key in ("query", "city", "degree", "experience", "salary", "jobType")}
        post = request.get("postData") or {}
        if post.get("mimeType", "").split(";")[0] == "application/json":
            try:
                posted = json.loads(post.get("text") or "{}")
                if isinstance(posted, dict):
                    query.update({key: str(posted[key]) for key in ("query", "city", "degree", "experience", "salary", "jobType")
                                  if isinstance(posted.get(key), (str, int))})
            except ValueError:
                pass
        else:
            query.update({item["name"]: str(item.get("value", "")) for item in post.get("params", [])
                          if item.get("name") in ("query", "city", "degree", "experience", "salary", "jobType")})
        if post.get("mimeType", "").split(";")[0] == "application/x-www-form-urlencoded":
            query.update({key: values[-1] for key, values in parse_qs(post.get("text") or "").items()
                          if key in ("query", "city", "degree", "experience", "salary", "jobType")})
        origin = {"keyword": query.get("query", ""), "request_filters": query,
                  "query_kind": "keyword-search" if query.get("query") else "unspecified"}
        if kind == "list":
            if not isinstance(data.get("jobList"), list):
                skipped["missing_job_list"] += 1
                continue
            for rank, raw in enumerate(data["jobList"], 1):
                events.append({"kind": "list_job", "payload": raw, "query": origin,
                               "captured_at": entry.get("startedDateTime"), "rank": rank})
        else:
            events.append({"kind": "detail", "payload": data, "captured_at": entry.get("startedDateTime")})
    # Request/response headers, cookies and authentication state are not returned.
    return events, "har", dict(skipped), digest


def row_to_job(row):
    from .models import job_from_row

    return job_from_row(row)


def import_capture(source: Path, output: Path, ident: str, preserve_verified: bool = False):
    if output.exists() or output.resolve() == source.resolve():
        raise ValueError("导入只写新的独立数据库，不覆盖源文件或运行库")
    events, source_kind, skipped, digest = read_events(source)
    if any(not isinstance(event, dict) or not isinstance(event.get("payload"), dict) for event in events):
        raise ValueError("事件及其payload需要JSON对象")
    if source_kind != "saved-events" and preserve_verified:
        raise ValueError("原生响应不能自行证明完整访问，不接受preserve-verified")
    lists = [event for event in events if event.get("kind") == "list_job"]
    details = [event for event in events if event.get("kind") == "detail"]
    if not lists:
        raise ValueError("文件没有列表岗位，不能确定导入样本范围")
    captured = [timestamp(event.get("captured_at")) for event in lists]
    began = min((value for value in captured if value), default=stamp())
    plan = list({encode(event.get("query") or {}): event.get("query") or {} for event in lists}.values())
    provenance = {"mode": "offline-import", "source_kind": source_kind, "source_sha256": digest,
                  "imported_at": stamp(), "new_platform_requests": 0,
                  "verification": "preserved source declarations" if preserve_verified else "not inferred from file"}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".importing")
    if temporary.exists():
        raise FileExistsError("存在未完成的同名导入，请指定新的输出路径")
    processed, enriched = 0, 0
    with JobStore(temporary) as store:
        rounds = CollectionRound(store)
        rounds.begin_import(ident, plan, provenance, began)
        for event in sorted(lists, key=lambda event: timestamp(event.get("captured_at"))):
            origin = event.get("query") or {}
            raw = {key: value for key, value in event["payload"].items() if key not in
                   {"detail", "detail_access_verified", "detail_source", "detail_preview", "detail_error"}}
            job = parse_list_job(raw, origin.get("keyword", ""), event.get("rank") or 1)
            when = timestamp(event.get("captured_at"))
            job.raw["capture_source"] = {**provenance, "captured_at": when, "kind": "list"}
            old = store.conn.execute("SELECT first_seen_at FROM jobs WHERE canonical_id=?", (job.canonical_key(),)).fetchone()
            old_observation = store.conn.execute(
                "SELECT first_observed_at FROM collection_observations WHERE round_id=? AND canonical_id=?",
                (ident, job.canonical_key())).fetchone()
            store.upsert(job)
            store.conn.execute("UPDATE jobs SET first_seen_at=?,last_seen_at=?,collected_at=? WHERE canonical_id=?",
                               (old[0] if old else when, when, when, job.canonical_key()))
            rounds.observe(ident, job.canonical_key(), origin)
            store.conn.execute("UPDATE collection_observations SET first_observed_at=?,last_observed_at=? WHERE round_id=? AND canonical_id=?",
                               (old_observation[0] if old_observation else when, when, ident, job.canonical_key()))
            store.conn.commit()
            processed += 1
        for event in sorted(details, key=lambda event: timestamp(event.get("captured_at"))):
            data = event.get("payload") or {}
            cid = "boss:" + str((data.get("jobInfo") or {}).get("encryptId") or "")
            row = store.conn.execute("SELECT * FROM jobs WHERE canonical_id=?", (cid,)).fetchone()
            if row is None:
                skipped["detail_outside_list_sample"] = skipped.get("detail_outside_list_sample", 0) + 1
                continue
            when = timestamp(event.get("captured_at"))
            verified = bool(preserve_verified and when and event.get("access_verified") is True)
            job = apply_detail(row_to_job(row), data, access_verified=verified, fetched_at=when)
            if not when:
                job.detail_fetched_at = ""
                job.raw["detail_source"]["fetched_at"] = ""
            job.raw["detail_source"]["capture_file_sha256"] = digest
            store.upsert(job, observed=False)
            rounds.update_detail(ident, cid)
            enriched += 1
        rounds.finish(ident, "imported")
        check = store.conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise ValueError("导入数据库完整性检查失败")
        count = rounds.count(ident)
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.conn.execute("PRAGMA journal_mode=DELETE")
    temporary.rename(output)
    result = {**provenance, "round_id": ident, "output": str(output.resolve()), "list_observations": processed,
              "unique_jobs": count, "details_imported": enriched, "skipped": skipped, "sqlite_check": check,
              "query_completion_inferred": False}
    output.with_suffix(".import.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    return result
