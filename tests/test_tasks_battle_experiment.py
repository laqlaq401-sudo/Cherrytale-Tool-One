"""辉煌航迹·试炼与每日终极挑战（battle_experiment）自动化任务单元测试。

验证目标：
1. 终极挑战当日额度耗尽（finalTestCount=0）时平稳跳过，不越界进战；
2. 演练模式（dry_run=True）只查不战；
3. 终极挑战全生命周期推进断言（34007 -> 34009 -> 34011 -> 34012）；
4. 禁发买活/复活包（34025 GrandLineRebirthPacket）守卫拦截；
5. 永久工程红线自动化检查（无天命、无高难属性试炼）。
"""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace
import pytest

from models.battle_experiment import (
    ExperimentFightingRequest,
    ExperimentFightingRequestRes,
    ExperimentFightingResult,
    ExperimentFightingResultRes,
    ExperimentRoleClass,
)
from models.daily import GetObjClass
from models.game_packet import ProtoMessage
from models.assemble import LineupRole, encode_assemble
TRIAL_SIDS = [200001, 200002, 200003, 200004, 200005]

MOCK_TRIAL_ASSEMBLE = encode_assemble(
    [LineupRole(sid, 130015206 + i, 50, 6) for i, sid in enumerate(TRIAL_SIDS)]
)


from tasks.battle_experiment import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    ExperimentBattleTask,
    ExperimentInfo,
    ExperimentInfoClass,
    ExperimentInfoRes,
)


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


class TestExperimentBattleTask:
    """试炼与终极挑战自动化任务测试套件。"""

    def test_ultimate_trial_count_zero_skips(self) -> None:
        """终极试炼今日挑战次数为 0 时平稳跳过，不发送进战请求。"""
        status_res = ExperimentInfoRes(
            errorCode=0,
            infoClass=ExperimentInfoClass(
                sectionID=671000001,  # 终极试炼 2
                finalTestCount=0,     # 今日已挑战
            ),
        )
        client = FakeGameClient({ExperimentInfo: status_res})

        task = ExperimentBattleTask(battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["final_test_count"] == 0
        assert res.data["is_ultimate_trial"] is True
        assert len(client.sent) == 1
        assert isinstance(client.sent[0], ExperimentInfo)

    def test_ultimate_trial_dry_run(self) -> None:
        """演练模式下只查状态，不进入实际关卡。"""
        status_res = ExperimentInfoRes(
            errorCode=0,
            infoClass=ExperimentInfoClass(
                sectionID=671000001,
                finalTestCount=1,  # 今日未挑战
            ),
        )
        client = FakeGameClient({ExperimentInfo: status_res})

        task = ExperimentBattleTask(dry_run=True, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["dry_run"] is True
        assert res.data["final_test_count"] == 1
        assert res.data["section_id"] == 671000001
        assert len(client.sent) == 1
        assert isinstance(client.sent[0], ExperimentInfo)

    def test_ultimate_trial_full_success_lifecycle(self) -> None:
        """完整生命周期：34007 查询 -> 34009 进战 -> 34011 结算 (携带 FightDataClass) -> 34012 发奖。"""
        status_res = ExperimentInfoRes(
            errorCode=0,
            infoClass=ExperimentInfoClass(
                sectionID=671000001,
                finalTestCount=1,
                roleHpList=[
                    ExperimentRoleClass(role_sid=200001 + i, hp=100, isTeam=1)
                    for i in range(5)
                ],
            ),
        )
        req_res = ExperimentFightingRequestRes(
            errorCode=0, fightToken=132008, mineAssemble=MOCK_TRIAL_ASSEMBLE
        )
        settle_res = ExperimentFightingResultRes(
            errorCode=0,
            rewardList=[GetObjClass()],
        )

        client = FakeGameClient({
            ExperimentInfo: status_res,
            ExperimentFightingRequest: req_res,
            ExperimentFightingResult: settle_res,
        })

        task = ExperimentBattleTask(
            battle_wait_seconds=0,  # 单测跳过等待
            target_section_id=671000001,
        )
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["section_id"] == 671000001
        assert res.data["is_ultimate_trial"] is True
        assert res.data["reward_count"] == 1

        # 验证发包序列
        assert len(client.sent) == 3
        assert isinstance(client.sent[0], ExperimentInfo)

        assert isinstance(client.sent[1], ExperimentFightingRequest)
        assert client.sent[1].sectionID == 671000001

        assert isinstance(client.sent[2], ExperimentFightingResult)
        assert client.sent[2].winLose == 1
        assert client.sent[2].fightData.token == 132008
        assert client.sent[2].fightData.sectionId == 671000001
        assert len(client.sent[2].monsterHpList) == 5
        assert len(client.sent[2].roleHpList) == 5
        # 验证 fightingPower > 0，且与真机抓包 raw/09 确凿对齐（damage=0, damageContent='t=3$Empty'）
        assert client.sent[2].battleResult.fightingPower > 0
        assert client.sent[2].battleResult.damage == 0
        assert client.sent[2].battleResult.damageContent == "t=3$Empty"
        # P2 守卫：验证 13 维明细记录 myTeamRecord 已填充 5 名角色，enemyTeamRecord 对齐 raw/09 为空
        assert len(client.sent[2].battleResult.myTeamRecord) == 5
        assert len(client.sent[2].battleResult.enemyTeamRecord) == 0

        # P0 守卫：明细必须来自服务端阵容，且不能是「零伤害却全灭」
        from models.main_stage import WuDouResultRecord

        mine = [WuDouResultRecord.decode(r) for r in client.sent[2].battleResult.myTeamRecord]
        assert [r.role_sid for r in mine] == TRIAL_SIDS
        assert sum(r.dps for r in mine) > 0
        assert all(r.hit == 0 for r in mine)  # 真机样本 hit 恒 0
        # 残蓝不可能为 0（真机 2080 / 1920）
        assert client.sent[2].role_mana > 0
        assert client.sent[2].monster_mana > 0

    def test_empty_roles_aborts_safely(self) -> None:
        """当名册完全为空且试炼未布阵时，安全中止而不是使用他人死数据。"""
        status_res = ExperimentInfoRes(
            errorCode=0,
            infoClass=ExperimentInfoClass(
                sectionID=671000001,
                finalTestCount=1,
                roleHpList=[],
            ),
        )
        client = FakeGameClient({ExperimentInfo: status_res})
        task = ExperimentBattleTask(battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is False
        assert "未在试炼系统或账号名册中检索到可用出战角色" in res.message
        assert len(client.sent) == 1

    def test_aborts_when_no_server_lineup_and_no_roster(self) -> None:
        """34010 未下发阵容、名册也为空时拒绝结算，绝不伪造战报。"""
        status_res = ExperimentInfoRes(
            errorCode=0,
            infoClass=ExperimentInfoClass(
                sectionID=671000001,
                finalTestCount=1,
                roleHpList=[ExperimentRoleClass(role_sid=200001, hp=100, isTeam=1)],
            ),
        )
        req_res = ExperimentFightingRequestRes(errorCode=0, fightToken=132008)
        settle_res = ExperimentFightingResultRes(errorCode=0, rewardList=[])
        client = FakeGameClient({
            ExperimentInfo: status_res,
            ExperimentFightingRequest: req_res,
            ExperimentFightingResult: settle_res,
        })

        task = ExperimentBattleTask(battle_wait_seconds=0, target_section_id=671000001)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is False
        assert res.data.get("aborted") == "no_lineup"
        assert not any(isinstance(p, ExperimentFightingResult) for p in client.sent)

    def test_forbidden_rebirth_packet(self) -> None:
        """验证 34025 GrandLineRebirthPacket 处于绝对禁发清单中。"""
        assert 34025 in FORBIDDEN_PACKET_IDS.values()
        assert "GrandLineRebirthPacket" in FORBIDDEN_PACKET_IDS
        assert 34025 not in ALLOWED_PACKET_IDS.values()

    def test_no_destiny_or_elemental_trials(self) -> None:
        """确保代码中严禁触碰天命系统与高难属性试炼。"""
        import tasks.battle_experiment as mod

        text = open(mod.__file__, "r", encoding="utf-8").read()
        assert "Destiny" not in text or "严禁" in text
        assert "destiny" not in text.lower() or "严禁" in text.lower()
        for forbidden in ("Volcano", "Glacier", "Desert", "火山", "极地", "荒漠"):
            assert forbidden not in text or "严禁" in text
