"""战斗阵容快照（BattleAssembleClass）解析：**只认服务端下发的真实出战名单**。

【这个模块解决什么问题】
竞技场 26004、失控炼成阵 22108、试炼 34010 的响应里都带同一份
``BattleAssembleClass``（dump.cs TypeDefIndex 9200），内容是**服务端认定的
真实出战角色**（roleSid / roleID / roleLV / roleStar）。战报存证里的
``myTeamRecord`` / ``enemyTeamRecord`` 必须用这份名单：

- 用「名册前 5 名」→ 与实际出战阵容可能不是同一批人；
- 用写死的角色 ID（哪怕抄自某份抓包）→ 换个对手/换个号立刻对不上。

服务端手里本来就有正确名单（它自己下发的），对不上就是一眼假。

【字段依据（dump.cs，不是抓包猜的）】
- ``BattleAssembleClass``(9200)：``settingId``(1) / ``team``(2) / ``eigen``(3)
- ``RoleBattleOrDetailClass``(9104)：``roleClassData``(1) / ``equipClassDataList``(2) / ...
- ``RoleClass``(9106)：复用 :class:`models.role.RoleClass`（同一协议类，勿重复建模）

【已用真实字节验证（2026-10-18）】
竞技场 raw/020 的 ``mineAssemble`` 解出的 5 个 roleSid 与 ``26019.myTeamRecord``
逐字一致；炼成阵 22108 / 试炼 34010 的 ``mineAssemble`` 与各自结算包的
``roleHpList`` 同序同集合 —— 即「这条链就是出战名单的权威来源」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import Field

from models.game_packet import ProtoMessage
from models.role import RoleClass


class RoleBattleOrDetailClass(ProtoMessage):
    """阵容成员条目（dump.cs ``RoleBattleOrDetailClass`` TypeDefIndex 9104）。

    只建模 ``roleClassData``(1)；装备/天命/圣坛等字段（2~7）与战报无关，
    解码时按未知编号跳过。
    """

    proto_class_name = "RoleBattleOrDetailClass"

    roleClassData: RoleClass | None = None

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        return {"roleClassData": 1}


class BattleAssembleClass(ProtoMessage):
    """服务端下发的战斗阵容快照（dump.cs TypeDefIndex 9200）。"""

    proto_class_name = "BattleAssembleClass"

    settingId: list[int] = Field(default_factory=list)
    team: list[RoleBattleOrDetailClass] = Field(default_factory=list)
    #: 特性/天赋（``EigenClass``，本模块不消费）
    eigen: list[bytes] = Field(default_factory=list)

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        return {"settingId": 1, "team": 2, "eigen": 3}


@dataclass(frozen=True)
class LineupRole:
    """从服务端阵容快照解出的一名出战角色（战报存证的唯一数据源）。"""

    role_sid: int
    role_main_id: int
    lv: int
    star: int


def decode_lineup(raw: bytes | bytearray | None) -> list[LineupRole]:
    """把 ``mineAssemble`` / ``opponentAssemble`` 解成出战名单。

    :param raw: 服务端下发的阵容快照原始字节；``None`` / 空字节返回空列表。
    :return: 按服务端顺序排列的出战角色（最多 5 人，敌方 NPC 训练家可能只有 2 人）。
    :raises models.protobuf_wire.ProtobufWireError: 字节不是合法的 ``BattleAssembleClass`` 时。

    **故意不吞异常**：调用方需要区分「服务端没下发阵容」和「下发了但解不开」，
    前者可以回退到账号名册，后者说明协议模型与线上不一致，应该暴露出来。
    """
    if not raw:
        return []
    assemble = BattleAssembleClass.decode(bytes(raw))
    lineup: list[LineupRole] = []
    for member in assemble.team:
        role = member.roleClassData
        # 以 roleID（英雄原型）为准判断是不是有效成员：敌方 NPC 训练家可能不下发 roleSid，
        # 但 roleID 一定有；战报里敌方 role_sid 本来就恒填 -1。
        if role is None or role.roleID <= 0:
            continue
        lineup.append(
            LineupRole(
                role_sid=max(role.roleSid, 0),
                role_main_id=role.roleID,
                lv=max(role.roleLV, 1),
                star=max(role.roleStar, 1),
            )
        )
    return lineup


def encode_assemble(lineup: list[LineupRole]) -> bytes:
    """把出战名单编码成 ``BattleAssembleClass`` 字节（单测造夹具用）。

    生产代码不发这个包 —— 它只用来在测试里构造「服务端下发的阵容快照」，
    避免测试依赖真实抓包字节。
    """
    return BattleAssembleClass(
        team=[
            RoleBattleOrDetailClass(
                roleClassData=RoleClass(
                    roleSid=r.role_sid,
                    roleID=r.role_main_id,
                    roleLV=r.lv,
                    roleStar=r.star,
                )
            )
            for r in lineup
        ]
    ).encode()


#: 竞技场/试炼的队伍上限（客户端编队槽位固定 5 个）
MAX_TEAM_SIZE: Final[int] = 5
