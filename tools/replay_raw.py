#!/usr/bin/env python3
"""原样重放抓包报文：把一段真实抓到的请求**一字不改**地发出去。

【它解决什么问题】
当"我们组装的包"被服务端拒绝，而"抓包里的包"是成功的，就只剩两种可能：

1. 我们的字节与抓包**仍有差异**（某个字段值/顺序/编码细节）；
2. 与字节无关，是**服务端状态**问题（设备绑定、时间戳过期、账号状态）。

这两条路的排查方向完全相反。判别方法很直接：**把抓包的字节原样重发一次**。

- 原样重发**成功** → 差异在我们这边 → 逐字节 diff 立刻能定位；
- 原样重发**也失败** → 与字节无关 → 别再折腾组装代码了。

【为什么还需要 --fresh-timestamp】
抓包里的 ``timeStampClient`` 是**当时**的时间。隔几小时再原样重发，
服务端可能因"时间戳过旧"拒绝，从而把「时间戳问题」误判成「字节差异」。
加上这个开关把时间戳换成当前值，两个实验一对比就能区分开。

【用法】
    python tools/replay_raw.py captures/raw-login-request.b64
    python tools/replay_raw.py captures/raw-login-request.b64 --fresh-timestamp

【⚠️ 会真的发包】
请只重放你**自己账号**的抓包。它做的是官方客户端做过的事，
但重放别人的报文显然不合适。
"""

from __future__ import annotations

import argparse
import base64
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from client.game_client import GameClient  # noqa: E402
from models.envelope import parse_envelope  # noqa: E402
from models.protobuf_wire import (  # noqa: E402
    WIRE_LENGTH_DELIMITED,
    WIRE_VARINT,
    ProtobufWireError,
    decode_varint,
    encode_int,
)

#: ``timeStampClient`` 的字段编号（RootPacket 元信息，实测）
_TIMESTAMP_CLIENT_TAG: int = 12


def force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def split_http_message(raw: bytes) -> bytes:
    """从「完整 HTTP 报文」里取出 body。

    :raises ValueError: 找不到头与体之间的空行时（说明不是完整报文）。

    一个容易忽略的点：分隔符是 ``\\r\\n\\r\\n``（**两次** CRLF），
    只写一次会把一行的结尾当成"头部结束"，截出来的 body 会多几个字节。
    """
    marker = b"\r\n\r\n"
    position = raw.find(marker)
    if position < 0:
        raise ValueError("报文里找不到头/体分隔符 \\r\\n\\r\\n，可能不是完整 HTTP 报文")
    return raw[position + len(marker) :]


def load_raw_request(path: Path) -> tuple[bytes, bytes]:
    """读 base64 文件，返回 ``(完整报文, body)``。"""
    text = path.read_text(encoding="utf-8").strip()
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{path} 不是合法 base64：{exc}") from exc
    return raw, split_http_message(raw)


def replace_varint_field(body: bytes, field_number: int, value: int) -> bytes:
    """替换某个 varint 字段的值。

    :raises ValueError: body 里找不到该字段时。
    """
    position = 0
    while position < len(body):
        tag_start = position
        tag, position = decode_varint(body, position)
        number, wire_type = tag >> 3, tag & 7
        if wire_type == WIRE_VARINT:
            _old, end = decode_varint(body, position)
            if number == field_number:
                return body[:tag_start] + encode_int(number, value) + body[end:]
            position = end
        elif wire_type == WIRE_LENGTH_DELIMITED:
            length, start = decode_varint(body, position)
            position = start + length
        else:
            raise ValueError(f"不支持的线类型 {wire_type}（字段 {number}）")
    raise ValueError(f"body 里没有字段 {field_number}")


def replace_string_field(body: bytes, field_number: int, value: str) -> bytes:
    """替换某个 length-delimited 字段的值（等长度替换）。

    :raises ValueError: 找不到字段，或新值长度与旧值不同时 ——
        长度不同会让后面所有字段偏移，那种"半新半旧"的包没有任何诊断价值。
    """
    from models.protobuf_wire import encode_string

    position = 0
    while position < len(body):
        tag_start = position
        tag, position = decode_varint(body, position)
        number, wire_type = tag >> 3, tag & 7
        if wire_type == WIRE_LENGTH_DELIMITED:
            length, start = decode_varint(body, position)
            end = start + length
            if number == field_number:
                old = body[start:end]
                new = value.encode("utf-8")
                if len(new) != len(old):
                    raise ValueError(
                        f"字段 {field_number} 新值长度 {len(new)} 与旧值 {len(old)} 不同，"
                        "等长替换才能保证后续字段不错位"
                    )
                return body[:tag_start] + encode_string(number, value) + body[end:]
            position = end
        elif wire_type == WIRE_VARINT:
            _old, position = decode_varint(body, position)
        else:
            raise ValueError(f"不支持的线类型 {wire_type}（字段 {number}）")
    raise ValueError(f"body 里没有字段 {field_number}")


#: ``vCode`` 的字段编号（RootPacket 元信息，实测）
_VCODE_TAG: int = 14


def replace_timestamp(body: bytes, *, new_value: int | None = None) -> bytes:
    """把 ``timeStampClient``（字段 12）的值换成当前时间。

    :param new_value: 指定值；``None`` 表示取当前毫秒时间戳。
    :raises ValueError: body 里找不到该字段时。

    实现方式：顺序扫一遍字段，定位它的字节范围再替换 ——
    不用"搜索 0x60"是因为那个字节完全可能出现在其它字段的载荷里
    （子包里就有一堆），搜到错误位置会生成一个语义完全不同的包。
    """
    stamp = int(time.time() * 1000) if new_value is None else new_value
    return replace_varint_field(body, _TIMESTAMP_CLIENT_TAG, stamp)


def dump(label: str, data: bytes, *, width: int = 64) -> None:
    print(f"{label}（{len(data)} 字节）：")
    text = data.hex()
    for index in range(0, len(text), width):
        print("  " + text[index : index + width])


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="replay_raw.py",
        description="把抓包报文原样重放，用于区分「字节差异」与「服务端状态」",
    )
    parser.add_argument("file", help="base64 文件（内容为完整 HTTP 报文）")
    parser.add_argument(
        "--fresh-timestamp",
        action="store_true",
        help="把 timeStampClient 换成当前时间（抓包时间戳过旧时用）",
    )
    parser.add_argument(
        "--random-vcode",
        action="store_true",
        help="把 vCode 换成新的随机值 —— 用来判别「vCode 是否必须与时间戳配对」",
    )
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"✘ 文件不存在：{path}")
        return 1

    try:
        raw, body = load_raw_request(path)
    except ValueError as exc:
        print(f"✘ {exc}")
        return 1

    print("=" * 72)
    print(f"来源文件 : {path}")
    print(f"完整报文 : {len(raw)} 字节（含 HTTP 头）")
    print(f"请求体   : {len(body)} 字节")

    if args.random_vcode:
        try:
            updated = replace_string_field(body, _VCODE_TAG, secrets.token_hex(16))
        except ValueError as exc:
            print(f"✘ 替换 vCode 失败：{exc}")
            return 1
        print("vCode    : 已替换为**新的随机值**（其余字节保持抓包原样）")
        body = updated

    if args.fresh_timestamp:
        try:
            updated = replace_timestamp(body)
        except (ValueError, ProtobufWireError) as exc:
            print(f"✘ 替换时间戳失败：{exc}")
            return 1
        print(f"时间戳   : 已替换为当前（请求体长度 {len(body)} → {len(updated)}）")
        body = updated

    dump("请求体", body)

    print("=" * 72)
    with GameClient() as game:
        response = game.post_raw(body)
        print(
            f"← HTTP {response.status_code} | {len(response.content)} 字节 | "
            f"{response.headers.get('Content-Type')}"
        )
        dump("响应体", response.content)

        print("=" * 72)
        if not response.content:
            print("响应体为空")
            return 1
        try:
            view = parse_envelope(response.content)
        except Exception as exc:  # noqa: BLE001
            print(f"解析失败：{type(exc).__name__}: {exc}")
            return 1
        print(f"{view.describe()}")
        dump("响应子包", view.sub_packet_bytes[:256])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
