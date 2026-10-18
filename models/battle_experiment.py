"""辉煌航迹·初始试炼（Experiment Trial）战斗协议数据模型。

【业务说明】
对应 SystemID 77「輝煌航跡」中的初始试炼关卡（配置表 TextAsset ``rule_experiment``），
协议模块 ``ExperimentModule``（34001~34026）。
本模块定义初始试炼常规关卡挑战与结算发奖的 Protobuf 协议结构：
- 34009 ``ExperimentFightingRequest``：申请进入初始试炼关卡（通常 sectionID 在 671000001~671000042）
- 34010 ``ExperimentFightingRequestRes``：进战响应，下发 ``fightToken`` 与我方编队数据
- 34011 ``ExperimentFightingResult``：提交通关结果，携带怪物存活状态、我方状态与 ``FightDataClass``
- 34012 ``ExperimentFightingResultRes``：试炼通关结算响应，下发首通/通关道具奖励

【安全与红线边界（Red Lines）】
- 仅支持基础「初始试炼」常规关卡推进（1~42 关）；
- 严禁触碰任何后续高难度的属性试炼（火山/极地/荒漠/风暴等航道）或终极噩梦关卡；
- 严禁引入任何天命（Destiny）相关的修改或注入。
"""

from __future__ import annotations

import logging
from typing import ClassVar, Final

from pydantic import Field

from models import proto_fields
from models.daily import GetObjClass
from models.game_packet import ProtoMessage
from models.main_stage import BattleResultClass, FightDataClass
from models.packet_ids import packet_id

_LOGGER: Final[logging.Logger] = logging.getLogger("models.battle_experiment")

#: 动态注册字段表
_EXPERIMENT_PROTO_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "ExperimentTeamClass": ("position", "hp"),
    "ExperimentRoleClass": ("role_sid", "hp", "isTeam"),
    "ExperimentFightingRequest": (
        "sectionID",
        "version",
        "teamSid",
        "mirrorBuffID",
        "mirrorMachineID",
    ),
    "ExperimentFightingRequestRes": (
        "errorCode",
        "fightToken",
        "fightGlobalEffectClass",
        "mineAssemble",
    ),
    "ExperimentFightingResult": (
        "winLose",
        "monsterHpList",
        "monster_mana",
        "roleHpList",
        "role_mana",
        "battleResult",
        "fightData",
    ),
    "ExperimentFightingResultRes": ("errorCode", "rewardList", "costObjList"),
}

#: 协议消息号映射
PACKET_ID_MAP: Final[dict[str, int]] = {
    "ExperimentFightingRequest": packet_id("ExperimentFightingRequest"),        # 34009
    "ExperimentFightingRequestRes": packet_id("ExperimentFightingRequestRes"),  # 34010
    "ExperimentFightingResult": packet_id("ExperimentFightingResult"),          # 34011
    "ExperimentFightingResultRes": packet_id("ExperimentFightingResultRes"),    # 34012
}


class ExperimentBattleProtoMessage(ProtoMessage):
    """试炼报文基类，提供字段映射兜底保障。"""

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        if cls.proto_class_name in _EXPERIMENT_PROTO_FIELDS:
            return {
                name: index
                for index, name in enumerate(_EXPERIMENT_PROTO_FIELDS[cls.proto_class_name], start=1)
            }
        return super().field_tag_map()


def get_mirror_machine_id(mirror_buff_id: int) -> int:
    """由 mirrorBuffID 动态推导对应的 mirrorMachineID。

    【逆向算法来源】
    对齐游戏资产 MirrorRankData 与 MirrorDrawMachineData：
    mirrorBuffID 属于某个卡池组（如 926000000..926000054 属于 926900001），
    该卡池组唯一对应一台镜像机体（925000001..925000006）。
    当未选定增益（mirror_buff_id <= 0）时返回 0。
    """
    if mirror_buff_id <= 0:
        return 0
    offset = (mirror_buff_id - 926000000) // 100
    if 0 <= offset <= 5:
        return 925000001 + offset
    return 0


class ExperimentTeamClass(ExperimentBattleProtoMessage):
    """试炼敌方怪物阵亡与血量状态（TypeDefIndex 9069）。"""

    proto_class_name = "ExperimentTeamClass"

    #: 怪物阵位（1~5 号站位）
    position: int = 0
    #: 怪物剩余血量比率（胜利结算时敌方全灭，hp=0）
    hp: int = 0


class ExperimentRoleClass(ExperimentBattleProtoMessage):
    """试炼我方参战英雄剩余状态（TypeDefIndex 9067）。"""

    proto_class_name = "ExperimentRoleClass"

    #: 英雄实例 SID
    role_sid: int = 0
    #: 英雄剩余血量百分比（如 98, 100 代表 98%~100% 健康存活）
    hp: int = 100
    #: 是否在出战阵容中（1=在队伍中）
    isTeam: int = 1


class ExperimentFightingRequest(ExperimentBattleProtoMessage):
    """申请进入初始试炼关卡战斗（消息号 34009）。"""

    proto_class_name = "ExperimentFightingRequest"

    #: 关键 wire-format 约束：抓包证实 teamSid 为非 packed 的 repeated varint（每个元素自带 tag 3）
    unpacked_fields: ClassVar[frozenset[str]] = frozenset({"teamSid"})

    #: 试炼关卡编号（如 671000001 起始的常规试炼）
    sectionID: int = 0
    #: 关卡配置版本号（无版本时默认填 -1）
    version: int = -1
    #: 我方出战 5 名英雄的 SID 列表
    teamSid: list[int] = Field(default_factory=list)
    #: 镜像增益 ID（无增益填 0）
    mirrorBuffID: int = 0
    #: 镜像装置 ID（无装置填 0）
    mirrorMachineID: int = 0


class ExperimentFightingRequestRes(ExperimentBattleProtoMessage):
    """申请进入初始试炼关卡响应（消息号 34010）。"""

    proto_class_name = "ExperimentFightingRequestRes"

    #: 错误码（0 = 成功）
    errorCode: int = 0
    #: ★ 本次试炼战斗的令牌，必须在结算时装入 FightDataClass 回传
    fightToken: int = 0
    #: 全局被动增益数据
    fightGlobalEffectClass: bytes = b""
    #: 我方编队快照
    mineAssemble: bytes = b""

    def is_success(self) -> bool:
        """是否成功进入关卡。"""
        return self.errorCode == 0


class ExperimentFightingResult(ExperimentBattleProtoMessage):
    """提交试炼关卡战斗结果（消息号 34011）。"""

    proto_class_name = "ExperimentFightingResult"

    #: 胜负结果（0 = 胜利, 1 = 失败）
    winLose: int = 0
    #: 敌方阵位阵亡状态（全死则 5 个怪物均为 hp=0）
    monsterHpList: list[ExperimentTeamClass] = Field(default_factory=list)
    #: 敌方剩余能量（通常为 0）
    monster_mana: int = 0
    #: 我方英雄存活与血量状态
    roleHpList: list[ExperimentRoleClass] = Field(default_factory=list)
    #: 我方剩余能量
    role_mana: int = 0
    #: 详细战斗存证数据
    battleResult: BattleResultClass = Field(default_factory=BattleResultClass)
    #: ★ 试炼凭证：装载 fightToken 与 sectionID
    fightData: FightDataClass = Field(default_factory=FightDataClass)


class ExperimentFightingResultRes(ExperimentBattleProtoMessage):
    """试炼关卡结算发奖响应（消息号 34012）。"""

    proto_class_name = "ExperimentFightingResultRes"

    #: 错误码（0 = 成功）
    errorCode: int = 0
    #: 关卡通关首通或通关道具奖励
    rewardList: list[GetObjClass] = Field(default_factory=list)
    #: 消耗物品清单（常规关卡通常为空）
    costObjList: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否成功通关发奖。"""
        return self.errorCode == 0
