"""一次性：看 BOSS 的 security-js 挑战脚本到底探测什么、用什么算法。

结论已进记忆（boss-job-radar「stoken 是什么」一节），留作证据。
用途是回答「stoken 能不能在 Python 里本地算」——
2026-08-28 归档的 `stoken_iv8_deadend.py` 说不能（服务端还校验环境指纹），
本脚本是对「指纹」这个说法的直接取证。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "data" / "calibration" / "security.js"


def main() -> None:
    s = JS.read_text(encoding="utf-8", errors="replace")
    print(f"脚本大小 {len(s)} 字节\n")

    print("=== 环境探测点（浏览器指纹相关）===")
    probes = [
        "navigator", "screen", "plugins", "languages", "platform",
        "hardwareConcurrency", "deviceMemory", "webdriver", "WebGL",
        "getContext", "toDataURL", "AudioContext", "Notification",
        "permissions", "localStorage", "sessionStorage", "cookie",
        "setTimeout", "Math.random", "charCodeAt", "fromCharCode",
        "btoa", "atob", "XMLHttpRequest", "fetch", "appendChild",
        "createElement", "addEventListener", "iframe", "Worker",
    ]
    for p in probes:
        n = s.count(p)
        if n:
            print(f"  {p:<22} {n}")

    print("\n=== 混淆规模 ===")
    # 正则里要匹配「反斜杠 + x + 两位十六进制」，反斜杠本身必须转义成 \\
    print(f"  十六进制转义: {len(re.findall(r'\\\\x[0-9a-fA-F]{2}', s))}")
    print(f"  _0x 风格标识符: {len(set(re.findall(r'_0x[0-9a-f]+', s)))} 个唯一")
    # 长字符串数组是「明文关键字消失」的直接解释
    print(f"  长字符串字面量(>200字符): {len(re.findall(chr(34) + '[^' + chr(34) + ']{200,}' + chr(34), s))}")

    print("\n=== 哈希算法特征常量 ===")
    # MD5 的初始状态常量，十进制与十六进制两种写法都查
    for name, variants in [
        ("MD5 h0 (0x67452301)", ["0x67452301", "1732584193"]),
        ("MD5 h1 (0xefcdab89)", ["0xefcdab89", "4023233417"]),
        ("SHA1/SHA256 迹象", ["0x5a827999", "0x6a09e667"]),
        ("类型化数组", ["ArrayBuffer", "Uint32Array", "Uint8Array"]),
    ]:
        hits = {v: s.count(v) for v in variants if s.count(v)}
        print(f"  {name}: {hits or '无'}")

    print("\n=== 是否引用 cookie 名 ===")
    for c in ("__zp_stoken__", "zp_stoken", "stoken", "__zp"):
        print(f"  {c}: {s.count(c)}")

    print("\n=== 判读 ===")
    print("  开头的 h0..h3 + 64 字节块结构是标准 MD5 实现。")
    print("  明文关键字（seed/cookie/__zp_stoken__）计数为 0，说明字符串被拆成")
    print("  数组/十六进制转义，运行时再拼 —— 这正是静态分析读不出逻辑的原因，")
    print("  也是「本地复现签名」代价高的原因：要跟着混淆走一遍，")
    print("  而服务端还会校验环境指纹（见 stoken_iv8_deadend.py 的第 1 条）。")


if __name__ == "__main__":
    main()
