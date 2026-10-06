"""工会签到任务（``tasks/alliance_sign_in.py``）的单元测试。

运行方式：

    python -m pytest tests/test_tasks_alliance_sign_in.py -q
    python -m tests.test_tasks_alliance_sign_in

【本文件要钉死的四条规则】
1. **只发白名单消息号**（21055 拉面板、21057 签到、21061 领连线奖励）
   —— 从请求字节解消息号核对；
2. **挑格子的推断**（★ 2026-10-03 按抓包重写）：``bingoData`` 只含已占格子，
   位置取 ``max(已占 pos) + 1``（空面板取 0）—— 旧实现"取 iconData 为空的
   格子"永远为空，任务从未真正签过；
3. **演练模式零发包**；
4. **"没入会"是 ok=True**；签到成功后连线奖励失败**不推翻**签到成败。

消息号写死在本文件（不 import 生产常量）：协议编号被改动会在这里直接暴露。
"""

from __future__ import annotations

import pytest

from client.exceptions import NetworkError
from client.game_client import GameClient
from client.session import GameSession
from models.alliance import AllianceBingoSignInPacket
from models.envelope import parse_envelope
from models.protobuf_wire import encode_int, encode_message
from models.session_state import GameSessionState
from tasks.base import create_task

from tests.test_game_client import FakeResponse

import tasks.alliance_sign_in  # noqa: F401  导入即注册

#: 消息号写死在测试里（依据见 models/packet_ids.py 的 Alliance 段）
PACKET_BINGO_HOME = 21055
PACKET_BINGO_HOME_RES = 21056
PACKET_BINGO_SIGN_IN = 21057
PACKET_BINGO_SIGN_IN_RES = 21058
PACKET_BINGO_REWARD = 21061
PACKET_BINGO_REWARD_RES = 21062

ALLIANCE_SUCCESS = 1  # AllianceNetWorkModule.eResErrCode.Success
ERR_NOT_IN_ALLIANCE = 19


def make_session() -> GameSession:
    """构造一个**不会联网**的会话（域名是保留域名）。"""
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(GameSessionState(token="tok", player_id=1, server_id=1))
    return session


# ---------------------------------------------------------------------------
# 报文构造（字段编号见 models/proto_fields.py）
# ---------------------------------------------------------------------------
def cell(pos: int, *, signed: bool) -> bytes:
    """``AllianceBingoClass``：1 SignInID、2 pos、3 iconData（有值=已签）。"""
    payload = encode_int(1, 940000000 + pos) + encode_int(2, pos)
    if signed:
        payload += encode_message(3, b"\x01\x02")
    return payload


def bingo_home_payload(
    cells: list[tuple[int, bool]], error_code: int = ALLIANCE_SUCCESS
) -> bytes:
    """21056 载荷：1 errorCode、2 bingoData（repeated，只含列出的格子）。"""
    payload = encode_int(1, error_code)
    for pos, signed in cells:
        payload += encode_message(2, cell(pos, signed=signed))
    return payload


def sign_in_payload(
    *, error_code: int = ALLIANCE_SUCCESS, reward_item: tuple[int, int] | None = None
) -> bytes:
    """21058 载荷：1 errorCode、4 getObjClass（GetObjClass.itemList → ItemClass）。"""
    payload = encode_int(1, error_code)
    if reward_item is not None:
        item_id, amount = reward_item
        # GetObjClass: 1 itemList；ItemClass: 2 itemID、3 itemAmount
        payload += encode_message(
            4, encode_message(1, encode_int(2, item_id) + encode_int(3, amount))
        )
    return payload


def bingo_reward_payload(
    *, error_code: int = ALLIANCE_SUCCESS, reward_item: tuple[int, int] | None = None
) -> bytes:
    """21062 载荷：1 errorCode、2 getObjClass —— 与 10.3 抓包同构。"""
    payload = encode_int(1, error_code)
    if reward_item is not None:
        item_id, amount = reward_item
        payload += encode_message(
            2, encode_message(1, encode_int(2, item_id) + encode_int(3, amount))
        )
    return payload


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """拼响应字节：字段 1 = 消息号，业务子包槽位编号 = 消息号本身。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


def patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    home_payload: bytes | None = None,
    sign_payload: bytes | None = None,
    reward_payload: bytes | None = None,
) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成"记录请求 + 按消息号回放响应"的替身。"""
    recorded: list[bytes] = []
    home = home_payload if home_payload is not None else bingo_home_payload(
        [(0, True)]
    )
    sign = sign_payload if sign_payload is not None else sign_in_payload()
    reward = (
        reward_payload
        if reward_payload is not None
        else bingo_reward_payload(reward_item=(200000050, 100))
    )

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        recorded.append(body)
        packet_id = parse_envelope(body).packet_id
        if packet_id == PACKET_BINGO_HOME:
            return FakeResponse(envelope_bytes(PACKET_BINGO_HOME_RES, home))
        if packet_id == PACKET_BINGO_SIGN_IN:
            return FakeResponse(envelope_bytes(PACKET_BINGO_SIGN_IN_RES, sign))
        if packet_id == PACKET_BINGO_REWARD:
            return FakeResponse(envelope_bytes(PACKET_BINGO_REWARD_RES, reward))
        raise AssertionError(f"测试替身收到了意料之外的请求：{packet_id}")

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def sent_packet_ids(recorded: list[bytes]) -> list[int]:
    """从记录的请求体里解出全部消息号（按发送顺序）。"""
    return [parse_envelope(body).packet_id for body in recorded]


def sent_sign_positions(recorded: list[bytes]) -> list[int]:
    """解出所有 21057 的 pos（顺带证明字段编号对得上）。"""
    positions: list[int] = []
    for body in recorded:
        view = parse_envelope(body)
        if view.packet_id == PACKET_BINGO_SIGN_IN:
            positions.append(AllianceBingoSignInPacket.decode(view.sub_packet_bytes).pos)
    return positions


class TestSignInFlow:
    def test_signs_next_free_position_and_claims_reward(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """已占 {0,2} → 推断签 pos=3；签到后发 21061 领连线奖励。

        ★ 2026-10-03 重写：旧实现找"iconData 为空的格子"（响应里根本不存在）
        恒报"已签过"；新实现 max(已占)+1，并在签到成功后领 21061。
        """
        recorded = patch_transport(
            monkeypatch,
            home_payload=bingo_home_payload([(0, True), (2, True)]),
            sign_payload=sign_in_payload(reward_item=(200000030, 2)),
            reward_payload=bingo_reward_payload(reward_item=(200000050, 100)),
        )
        task = create_task("alliance_sign_in", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [
            PACKET_BINGO_HOME,
            PACKET_BINGO_SIGN_IN,
            PACKET_BINGO_REWARD,
        ]
        assert sent_sign_positions(recorded) == [3]  # max(0,2)+1
        assert result.ok is True
        assert "3 号格子" in result.message
        # ★ 2026-10-04 口径修正：21056 的 itemAmount 语义**尚未实证**
        # （同源 6006/11010/37020 已证实是"操作后持有量"），所以按保守口径
        # 只报**件数**，不再把口径未定的数字当"获得"念出来（原来打印"工会币×2"）。
        assert "获得 1 件道具" in result.message
        assert "工会币×2" not in result.message
        assert result.data["bingo_reward"] is not None

    def test_reward_failure_does_not_fail_sign_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """签到成功但 21061 被拒 → 整体仍 ok=True（签到已生效），如实附注。"""
        recorded = patch_transport(
            monkeypatch,
            home_payload=bingo_home_payload([(0, True)]),
            sign_payload=sign_in_payload(),
            reward_payload=bingo_reward_payload(error_code=31),
        )
        task = create_task("alliance_sign_in", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [
            PACKET_BINGO_HOME,
            PACKET_BINGO_SIGN_IN,
            PACKET_BINGO_REWARD,
        ]
        assert result.ok is True
        assert "连线奖励未领到" in result.message
        assert result.data["bingo_reward"] is None

    def test_not_in_alliance_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """21055 回 errorCode=19（未入会）→ ok=True，如实说明，且不发后续包。"""
        recorded = patch_transport(
            monkeypatch,
            home_payload=bingo_home_payload([], error_code=ERR_NOT_IN_ALLIANCE),
        )
        task = create_task("alliance_sign_in", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [PACKET_BINGO_HOME]
        assert result.ok is True
        assert "未加入工会" in result.message

    def test_reward_error_32_is_benign(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """★ 2026-10-04 实测：签到成功但当天无可领连线（-32）→ 整体 ok=True。

        21061 回 -32 表示"尚未连成线或已领过"，不是失败 —— 签到本身已生效。
        """
        recorded = patch_transport(
            monkeypatch,
            home_payload=bingo_home_payload([(0, True)]),
            sign_payload=sign_in_payload(),
            reward_payload=bingo_reward_payload(error_code=-32),
        )
        task = create_task("alliance_sign_in", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [
            PACKET_BINGO_HOME,
            PACKET_BINGO_SIGN_IN,
            PACKET_BINGO_REWARD,
        ]
        assert result.ok is True
        assert "连线奖励暂无可领" in result.message
        assert "32" in (result.data["bingo_reward"] or "")


    def test_server_rejection_is_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """面板正常但签到被拒 → ok=False，错误码可读；不发 21061。"""
        patch_transport(
            monkeypatch,
            sign_payload=sign_in_payload(error_code=31),
        )
        task = create_task("alliance_sign_in", make_session())
        result = task.run()

        assert result.ok is False
        assert "errorCode=31" in result.message
        assert "HaveBeen_Receive" in result.message  # 错误码带可读名


class TestDryRun:
    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[bytes] = []

        def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
            recorded.append(body)
            raise NetworkError("测试替身：禁止在单元测试中真的发包")

        monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
        task = create_task("alliance_sign_in", make_session(), dry_run=True)
        result = task.run()

        assert recorded == [], "演练模式不该发出任何请求"
        assert result.ok is True
        assert "演练" in result.message


if __name__ == "__main__":  # 支持 `python -m tests.test_tasks_alliance_sign_in`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
