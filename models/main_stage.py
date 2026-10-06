"""主线关卡（Main Stage：11015/11016、11017/11018、11011/11012、11007/11008）模型与协议定义。

【协议全貌与业务链路】
1. 进度同步：
   - 11011 ``GetLastSectionPacket``（空包）→ 11012 ``GetLastSectionRes``：
     获取玩家当前边界关卡列表（``sectionStateList``）与通关状态（``sectionState=2`` 为已通关，``0`` 为未通关）。
2. 关卡开战：
   - 11015 ``FightingRequestPacket{sectionID, teamID}`` → 11016 ``FightingResponseRes``：
     向服务端发起战斗请求，获取本次战斗的令牌 ``fightToken`` 与奖励预览。
3. 战斗结算：
   - 模拟战斗耗时秒数 ``timeCostSec``（通常为 15~25 秒，禁止 0 秒）。
   - 计算签名令牌：
     ``token = MD5(f"{fightToken}{timeCostSec}{winLose}03120908")``
     （盐值来源于客户端全局字符串字面量池，逆向实测确认）。
   - 组装存证 ``battleServerVerify``：
     普通关卡（`cheatCheck=0`，占 95.7%）存证解压为 ``{"BattleType":0,"SectionId":<sectionID>,"Proofs":[]}``。
   - 11017 ``FightingResultPacket`` → 11018 ``FightingResultRes``：
     上报三星战斗结果（``starCount=[1, 1, 1]``，``winLose=1``）。
4. 小章节宝箱领取：
   - 当小章节（Area）的最后一关（通常为第 6 关，新手 1-1 为第 3 关）满星通关后，小章累计星数达到 18 星（或 9 星）。
   - 发送 11007 ``ReceiveStarRewardPacket{areaID}`` → 11008 ``ReceiveStarRewardRes``：
     领取本小章节的全部星数宝箱。

【安全护栏设计】
- 针对关键校验关卡（`cheatCheck=1`，全主线仅 23 关）：
  严格遵守风控规则，不提供自动绕过开关，遇到必须停止并提示用户在真实游戏内手动通关。
- 绝不主动购买体力：
  推图仅消耗现有体力，体力不足平稳停机，绝不调用买体力协议。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
from typing import Any, ClassVar, Final

from pydantic import Field

from models.activity_stage import SectionStateClass
from models.base import ProtocolModel
from models.daily import GetObjClass
from models.game_packet import ProtoMessage
from models.packet_ids import packet_id
from models.wallet import GetAllItemPacket, GetAllItemRes

_LOGGER: Final[logging.Logger] = logging.getLogger("models.main_stage")

#: 本板块允许发出的请求报文（白名单）
ALLOWED_PACKET_NAMES: Final[tuple[str, ...]] = (
    "GetAllItemPacket",
    "SetUseTeamIDPacket",
    "GetLastSectionPacket",
    "FightingRequestPacket",
    "FightingResultPacket",
    "ReceiveStarRewardPacket",
)

#: ``{类名: 消息号}``（供 tasks/packet_guard.guarded_send 使用）
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in ALLOWED_PACKET_NAMES
}

#: 明确禁发的报文（命中即拒绝）
FORBIDDEN_PACKET_NAMES: Final[tuple[str, ...]] = (
    "BuyEnergyPacket",  # 24005 禁止在推图过程中自动消耗钻石买体力
    "VIPDetailPacket",  # 24011 禁止在推图过程中查询/购买 VIP 体力
    "BuyActivityStagePacket",  # 11027 禁止花钻石买活动关卡次数
)

#: ``{类名: 消息号}``
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in FORBIDDEN_PACKET_NAMES
}

#: ``{消息号: 一句话说明}``
PACKET_PURPOSE: Final[dict[int, str]] = {
    2007: "查询背包与体力（只读，零消耗）",
    5013: "设置/切换当前出战队伍（设定为 PVE1 编队）",
    11011: "获取玩家当前主线最高/边界关卡进度（只读，请求体为空）",
    11015: "向服务端申请进入主线关卡战斗（获得 fightToken）",
    11017: "向服务端上报战斗胜利与三星结果（消耗体力，完成关卡）",
    11007: "领取小章节满星宝箱奖励",
}

#: 主线/常规 PVE 出战队伍编号（对应客户端底层 TeamType.PVE1 = 40）
TEAM_TYPE_PVE1: Final[int] = 40

#: 签名固定盐值（逆向自 GameAssembly.dll 的静态字面量）
FIGHT_VERIFY_SALT: Final[str] = "03120908"


def make_fight_verify_token(fight_token: int, time_cost_sec: int, win_lose: int = 1) -> str:
    """计算 11017 战斗结算的 MD5 签名 token。

    【算法来源（实测反汇编）】
    BattleResultModule.SendFightingResultPacket -> GetFightVerifyData::
        string text = string.Concat(fightToken.ToString(), timeCostSec.ToString(), winLose.ToString(), "03120908");
        return GetMd5HashWithString(text); // 32位小写十六进制 MD5
    """
    raw_str = f"{fight_token}{time_cost_sec}{win_lose}{FIGHT_VERIFY_SALT}"
    return hashlib.md5(raw_str.encode("utf-8")).hexdigest().lower()


def make_battle_server_verify(section_id: int) -> str:
    """生成 cheatCheck=0 关卡的 battleServerVerify 存证字符串。

    【结构依据】
    实测与 BattleCenter.ToServerVerify 保持一致：
    gzipped + base64 的 {"BattleType":0,"SectionId":<sectionID>,"Proofs":[]}。
    """
    payload = json.dumps(
        {"BattleType": 0, "SectionId": int(section_id), "Proofs": []},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.b64encode(gzip.compress(payload)).decode("ascii")


# ---------------------------------------------------------------------------
# 协议数据模型
# ---------------------------------------------------------------------------

class SetUseTeamIDPacket(ProtoMessage):
    """5013 设置/切换当前出战队伍。"""

    proto_class_name = "SetUseTeamIDPacket"

    teamID: int = TEAM_TYPE_PVE1


class SetUseTeamIDRes(ProtoMessage):
    """5014 设置/切换出战队伍响应。"""

    proto_class_name = "SetUseTeamIDRes"

    errorCode: int = 0

    def is_success(self) -> bool:
        return self.errorCode == 0


class FightDataClass(ProtoMessage):
    """进战基础凭证类（嵌套在 FightingResultPacket 内）。"""

    proto_class_name = "FightDataClass"

    token: int = 0
    sectionId: int = 0


class BattleAchiComboClass(ProtoMessage):
    """连击统计（BattleAchiComboClass，tag 5）。"""

    proto_class_name = "BattleAchiComboClass"

    totalComboCount: int = 0
    killBossCount: int = 0
    eleCount: list[bytes] = Field(default_factory=list)
    actorCount: list[bytes] = Field(default_factory=list)


class BattleAchiInterupClass(ProtoMessage):
    """打断统计（BattleAchiInterupClass，tag 6）。"""

    proto_class_name = "BattleAchiInterupClass"

    totalInterupCount: int = 1
    skillCount: list[bytes] = Field(default_factory=list)
    actorCount: list[bytes] = Field(default_factory=list)


class BattleAchiAwakeClass(ProtoMessage):
    """觉醒统计（BattleAchiAwakeClass，tag 7）。"""

    proto_class_name = "BattleAchiAwakeClass"

    totalInterupCount: int = 0
    actorCount: list[bytes] = Field(default_factory=list)


class BattleResultClass(ProtoMessage):
    """战斗结果详情（嵌套在 FightingResultPacket 内）。"""

    proto_class_name = "BattleResultClass"

    teamId: int = TEAM_TYPE_PVE1
    fightingPower: int = 0
    damageContent: str = ""
    damage: int = 0
    yimoLv: int = -1  # 对应客户端 get_NullDataOnSetting() 的默认值 -1
    myTeamRecord: list[bytes] = Field(default_factory=list)
    enemyTeamRecord: list[bytes] = Field(default_factory=list)
    battleServerVerify: str = ""


class FightingRequestPacket(ProtoMessage):
    """11015 请求进入关卡战斗。"""

    proto_class_name = "FightingRequestPacket"

    sectionID: int = 0
    teamID: int = TEAM_TYPE_PVE1


class FightingResponseRes(ProtoMessage):
    """11016 进入战斗响应。"""

    proto_class_name = "FightingResponseRes"

    errorCode: int = 0
    fightToken: int = 0
    rewardList: list[GetObjClass] = Field(default_factory=list)
    fightGlobalEffectClass: bytes | None = None
    mineAssemble: bytes | None = None

    def is_success(self) -> bool:
        return self.errorCode == 0


class FightingResultPacket(ProtoMessage):
    """11017 战斗结果结算上报。"""

    proto_class_name = "FightingResultPacket"
    unpacked_fields: ClassVar[frozenset[str]] = frozenset({"starCount"})

    fightData: FightDataClass | None = None
    winLose: int = 1  # 1 = 胜利
    starCount: list[int] = Field(default_factory=lambda: [1, 1, 1])  # 3星条件（客户端按非 packed repeated varint 编码）
    timeCostSec: int = 15  # 模拟战斗耗时秒数
    ComboRecord: BattleAchiComboClass | None = Field(default_factory=BattleAchiComboClass)
    InterupRecord: BattleAchiInterupClass | None = Field(default_factory=BattleAchiInterupClass)
    AwakeRecord: BattleAchiAwakeClass | None = Field(default_factory=BattleAchiAwakeClass)
    battleResult: BattleResultClass | None = None
    addList: list[bytes] = Field(default_factory=list)
    token: str = ""


class FightingResultRes(ProtoMessage):
    """11018 战斗结算响应。"""

    proto_class_name = "FightingResultRes"

    errorCode: int = 0
    rewardList: list[GetObjClass] = Field(default_factory=list)
    costList: list[GetObjClass] = Field(default_factory=list)
    newSections: list[int] = Field(default_factory=list)
    cgGameRewardList: list[bytes] = Field(default_factory=list)
    BattleResultMissionData: list[bytes] = Field(default_factory=list)
    fullStarRewardList: list[GetObjClass] = Field(default_factory=list)
    mapBonusRewardList: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        return self.errorCode == 0

    @property
    def energy_left(self) -> int | None:
        """消耗结算后最新的体力持有量（通过 costList 顺带回传）。"""
        for get_obj in self.costList:
            for item in get_obj.itemList:
                if item.itemID == 200000002:  # ENERGY_ID
                    return item.itemAmount
        return None


class GetLastSectionPacket(ProtoMessage):
    """11011 获取当前最高关卡边界状态（空包）。"""

    proto_class_name = "GetLastSectionPacket"


class GetLastSectionRes(ProtoMessage):
    """11012 当前最高关卡边界状态响应。"""

    proto_class_name = "GetLastSectionRes"

    receiveStarReward: list[int] = Field(default_factory=list)
    sectionStateList: list[SectionStateClass] = Field(default_factory=list)


class ReceiveStarRewardPacket(ProtoMessage):
    """11007 领取小章节星数宝箱奖励。"""

    proto_class_name = "ReceiveStarRewardPacket"

    areaID: int = 0


class ReceiveStarRewardRes(ProtoMessage):
    """11008 领取小章节星数宝箱响应。"""

    proto_class_name = "ReceiveStarRewardRes"

    errorCode: int = 0
    rewardList: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        return self.errorCode == 0
