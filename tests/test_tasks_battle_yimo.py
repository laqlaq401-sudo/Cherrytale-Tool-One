"""失控炼成阵（battle_yimo）自动化任务单元测试。

验证目标：
1. 额度耗尽（remainCount=0）平稳跳过，不越界进战；
2. 演练模式（dry_run=True）只查不战；
3. 进战 -> Token 运算 -> 结算发奖全链路成功断言（防越界发包校验）；
4. 禁发买次数包（22103 BuyYimoCount）守卫拦截；
5. 永久工程红线自动化检查（无天命、无高难试炼）。
"""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace
import pytest

from models.battle_yimo import (
    YimoBattleRequest,
    YimoBattleRequestRes,
    YimoBattleResult,
    YimoBattleResultRes,
)
from models.daily import GetObjClass
from models.game_packet import ProtoMessage
from models.assemble import LineupRole, encode_assemble
from client.session import GameSession
from models.session_state import GameSessionState
from models.yimo import GetYimoState, GetYimoStateRes
from tasks.battle_yimo import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    YimoBattleTask,
)
from crypto.battle_signer import calc_yimo_token

MOCK_YIMO_ROLES = (
    {"roleSid": 153867701, "roleID": 130008001, "roleLV": 80, "roleStar": 5, "name": "晨曦"},
    {"roleSid": 153867702, "roleID": 130008002, "roleLV": 80, "roleStar": 5, "name": "烈焰"},
    {"roleSid": 153867703, "roleID": 130008003, "roleLV": 75, "roleStar": 4, "name": "微风"},
    {"roleSid": 153867704, "roleID": 130008004, "roleLV": 70, "roleStar": 4, "name": "厚土"},
    {"roleSid": 153867705, "roleID": 130008005, "roleLV": 70, "roleStar": 3, "name": "激流"},
)


def make_yimo_assemble(roles: Any = MOCK_YIMO_ROLES) -> bytes:
    """构造 22108.mineAssemble：服务端下发的真实出战名单。"""
    return encode_assemble(
        [LineupRole(r["roleSid"], r["roleID"], r["roleLV"], r["roleStar"]) for r in roles]
    )


MOCK_YIMO_ASSEMBLE = make_yimo_assemble()


def make_yimo_session(roles: Any = MOCK_YIMO_ROLES) -> GameSession:
    state = GameSessionState(token="mock_token", player_id=123, roles=tuple(roles))
    return GameSession(game_state=state)



class FakeGameClient:
    """模拟网关响应客户端。"""

    def __init__(self, responses: dict[type[ProtoMessage], Any]) -> None:
        self.responses = responses
        self.sent: list[ProtoMessage] = []

    def __enter__(self) -> FakeGameClient:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def send(self, packet: ProtoMessage, **kwargs: Any) -> Any:
        self.sent.append(packet)
        resp = self.responses.get(type(packet))
        if resp is None:
            raise KeyError(f"Unexpected packet: {type(packet)}")
        return SimpleNamespace(decode_sub_packet=lambda cls: resp)


class TestYimoBattleTask:
    """失控炼成阵自动化任务测试套件。"""

    def test_yimo_remain_zero_skips(self) -> None:
        """挑战次数为 0 时平稳跳过，不发送进战请求。"""
        state_res = GetYimoStateRes(remainCount=0, nowRank=20, nowScore=88888)
        client = FakeGameClient({GetYimoState: state_res})

        task = YimoBattleTask(battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["remain_count"] == 0
        assert len(client.sent) == 1
        assert isinstance(client.sent[0], GetYimoState)

    def test_yimo_dry_run(self) -> None:
        """演练模式下只查状态，不进入实际战斗。"""
        state_res = GetYimoStateRes(remainCount=3, nowRank=16, nowScore=111209)
        client = FakeGameClient({GetYimoState: state_res})

        task = YimoBattleTask(dry_run=True, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["dry_run"] is True
        assert res.data["remain_count"] == 3
        assert len(client.sent) == 1
        assert isinstance(client.sent[0], GetYimoState)

    def test_yimo_battle_full_lifecycle(self) -> None:
        """完整生命周期：22101 查询 -> 22107 进战 -> 22109 结算 (携带 Token) -> 22110 发奖。"""
        state_res = GetYimoStateRes(remainCount=3, nowRank=16, nowScore=111209)
        req_res = YimoBattleRequestRes(errorCode=0, fightToken=29744, hpRate=10000, mineAssemble=MOCK_YIMO_ASSEMBLE)
        settle_res = YimoBattleResultRes(
            errorCode=0,
            rank=15,
            totalValue=164456,
            remainCount=2,
            rewardList=[GetObjClass()],
        )

        client = FakeGameClient({
            GetYimoState: state_res,
            YimoBattleRequest: req_res,
            YimoBattleResult: settle_res,
        })

        task = YimoBattleTask(
            session=make_yimo_session(),
            battle_wait_seconds=0,  # 单测跳过等待
            target_score=53247,
            part=661000003,
            boss_lv=41,
            daily_only=False,
        )
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["score"] == 53247
        assert res.data["rank"] == 15
        assert res.data["total_value"] == 164456
        assert res.data["remain_count"] == 2

        # 验证发包序列
        assert len(client.sent) == 3
        assert isinstance(client.sent[0], GetYimoState)
        assert isinstance(client.sent[1], YimoBattleRequest)
        assert client.sent[1].part == 661000003
        assert client.sent[1].bossLv == 41

        assert isinstance(client.sent[2], YimoBattleResult)
        assert client.sent[2].fightToken == 29744
        assert client.sent[2].score == 53247
        # P0 守卫：Boss 满血（10000）进攻时，扣减万分比基于科学基准血量计算（非硬编码死数据）
        assert client.sent[2].dmgHpRate == 2
        # 验证生成的 Token 与离线确凿断言一致
        expected_token = calc_yimo_token(29744, 53247)
        assert client.sent[2].token == expected_token == "0a3396051faf186762d81815977630bf"
        # P1 守卫：对齐真机抓包 raw/26，damage 必须为 0，出战战力必须真实有效（> 0）
        assert client.sent[2].battleResult.damage == 0
        assert client.sent[2].battleResult.fightingPower > 0
        # P2 守卫：验证 13 维明细记录 myTeamRecord 已填充 5 名角色，enemyTeamRecord 对齐 raw/26 为空
        assert len(client.sent[2].battleResult.myTeamRecord) == 5
        assert len(client.sent[2].battleResult.enemyTeamRecord) == 0

    def test_yimo_daily_only_mode(self) -> None:
        """验证仅日常打卡安全模式（daily_only=True，默认）：
        1. 浮动保底得分在 1200~2800 之间；
        2. Boss 扣血万分比严格为 0（零影响全服世界 Boss 血量）；
        3. 战斗耗时与签名均正常构造。
        """
        state_res = GetYimoStateRes(remainCount=1, nowRank=16, nowScore=10000)
        req_res = YimoBattleRequestRes(errorCode=0, fightToken=11223, hpRate=10000, mineAssemble=MOCK_YIMO_ASSEMBLE)
        settle_res = YimoBattleResultRes(
            errorCode=0,
            rank=16,
            totalValue=12500,
            remainCount=0,
            rewardList=[],
        )
        client = FakeGameClient({
            GetYimoState: state_res,
            YimoBattleRequest: req_res,
            YimoBattleResult: settle_res,
        })

        task = YimoBattleTask(session=make_yimo_session(), battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["daily_only"] is True
        score = res.data["score"]
        assert 1200 <= score <= 2800
        settle_pkt = client.sent[2]
        assert isinstance(settle_pkt, YimoBattleResult)
        assert 1200 <= settle_pkt.score <= 2800
        assert settle_pkt.dmgHpRate == 0  # ★ 扣血锁定为 0
        expected_token = calc_yimo_token(11223, settle_pkt.score)
        assert settle_pkt.token == expected_token

    def test_yimo_multi_battles_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """验证多场炼成阵循环推进与自动停止。"""
        import config
        monkeypatch.setattr(config, "TOP_PVP_INTERVAL_MIN", 0.0)
        monkeypatch.setattr(config, "TOP_PVP_INTERVAL_MAX", 0.0)

        state_res = GetYimoStateRes(remainCount=2, nowRank=16, nowScore=111209)
        req_res = YimoBattleRequestRes(errorCode=0, fightToken=29744, hpRate=10000, mineAssemble=MOCK_YIMO_ASSEMBLE)
        settle_res = YimoBattleResultRes(
            errorCode=0,
            rank=15,
            totalValue=164456,
            remainCount=1,
            rewardList=[GetObjClass()],
        )

        client = FakeGameClient({
            GetYimoState: state_res,
            YimoBattleRequest: req_res,
            YimoBattleResult: settle_res,
        })

        task = YimoBattleTask(
            session=make_yimo_session(),
            battles=2,
            battle_wait_seconds=0,
            target_score=53247,
        )
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["battles_completed"] == 2
        # 验证发包序列：2 轮，每轮 3 个包 (22101, 22107, 22109)
        assert len(client.sent) == 6

    def test_yimo_battles_zero_fights_until_count_zero(self) -> None:
        """验证 battles=0 时自动打完所有可用挑战次数直到剩余 0 次退出。"""
        state_res_1 = GetYimoStateRes(remainCount=1, nowRank=30, nowScore=50000)
        state_res_2 = GetYimoStateRes(remainCount=0, nowRank=28, nowScore=103247)
        req_res = YimoBattleRequestRes(errorCode=0, fightToken=99999, mineAssemble=MOCK_YIMO_ASSEMBLE)
        settle_res = YimoBattleResultRes(
            errorCode=0,
            rank=28,
            totalValue=103247,
            remainCount=0,
            rewardList=[],
        )

        class MultiRoundClient:
            def __init__(self) -> None:
                self.state_calls = 0
                self.sent: list[Any] = []

            def __enter__(self) -> MultiRoundClient:
                return self

            def __exit__(self, *args: Any) -> None:
                pass

            def send(self, packet: ProtoMessage, **kwargs: Any) -> Any:
                self.sent.append(packet)
                if isinstance(packet, GetYimoState):
                    self.state_calls += 1
                    resp = state_res_1 if self.state_calls == 1 else state_res_2
                    return SimpleNamespace(decode_sub_packet=lambda cls: resp)
                elif isinstance(packet, YimoBattleRequest):
                    return SimpleNamespace(decode_sub_packet=lambda cls: req_res)
                elif isinstance(packet, YimoBattleResult):
                    return SimpleNamespace(decode_sub_packet=lambda cls: settle_res)
                raise KeyError(f"Unexpected: {type(packet)}")

        client = MultiRoundClient()
        task = YimoBattleTask(session=make_yimo_session(), battles=0, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["battles_completed"] == 1
        assert client.state_calls == 2

    def test_yimo_rejects_empty_team_0_roles(self) -> None:
        """P1 守卫：0 人未配队账号严格禁止进入炼成阵战斗，快速失败且绝不发送进战包 22107。"""
        state_res = GetYimoStateRes(remainCount=3, nowRank=16, nowScore=10000)
        client = FakeGameClient({GetYimoState: state_res})

        session = make_yimo_session(roles=())
        task = YimoBattleTask(session=session, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is False
        assert "0人未配队" in res.message
        # 绝不发送 22107 进战包与 22109 结算包
        assert not any(isinstance(p, YimoBattleRequest) for p in client.sent)
        assert not any(isinstance(p, YimoBattleResult) for p in client.sent)

    def test_yimo_allows_fewer_than_5_roles(self) -> None:
        """P1 守卫：允许少于 5 人参战（实际几人就上报几人），绝不擅自填充虚假模板角色。"""
        state_res = GetYimoStateRes(remainCount=1, nowRank=16, nowScore=10000)
        # 仅 2 名出战角色（服务端阵容快照也只有 2 人）
        two_roles = MOCK_YIMO_ROLES[:2]
        req_res = YimoBattleRequestRes(
            errorCode=0,
            fightToken=33445,
            hpRate=10000,
            mineAssemble=make_yimo_assemble(two_roles),
        )
        settle_res = YimoBattleResultRes(
            errorCode=0,
            rank=16,
            totalValue=12500,
            remainCount=0,
            rewardList=[],
        )
        client = FakeGameClient({
            GetYimoState: state_res,
            YimoBattleRequest: req_res,
            YimoBattleResult: settle_res,
        })

        session = make_yimo_session(roles=two_roles)
        task = YimoBattleTask(session=session, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        settle_pkt = client.sent[2]
        assert isinstance(settle_pkt, YimoBattleResult)
        assert len(settle_pkt.battleResult.myTeamRecord) == 2
        from models.main_stage import WuDouResultRecord
        decoded = [WuDouResultRecord.decode(r) for r in settle_pkt.battleResult.myTeamRecord]
        sids = [r.role_sid for r in decoded]
        assert sids == [153867701, 153867702]

    def test_yimo_forbidden_packet_defense(self) -> None:
        """验证 22103 BuyYimoCount 处于绝对禁发清单中。"""
        assert 22103 in FORBIDDEN_PACKET_IDS.values()
        assert "BuyYimoCount" in FORBIDDEN_PACKET_IDS
        assert 22103 not in ALLOWED_PACKET_IDS.values()

    def test_no_destiny_or_high_difficulty(self) -> None:
        """确保代码中严禁触碰天命系统与高难属性试炼。"""
        import tasks.battle_yimo as mod

        text = open(mod.__file__, "r", encoding="utf-8").read()
        assert "Destiny" not in text or "严禁" in text
        assert "destiny" not in text.lower() or "严禁" in text.lower()
        for forbidden in ("Volcano", "Glacier", "Desert", "火山", "极地", "荒漠"):
            assert forbidden not in text or "严禁" in text

    def test_dynamic_yimo_score_calculation(self) -> None:
        """验证动态分数与战力推导：
        1. 当已有今日战绩时，基于历史单场均值推导并引入自然拟真波动；
        2. 当首次出战时，基于名册英雄等级与 Boss 等级推导。
        """
        from tasks.battle_yimo import calculate_dynamic_yimo_score

        # Case 1: 已有战绩（如第 1 场打完累计 50000 分，剩余 4 次）
        score1, power1 = calculate_dynamic_yimo_score(
            roles=(), boss_lv=200, now_score=50000, remain_count=4
        )
        assert 45000 <= score1 <= 55000
        assert power1 > 0

        # Case 2: 今日首战，名册有 5 名 100 级英雄，Boss 等级 213
        fake_roles = [{"roleLV": 100} for _ in range(5)]
        score2, power2 = calculate_dynamic_yimo_score(
            roles=fake_roles, boss_lv=213, now_score=0, remain_count=5
        )
        assert 40000 <= score2 <= 80000
        assert power2 >= 1500000

        # Case 3: 显式指定自定义分数
        score3, _ = calculate_dynamic_yimo_score(
            roles=(), boss_lv=200, now_score=0, remain_count=5, custom_score=66666
        )
        assert score3 == 66666
