"""每日免费抽卡任务（tasks/daily_free_draw.py）的单元测试。"""

from __future__ import annotations

import pytest

from client.game_client import GameClient
from client.session import GameSession
from models.draw import (
    DrawMachineNewDataClass,
    DrawNewPacket,
    DrawNewRes,
    GetDrawMachineDataNewPacket,
    GetDrawMachineDataNewRes,
    load_free_draw_machines,
)
from models.envelope import parse_envelope
from models.session_state import GameSessionState
from tasks.base import create_task
import tasks.daily_free_draw as daily_free_draw


def make_session() -> GameSession:
    """构造不联网的会话。"""
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


def test_load_free_draw_machines():
    """验证配置表或兜底能解析出免费抽卡卡池。"""
    pools = load_free_draw_machines()
    assert isinstance(pools, dict)
    assert 252000710 in pools
    assert pools[252000710]["freeTimes"] >= 1


def test_daily_free_draw_skip_when_exhausted(monkeypatch: pytest.MonkeyPatch):
    """当所有免费卡池今日次数已耗尽时，跳过抽卡，绝不发 12101。"""
    session = make_session()
    task = create_task("daily_free_draw", session)

    # 模拟服务端回传：drawCount == 1，等于免费上限 1
    mock_res = GetDrawMachineDataNewRes(
        drawMachineDataClass=[
            DrawMachineNewDataClass(drawMachineID=252000710, drawCount=1),
            DrawMachineNewDataClass(drawMachineID=252000810, drawCount=1),
        ]
    )

    sent_packets = []

    def mock_guarded_send(game, packet, expect, **kwargs):
        sent_packets.append(packet)
        # 包装成 MockEnvelopeView
        class MockView:
            def decode_sub_packet(self, model_cls):
                if model_cls == GetDrawMachineDataNewRes:
                    return mock_res
                raise AssertionError(f"Unexpected expect: {model_cls}")

        return MockView()

    monkeypatch.setattr(daily_free_draw, "guarded_send", mock_guarded_send)

    res = task.execute()
    assert res.ok is True
    assert "已全部完成或无可用免费卡池" in res.message
    # 必须只发了 12105 查询，没有发出 12101 抽卡！
    assert len(sent_packets) == 1
    assert isinstance(sent_packets[0], GetDrawMachineDataNewPacket)


def test_daily_free_draw_success(monkeypatch: pytest.MonkeyPatch):
    """当存在未抽的免费卡池时，发送 12101 并验证 0 钻石消耗。"""
    session = make_session()
    task = create_task("daily_free_draw", session)

    # 模拟服务端回传：252000710 drawCount=0 (可用免费)
    machine_res = GetDrawMachineDataNewRes(
        drawMachineDataClass=[
            DrawMachineNewDataClass(drawMachineID=252000710, drawCount=0),
            DrawMachineNewDataClass(drawMachineID=252000810, drawCount=1),
        ]
    )
    draw_res = DrawNewRes(errorCode=0, getList=[b"item1"], costList=[])

    sent_packets = []

    def mock_guarded_send(game, packet, expect, **kwargs):
        sent_packets.append(packet)

        class MockView:
            def decode_sub_packet(self, model_cls):
                if model_cls == GetDrawMachineDataNewRes:
                    return machine_res
                if model_cls == DrawNewRes:
                    return draw_res
                raise AssertionError(f"Unexpected expect: {model_cls}")

        return MockView()

    monkeypatch.setattr(daily_free_draw, "guarded_send", mock_guarded_send)

    res = task.execute()
    assert res.ok is True
    assert "已完成 1 个卡池的免费抽卡" in res.message
    assert res.data["drawn_count"] == 1
    assert 252000710 in res.data["drawn_pools"]

    # 核对发包：先 12105 再 12101
    assert len(sent_packets) == 2
    assert isinstance(sent_packets[0], GetDrawMachineDataNewPacket)
    assert isinstance(sent_packets[1], DrawNewPacket)
    assert sent_packets[1].drawMachineID == 252000710
    assert sent_packets[1].actionType == 0
    assert sent_packets[1].useTicket == 1


def test_daily_free_draw_dry_run():
    """演练模式：组装报文但不实际发包。"""
    session = make_session()
    task = create_task("daily_free_draw", session, dry_run=True)
    res = task.execute()
    assert res.ok is True
    assert "演练完成" in res.message
    assert res.data["sent_packets"] == ()
