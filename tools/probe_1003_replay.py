"""临时脚本：用抓包的 ``(timeStampClient, vCode)`` 构造 1003 并真发。

【为什么这是决定性实验】
我们的 1003 已经与抓包**结构完全一致**（178 字节，字段逐个对齐），唯一差别就是
``timeStampClient`` 与 ``vCode`` 这两个"每次都变"的字段。所以只要把这两个换成
抓包里的真值，我们的包就应当与抓包**等价**：

    - 若这样发仍然被拒 → 说明差异不在这些字段上，得另找原因；
    - 若这样发成功     → 说明只要用"抓包那一对"就能过，
      那么问题就是「我们固定用的那一对只对 1001 有效」，需要复刻 vCode 算法。

【数据来源（两处独立印证，误差仅 273ms）】
    - 1003.har 的 startedDateTime：2026-09-20T20:41:04.858+08:00 → 1789908064858
    - 用户提供的 1003 报文里的 timeStampClient → 1789908064585
    - vCode 取自 1003.har 里**可读**的文本 → 93ABA44B13800136EBED3CF097981137

用完即删。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.game_client import GameClient  # noqa: E402
from models.envelope import parse_envelope  # noqa: E402
from models.handshake import SpecialActivityRefreshClass  # noqa: E402
from models.server import (  # noqa: E402
    AccountClientInfoLoginPacket,
    AccountClientInfoLoginRes,
    LoginPacket,
)

TS = 1789908064585
VCODE = "93ABA44B13800136EBED3CF097981137"
ACTIVE_MISSION = "2DB9B982CA690276061B90E63F3C3D38"
SERVER_ID = 98
ACCOUNT_ID = 10000004
USER_ID = "ER00000000-0000-0000-0000-000000000000"


def main() -> int:
    with GameClient() as game:
        game.handshake()
        view = game.send(
            AccountClientInfoLoginPacket.for_platform_user(USER_ID),
            expect=AccountClientInfoLoginRes,
            include_defaults=True,
        )
        account = view.decode_sub_packet(AccountClientInfoLoginRes)
        print(f"1001 成功：accountID={account.accountID}")

        server = account.choose_server(server_id=SERVER_ID)
        packet = LoginPacket.for_server(
            server,
            account_id=ACCOUNT_ID,
            platform_user_id=USER_ID,
            channel_platform_type=2,
            client_version="2.2.0-el-h-win",
            language_code="zhcn",
        )

        game.envelope.time_stamp_client = TS
        game.envelope.v_code = VCODE
        game.envelope.activity = SpecialActivityRefreshClass(
            activeMission=ACTIVE_MISSION, puzzleRoleGroup=""
        )
        body = game.build(packet, include_defaults=True).body
        print(f"构造的 1003 = {len(body)} 字节（抓包 = 178 字节）")
        print(f"  {body.hex()}")

        response = game.post_raw(body)
        print(f"← HTTP {response.status_code} | {len(response.content)} 字节")
        print(f"  {response.content[:130].hex()}")
        try:
            print(f"  {parse_envelope(response.content).describe()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  解析失败：{exc}")
        print(f"token = {game.envelope.token!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
