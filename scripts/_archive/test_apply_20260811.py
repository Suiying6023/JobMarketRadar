#!/usr/bin/env python3
import json, sqlite3, time, urllib.request
from pathlib import Path

DB = Path(__file__).parent.parent / "data/hangzhou_agent.sqlite3"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

cid = con.execute(
    "SELECT canonical_id FROM jobs WHERE retention_status='active' "
    "AND job_description!='' LIMIT 1"
).fetchone()[0]
print(f"测试岗位: {cid}")

def post(path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:8000{path}",
        data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

print("POST /apply →", post("/apply", {"id": cid}))
print("POST /note  →", post("/note", {"id": cid, "message": "HR 说下周面"}))

time.sleep(0.5)
rows = list(con.execute("SELECT action, message, created_at FROM chat_actions WHERE canonical_id=?", (cid,)))
print(f"\nchat_actions ({len(rows)} 条):")
for r in rows:
    print(" ", dict(r))
print("bookmarked:", con.execute("SELECT bookmarked FROM jobs WHERE canonical_id=?", (cid,)).fetchone()[0])


DB = Path(__file__).parent.parent / "data/hangzhou_agent.sqlite3"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

# 取一条 active 岗位测试
cid = con.execute(
    "SELECT canonical_id FROM jobs WHERE retention_status='active' "
    "AND job_description!='' LIMIT 1"
).fetchone()[0]
print(f"测试岗位: {cid}")

# 测试 apply
r = requests.post("http://127.0.0.1:8000/apply", json={"id": cid})
print(f"POST /apply → {r.status_code}: {r.text}")

# 测试 note
r = requests.post("http://127.0.0.1:8000/note", json={"id": cid, "message": "HR 说下周面"})
print(f"POST /note → {r.status_code}: {r.text}")

# 验证入库
time.sleep(0.5)
rows = list(con.execute("SELECT action, message, created_at FROM chat_actions WHERE canonical_id=?", (cid,)))
print(f"\n=== chat_actions 表（{len(rows)} 条）===")
for r in rows:
    print(f"  {dict(r)}")

bookmarked = con.execute("SELECT bookmarked FROM jobs WHERE canonical_id=?", (cid,)).fetchone()[0]
print(f"\nbookmarked: {bookmarked}")
