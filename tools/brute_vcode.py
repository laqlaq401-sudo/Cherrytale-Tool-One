#!/usr/bin/env python3
"""暴力测试 ``vCode`` 的生成算法（纯离线，不联网）。

【为什么可以这样试】
我们手里有一对**精确样本**（来自真实抓包，两个值都是确定无疑的）：

    timeStampClient = 1789905913885
    vCode           = 92AEDFDDD86362AD5A1FB38C817EAC80

而判据是 **32 位十六进制精确相等** —— 只要命中，就绝不可能是巧合。

【背景：为什么非要算出它】
实测结论（四条对照实验）：
    - 原样重用抓包里的 ``(时间戳, vCode)`` 这一对 → **通过**（且可重复用、不校验新鲜度）；
    - 只换时间戳、或只换 vCode、或两个都换 → **全部被拒**。
这说明两者之间存在函数关系 ``vCode = f(timeStampClient)``，
而真实客户端**每个包都算一个新值**。我们要么复刻这个算法，要么就只能一直用
"抓包那一对"（那只能过 1001，过不了 1003）。

【用法】
    python tools/brute_vcode.py
    python tools/brute_vcode.py --target 92AEDFDDD86362AD5A1FB38C817EAC80 --timestamp 1789905913885
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: 默认样本（实测抓包，来自 captures/raw-login-request.b64 的那次登录）
DEFAULT_TIMESTAMP = 1789905913885
DEFAULT_TARGET = "92AEDFDDD86362AD5A1FB38C817EAC80"

#: 抓包文件（用来提取真实参与值：deviceID / userId / 版本号……）
CAPTURE_FILE = Path(__file__).resolve().parent.parent / "captures/raw-login-request.b64"

#: 常见盐值候选（``ecchigame`` 来自抓包里的 ``customAccount`` 字段，最可疑）
SALTS: tuple[str, ...] = (
    "",
    "-1",
    "0",
    "9",
    "auer",
    "AUER",
    "auerweb",
    "ecchi",
    "ecchigame",
    "EcchiGame",
    "cherrytale",
    "Cherrytale",
    "CHERRYTALE",
    "CherryTale",
    "ct",
    "CT",
    "salt",
    "Salt",
    "SALT",
    "key",
    "Key",
    "secret",
    "vcode",
    "game",
)

#: 分隔符候选
SEPARATORS: tuple[str, ...] = ("", "-", "_", "|", ":", ",", ".", " ")


def load_capture_values() -> dict[str, str]:
    """从抓包的 1001 请求里取出**精确的**字段值（用项目自己的解码器）。

    早先用"正则扫描可打印串"的办法提出来的是噪声
    （``'2.2.0-el-h-winH'``、``'zhcnp'`` 都混进了后续字节），
    deviceID 更是完全没提到 —— 而它恰恰是最可能的种子值。
    """
    values: dict[str, str] = {}
    if not CAPTURE_FILE.exists():
        return values

    from models.envelope import parse_envelope
    from models.server import AccountClientInfoLoginPacket

    raw = base64.b64decode(CAPTURE_FILE.read_text(encoding="utf-8").strip())
    body = raw.split(b"\r\n\r\n", 1)[-1]
    packet = parse_envelope(body).decode_sub_packet(AccountClientInfoLoginPacket)
    for name in type(packet).model_fields:
        value = getattr(packet, name)
        if isinstance(value, str) and value and value != "-1":
            values[name] = value
    return values


def timestamp_forms(stamp: int) -> dict[str, str]:
    """时间戳的几种常见写法。"""
    return {
        "decimal": str(stamp),
        "seconds": str(stamp // 1000),
        "hex": format(stamp, "x"),
        "hex_upper": format(stamp, "X"),
        "le_hex": stamp.to_bytes(8, "little").hex(),
        "be_hex": stamp.to_bytes(8, "big").hex(),
    }


def hashes(text: str) -> dict[str, str]:
    """同一个字符串的几种哈希（大写 / 小写）。"""
    data = text.encode("utf-8")
    return {
        "md5": hashlib.md5(data).hexdigest().upper(),
        "md5_lower": hashlib.md5(data).hexdigest(),
        "sha1_32": hashlib.sha1(data).hexdigest()[:32].upper(),
        "sha256_32": hashlib.sha256(data).hexdigest()[:32].upper(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="brute_vcode.py", description="暴力测试 vCode 生成算法（离线）"
    )
    parser.add_argument("--target", default=DEFAULT_TARGET, help="已知的 vCode")
    parser.add_argument("--timestamp", type=int, default=DEFAULT_TIMESTAMP)
    args = parser.parse_args(argv)

    target = args.target.upper()
    print(f"目标 vCode = {target}")
    print(f"时间戳     = {args.timestamp}")

    extra = load_capture_values()
    print(f"从抓包提取的真实值：{extra}")
    print()

    # 参与运算的"片段"池
    pieces: dict[str, str] = {}
    for name, value in timestamp_forms(args.timestamp).items():
        pieces[f"ts.{name}"] = value
    for name, value in extra.items():
        pieces[name] = value
    for salt in SALTS:
        if salt:
            pieces[f"salt.{salt}"] = salt

    names = list(pieces)
    tried = 0
    for size in (1, 2, 3):
        for combo in itertools.permutations(names, size):
            for separator in SEPARATORS:
                text = separator.join(pieces[name] for name in combo)
                tried += 1
                for kind, digest in hashes(text).items():
                    if digest == target:
                        print("✔ 命中了！")
                        print(f"   算法 = {kind}({' + '.join(combo)}，分隔符 {separator!r})")
                        print(f"   输入 = {text!r}")
                        return 0

    print(f"✘ 试了 {tried} 种组合，未命中。")
    print("  说明 f 不是「几个已知值拼接后直接哈希」这种简单形式 ——")
    print("  下一步应反汇编 NetWorkModule.GetRootPacketVCode 的完整调用链。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
