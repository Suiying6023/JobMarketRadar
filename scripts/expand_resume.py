"""expand_list 断点续跑：每轮 1 个关键词，浏览器崩溃自动重启，风控熔断整轮停。

背景（2026-08-02 实测）：expand_list 单进程连跑约 3 个关键词（15 页 + 2 次
security-check 恢复）后 Chrome 会崩溃（chrome+python 进程一起消失，无 Traceback）。
这是浏览器环境性崩溃，不是风控。本 wrapper：
1. 每轮只跑 1 个关键词 → 浏览器生命周期短，避免累积崩溃
2. 子进程非 0 退出且输出未含该词最后页 → 判为崩溃，重启续跑（最多 5 次）
3. 子进程 exit 2 → 熔断/风控，整轮停止，不重启
4. 全部关键词跑完即停

用法: python scripts/expand_resume.py [--pages N] [--delay S] [--start N] [--end N]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

# 从 config 读关键词，避免重复维护
sys.path.insert(0, str(ROOT / "src"))
from job_radar.config import load_config  # noqa: E402


def keyword_done(kw: str, pages: int, output_text: str) -> bool:
    """该关键词是否已完成（输出含最后页行）。"""
    return f"[{kw}|页{pages}]" in output_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=5)
    parser.add_argument("--delay", type=float, default=8.0)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=None)
    args = parser.parse_args()

    cfg = load_config("config.hangzhou.yaml")
    keywords = cfg.keywords[args.start:args.end]
    if not keywords:
        print("关键词切片为空")
        return
    print(f"expand_resume 启动: {len(keywords)} 个关键词, pages={args.pages}, "
          f"列表: {keywords}", flush=True)

    for idx, kw in enumerate(keywords, start=args.start):
        print(f"\n=== [{idx}] 关键词「{kw}」===", flush=True)
        done = False
        for attempt in range(1, 6):  # 每词最多 5 次尝试
            cmd = [str(VENV_PY), "-u", str(ROOT / "scripts" / "expand_list.py"),
                   "--pages", str(args.pages), "--delay", str(args.delay),
                   "--start", str(idx), "--end", str(idx + 1)]
            print(f"  尝试 {attempt}: {' '.join(cmd)}", flush=True)
            try:
                result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
            except subprocess.TimeoutExpired:
                print(f"  尝试 {attempt} 超时(15min)，视为浏览器卡死，重启", flush=True)
                continue
            out = result.stdout + result.stderr
            print(out, flush=True)  # 透传子进程输出
            if result.returncode == 2:
                print(f"  关键词「{kw}」触发熔断(exit 2)，整轮停止", flush=True)
                return
            if keyword_done(kw, args.pages, out):
                print(f"  关键词「{kw}」完成", flush=True)
                done = True
                break
            # 非 0 或输出不完整 → 浏览器崩溃，重启
            print(f"  尝试 {attempt} 未完成(rc={result.returncode})，可能浏览器崩溃，重启",
                  flush=True)
            time.sleep(5)  # 等浏览器/端口释放
        if not done:
            print(f"  关键词「{kw}」尝试 5 次仍失败，跳过（记录待办）", flush=True)
            continue

    print("\nexpand_resume 全部结束", flush=True)


if __name__ == "__main__":
    from _online_guard import guard
    guard()
    main()
