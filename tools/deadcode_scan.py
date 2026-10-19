"""只读死代码扫描：找出 src/job_radar 下没有任何外部引用的模块级函数/类。

判据很朴素但有效：名字在整个仓库的 .py 里，除了定义那一行，还出现过没有。
不做跨文件语义分析（本项目没有动态注册机制），但会把 `getattr(self, "x")`
这类间接引用也算进去，避免误报。

输出分三档：
  · DEAD     —— 零外部引用
  · TESTONLY —— 只有 tests/ 引用
  · OK       —— 有 src/ 或 local/ 的真实调用点
"""
from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def iter_py(base: Path):
    for p in base.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def main() -> None:
    # 1. 收集所有模块级定义
    defs: dict[str, list[tuple[Path, int, str]]] = defaultdict(list)
    for path in iter_py(SRC):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            print(f"  [语法错误] {path.relative_to(ROOT)}: {exc}")
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # 带装饰器的（typer 的 @app.command()、dataclass 等）靠装饰器注册，
                # 名字不会被别处引用 —— 一律当作活代码，避免整片 CLI 命令误报。
                if node.decorator_list:
                    continue
                kind = "class" if isinstance(node, ast.ClassDef) else "def"
                defs[node.name].append((path, node.lineno, kind))

    # 2. 统计引用
    files = list(iter_py(ROOT / "src")) + list(iter_py(ROOT / "tests")) + list(iter_py(ROOT / "local"))
    text_cache: dict[Path, list[str]] = {}
    for path in files:
        try:
            text_cache[path] = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

    report = {"dead": [], "testonly": [], "ok": []}
    for name, where in defs.items():
        if name.startswith("__") or name in ("main",):
            continue
        pat = re.compile(rf"\b{re.escape(name)}\b")
        hits = {"src": 0, "tests": 0, "local": 0}
        for path, lines in text_cache.items():
            area = ("src" if SRC in path.parents
                    else "tests" if (ROOT / "tests") in path.parents
                    else "local")
            for idx, line in enumerate(lines, start=1):
                if not pat.search(line):
                    continue
                # 跳过定义行本身
                if any(path == p and idx == ln for p, ln, _ in where):
                    continue
                # 跳过注释行
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                hits[area] += 1
        entry = {
            "name": name,
            "kind": where[0][2],
            "file": str(where[0][0].relative_to(ROOT)),
            "line": where[0][1],
            "refs": hits,
        }
        if hits["src"] or hits["local"]:
            report["ok"].append(entry)
        elif hits["tests"]:
            report["testonly"].append(entry)
        else:
            report["dead"].append(entry)

    print(f"=== 模块级定义总数: {len(defs)} ===")
    print(f"  有真实引用 : {len(report['ok'])}")
    print(f"  仅测试引用 : {len(report['testonly'])}")
    print(f"  零引用     : {len(report['dead'])}")
    print()
    for key, title in (("dead", "DEAD（零外部引用）"), ("testonly", "TESTONLY（只有测试引用）")):
        print(f"--- {title} ---")
        for e in sorted(report[key], key=lambda x: (x["file"], x["line"])):
            print(f"  {e['file']}:{e['line']:<5} {e['kind']:5} {e['name']:<34} refs={e['refs']}")
        print()

    out = ROOT / "local" / "deadcode.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 未被任何地方 import 的模块 ----
    print("--- 未被 import 的模块 ---")
    all_text = "\n".join("\n".join(lines) for lines in text_cache.values())
    orphans = []
    for path in iter_py(SRC / "job_radar"):
        rel = path.relative_to(SRC / "job_radar")
        mod = ".".join(rel.with_suffix("").parts)
        short = rel.stem
        if short in ("__init__", "__main__"):
            continue
        patterns = (rf"job_radar\.{re.escape(mod)}\b",
                    rf"\bfrom\s+\.{{1,2}}[\w.]*\b{re.escape(short)}\b\s+import",
                    rf"\bimport\s+[^\n]*\b{re.escape(short)}\b")
        if not any(re.search(p, all_text) for p in patterns):
            orphans.append(str(rel))
    for name in sorted(orphans):
        print(f"  {name}")
    if not orphans:
        print("  （无）")


if __name__ == "__main__":
    main()
