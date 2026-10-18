"""战斗明细记录构建器（WuDouResultRecord 存证填充器）。

【数据来源铁律（2026-10-19 重写）】
战报里的每一名角色都必须来自**服务端下发的阵容快照**
（``BattleAssembleClass``，见 :mod:`models.assemble`）：
竞技场 ``26004.mineAssemble/opponentAssemble``、失控炼成阵 ``22108.mineAssemble``、
试炼 ``34010.mineAssemble``。

因此本模块**不再包含任何写死的角色 ID / 等级 / 星级**。
历史上 ``build_enemy_team_records`` 里那两组 ``roleMainId`` 是抄自单份抓包的
死数据（一份来自竞技场对手阵容、一份来自主线小怪），换号换对手立刻对不上，
已从 PVP 三玩法（竞技场/炼成阵/试炼）的调用链上移除，只留给主线 PVE 兜底。

【哪些是「服务端数据」、哪些是「本地拟合」】
====================  ==========================================  ==========
字段                    来源                                         说明
====================  ==========================================  ==========
role_sid              ``RoleClass.roleSid``（服务端）                敌方恒填 -1（真机如此）
roleMainId            ``RoleClass.roleID``（服务端）                 ——
lv / roleStarLv       ``RoleClass.roleLV / roleStar``（服务端）      ——
dps / tank / heal     **本地拟合**（服务端只下发名单，不下发伤害）  按总伤害与总战力分摊
power                 **本地拟合**                                  按战力权重分摊，合计=队伍总战力
hit / beHit / shield  **本地拟合**                                  按玩法给合理区间
====================  ==========================================  ==========

拟合只保证「内部自洽 + 量级合理」，绝不冒充服务端数值。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from models.assemble import MAX_TEAM_SIZE, LineupRole
from models.main_stage import WuDouResultRecord

#: 输出伤害权重（主 C / 副 C / 辅 / 坦 / 治）——只决定分配比例，不决定总量
DPS_WEIGHTS: tuple[float, ...] = (0.40, 0.28, 0.18, 0.10, 0.04)

#: 战力权重（同一队伍里各角色的战力占比）
POWER_WEIGHTS: tuple[float, ...] = (0.26, 0.23, 0.20, 0.17, 0.14)

#: 单角色战力的经验折算系数（与主线 ``resolve_account_team_power`` 同源）
_POWER_PER_LEVEL = 3200
_POWER_STAR_STEP = 0.18
_POWER_TEAM_BONUS = 1.20


def estimate_role_power(lv: int, star: int) -> int:
    """按等级/星级折算单角色战力（服务端协议里没有单角色战力字段）。"""
    lv = max(int(lv), 1)
    star = max(int(star), 1)
    return int(lv * _POWER_PER_LEVEL * (1.0 + (star - 1) * _POWER_STAR_STEP))


def estimate_lineup_power(lineup: Sequence[LineupRole]) -> int:
    """按**服务端下发的阵容**折算队伍总战力。

    与「名册前 5 名」的区别：只统计真正上场的这几个人，
    因此不会出现「上报战力是别人的、名单是另几个人」的错位。
    """
    if not lineup:
        return 0
    total = sum(estimate_role_power(r.lv, r.star) for r in lineup)
    return max(int(total * _POWER_TEAM_BONUS), 20000)


def lineup_from_roles(
    roles: Any,
    target_sids: Sequence[int] | None = None,
) -> list[LineupRole]:
    """从账号名册（1004/5011 真实数据）兜底出出战名单。

    **只做兜底**：正常路径应使用 :func:`models.assemble.decode_lineup` 解服务端快照。
    这里同样不造数据 —— 名册里找不到的 sid 直接跳过，绝不补幽灵角色。
    """
    sid_map: dict[int, Any] = {}
    for r in roles or ():
        if isinstance(r, dict):
            sid = int(r.get("roleSid", 0) or 0)
        else:
            sid = int(getattr(r, "roleSid", 0) or 0)
        if sid > 0:
            sid_map[sid] = r

    if target_sids:
        picked = [sid_map[sid] for sid in target_sids if sid in sid_map]
    else:
        picked = sorted(
            sid_map.values(),
            key=lambda x: (
                int((x.get("roleLV") if isinstance(x, dict) else getattr(x, "roleLV", 1)) or 1),
                int((x.get("roleStar") if isinstance(x, dict) else getattr(x, "roleStar", 1)) or 1),
            ),
            reverse=True,
        )[:MAX_TEAM_SIZE]

    lineup: list[LineupRole] = []
    for r in picked:
        if isinstance(r, dict):
            sid = int(r.get("roleSid", 0) or 0)
            main_id = int(r.get("roleID") or r.get("roleMainId") or 0)
            lv = int(r.get("roleLV", 1) or 1)
            star = int(r.get("roleStar", 1) or 1)
        else:
            sid = int(getattr(r, "roleSid", 0) or 0)
            main_id = int(getattr(r, "roleID", 0) or getattr(r, "roleMainId", 0) or 0)
            lv = int(getattr(r, "roleLV", 1) or 1)
            star = int(getattr(r, "roleStar", 1) or 1)
        if sid > 0:
            lineup.append(LineupRole(role_sid=sid, role_main_id=main_id, lv=lv, star=star))
    return lineup


def _split(total: int, weights: Sequence[float]) -> list[int]:
    """按权重把整数 ``total`` 切成若干份，**保证合计严格等于 total**。"""
    count = len(weights)
    if count == 0:
        return []
    if total <= 0:
        return [0] * count
    norm = sum(weights) or 1.0
    raw = [total * (w / norm) for w in weights]
    parts = [int(x) for x in raw]
    parts[-1] += total - sum(parts)
    if parts[-1] < 0:  # 极端舍入保护
        parts[-1] = 0
    return parts


@dataclass(frozen=True)
class RecordShape:
    """一个玩法里「本地拟合字段」的取值区间（只描述形状，不含任何具体角色数据）。"""

    #: 命中次数区间
    hit: tuple[int, int] = (1, 6)
    #: 被击次数区间
    be_hit: tuple[int, int] = (0, 3)
    #: 护盾量占该角色战力的比例区间
    shield_ratio: tuple[float, float] = (0.0, 0.0)
    #: 治疗量占该角色战力的比例区间（仅最后一名角色生效，模拟治疗位）
    heal_ratio: tuple[float, float] = (0.0, 0.0)
    #: 治疗量占**本场总伤害**的比例区间（主线 PVE 沿用的旧口径）
    heal_from_damage: tuple[float, float] = (0.0, 0.0)
    #: 承伤绝对值区间（我方用；敌方的承伤由总伤害分摊）
    tank: tuple[int, int] = (0, 0)
    #: 最低承伤/状态值区间
    low_dtps: tuple[int, int] = (0, 0)
    #: 是否丢弃零伤害条目（真机试炼：没输出的角色客户端根本不写进 myTeamRecord）
    drop_zero_damage: bool = False


#: 竞技场·我方（真机形状：tank 恒 0、有护盾、有治疗、被击次数偏多）
ARENA_MINE = RecordShape(
    hit=(1, 11),
    be_hit=(3, 14),
    shield_ratio=(0.008, 0.042),
    heal_ratio=(0.012, 0.045),
)

#: 竞技场·敌方（真机形状：dps 恒 0、承伤=我方输出、命中 1~10 次）
ARENA_ENEMY = RecordShape(
    hit=(1, 10),
    be_hit=(2, 8),
    low_dtps=(2000, 8000),
)

#: 失控炼成阵·我方（真机形状：tank 极小、beHit 恒 0、命中次数多）
YIMO_MINE = RecordShape(
    hit=(2, 20),
    be_hit=(0, 0),
    heal_ratio=(0.0005, 0.002),
    tank=(10, 80),
)

#: 试炼·我方（真机形状：hit/beHit 恒 0、tank 基本 0、零输出角色不写条目）
EXPERIMENT_MINE = RecordShape(
    hit=(0, 0),
    be_hit=(0, 0),
    tank=(0, 0),
    drop_zero_damage=True,
)


def build_lineup_records(
    lineup: Sequence[LineupRole],
    *,
    total_damage: int,
    team_power: int,
    enemy: bool = False,
    shape: RecordShape = RecordShape(),
) -> list[bytes]:
    """按服务端阵容生成 ``WuDouResultRecord`` 字节列表。

    :param lineup: 服务端下发的出战名单（敌方同样来自 ``opponentAssemble``）。
    :param total_damage: 本场总伤害；我方按 DPS 权重分摊为 ``dps``，
        敌方按同一权重分摊为 ``tank``（两边合计都等于它，避免出现
        「我方打了 10 万、敌方只掉了 500」这种一眼假的账）。
    :param team_power: 队伍总战力；按战力权重分摊为每人的 ``power``。
    :param enemy: 敌方记录（``role_sid`` 恒 -1、``dps`` 恒 0）。
    :param shape: 该玩法的拟合形状。
    :return: 每名角色一条 protobuf 字节（零伤害条目按 ``shape`` 决定是否丢弃）。
    """
    members = list(lineup)[:MAX_TEAM_SIZE]
    if not members:
        return []

    count = len(members)
    dps_parts = _split(total_damage, DPS_WEIGHTS[:count])
    power_parts = _split(team_power, POWER_WEIGHTS[:count])

    records: list[bytes] = []
    for index, member in enumerate(members):
        hero_power = power_parts[index]
        if enemy:
            dps = 0
            tank = dps_parts[index]  # 敌方承伤 = 我方对它的输出
            heal = 0
            shield = 0
            low_dtps = random.randint(*shape.low_dtps) if shape.low_dtps[1] > 0 else 0
        else:
            dps = dps_parts[index]
            tank = random.randint(*shape.tank) if shape.tank[1] > 0 else 0
            if index == count - 1 and shape.heal_from_damage[1] > 0:
                heal = int(total_damage * random.uniform(*shape.heal_from_damage))
            elif index == count - 1 and shape.heal_ratio[1] > 0:
                heal = int(hero_power * random.uniform(*shape.heal_ratio))
            else:
                heal = 0
            shield = (
                int(hero_power * random.uniform(*shape.shield_ratio))
                if shape.shield_ratio[1] > 0
                else 0
            )
            low_dtps = 0

        if shape.drop_zero_damage and not enemy and dps <= 0:
            continue

        top_dps = int(dps * random.uniform(0.15, 0.55)) if dps > 0 else 0

        records.append(
            WuDouResultRecord(
                role_sid=-1 if enemy else member.role_sid,
                roleMainId=member.role_main_id,
                lv=member.lv,
                dps=dps,
                tank=tank,
                heal=heal,
                hit=random.randint(*shape.hit),
                beHit=random.randint(*shape.be_hit),
                shield=shield,
                power=hero_power,
                topDps=top_dps,
                lowDtps=low_dtps,
                roleStarLv=member.star,
            ).encode(include_defaults=True)
        )

    return records


# ---------------------------------------------------------------------------
# 兼容层：主线 PVE 仍在用（PVP 三玩法禁止再调用下面两个函数）
# ---------------------------------------------------------------------------
#: 主线 PVE·我方（沿用历史口径：命中 4~12 次、承伤 0~300、治疗约占伤害 12%）
PVE_MINE = RecordShape(
    hit=(4, 12),
    be_hit=(0, 3),
    tank=(0, 300),
    heal_from_damage=(0.10, 0.16),
)


def build_my_team_records(
    roles: Any,
    total_damage: int,
    team_power: int,
    target_sids: list[int] | None = None,
) -> list[bytes]:
    """主线 PVE 兼容入口：把账号名册转成出战名单后生成我方记录。

    .. deprecated::
       PVP 三玩法（竞技场/炼成阵/试炼）请改用
       :func:`models.assemble.decode_lineup` + :func:`build_lineup_records`，
       否则会绕过服务端下发的真实阵容。
    """
    lineup = lineup_from_roles(roles, target_sids)
    if not lineup:
        raise ValueError("出战角色列表为空（0人未配队），禁止进入战斗与上报战报")
    return build_lineup_records(
        lineup,
        total_damage=total_damage,
        team_power=team_power,
        shape=PVE_MINE,
    )


def build_enemy_team_records(
    enemy_power: int,
    is_pvp: bool = False,
    opponent_lv: int = 40,
    is_npc: bool = False,
) -> list[bytes]:
    """主线 PVE 兼容入口：按主线小怪原型生成敌方记录（**仅 PVE**）。

    .. warning::
       这里的 ``roleMainId`` 来自主线小怪（1307031xx 系列），与抓包 raw/102 的
       敌方记录一致。PVP/试炼的敌方名单必须从 ``opponentAssemble`` 解出来 ——
       抄一份抓包里的对手阵容去填另一场战斗，是最容易被服务端识破的假数据，
       所以这里对 PVP 调用直接报错，而不是"顺手复用"。
    """
    if is_pvp or is_npc:
        raise ValueError(
            "build_enemy_team_records 只用于主线 PVE；"
            "PVP 敌方名单必须来自 26004.opponentAssemble（见 models.assemble）"
        )

    enemy_weights = (0.28, 0.22, 0.20, 0.16, 0.14)
    main_ids = (130703103, 130703103, 130705103, 130703903, 130703503)
    records: list[bytes] = []
    for index, main_id in enumerate(main_ids):
        single_power = max(int(enemy_power * enemy_weights[index]), 1000)
        taken_damage = max(int(single_power * random.uniform(0.02, 0.05)), 100)
        records.append(
            WuDouResultRecord(
                role_sid=-1,
                roleMainId=main_id,
                lv=max(opponent_lv, 1),
                dps=0,
                tank=taken_damage,
                heal=0,
                hit=random.randint(1, 3),
                beHit=random.randint(2, 6),
                shield=0,
                power=single_power,
                topDps=0,
                lowDtps=random.randint(50, 300),
                roleStarLv=2,
            ).encode(include_defaults=True)
        )
    return records
