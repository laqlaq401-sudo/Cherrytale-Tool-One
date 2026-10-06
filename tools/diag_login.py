#!/usr/bin/env python3
"""登录包诊断工具：发一次 1001，把**请求与响应的原始字节**完整打出来。

【为什么需要它】
``main.py run login`` 在失败时只会抛出一句「子包无法按 Xxx 解析」——
而那通常意味着**服务端回的不是你以为的那个包**（多半是错误响应）。
此时最需要看的是「我发了什么、它回了什么」的原始字节，本工具就是干这个的。

【它做了什么】
1. 打印将发送的 1001 请求体（hex，可与抓包逐字节对照）；
2. 打印响应的 **HTTP 状态 + 原始 hex**；
3. 用 ``parse_envelope`` 解出响应里有哪些包、哪些元信息字段。

【⚠️ 它会真的发包】
这是**有副作用**的操作（登录），但它做的是"官方客户端本来就会做的事"，
参数也全部来自真实抓包，所以风险等同于正常启动一次游戏客户端。
如果只想看字节，请用 ``python main.py run login --dry-run``（那个不联网）。

【用法】
    python tools/diag_login.py
    python tools/diag_login.py --no-handshake   # 跳过握手（少发一个包）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from client.game_client import GameClient  # noqa: E402
from client.platform_client import PlatformClient  # noqa: E402
from models.envelope import parse_envelope  # noqa: E402
from models.server import AccountClientInfoLoginPacket  # noqa: E402


def force_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def dump(label: str, data: bytes, *, width: int = 64) -> None:
    """打印一段字节（hex 分行 + 长度）。"""
    print(f"{label}（{len(data)} 字节）：")
    text = data.hex()
    for index in range(0, len(text), width):
        print("  " + text[index : index + width])


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="diag_login.py", description="发一次 1001 并打印请求/响应原始字节"
    )
    parser.add_argument(
        "--no-handshake", action="store_true", help="跳过握手（默认会先握手取配置指纹）"
    )
    parser.add_argument(
        "--vcode", metavar="HEX", help="指定 vCode（默认随机）—— 用于复现抓包的字节"
    )
    parser.add_argument(
        "--setting-md5", metavar="值", help="指定 settingMd5（默认 -1，与抓包一致）"
    )
    args = parser.parse_args(argv)

    if not config.has_auth_token():
        print("✘ 未配置平台令牌，无法取 userId（见 .auth_token 或 CHERRYTALE_AUTH_TOKEN）")
        return 1

    print("=" * 72)
    print("① 平台身份")
    with PlatformClient() as platform:
        user = platform.get_user_info()
    print(f"  userId = {user.user_id}")
    print(f"  coins  = {user.coins}")

    packet = AccountClientInfoLoginPacket.for_platform_user(user.user_id)
    print(f"  登录包：socialAccountType={packet.socialAccountType}"
          f" customAccount={packet.customAccount!r}"
          f" deviceID={packet.deviceID[:12]}…")

    with GameClient() as game:
        # 允许覆盖两个"每次请求都会变"的字段，用于复现抓包字节
        if args.vcode:
            game.envelope.v_code = args.vcode
            print(f"  vCode 覆盖为 {args.vcode}")
        if args.setting_md5 is not None:
            game.envelope.setting_md5 = args.setting_md5
            print(f"  settingMd5 覆盖为 {args.setting_md5!r}")

        print("=" * 72)
        if args.no_handshake:
            print("② 握手：已跳过")
        else:
            print("② 握手（取服务端配置与活动指纹）")
            server_config = game.handshake()
            print(f"  {server_config.summary()}")
            print(f"  activity 指纹 = {game.envelope.activity}")

        print("=" * 72)
        print("③ 发送 1001")
        request = game.build(packet, include_defaults=True)
        dump("  请求体", request.body)

        response = game.post_raw(request.body)
        print(f"  ← HTTP {response.status_code} | {len(response.content)} 字节 | "
              f"{response.headers.get('Content-Type')}")
        dump("  响应体", response.content)

        print("=" * 72)
        print("④ 响应解析")
        if not response.content:
            print("  响应体为空 —— 服务端没有返回任何内容（可能是 5xx 或连接被重置）")
            return 1
        try:
            view = parse_envelope(response.content)
        except Exception as exc:  # noqa: BLE001
            print(f"  解析失败：{type(exc).__name__}: {exc}")
            return 1

        print(f"  {view.describe()}")
        print(f"  主响应子包 {len(view.sub_packet_bytes)} 字节")
        dump("  子包", view.sub_packet_bytes[:256])
        for name, field in view.meta.items():
            print(f"  元信息 {name}: {field.describe()[:100]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
