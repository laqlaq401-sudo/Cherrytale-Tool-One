"""WuDouResultRecord 存证构建器单元测试。

验证目标：
1. 服务端阵容（``LineupRole``）→ 13 维记录：SID / roleMainId / 等级 / 星级 100% 来自名单；
2. 战力与 DPS 合计严格等于传入的队伍总战力 / 总伤害（两边账对得上）；
3. 敌方记录 ``role_sid`` 恒为 -1，且承伤合计等于我方输出；
4. PVP 敌方名单禁止走 PVE 的固定小怪原型（避免抄抓包死数据）。
"""

from __future__ import annotations

import pytest

from models.assemble import LineupRole
from models.main_stage import WuDouResultRecord
from tasks.battle_record_builder import (
    ARENA_MINE,
    EXPERIMENT_MINE,
    build_enemy_team_records,
    build_lineup_records,
    build_my_team_records,
    estimate_lineup_power,
    lineup_from_roles,
)


def _decode(records: list[bytes]) -> list[WuDouResultRecord]:
    return [WuDouResultRecord.decode(r) for r in records]


class TestBattleRecordBuilder:
    def test_build_my_team_records_with_real_roles(self) -> None:
        roles = [
            {"roleSid": 153867724, "roleID": 130008002, "roleLV": 50, "roleStar": 3},
            {"roleSid": 153867951, "roleID": 130000802, "roleLV": 48, "roleStar": 3},
            {"roleSid": 153857928, "roleID": 130003602, "roleLV": 45, "roleStar": 2},
            {"roleSid": 153857625, "roleID": 130003302, "roleLV": 45, "roleStar": 2},
            {"roleSid": 153019524, "roleID": 130000103, "roleLV": 40, "roleStar": 2},
            {"roleSid": 153019599, "roleID": 130000999, "roleLV": 10, "roleStar": 1},
        ]
        records = build_my_team_records(roles, 50000, 2500000)
        assert len(records) == 5

        decoded = _decode(records)
        assert decoded[0].role_sid == 153867724
        assert decoded[0].roleMainId == 130008002
        assert decoded[0].lv == 50
        assert decoded[0].roleStarLv == 3
        assert decoded[0].power > 0
        assert decoded[0].dps > 0
        assert decoded[0].topDps > 0
        # 治疗位（最后一名）
        assert decoded[4].heal > 0
        # 总 dps 严格等于本场总伤害
        assert sum(r.dps for r in decoded) == 50000

    def test_lineup_records_come_from_server_lineup(self) -> None:
        """记录里的 roleSid/roleMainId/lv/star 必须逐字来自服务端阵容。"""
        lineup = [
            LineupRole(role_sid=114421744, role_main_id=130003906, lv=50, star=6),
            LineupRole(role_sid=227811156, role_main_id=130014806, lv=48, star=5),
        ]
        records = build_lineup_records(
            lineup, total_damage=90000, team_power=3000000, shape=ARENA_MINE
        )
        decoded = _decode(records)
        assert [(r.role_sid, r.roleMainId, r.lv, r.roleStarLv) for r in decoded] == [
            (114421744, 130003906, 50, 6),
            (227811156, 130014806, 48, 5),
        ]
        assert sum(r.dps for r in decoded) == 90000
        assert sum(r.power for r in decoded) == 3000000

    def test_enemy_records_use_lineup_and_balance_damage(self) -> None:
        """敌方承伤合计 == 我方输出；role_sid 恒 -1。"""
        enemy_lineup = [
            LineupRole(role_sid=0, role_main_id=130000100, lv=10, star=1),
            LineupRole(role_sid=0, role_main_id=130000200, lv=10, star=1),
        ]
        records = build_lineup_records(
            enemy_lineup,
            total_damage=12000,
            team_power=12000,
            enemy=True,
        )
        decoded = _decode(records)
        assert len(decoded) == 2
        assert all(r.role_sid == -1 for r in decoded)
        assert [r.roleMainId for r in decoded] == [130000100, 130000200]
        assert all(r.dps == 0 for r in decoded)
        assert sum(r.tank for r in decoded) == 12000
        assert sum(r.power for r in decoded) == 12000

    def test_experiment_drops_zero_damage_records(self) -> None:
        """零输出角色不写进 myTeamRecord（真机试炼 5 人只有 3 条记录）。"""
        lineup = [LineupRole(200001 + i, 130015206 + i, 50, 6) for i in range(5)]
        records = build_lineup_records(
            lineup, total_damage=1, team_power=100000, shape=EXPERIMENT_MINE
        )
        decoded = _decode(records)
        assert 0 < len(decoded) <= 5
        assert all(r.dps > 0 for r in decoded)

    def test_lineup_from_roles_never_fabricates(self) -> None:
        """名册里没有的 sid 直接跳过，绝不补造角色。"""
        roles = [{"roleSid": 111, "roleID": 130001, "roleLV": 10, "roleStar": 1}]
        lineup = lineup_from_roles(roles, target_sids=[111, 999])
        assert [r.role_sid for r in lineup] == [111]
        assert estimate_lineup_power(lineup) > 0

    def test_build_enemy_team_records_pve(self) -> None:
        records = build_enemy_team_records(enemy_power=80000, is_pvp=False, opponent_lv=25)
        assert len(records) == 5

        decoded = _decode(records)
        assert all(r.role_sid == -1 for r in decoded)
        assert all(r.lv == 25 for r in decoded)
        assert all(r.roleStarLv == 2 for r in decoded)
        assert all(r.tank > 0 for r in decoded)

    def test_build_enemy_team_records_refuses_pvp(self) -> None:
        """PVP 敌方名单必须来自 opponentAssemble，禁止复用 PVE 小怪原型。"""
        with pytest.raises(ValueError):
            build_enemy_team_records(enemy_power=3000000, is_pvp=True, opponent_lv=50)
