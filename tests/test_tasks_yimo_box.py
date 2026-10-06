"""失控炼成阵全服通关宝箱任务（tasks/yimo_box.py）的单元测试。"""

from __future__ import annotations

import pytest

from client.session import GameSession
from models.session_state import GameSessionState
from models.yimo import (
    GetYimoState,
    GetYimoStateRes,
    OpenYimoRewardPacket,
    OpenYimoRewardRes,
    YimoBossDailyRewardClass,
)
from tasks.base import create_task
import tasks.yimo_box as yimo_box


def make_session() -> GameSession:
    session = GameSession("https://example.invalid", timeout=1.0)
    session.set_game_state(
        GameSessionState(
            token="test-token",
            player_id=888,
            server_id=1,
            platform_user_id="user-123",
        )
    )
    return session


def test_yimo_box_all_claimed_stops_repeating(monkeypatch: pytest.MonkeyPatch):
    """当 3 份宝箱均已领取（state == 2）时，汇报 all_claimed=True 且不发领取包。"""
    session = make_session()
    task = create_task("yimo_box", session)

    state_res = GetYimoStateRes(
        rewardData=[
            YimoBossDailyRewardClass(rewardID=662000001, rewardState=2),
            YimoBossDailyRewardClass(rewardID=662000002, rewardState=2),
            YimoBossDailyRewardClass(rewardID=662000003, rewardState=2),
        ]
    )

    sent = []

    def mock_guarded_send(game, packet, expect, **kwargs):
        sent.append(packet)

        class MockView:
            def decode_sub_packet(self, model_cls):
                return state_res

        return MockView()

    monkeypatch.setattr(yimo_box, "guarded_send", mock_guarded_send)

    res = task.execute()
    assert res.ok is True
    assert res.data["all_claimed"] is True
    assert res.data["claimed_count"] == 3
    assert "已全部领满" in res.message
    # 只发了 22101 查状态，未发 22105 领奖
    assert len(sent) == 1
    assert isinstance(sent[0], GetYimoState)


def test_yimo_box_claimable_triggers_claim(monkeypatch: pytest.MonkeyPatch):
    """当存在 state == 1 的达标宝箱时，发送 22105 领取并正确更新状态。"""
    session = make_session()
    task = create_task("yimo_box", session)

    state_res = GetYimoStateRes(
        rewardData=[
            YimoBossDailyRewardClass(rewardID=662000001, rewardState=2),
            YimoBossDailyRewardClass(rewardID=662000002, rewardState=1),  # 可领
            YimoBossDailyRewardClass(rewardID=662000003, rewardState=0),  # 未达标
        ]
    )
    open_res = OpenYimoRewardRes(errorCode=0, getList=[b"reward_data"])

    sent = []

    def mock_guarded_send(game, packet, expect, **kwargs):
        sent.append(packet)

        class MockView:
            def decode_sub_packet(self, model_cls):
                if model_cls == GetYimoStateRes:
                    return state_res
                if model_cls == OpenYimoRewardRes:
                    return open_res
                raise AssertionError(f"Unexpected expect: {model_cls}")

        return MockView()

    monkeypatch.setattr(yimo_box, "guarded_send", mock_guarded_send)

    res = task.execute()
    assert res.ok is True
    assert "成功领取失控炼成阵全服宝箱" in res.message
    assert res.data["all_claimed"] is False  # 还有一份未达标，未领满
    assert res.data["claimed_count"] == 2  # 原来1 + 本次1 = 2

    # 发了 22101 查询与 22105 领奖
    assert len(sent) == 2
    assert isinstance(sent[0], GetYimoState)
    assert isinstance(sent[1], OpenYimoRewardPacket)


def test_yimo_box_locked_waits_for_hourly(monkeypatch: pytest.MonkeyPatch):
    """当无达标待领宝箱且未领满时，汇报 all_claimed=False，等待每小时轮询。"""
    session = make_session()
    task = create_task("yimo_box", session)

    state_res = GetYimoStateRes(
        rewardData=[
            YimoBossDailyRewardClass(rewardID=662000001, rewardState=0),
            YimoBossDailyRewardClass(rewardID=662000002, rewardState=0),
            YimoBossDailyRewardClass(rewardID=662000003, rewardState=0),
        ]
    )

    sent = []

    def mock_guarded_send(game, packet, expect, **kwargs):
        sent.append(packet)

        class MockView:
            def decode_sub_packet(self, model_cls):
                return state_res

        return MockView()

    monkeypatch.setattr(yimo_box, "guarded_send", mock_guarded_send)

    res = task.execute()
    assert res.ok is True
    assert res.data["all_claimed"] is False
    assert res.data["claimed_count"] == 0
    assert "暂无达标待领宝箱" in res.message
    assert len(sent) == 1


def test_yimo_box_dry_run():
    """演练模式。"""
    session = make_session()
    task = create_task("yimo_box", session, dry_run=True)
    res = task.execute()
    assert res.ok is True
    assert "演练完成" in res.message
