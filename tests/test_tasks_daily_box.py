"""日常 / 周常宝箱任务（``tasks/daily_box.py``）的单元测试。

运行方式：

    python -m pytest tests/test_tasks_daily_box.py -q
    python -m tests.test_tasks_daily_box

【本文件要钉死的一条规则】
**"已经领过"只认服务端清单**（``DailyMissionRes.dmrReceivedList`` /
``wmrReceivedList``），不能拿本地配置去决定跳过 —— 本地数据过期会造成漏领，
而漏领是静默的。

这里用受控的假配置 + 假的 6002 响应来驱动 ``_plan_boxes()``，
既不需要联网，也不需要真实素材文件。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from client.exceptions import NetworkError
from client.game_client import GameClient
from client.session import GameSession
from models.daily import DailyMissionListClass, DailyMissionRes
from models.session_state import GameSessionState
from tasks.base import create_task
from tests.reward_guard import assert_no_bare_identifiers

import tasks.daily_box as daily_box  # 导入即注册，同时便于 monkeypatch 模块内引用


def make_session() -> GameSession:
    """构造一个**不会联网**的会话（域名是保留域名）。

    ``game_state`` 是业务任务发包的硬要求（``open_game_client()`` 靠它注入令牌）。
    """
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(GameSessionState(token="tok", player_id=1, server_id=1))
    return session


def mission(mid: int, *, count: int = 1, point: int = 1) -> SimpleNamespace:
    """假的日常/周常任务配置（只保留计算活跃点用到的三个字段）。"""
    return SimpleNamespace(mission_id=mid, required_count=count, add_point=point)


def box(box_id: int, *, need: int) -> SimpleNamespace:
    """假的宝箱配置（只保留 ID 与所需活跃点）。"""
    return SimpleNamespace(box_id=box_id, required_point=need)


@pytest.fixture
def fake_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """把任务用到的四张配置表换成受控数据（不依赖真实素材目录）。"""
    monkeypatch.setattr(daily_box, "load_daily_missions", lambda: [mission(1, point=5)])
    monkeypatch.setattr(
        daily_box,
        "load_daily_reward_boxes",
        lambda: [box(939000001, need=1), box(939000002, need=3), box(939000003, need=9)],
    )
    monkeypatch.setattr(daily_box, "load_weekly_missions", lambda: [mission(2, point=3)])
    monkeypatch.setattr(
        daily_box,
        "load_weekly_reward_boxes",
        lambda: [box(251000000, need=1), box(251000001, need=3)],
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成"记录 + 拒绝"的替身。"""
    recorded: list[bytes] = []

    def fake_post_raw(self: GameClient, body: bytes) -> object:
        recorded.append(body)
        raise NetworkError("测试替身：禁止在单元测试中真的发包")

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


class TestPlanBoxes:
    def test_skips_already_received_and_unreachable(
        self, fake_config: None
    ) -> None:
        """已领的跳过、活跃点不够的跳过、剩下的才尝试。"""
        task = create_task("daily_box", make_session())
        state = DailyMissionRes(
            dmInitList=[DailyMissionListClass(misID=1, completedTimes=1)],
            dmrReceivedList=[939000001],  # 第 1 个已领
            wmInitList=[DailyMissionListClass(misID=2, completedTimes=1)],
            wmrReceivedList=[251000000],  # 周常第 1 个已领
        )

        daily_pending, weekly_pending, daily_point, weekly_point = task._plan_boxes(  # type: ignore[attr-defined]
            state
        )

        assert daily_point == 5  # 假配置：完成 1 条就给 5 点
        assert weekly_point == 3
        # 需 1/3 点的可达、需 9 点不可达；已领的 939000001 被排除
        assert daily_pending == [939000002]
        assert weekly_pending == [251000001]

    def test_no_point_means_nothing_reachable(self, fake_config: None) -> None:
        """服务端说任务一次都没做 → 活跃点 0 → 无可领宝箱（不做本地推测）。"""
        task = create_task("daily_box", make_session())
        state = DailyMissionRes()  # 空响应
        daily_pending, weekly_pending, daily_point, weekly_point = task._plan_boxes(  # type: ignore[attr-defined]
            state
        )
        assert (daily_point, weekly_point) == (0, 0)
        assert daily_pending == []
        assert weekly_pending == []

    def test_everything_received_yields_empty(self, fake_config: None) -> None:
        task = create_task("daily_box", make_session())
        state = DailyMissionRes(
            dmInitList=[DailyMissionListClass(misID=1, completedTimes=1)],
            dmrReceivedList=[939000001, 939000002, 939000003],
            wmInitList=[DailyMissionListClass(misID=2, completedTimes=1)],
            wmrReceivedList=[251000000, 251000001],
        )
        daily_pending, weekly_pending, _, _ = task._plan_boxes(state)  # type: ignore[attr-defined]
        assert daily_pending == []
        assert weekly_pending == []


class TestRewardText:
    def test_reward_text_joins_items(self) -> None:
        from models.daily import DMRewardRes, GetObjClass, ItemClass

        res = DMRewardRes(
            errorCode=0,
            rewardList=[
                GetObjClass(
                    itemList=[
                        ItemClass(itemID=200000001, itemAmount=60),
                        ItemClass(itemID=200000050, itemAmount=1000),
                    ]
                )
            ],
        )
        text = daily_box.DailyBoxTask._balance_text(res.rewardList)
        # 2026-10-03 精简原则：认识名字的道具翻成中文，不认识的裸 ID 不进结果消息
        # ★ 同日措辞修正：6006 里的数量是领取后余额（Q-021），不是本次增量
        assert "钻石×60" in text
        assert "当前持有" in text
        assert "200000050" not in text
        assert_no_bare_identifiers(text)

    def test_reward_text_without_items(self) -> None:
        from models.daily import DMRewardRes

        assert daily_box.DailyBoxTask._balance_text(DMRewardRes().rewardList) == ""


class TestClaim:
    def test_claim_success_with_rewards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from models.daily import DMReward, DMRewardRes, GetObjClass, ItemClass, MissionType

        task = create_task("daily_box", make_session())
        sent_packets: list[DMReward] = []

        def fake_send(self: GameClient, packet: DMReward, **kwargs: object) -> object:
            sent_packets.append(packet)
            res = DMRewardRes(
                errorCode=0,
                rewardList=[GetObjClass(itemList=[ItemClass(itemID=1001, itemAmount=10)])],
            )
            mock_view = SimpleNamespace(decode_sub_packet=lambda cls: res)
            return mock_view

        monkeypatch.setattr(GameClient, "send", fake_send)
        with task.open_game_client() as game:  # type: ignore[attr-defined]
            ok, text = task._claim(game, MissionType.DAILY_REWARD_BOX)  # type: ignore[attr-defined]

        assert ok is True
        # 1001 不在已知道具表里 → 不出现在结果文本里（裸 ID 不进运行视图）
        assert text == ""
        assert len(sent_packets) == 1
        assert sent_packets[0].misID == -1
        assert sent_packets[0].mistypeID == int(MissionType.DAILY_REWARD_BOX)

    def test_claim_empty_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from models.daily import DMReward, DMRewardRes, MissionType

        task = create_task("daily_box", make_session())

        def fake_send(self: GameClient, packet: DMReward, **kwargs: object) -> object:
            res = DMRewardRes(errorCode=-1)
            mock_view = SimpleNamespace(decode_sub_packet=lambda cls: res)
            return mock_view

        monkeypatch.setattr(GameClient, "send", fake_send)
        with task.open_game_client() as game:  # type: ignore[attr-defined]
            ok, text = task._claim(game, MissionType.WEEKLY_REWARD_BOX)  # type: ignore[attr-defined]

        assert ok is True
        assert text == ""

    def test_claim_error_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from models.daily import DMReward, DMRewardRes, MissionType

        task = create_task("daily_box", make_session())

        def fake_send(self: GameClient, packet: DMReward, **kwargs: object) -> object:
            res = DMRewardRes(errorCode=-2)
            mock_view = SimpleNamespace(decode_sub_packet=lambda cls: res)
            return mock_view

        monkeypatch.setattr(GameClient, "send", fake_send)
        with task.open_game_client() as game:  # type: ignore[attr-defined]
            ok, text = task._claim(game, MissionType.DAILY_REWARD_BOX)  # type: ignore[attr-defined]

        assert ok is False
        assert "errorCode=-2" in text


class TestDryRun:
    def test_dry_run_sends_nothing(self, fake_config: None, calls: list[bytes]) -> None:
        task = create_task("daily_box", make_session(), dry_run=True)
        result = task.run()

        assert result.ok is True
        assert "演练完成" in result.message
        assert calls == [], "演练模式不该发出任何请求"

    def test_real_run_stops_at_network_stub(
        self, fake_config: None, calls: list[bytes]
    ) -> None:
        """真跑时会先拉状态（被替身拦下）—— 说明流程确实走到了网络层。"""
        task = create_task("daily_box", make_session())
        result = task.run()
        assert calls, "应当先发 6001 拉状态"
        assert result.ok is False  # 替身抛 NetworkError → 失败结果


if __name__ == "__main__":  # 支持 `python -m tests.test_tasks_daily_box`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
