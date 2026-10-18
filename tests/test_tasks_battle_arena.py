"""天梯竞技场（battle_arena）自动化任务单元测试。

验证目标：
1. 挑战次数为 0 时平稳跳过对决，直接执行一键领奖；
2. 演练模式（dry_run=True）只查不打；
3. 严格仅选系统 NPC 训练家（713000001~713000011），拒绝挑战高战真人队伍；
4. 全真人队伍时安全跳过对决，杜绝风控检测；
5. 竞技场全生命周期推进断言（26001 -> 26003 -> 26019 -> 26017 一键领奖）；
6. 禁发买次数包（26015 BuyChallengePacket）与刷新包（26011）守卫拦截；
7. 永久工程红线自动化检查（无天命、无高难试炼）。
"""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace
import pytest

from models.battle_arena import (
    ChallengePacket,
    ChallengeRes,
    ChallengeResultPacket,
    ChallengeResultRes,
    PVPResultsClass,
    ReceiveRewardPacket,
    ReceiveRewardRes,
)
from models.game_packet import ProtoMessage
from models.assemble import LineupRole, encode_assemble
from models.pvp import PVPHomePacket, PVPHomeRes
from client.session import GameSession
from models.session_state import GameSessionState
from tasks.battle_arena import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    ArenaBattleTask,
    PVPListClass,
    is_npc_opponent,
)
from crypto.battle_signer import calc_arena_token

MOCK_ARENA_ROLES = (
    {"roleSid": 153867701, "roleID": 130008001, "roleLV": 80, "roleStar": 5, "name": "晨曦"},
    {"roleSid": 153867702, "roleID": 130008002, "roleLV": 80, "roleStar": 5, "name": "烈焰"},
    {"roleSid": 153867703, "roleID": 130008003, "roleLV": 75, "roleStar": 4, "name": "微风"},
    {"roleSid": 153867704, "roleID": 130008004, "roleLV": 70, "roleStar": 4, "name": "厚土"},
    {"roleSid": 153867705, "roleID": 130008005, "roleLV": 70, "roleStar": 3, "name": "激流"},
)


def make_assemble(roles: Any = MOCK_ARENA_ROLES, *, enemy: bool = False) -> bytes:
    """构造服务端下发的阵容快照（26004.mineAssemble / opponentAssemble）。

    NPC 训练家的真实阵容是 2 个单位（PVPTrainerData 的 trainerTeam 只有 2 个 ID），
    这里用 ``enemy_units`` 参数模拟，验证代码不会擅自补齐成 5 个。
    """
    if enemy:
        lineup = [LineupRole(900000001 + i, 130003906 + i, 50, 6) for i in range(5)]
    else:
        lineup = [
            LineupRole(r["roleSid"], r["roleID"], r["roleLV"], r["roleStar"]) for r in roles
        ]
    return encode_assemble(lineup)


MOCK_MINE_ASSEMBLE = make_assemble()
MOCK_ENEMY_ASSEMBLE = make_assemble(enemy=True)


def make_arena_session(roles: Any = MOCK_ARENA_ROLES) -> GameSession:
    state = GameSessionState(token="mock_token", player_id=123, roles=tuple(roles))
    return GameSession(game_state=state)


class FakeGameClient:
    """模拟网关响应客户端。"""

    def __init__(self, responses: dict[type[ProtoMessage], Any]) -> None:
        self.responses = dict(responses)
        if ReceiveRewardPacket not in self.responses:
            self.responses[ReceiveRewardPacket] = ReceiveRewardRes(errorCode=-1)
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


class TestArenaBattleTask:
    """竞技场自动化任务测试套件。"""

    def test_arena_challenge_count_zero_skips_and_claims(self) -> None:
        """挑战次数为 0 时跳过对决，并尝试执行一键领奖。"""
        home_res = PVPHomeRes(challengeCount=0)
        client = FakeGameClient({
            PVPHomePacket: home_res,
            ReceiveRewardPacket: ReceiveRewardRes(errorCode=-1),
        })

        task = ArenaBattleTask(battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["battles_completed"] == 0
        assert len(client.sent) == 2
        assert isinstance(client.sent[0], PVPHomePacket)
        assert isinstance(client.sent[1], ReceiveRewardPacket)

    def test_arena_dry_run(self) -> None:
        """演练模式下只查状态与候选人，不进入实际战斗。"""
        opp = PVPListClass(playerID=713000001, name="", fighting=12000)
        home_res = PVPHomeRes(challengeCount=5, pvpList=[opp.encode()])
        client = FakeGameClient({PVPHomePacket: home_res})

        task = ArenaBattleTask(dry_run=True, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["dry_run"] is True
        assert res.data["challenge_count"] == 5
        assert res.data["target_player_id"] == 713000001
        assert res.data["is_npc"] is True
        assert len(client.sent) == 1
        assert isinstance(client.sent[0], PVPHomePacket)

    def test_arena_pick_weakest_npc_opponent(self) -> None:
        """验证择弱逻辑：自动挑选候选列表中战力最低的 NPC 训练师（忽略真人队伍）。"""
        opp_real1 = PVPListClass(playerID=1001, name="高战真人", fighting=5000000, allianceName="某公会")
        opp_npc1 = PVPListClass(playerID=713000002, name="", fighting=20000)
        opp_npc2 = PVPListClass(playerID=713000001, name="", fighting=12000)
        opp_real2 = PVPListClass(playerID=1002, name="真人对手", fighting=2500000, allianceName="某公会")

        home_res = PVPHomeRes(
            challengeCount=5,
            pvpList=[opp_real1.encode(), opp_npc1.encode(), opp_npc2.encode(), opp_real2.encode()],
        )
        req_res = ChallengeRes(errorCode=0, fightToken=88888, mineAssemble=MOCK_MINE_ASSEMBLE, opponentAssemble=MOCK_ENEMY_ASSEMBLE)
        settle_res = ChallengeResultRes(
            errorCode=0,
            myResults=PVPResultsClass(victory=1, myIntegral=4100, myRanking=28),
        )

        client = FakeGameClient({
            PVPHomePacket: home_res,
            ChallengePacket: req_res,
            ChallengeResultPacket: settle_res,
            ReceiveRewardPacket: ReceiveRewardRes(errorCode=-1),
        })

        task = ArenaBattleTask(session=make_arena_session(), battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["target_player_id"] == 713000001  # 准确命中战力更低的 NPC 713000001
        assert res.data["target_fighting"] == 12000
        assert client.sent[1].playerID == 713000001

    def test_arena_skips_when_all_opponents_are_real_players(self) -> None:
        """当候选对手全为真人队伍（没有 NPC 训练师）时，安全跳过对决，杜绝风控风险。"""
        opp1 = PVPListClass(playerID=2001, name="真人1", fighting=1800000, allianceName="公会A")
        opp2 = PVPListClass(playerID=2002, name="真人2", fighting=3200000, allianceName="公会B")
        home_res = PVPHomeRes(challengeCount=5, pvpList=[opp1.encode(), opp2.encode()])
        client = FakeGameClient({
            PVPHomePacket: home_res,
            ReceiveRewardPacket: ReceiveRewardRes(errorCode=-1),
        })

        task = ArenaBattleTask(battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["battles_completed"] == 0
        # 绝不发送 26003 挑战包，只发了 26001 查询与 26017 领奖
        assert not any(isinstance(p, ChallengePacket) for p in client.sent)

    def test_arena_full_lifecycle(self) -> None:
        """完整生命周期：26001 查询 -> 26003 进战 -> 26019 结算 (携带 Token) -> 26017 一键领奖。"""
        opp = PVPListClass(playerID=713000001, name="", fighting=25000)
        home_res = PVPHomeRes(challengeCount=5, pvpList=[opp.encode()])
        req_res = ChallengeRes(errorCode=0, fightToken=29324, mineAssemble=MOCK_MINE_ASSEMBLE, opponentAssemble=MOCK_ENEMY_ASSEMBLE)
        settle_res = ChallengeResultRes(
            errorCode=0,
            myResults=PVPResultsClass(victory=1, myIntegral=4060, myRanking=30),
        )

        client = FakeGameClient({
            PVPHomePacket: home_res,
            ChallengePacket: req_res,
            ChallengeResultPacket: settle_res,
            ReceiveRewardPacket: ReceiveRewardRes(errorCode=-1),
        })

        task = ArenaBattleTask(session=make_arena_session(), battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["my_ranking"] == 30
        assert res.data["my_integral"] == 4060
        assert res.data["victory"] == 1

        # 验证发包序列：26001 -> 26003 -> 26019 -> 26017
        assert len(client.sent) == 4
        assert isinstance(client.sent[0], PVPHomePacket)
        assert isinstance(client.sent[1], ChallengePacket)
        assert client.sent[1].playerID == 713000001
        assert client.sent[1].actionType == 0

        assert isinstance(client.sent[2], ChallengeResultPacket)
        assert client.sent[2].playerID == 713000001
        assert client.sent[2].fightToken == 29324
        assert client.sent[2].result == 0
        # 验证竞技场签名与抓包基准一致
        expected_token = calc_arena_token(29324, 713000001, win_lose=0)
        assert client.sent[2].token == expected_token
        # 验证动态战力与存证字段（P0：fightingNumber 是我方队伍战力，与 battleResult.fightingPower 一致）
        assert client.sent[2].fightingNumber == client.sent[2].battleResult.fightingPower
        assert client.sent[2].fightingNumber > 0
        assert client.sent[2].battleResult.battleServerVerify != ""
        # P0 守卫：获胜伤害绝不可为 0
        assert client.sent[2].battleResult.damage > 0
        # P1 守卫：验证 13 维明细记录 myTeamRecord 与 enemyTeamRecord 均已填充 5 名角色
        assert len(client.sent[2].battleResult.myTeamRecord) == 5
        assert len(client.sent[2].battleResult.enemyTeamRecord) == 5
        assert client.sent[2].battleResult.damageContent.startswith("t=")
        # P2 守卫：硬编码 $Arena$ 脚本特征已消除，包含首发角色名
        assert "$Arena$" not in client.sent[2].battleResult.damageContent
        assert "$晨曦$" in client.sent[2].battleResult.damageContent

        # 验证发出的第 4 个包是 26017 一键领奖
        assert isinstance(client.sent[3], ReceiveRewardPacket)
        assert client.sent[3].missionID == -1

    def test_arena_one_key_reward_claim(self) -> None:
        """验证挑战结束后自动发送 26017 一键领取竞技场胜场与连胜奖励并解析道具。"""
        opp = PVPListClass(playerID=713000001, name="", fighting=10000)
        home_res = PVPHomeRes(challengeCount=1, pvpList=[opp.encode()])
        req_res = ChallengeRes(errorCode=0, fightToken=54321, mineAssemble=MOCK_MINE_ASSEMBLE, opponentAssemble=MOCK_ENEMY_ASSEMBLE)
        settle_res = ChallengeResultRes(
            errorCode=0,
            myResults=PVPResultsClass(victory=1, myIntegral=4150, myRanking=25),
        )
        from models.daily import GetObjClass, ItemClass
        reward_items = [GetObjClass(itemList=[ItemClass(itemID=1001, itemAmount=100)])]
        claim_res = ReceiveRewardRes(errorCode=0, items=reward_items)

        client = FakeGameClient({
            PVPHomePacket: home_res,
            ChallengePacket: req_res,
            ChallengeResultPacket: settle_res,
            ReceiveRewardPacket: claim_res,
        })

        task = ArenaBattleTask(session=make_arena_session(), battles=1, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["battles_completed"] == 1
        assert res.data["rewards_claimed"]["claimed"] is True
        assert "一键领奖" in res.message
        claim_packet = client.sent[3]
        assert isinstance(claim_packet, ReceiveRewardPacket)
        assert claim_packet.missionID == -1

    def test_arena_damage_not_zero_and_fallback_power(self) -> None:
        """P0 守卫：竞技场获胜伤害 damage 必须大于 0，且对手战力缺失时杜绝上报死数据 1。"""
        opp = PVPListClass(playerID=713000001, name="", fighting=0)
        home_res = PVPHomeRes(challengeCount=5, myFighting=3500000, pvpList=[opp.encode()])
        req_res = ChallengeRes(errorCode=0, fightToken=112233, mineAssemble=MOCK_MINE_ASSEMBLE, opponentAssemble=MOCK_ENEMY_ASSEMBLE)
        settle_res = ChallengeResultRes(
            errorCode=0,
            myResults=PVPResultsClass(victory=1, myIntegral=4060, myRanking=30),
        )

        client = FakeGameClient({
            PVPHomePacket: home_res,
            ChallengePacket: req_res,
            ChallengeResultPacket: settle_res,
            ReceiveRewardPacket: ReceiveRewardRes(errorCode=-1),
        })

        task = ArenaBattleTask(
            session=make_arena_session(),
            battle_wait_seconds=0,
            target_player_id=713000001,
        )
        task.open_game_client = lambda: client

        res = task.execute()
        assert res.ok is True
        result_pkt = client.sent[2]
        assert isinstance(result_pkt, ChallengeResultPacket)
        assert result_pkt.battleResult.damage > 0
        assert result_pkt.fightingNumber == 3500000

    def test_arena_battles_zero_fights_until_count_zero(self) -> None:
        """验证 battles=0 时自动打完所有可用挑战次数直到剩余 0 次退出并领奖。"""
        opp = PVPListClass(playerID=713000001, name="", fighting=20000)
        home_res_1 = PVPHomeRes(challengeCount=1, pvpList=[opp.encode()])
        home_res_2 = PVPHomeRes(challengeCount=0, pvpList=[opp.encode()])
        req_res = ChallengeRes(errorCode=0, fightToken=12345, mineAssemble=MOCK_MINE_ASSEMBLE, opponentAssemble=MOCK_ENEMY_ASSEMBLE)
        settle_res = ChallengeResultRes(
            errorCode=0,
            myResults=PVPResultsClass(victory=1, myIntegral=4100, myRanking=28),
        )

        class MultiRoundClient:
            def __init__(self) -> None:
                self.home_calls = 0
                self.sent: list[Any] = []

            def __enter__(self) -> MultiRoundClient:
                return self

            def __exit__(self, *args: Any) -> None:
                pass

            def send(self, packet: ProtoMessage, **kwargs: Any) -> Any:
                self.sent.append(packet)
                if isinstance(packet, PVPHomePacket):
                    self.home_calls += 1
                    resp = home_res_1 if self.home_calls == 1 else home_res_2
                    return SimpleNamespace(decode_sub_packet=lambda cls: resp)
                elif isinstance(packet, ChallengePacket):
                    return SimpleNamespace(decode_sub_packet=lambda cls: req_res)
                elif isinstance(packet, ChallengeResultPacket):
                    return SimpleNamespace(decode_sub_packet=lambda cls: settle_res)
                elif isinstance(packet, ReceiveRewardPacket):
                    return SimpleNamespace(decode_sub_packet=lambda cls: ReceiveRewardRes(errorCode=-1))
                raise KeyError(f"Unexpected: {type(packet)}")

        client = MultiRoundClient()
        task = ArenaBattleTask(session=make_arena_session(), battles=0, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        assert res.data["battles_completed"] == 1
        assert client.home_calls == 2

    def test_arena_dynamic_team_and_my_fighting(self) -> None:
        """验证动态从 PVPHomeRes 提取队伍 ID 和战力（P0：fightingNumber 确凿为我方战力）。"""
        from models.server import TeamClass

        opp = PVPListClass(playerID=713000005, name="", fighting=20000)
        team_payload = TeamClass(teamID=65).encode()
        home_res = PVPHomeRes(
            challengeCount=1,
            myFighting=888888,
            team=team_payload,
            pvpList=[opp.encode()],
        )
        req_res = ChallengeRes(errorCode=0, fightToken=11111, mineAssemble=MOCK_MINE_ASSEMBLE, opponentAssemble=MOCK_ENEMY_ASSEMBLE)
        settle_res = ChallengeResultRes(
            errorCode=0,
            myResults=PVPResultsClass(victory=1, myIntegral=4200, myRanking=20),
        )

        client = FakeGameClient({
            PVPHomePacket: home_res,
            ChallengePacket: req_res,
            ChallengeResultPacket: settle_res,
            ReceiveRewardPacket: ReceiveRewardRes(errorCode=-1),
        })

        task = ArenaBattleTask(session=make_arena_session(), team_id=65, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        # 验证 26003 与 26019 的 teamId 使用配置的 65，战力动态提取自 home.myFighting
        assert client.sent[1].teamID == 65
        assert client.sent[2].battleResult.teamId == 65
        assert client.sent[2].battleResult.fightingPower == 888888
        assert client.sent[2].fightingNumber == 888888  # ★ P0：确认并非对手的 20000

    def test_arena_rejects_empty_team_0_roles(self) -> None:
        """P1 守卫：0 人未配队账号严格禁止进入战斗，快速失败且绝不发送进战包 26003。"""
        opp = PVPListClass(playerID=713000001, name="", fighting=20000)
        home_res = PVPHomeRes(challengeCount=5, pvpList=[opp.encode()])
        client = FakeGameClient({
            PVPHomePacket: home_res,
            ReceiveRewardPacket: ReceiveRewardRes(errorCode=-1),
        })

        session = make_arena_session(roles=())
        task = ArenaBattleTask(session=session, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is False
        assert "0人未配队" in res.message
        # 绝不发送 26003 进战包与 26019 结算包
        assert not any(isinstance(p, ChallengePacket) for p in client.sent)
        assert not any(isinstance(p, ChallengeResultPacket) for p in client.sent)

    def test_arena_allows_fewer_than_5_roles(self) -> None:
        """P1 守卫：允许少于 5 人参战（实际几人就上报几人），绝不擅自填充虚假模板角色。"""
        opp = PVPListClass(playerID=713000001, name="", fighting=20000)
        home_res = PVPHomeRes(challengeCount=1, pvpList=[opp.encode()])
        # 仅 2 名真实出战角色（服务端阵容快照也只有 2 人）
        two_roles = MOCK_ARENA_ROLES[:2]
        req_res = ChallengeRes(
            errorCode=0,
            fightToken=12345,
            mineAssemble=make_assemble(two_roles),
            opponentAssemble=MOCK_ENEMY_ASSEMBLE,
        )
        settle_res = ChallengeResultRes(
            errorCode=0,
            myResults=PVPResultsClass(victory=1, myIntegral=4100, myRanking=28),
        )
        client = FakeGameClient({
            PVPHomePacket: home_res,
            ChallengePacket: req_res,
            ChallengeResultPacket: settle_res,
            ReceiveRewardPacket: ReceiveRewardRes(errorCode=-1),
        })

        session = make_arena_session(roles=two_roles)
        task = ArenaBattleTask(session=session, battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        result_pkt = client.sent[2]
        assert isinstance(result_pkt, ChallengeResultPacket)
        # 严谨断言：我方记录严格为 2，杜绝填充幽灵占位符
        assert len(result_pkt.battleResult.myTeamRecord) == 2
        from models.main_stage import WuDouResultRecord
        decoded = [WuDouResultRecord.decode(r) for r in result_pkt.battleResult.myTeamRecord]
        sids = [r.role_sid for r in decoded]
        assert sids == [153867701, 153867702]


    def test_arena_enemy_lineup_comes_from_server_assemble(self) -> None:
        """敌方记录必须来自 26004.opponentAssemble（NPC 训练家只有 2 个单位）。

        历史上这里用固定 5 个写死的 roleMainId 凑数，服务端明明下发了真实阵容，
        对不上就是一眼假。本测试锁死「服务端给几个人就报几个人」。
        """
        opp = PVPListClass(playerID=713000001, name="", fighting=12000)
        home_res = PVPHomeRes(challengeCount=1, pvpList=[opp.encode()])
        # NPC 训练家：2 个单位（PVPTrainerData.trainerTeam 只有 130000100 / 130000200）
        npc_enemy = encode_assemble(
            [LineupRole(0, 130000100, 10, 1), LineupRole(0, 130000200, 10, 1)]
        )
        req_res = ChallengeRes(
            errorCode=0,
            fightToken=24680,
            mineAssemble=MOCK_MINE_ASSEMBLE,
            opponentAssemble=npc_enemy,
        )
        settle_res = ChallengeResultRes(
            errorCode=0, myResults=PVPResultsClass(victory=1, myIntegral=4100, myRanking=28)
        )
        client = FakeGameClient({
            PVPHomePacket: home_res,
            ChallengePacket: req_res,
            ChallengeResultPacket: settle_res,
            ReceiveRewardPacket: ReceiveRewardRes(errorCode=-1),
        })

        task = ArenaBattleTask(session=make_arena_session(), battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is True
        result_pkt = client.sent[2]
        from models.main_stage import WuDouResultRecord

        enemy = [WuDouResultRecord.decode(r) for r in result_pkt.battleResult.enemyTeamRecord]
        assert len(enemy) == 2
        assert [r.roleMainId for r in enemy] == [130000100, 130000200]
        assert [r.lv for r in enemy] == [10, 10]
        assert all(r.role_sid == -1 for r in enemy)
        # 我方 dps 合计 == 敌方承伤合计（两边账必须对得上）
        mine = [WuDouResultRecord.decode(r) for r in result_pkt.battleResult.myTeamRecord]
        assert sum(r.dps for r in mine) == sum(r.tank for r in enemy) == result_pkt.battleResult.damage

    def test_arena_aborts_when_opponent_assemble_missing(self) -> None:
        """26004 没下发对手阵容时拒绝结算：绝不伪造一份战报顶上去。"""
        opp = PVPListClass(playerID=713000001, name="", fighting=12000)
        home_res = PVPHomeRes(challengeCount=1, pvpList=[opp.encode()])
        req_res = ChallengeRes(
            errorCode=0, fightToken=13579, mineAssemble=MOCK_MINE_ASSEMBLE
        )
        client = FakeGameClient({
            PVPHomePacket: home_res,
            ChallengePacket: req_res,
            ReceiveRewardPacket: ReceiveRewardRes(errorCode=-1),
        })

        task = ArenaBattleTask(session=make_arena_session(), battle_wait_seconds=0)
        task.open_game_client = lambda: client  # type: ignore[assignment]

        res = task.execute()
        assert res.ok is False
        assert res.data["aborted"] == "missing_opponent_assemble"
        assert not any(isinstance(p, ChallengeResultPacket) for p in client.sent)

    def test_forbidden_packet_defense(self) -> None:
        """验证 26015 BuyChallengePacket 与 26011 RefreshPacket 处于绝对禁发清单中，26017 允许领奖。"""
        assert 26015 in FORBIDDEN_PACKET_IDS.values()
        assert 26011 in FORBIDDEN_PACKET_IDS.values()
        assert 26015 not in ALLOWED_PACKET_IDS.values()
        assert 26017 in ALLOWED_PACKET_IDS.values()

    def test_no_destiny_or_high_difficulty(self) -> None:
        """确保代码中严禁触碰天命系统与高难试炼。"""
        import tasks.battle_arena as mod

        text = open(mod.__file__, "r", encoding="utf-8").read()
        assert "Destiny" not in text or "严禁" in text
        assert "destiny" not in text.lower() or "严禁" in text.lower()
        for forbidden in ("Volcano", "Glacier", "Desert", "火山", "极地", "荒漠"):
            assert forbidden not in text or "严禁" in text
