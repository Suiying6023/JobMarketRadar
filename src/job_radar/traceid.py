"""BOSS traceid 生成器：从页面全局函数 generateBossTraceID 逆向移植到纯 Python。

BOSS 的 detail.json 请求头带 traceid（格式 F-<19位uuid><3位checksum>），纯 HTTP 脱离
浏览器后无法调用页面里的 JS 函数，只能本地复刻。本模块用相同算法本地生成，
已用页面真值对验证（8/8 通过）。

相关：scripts/pure_http_client.py、scripts/probe_cdp_network.py
"""
from __future__ import annotations

import random
import time

CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
MASK32 = 0xFFFFFFFF


def _s32(v: int) -> int:
    """JS ToInt32：低 32 位按有符号解释。"""
    v &= MASK32
    return v - 0x100000000 if v >= 0x80000000 else v


def _u32(v: float) -> int:
    """JS ToUint32 语义：对浮点值截断小数后取低 32 位无符号。"""
    return int(v) & MASK32


def _checksum(uuid16: str) -> str:
    """computeChecksum：三段滚动哈希混合后映射到 CHARS，取 3 位。"""
    t = n = i = 0
    for a, ch in enumerate(uuid16):
        t = _s32(_s32(t << 5) - _s32(t) + ord(ch))
    for o in range(len(uuid16) - 1, -1, -1):
        n = _s32(_s32(n << 7) - _s32(n) + ord(uuid16[o]) * (o + 1))
    s = len(uuid16) // 2
    for r, ch in enumerate(uuid16):
        i = _s32(_s32(i << 3) - _s32(i) + ord(ch) * (abs(r - s) + 1))

    # 乘法必须用浮点（JS 里 2654435761 * x 是 double 乘法，有舍入），
    # 整数乘法结果不同，会算错 checksum。
    c = _s32(t ^ n)
    c = _u32(2654435761.0 * abs(c))
    c2 = _u32(2246822507.0 * _u32(c ^ (c >> 16)))
    c_ch = CHARS[_u32(c2 ^ (c2 >> 13)) % len(CHARS)]

    l = _s32(n ^ i)
    l = _u32(3266489909.0 * abs(l))
    l2 = _u32(2654435761.0 * _u32(l ^ (l >> 16)))
    l_ch = CHARS[_u32(l2 ^ (l2 >> 13)) % len(CHARS)]

    d = _s32(i ^ t)
    d = _u32(668265261.0 * abs(d))
    d2 = _u32(2246822507.0 * _u32(d ^ (d >> 16)))
    d_ch = CHARS[_u32(d2 ^ (d2 >> 13)) % len(CHARS)]

    return c_ch + l_ch + d_ch


def generate() -> str:
    """生成一条 traceid，格式 F-<19位uuid><3位checksum>。"""
    ts = f"{int(time.time() * 1000):x}".lower()
    ts = ("0" * 13 + ts)[-13:]
    rand = "".join(random.choice(CHARS) for _ in range(6))
    uuid16 = ts + rand
    return "F-" + uuid16 + _checksum(uuid16)


def _self_test() -> None:
    """用页面真值对验证算法移植正确性。"""
    pairs = [
        ("0019fbc0b5778H6waOD", "ZIy"),
        ("0019fbc0b5778g9TOia", "S1t"),
        ("0019fbc0b5778SML9mn", "VBA"),
        ("0019fbc0b57780VxrIg", "0bl"),
        ("0019fbc0b57789AqsZA", "YK1"),
        ("0019fbc0b5778GhePXu", "BNN"),
        ("0019fbc0b5778LWtUrG", "6Zw"),
        ("0019fbc0b5778oiLS68", "uBt"),
    ]
    for uuid16, expect in pairs:
        got = _checksum(uuid16)
        assert got == expect, f"{uuid16}: expect {expect} got {got}"
    sample = generate()
    assert sample.startswith("F-"), sample
    assert len(sample) == 24, f"len {len(sample)}: {sample}"


if __name__ == "__main__":
    _self_test()
    print("self-test 通过，样例:", generate())
