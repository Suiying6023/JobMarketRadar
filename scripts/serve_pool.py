"""本地投递池浏览服务：静态服务 HTML 页面 + /demote 降级 API。

网页里"标记"按钮会调 POST /demote 把岗位降级为 sample（样本库），
降级后自动重新生成 HTML 保持同步。

用法:
  python scripts/serve_pool.py --port 8000
然后浏览器打开 http://127.0.0.1:8000/
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
DB = ROOT / "data" / "hangzhou_agent.sqlite3"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 不发 Access-Control-Allow-Origin: *。页面和接口同源，本来就不需要 CORS；
        # 发了通配反而让任意站点的 JS 能读本机这个服务的响应。
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        from urllib.parse import unquote
        path = unquote(self.path.split("?")[0])
        # 根路径直接给单页投递池。原先是一个链接目录页，指向七个分文件——
        # 那套已被 export_pool.py 的单页取代（分桶变成页内筛选），
        # 多一跳目录页只是让人多点一次。旧文件若还在，仍可按文件名直接访问。
        if path in ("/", "/index.html"):
            page = OUT / "投递池.html"
            if not page.exists():
                self._send(200, ('<meta charset="utf-8"><h3>还没有导出投递池</h3>'
                                 '<p>先跑 <code>uv run python scripts/export_pool.py</code></p>'
                                 ).encode(), "text/html; charset=utf-8")
                return
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            return
        # 目录穿越防护：原先直接 `OUT / path.lstrip("/")`，而 path 已经 unquote 过，
        # 所以 `GET /../.env` 和 `/..%2F.env` 都能读到项目根的 .env（含 API key）。
        # 服务只绑 127.0.0.1，但 _send 又发了 `Access-Control-Allow-Origin: *`，
        # 任意网页里的 JS 都能打本机这个端口把文件读走。resolve 后必须校验仍在 OUT 内。
        try:
            f = (OUT / path.lstrip("/")).resolve()
            f.relative_to(OUT.resolve())
        except (ValueError, OSError):
            self._send(403, b"Forbidden", "text/plain")
            return
        if f.exists() and f.is_file():
            ctype = "text/html; charset=utf-8" if f.suffix == ".html" else "application/octet-stream"
            self._send(200, f.read_bytes(), ctype)
        else:
            self._send(404, b"Not Found", "text/plain")

    def do_POST(self) -> None:
        from urllib.parse import unquote
        path = unquote(self.path.split("?")[0])
        if path not in ("/demote", "/restore", "/apply", "/note"):
            self._send(404, b"Not Found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            cid = data.get("id", "")
            # 「已投递」和「不合适」是完全不同的语义，原先只有 demote 一个动作：
            # 投过的岗位应该置灰但**保留可见**（要跟进沟通状态），而 demote 会把它
            # 从池子里移走——用户第二天打开页面无法知道昨天投了哪些，只能靠记忆。
            # chat_actions 表（storage.py 建的）一直是 0 行，从未被写入，正好用上。
            if path in ("/apply", "/note"):
                action = "applied" if path == "/apply" else "note"
                msg = str(data.get("message", ""))[:500]
                con = sqlite3.connect(DB)
                # 投递过的岗位一律打 bookmarked：它豁免所有自动降级，
                # 否则清理脚本下次重跑会把它从池子里悄悄拿掉。
                con.execute(
                    "INSERT INTO chat_actions(canonical_id, action, status, message) "
                    "VALUES (?, ?, 'done', ?)", (cid, action, msg))
                if action == "applied":
                    con.execute(
                        "UPDATE jobs SET bookmarked=1 WHERE canonical_id=?", (cid,))
                con.commit()
                con.close()
                self._send(200, json.dumps({"ok": True}).encode(), "application/json")
                return
            if path == "/demote":
                # 手工降级同时清掉 bookmark，否则投递池查询 (active OR bookmarked)
                # 会把它又捞回来
                sql = ("UPDATE jobs SET retention_status='sample', bookmarked=0 "
                       "WHERE canonical_id=? AND retention_status='active'")
                err = "未找到或已降级"
            else:
                # 撤销时打 bookmarked=1：手工判断优先于脚本判断。原先只改
                # retention_status='active'，下次 apply_glm_filters/clean_local_db
                # 重跑会按画像口径再次降级，用户的撤销被静默撤销。
                sql = ("UPDATE jobs SET retention_status='active', bookmarked=1 "
                       "WHERE canonical_id=? AND retention_status='sample'")
                err = "未找到或未降级"
            con = sqlite3.connect(DB)
            cur = con.execute(sql, (cid,))
            con.commit()
            con.close()
            if cur.rowcount == 0:
                self._send(200, json.dumps({"ok": False, "err": err}).encode(), "application/json")
                return
            # 重新生成 HTML，保持页面与 DB 同步
            subprocess.run([sys.executable, str(ROOT / "scripts" / "export_pool.py")],
                           capture_output=True)
            self._send(200, json.dumps({"ok": True}).encode(), "application/json")
        except Exception as exc:
            self._send(200, json.dumps({"ok": False, "err": str(exc)[:100]}).encode(), "application/json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"投递池服务: http://127.0.0.1:{args.port}/  (Ctrl+C 停止)")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
