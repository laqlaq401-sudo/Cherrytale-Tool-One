"""三大目标玩法（失控炼成阵 / 竞技场 / 初始试炼）协议数据模型单元测试。

验证内容：
1. 模型定义与字段编号的一致性；
2. 真实抓包字节（提取自 captures/）的无损双向编解码（反序列化 -> 序列化 -> 逐字节比对）；
3. 红线检查（严禁包含天命 Destiny、高难度试炼扩展）。
"""

from __future__ import annotations

import pytest

from models import proto_fields
from models.battle_arena import (
    ChallengePacket,
    ChallengeRes,
    ChallengeResultPacket,
    ChallengeResultRes,
    PVPResultsClass,
    PVPVerifyPacket,
)
from models.battle_experiment import (
    ExperimentFightingRequest,
    ExperimentFightingRequestRes,
    ExperimentFightingResult,
    ExperimentFightingResultRes,
    ExperimentRoleClass,
    ExperimentTeamClass,
)
from models.battle_yimo import (
    YimoBattleRequest,
    YimoBattleRequestRes,
    YimoBattleResult,
    YimoBattleResultRes,
)
from models.main_stage import BattleResultClass, FightDataClass


# ==============================================================================
# 真实抓包子包原始载荷样本（提取自 captures/old/1.0.0NEED/ 真实 SAZ 会话）
# ==============================================================================
# 1. 失控炼成阵 raw/01 (22107 请求)
YIMO_REQ_HEX = "08c39e98bb021029"

# 2. 失控炼成阵 raw/26 (22109 结算请求 384 字节完整载荷与 22110 响应)
YIMO_RESULT_REQ_HEX = (
    "08b0e80110904e18ff9f0322d002085010dbf2fa011a15743d3124456d7074792453"
    "636f72653a353332343720002829322808be9ed5481082a6ff3d183220919fa80428"
    "3530872c38064000480050fbc13b58bcea7360356806322808ebaadf6a10cec7ff3d"
    "183220cebefe03281d300038054000480050d7de3158abc1d601601d680632280889"
    "83d54810c280ff3d183220c0a6c2142816300038154000480050b1be35588ff9e601"
    "60166806322708baf1e65f1092c2ff3d183220969a8b022824300038034000480050"
    "cab93758ecf46760246806322708a0f0fa4d10defffe3d183220e6b3a602283d3000"
    "38034000480050ffb61f58a29162603d6806425c48347349414e342b73576f412f36"
    "7457636b6f734b636c4a44616b73534657794d746452436b354e4c736e4d7a2f4e4d"
    "556249794d7a4d304141466a486157416f767a387447496c712b6a5957674244735a"
    "61424d67414141413d3d2a2030613333393630353166616631383637363264383138"
    "31353937373633306266"
)
YIMO_RESULT_RES_HEX = (
    "080012320a0f08e3f1c07810b284af5f18a1ffce090a0e088e90fd7810b184af5f18"
    "b7dc010a0f08dfabcd7810f19ebb5f1884c3b302181020e9e4062804"
)

# 3. 竞技场 raw/020 (26003 进战请求)
ARENA_CHALLENGE_REQ_HEX = "080010eef3f802183c"
# 4. 竞技场 raw/243 (26020 响应)
ARENA_RESULT_RES_HEX = "08001209081e10dc1f18012000"

# 5. 初始试炼 raw/05 (34009 请求)
EXP_FIGHT_REQ_HEX = (
    "08c1cbfabf0210ffffffffffffffffff0118f4c98a4418a7fac455188983e24818a0"
    "b0d34d18fa8fe75f2082b2d6b9032884a7d6b803"
)
# 6. 初始试炼 raw/09 (34012 响应)
EXP_RESULT_RES_HEX = "0800120f0a0d08eae4deac0110ba97da5f1803"


class TestYimoModels:
    """失控炼成阵数据模型验证。"""

    def test_yimo_battle_request_roundtrip(self) -> None:
        raw_bytes = bytes.fromhex(YIMO_REQ_HEX)
        req = YimoBattleRequest.decode(raw_bytes)
        assert req.part == 661000003
        assert req.bossLv == 41
        assert req.encode() == raw_bytes

    def test_yimo_battle_result_roundtrip(self) -> None:
        raw_bytes = bytes.fromhex(YIMO_RESULT_REQ_HEX)
        result = YimoBattleResult.decode(raw_bytes)
        assert result.fightToken == 29744
        assert result.dmgHpRate == 10000
        assert result.score == 53247
        assert result.token == "0a3396051faf186762d81815977630bf"
        assert result.battleResult.teamId == 80
        assert result.battleResult.damage == 0
        assert result.battleResult.yimoLv == 41
        assert len(result.battleResult.myTeamRecord) == 5
        assert result.encode() == raw_bytes

    def test_yimo_battle_result_res_roundtrip(self) -> None:
        raw_bytes = bytes.fromhex(YIMO_RESULT_RES_HEX)
        res = YimoBattleResultRes.decode(raw_bytes)
        assert res.errorCode == 0
        assert res.is_success()
        assert res.rank == 16
        assert res.totalValue == 111209
        assert res.remainCount == 4
        assert len(res.rewardList) == 1
        assert len(res.rewardList[0].itemList) == 3
        assert res.encode() == raw_bytes


class TestArenaModels:
    """竞技场数据模型验证。"""

    def test_arena_challenge_request_roundtrip(self) -> None:
        raw_bytes = bytes.fromhex(ARENA_CHALLENGE_REQ_HEX)
        req = ChallengePacket.decode(raw_bytes)
        assert req.actionType == 0
        assert req.playerID == 6175214
        assert req.teamID == 60
        assert req.encode() == raw_bytes

    def test_arena_challenge_result_res_roundtrip(self) -> None:
        raw_bytes = bytes.fromhex(ARENA_RESULT_RES_HEX)
        res = ChallengeResultRes.decode(raw_bytes)
        assert res.errorCode == 0
        assert res.is_success()
        assert res.myResults is not None
        assert res.myResults.myRanking == 30
        assert res.myResults.myIntegral == 4060
        assert res.myResults.victory == 1
        assert res.myResults.winning_streak == 0
        assert res.encode() == raw_bytes

    def test_arena_pvp_verify_roundtrip(self) -> None:
        packet = PVPVerifyPacket(
            battleToken=29332,
            reportSid=0,
            winOrLose=-1,
            atkTeamPlayId=6175829,
            defTeamPlayId=0,
            battleType=4,
            message='{"SectionId":0,"damage":0,"Proofs":[]}',
            sectionID=0,
            fightRecord="[]",
            fightData="{}",
        )
        encoded = packet.encode()
        decoded = PVPVerifyPacket.decode(encoded)
        assert decoded.battleToken == 29332
        assert decoded.winOrLose == -1
        assert decoded.atkTeamPlayId == 6175829
        assert decoded.battleType == 4
        assert decoded.message == '{"SectionId":0,"damage":0,"Proofs":[]}'
        assert decoded.fightRecord == "[]"
        assert decoded.fightData == "{}"
        assert decoded.encode() == encoded


class TestExperimentModels:
    """初始试炼数据模型验证。"""

    def test_experiment_request_roundtrip(self) -> None:
        raw_bytes = bytes.fromhex(EXP_FIGHT_REQ_HEX)
        req = ExperimentFightingRequest.decode(raw_bytes)
        assert req.sectionID == 671000001
        assert req.version == -1
        assert req.teamSid == [142779636, 179387687, 152600969, 162846752, 200919034]
        assert req.mirrorBuffID == 926259458
        assert req.mirrorMachineID == 924160900
        # 验证非 packed 的 repeated varint 线类型完美重现
        assert req.encode() == raw_bytes

    def test_experiment_result_res_roundtrip(self) -> None:
        raw_bytes = bytes.fromhex(EXP_RESULT_RES_HEX)
        res = ExperimentFightingResultRes.decode(raw_bytes)
        assert res.errorCode == 0
        assert res.is_success()
        assert len(res.rewardList) == 1
        assert res.encode() == raw_bytes

    def test_experiment_result_components(self) -> None:
        team = ExperimentTeamClass(position=1, hp=0)
        assert team.encode() == bytes.fromhex("08011000")

        role = ExperimentRoleClass(role_sid=142779636, hp=98, isTeam=1)
        role_bytes = role.encode()
        decoded_role = ExperimentRoleClass.decode(role_bytes)
        assert decoded_role.role_sid == 142779636
        assert decoded_role.hp == 98
        assert decoded_role.isTeam == 1


class TestRedLinesAndSafety:
    """红线与安全约束自动化检查。"""

    def test_no_destiny_in_models(self) -> None:
        """确保三大模型模块均未导入或定义任何天命 Destiny 逻辑。"""
        import models.battle_arena as m_arena
        import models.battle_experiment as m_exp
        import models.battle_yimo as m_yimo

        for mod in (m_arena, m_exp, m_yimo):
            text = open(mod.__file__, "r", encoding="utf-8").read()
            assert "Destiny" not in text or "严禁" in text, f"{mod.__name__} 违规引用了 Destiny"
            assert "destiny" not in text.lower() or "严禁" in text.lower(), f"{mod.__name__} 违规引用了 destiny"

    def test_initial_trial_bounds(self) -> None:
        """确保初始试炼仅面向基础关卡，不触碰高难火山/极地/风暴试炼。"""
        import models.battle_experiment as m_exp

        text = open(m_exp.__file__, "r", encoding="utf-8").read()
        assert "Volcano" not in text
        assert "Glacier" not in text
        assert "Desert" not in text
