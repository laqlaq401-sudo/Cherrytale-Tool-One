#!/usr/bin/env python3
"""离线核对 1003（进区登录包）的字节：把「我们构造的包」与「抓包的包」逐字段对比。

【为什么需要它】
1003 的细节很容易踩坑，而这些坑**全都被服务端笼统地回一句 ABNORMAL_PACKET**，
从错误信息里看不出任何线索：

    - ``auerWebLogin_token`` 是字符串 ``"-1"``，不是空串；
    - ``hamiSubNoList`` 必须带**平台 userId**（服务端靠它定位账号）；
    - 信封的 activity 指纹（字段 16）要带；
    - ``vCode`` 与 ``timeStampClient`` 必须**成对**（单独换掉任一个都会被拒）。

本工具把这些差异**在本地就显示出来**，不必靠反复真实发包去试。

【用法】
    # 只打印我们构造的包（纯离线，不联网）
    python tools/verify_1003.py

    # 与抓包对比（body 的 hex 或 base64 都行，也接受整段 HTTP 报文）
    python tools/verify_1003.py --captured 088de9fd02106...
    python tools/verify_1003.py --captured-file captures/1003.txt

【⚠️ 关于两个「每次都变」的字段】
``vCode`` / ``timeStampClient`` 取自 ``config``（固定成抓包里那一对），
所以只有你给的抓包**也**用那一对时，才可能逐字节完全一致；
其它情况下请看**字段结构**是否相同，不要逐字节较真。
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from models.envelope import GameEnvelope  # noqa: E402
from models.handshake import SpecialActivityRefreshClass  # noqa: E402
from models.protobuf_wire import decode_varint  # noqa: E402
from models.server import LoginPacket, ServerListClass  # noqa: E402

#: 实测抓包里的取值（默认对齐它，便于与抓包对比）
DEFAULT_ACCOUNT_ID = 10000004
DEFAULT_USER_ID = "ER00000000-0000-0000-0000-000000000000"
DEFAULT_SERVER_ID = 98
DEFAULT_ACTIVE_MISSION = "2DB9B982CA690276061B90E63F3C3D38"


def force_utf8_output() -> None:
    """Windows 控制台默认编码打不出中文，这里强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def walk(data: bytes, indent: int = 0) -> None:
    """把一段 protobuf 按字段逐条打印（会递归进 length-delimited）。"""
    position = 0
    pad = "  " * indent
    while position < len(data):
        tag, position = decode_varint(data, position)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, position = decode_varint(data, position)
            print(f"{pad}#{number:<6} varint = {value}")
        elif wire == 2:
            length, start = decode_varint(data, position)
            payload = data[start : start + length]
            position = start + length
            text = payload.decode("utf-8", "replace")
            if payload and all(32 <= ord(ch) < 127 for ch in text):
                print(f"{pad}#{number:<6} text   = {text!r}")
            else:
                print(f"{pad}#{number:<6} bytes  = {length}B")
                walk(payload, indent + 1)
        else:
            print(f"{pad}#{number:<6} 线类型 {wire}（本工具不支持，后面的字段无法继续）")
            return


def parse_captured(raw_text: str) -> bytes:
    """把用户给的文本变成 body 字节：支持 hex / base64 / 整段 HTTP 报文。"""
    text = raw_text.strip()
    if all(ch in "0123456789abcdefABCDEF \t\r\n" for ch in text):
        return bytes.fromhex("".join(text.split()))
    try:
        blob = base64.b64decode(text, validate=False)
    except Exception:  # noqa: BLE001
        raise SystemExit("✘ 既不是 hex 也不是 base64，无法解析")
    marker = blob.find(b"\r\n\r\n")
    return blob[marker + 4 :] if marker >= 0 else blob


def build_ours(args: argparse.Namespace) -> bytes:
    """按参数构造我们的 1003 整包。"""
    server = ServerListClass(serverID=args.server_id, serverName="?")
    packet = LoginPacket.for_server(
        server,
        account_id=args.account_id,
        platform_user_id=args.user_id,
        channel_platform_type=config.CHANNEL_PLATFORM_TYPE_EL,
        client_version=config.GAME_CLIENT_VERSION,
        language_code=config.GAME_LANGUAGE,
    )
    envelope = GameEnvelope()
    envelope.activity = SpecialActivityRefreshClass(
        activeMission=args.active_mission, puzzleRoleGroup=""
    )
    return envelope.encode_with(packet, include_defaults=True)


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="verify_1003.py", description="离线核对 1003 的字节（不联网）"
    )
    parser.add_argument("--captured", metavar="HEX或BASE64", help="抓到的 1003 body")
    parser.add_argument("--captured-file", metavar="路径", help="从文件读取抓到的 body")
    parser.add_argument("--account-id", type=int, default=DEFAULT_ACCOUNT_ID)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID, help="平台 userId")
    parser.add_argument("--server-id", type=int, default=DEFAULT_SERVER_ID)
    parser.add_argument("--active-mission", default=DEFAULT_ACTIVE_MISSION)
    args = parser.parse_args(argv)

    ours = build_ours(args)

    captured: bytes | None = None
    if args.captured:
        captured = parse_captured(args.captured)
    elif args.captured_file:
        captured = parse_captured(Path(args.captured_file).read_text(encoding="utf-8"))

    if captured is None:
        print(f"我们构造的 1003（{len(ours)} 字节）：")
        walk(ours)
        print()
        print("提示：加 --captured <hex/base64> 可与抓包做逐字段对比。")
        return 0

    print(f"抓包 {len(captured)} 字节 | 我们 {len(ours)} 字节")
    print()
    print("========== 抓包 ==========")
    walk(captured)
    print()
    print("========== 我们 ==========")
    walk(ours)
    print()
    if captured == ours:
        print("✔ 逐字节完全一致")
        return 0

    print("✘ 存在差异（前几处）：")
    shown = 0
    for index in range(max(len(captured), len(ours))):
        left = captured[index] if index < len(captured) else None
        right = ours[index] if index < len(ours) else None
        if left != right:
            print(f"  偏移 {index:4}: 抓包 {left} vs 我们 {right}")
            shown += 1
            if shown >= 8:
                print("  …（只显示前 8 处）")
                break
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
