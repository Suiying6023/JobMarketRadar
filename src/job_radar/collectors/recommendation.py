"""Read recommendation responses created by normal page navigation and scrolling."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlsplit

from .boss import BossCollector, ORIGIN, SEARCH_BASE, response_condition

RECOMMEND_PATH = "/wapi/zpgeek/pc/recommend/job/list.json"


class RecommendationCollector(BossCollector):
    async def recommendation_page(self, page_number, *, expectation_label="", first=False):
        """Keep expectation context fixed within one recommendation scroll sequence."""
        self._require_online()
        if first:
            self._native_detail_responses = []
            self._recommendation_context = None
        pace = getattr(self, "pace", None)
        if pace:
            await pace("list")
        rowid = self._log_request(ORIGIN + RECOMMEND_PATH)
        self.request_count += 1
        is_recommendation, captured = self._native_response_reader(RECOMMEND_PATH, expected_params={"page": page_number})
        try:
            async with self.page.expect_response(is_recommendation, timeout=self.config.browser.navigation_timeout_ms) as pending:
                if first and not expectation_label:
                    await self._goto_search(SEARCH_BASE, already_paced=True)
                elif first:
                    link = self.page.get_by_role("link", name=expectation_label, exact=True)
                    if await link.count() != 1:
                        raise RuntimeError("Existing expectation tab is not uniquely identified")
                    await link.click()
                else:
                    await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            response = await pending.value
            text = captured.pop(response)
        except BaseException:
            self._finish_request(rowid, "error")
            raise
        if response.status in (401, 403, 429):
            self._trip_risk(rowid, f"原生推荐收到 HTTP {response.status}，需要人工复检", code=None,
                            outcome="risk", http_status=response.status,
                            category=response_condition(None, response.status, ""))
        body = self._decode_response(RECOMMEND_PATH,
            {"_rowid": rowid, "status": response.status, "text": text})
        data = body.get("zpData")
        if body.get("code") != 0 or not isinstance(data, dict) or not isinstance(data.get("jobList"), list):
            raise RuntimeError("Recommendation response is incomplete; cannot interpret it as an empty result")
        query = parse_qs(urlsplit(response.request.url).query, keep_blank_values=True)
        def one(name):
            values = query.get(name, [])
            if len(values) > 1:
                raise RuntimeError("Recommendation has duplicate context parameters")
            return values[0] if values else ""
        if one("page") != str(page_number):
            raise RuntimeError("Recommendation scroll page differs from expected page")
        context = {name: one(name) for name in ("city", "encryptExpectId", "expectInfo", "mixExpectType",
            "degree", "experience", "industry", "jobType", "salary", "scale")}
        fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        if not first and fingerprint != self._recommendation_context:
            raise RuntimeError("Recommendation expectation/filter context changed during pagination")
        self._recommendation_context = fingerprint
        safe = {name: one(name) for name in ("page", "pageSize", "city", "mixExpectType", "degree", "experience",
            "industry", "jobType", "salary", "scale") if name in query}
        body["_list_source"] = {"transport": "native", "source_kind": "recommendation", "method": response.request.method,
            "path": RECOMMEND_PATH, "parameters": safe, "request_query_keys": sorted(query),
            "expectation_context_hash": fingerprint, "expectation_label": expectation_label or "mixed_recommendation"}
        self.last_native_list_source = dict(body["_list_source"])
        return body
