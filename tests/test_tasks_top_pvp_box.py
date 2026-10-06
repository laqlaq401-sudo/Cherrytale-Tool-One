"""荣耀之巅宝箱任务（tasks/top_pvp_box.py）的单元测试。"""

from __future__ import annotations

import pytest

from client.session import GameSession
from models.session_state import GameSessionState
from models.top_pvp import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    ReceiveTopPvpRewardPacket,
    ReceiveTopPvpRewardRes,
)
from tasks.base import create_task
from tests.reward_guard import assert_no_bare_identifiers
import tasks.top_pvp_box as top_pvp_box


def make_session() -> GameSession:
    session = GameSession("https://example.invalid", timeout=1.0)
    session.set_game_state(
        GameSessionState(
            token="test-token",
            player_id=999,
            server_id=1,
            platform_user_id="user-456",
        )
    )
    return session


def test_top_pvp_box_success(monkeypatch: pytest.MonkeyPatch):
    """成功领取荣耀之巅宝箱。"""
    session = make_session()
    task = create_task("top_pvp_box", session)

    res_packet = ReceiveTopPvpRewardRes(errorCode=0, addList=[b"item1", b"item2"])

    sent = []

    def mock_guarded_send(game, packet, expect, **kwargs):
        sent.append(packet)

        class MockView:
            def decode_sub_packet(self, model_cls):
                return res_packet

        return MockView()

    monkeypatch.setattr(top_pvp_box, "guarded_send", mock_guarded_send)

    res = task.execute()
    assert res.ok is True
    assert "荣耀之巅宝箱领取成功" in res.message
    # ★ 2026-10-04：`reward_count` 改名为 `reward_group_count` —— addList 是
    # list[bytes]（未解析的 GetObjClass），len() 是**组数**不是件数，名字必须如实。
    assert res.data["reward_group_count"] == 2
    assert "项奖励道具" not in res.message, "组数不能当件数报给用户"
    assert_no_bare_identifiers(res.message)
    assert len(sent) == 1
    assert isinstance(sent[0], ReceiveTopPvpRewardPacket)


def test_top_pvp_box_already_claimed_or_unavailable(monkeypatch: pytest.MonkeyPatch):
    """今日已领过或暂无宝箱（返回非 0 错误码）依然正常结束，不抛异常。"""
    session = make_session()
    task = create_task("top_pvp_box", session)

    res_packet = ReceiveTopPvpRewardRes(errorCode=-13, addList=[])

    sent = []

    def mock_guarded_send(game, packet, expect, **kwargs):
        sent.append(packet)

        class MockView:
            def decode_sub_packet(self, model_cls):
                return res_packet

        return MockView()

    monkeypatch.setattr(top_pvp_box, "guarded_send", mock_guarded_send)

    res = task.execute()
    assert res.ok is True
    assert "今日已领取或当前暂无待领奖励" in res.message
    assert res.data["error_code"] == -13
    assert len(sent) == 1


def test_top_pvp_box_safety():
    """安全属性与白名单校验。"""
    session = make_session()
    task = create_task("top_pvp_box", session)

    assert task.spends_diamond is False
    assert 39003 in ALLOWED_PACKET_IDS.values()
    assert 39005 in FORBIDDEN_PACKET_IDS.values()


def test_top_pvp_box_dry_run():
    """演练模式下不发包。"""
    session = make_session()
    task = create_task("top_pvp_box", session, dry_run=True)
    res = task.execute()

    assert res.ok is True
    assert "演练完成" in res.message
    assert res.data["planned_packets"] == (39003,)
    assert res.data["sent_packets"] == ()
