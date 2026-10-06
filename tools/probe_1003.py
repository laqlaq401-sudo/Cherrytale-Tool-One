#!/usr/bin/env python3
"""探测 1003（进区）的正确报文形态：四种「默认值 × 活动指纹」组合各发一次。

【为什么需要它】
1001（账号登录）已经打通，但 1003 一律被拒（``ServerExceptionRes`` +
``ABNORMAL_PACKET``）。抓包里**没有** 1003/1004 —— ``cherrytale-fiddler-1.har``
只记录到「握手 + 1001 + 进入游戏之后」的请求（进区后请求里那个 32 字符
token 只能来自 1004，但 1004 本身没被抓到），所以无法逐字节比对。

于是换成「穷举组合 + 看服务端反应」：信封有两个可切开关，各两种取值，
四种组合各发一次（同一个 1001 会话，只重复发 1003）：

    =====  ==================  ===================
    组合   include_defaults    include_activity
    =====  ==================  ===================
    A      True                True
    B      True                False    ← 当前实现
    C      False               True
    D      False               False
    =====  ==================  ===================

哪一种能拿到 1004（并回填出 ``token``），那就是正确形态。

【⚠️ 它会真的发包】发 1 次 1001 + 最多 4 次 1003，等同于启动一次客户端。

【用法】
    python tools/probe_1003.py
    python tools/probe_1003.py --server-id 95
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.game_client import GameClient  # noqa: E402
from client.platform_client import PlatformClient  # noqa: E402
from models.envelope import parse_envelope  # noqa: E402
from models.server import (  # noqa: E402
    AccountClientInfoLoginPacket,
    AccountClientInfoLoginRes,
    LoginPacket,
)


def force_utf8_output() -> None:
    """Windows 控制台默认编码打不出中文，这里强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="probe_1003.py", description="穷举 1003 的报文形态（真发包）"
    )
    parser.add_argument("--server-id", type=int, help="指定区服编号（默认自动挑有角色的区）")
    args = parser.parse_args(argv)

    with PlatformClient() as platform:
        user = platform.get_user_info()
    print(f"平台 userId = {user.user_id}")
    print("=" * 72)

    with GameClient() as game:
        server_config = game.handshake()
        print(f"握手：{server_config.summary()}")

        view = game.send(
            AccountClientInfoLoginPacket.for_platform_user(user.user_id),
            expect=AccountClientInfoLoginRes,
            include_defaults=True,
        )
        account = view.decode_sub_packet(AccountClientInfoLoginRes)
        print(f"1001 成功：accountID={account.accountID}，区服 {len(account.allServerList)} 个")

        server = account.choose_server(
            server_id=args.server_id, prefer_with_character=True
        )
        print(
            f"选中区服 #{server.serverID} {server.serverName}"
            f"（本账号有角色={server.has_character()}）"
        )
        print("=" * 72)

        zone_packet = LoginPacket.for_server(
            server,
            account_id=account.accountID,
            platform_user_id=user.user_id,
        )

        for defaults, activity in itertools.product((True, False), repeat=2):
            label = f"defaults={str(defaults):5} activity={str(activity):5}"
            body = game.build(
                zone_packet, include_defaults=defaults, include_activity=activity
            ).body
            response = game.post_raw(body)
            raw = response.content
            print(f"  {label} | 请求 {len(body):4}B | 响应 {len(raw):5}B")
            print(f"      原始响应：{raw[:110].hex()}")
            try:
                zone_view = parse_envelope(raw)
            except Exception as exc:  # noqa: BLE001
                print(f"      ✘ 解析失败：{exc}")
                continue
            game.envelope.update_from(zone_view)
            print(f"      → {zone_view.describe()[:78]}")
            if game.envelope.token:
                print(f"\n  ✔ 拿到 token！正确形态 = {label}")
                return 0

        print("\n  ✘ 四种组合都没拿到 token")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
