"""日常任务相关报文（消息号 6001~6012）。

【字段来源与编号依据】
- 字段名与类型：``dump.cs`` 的 ``com.auer.game.protobuf`` 命名空间；
- 字段编号：``models/proto_fields.py``（= 字段声明顺序，从 1 开始）。

⚠️ 编号规则**尚未经真实报文验证**，详见 ``models/proto_fields.py`` 开头的说明。

【与 models/game_config.py 的分工 —— 这一点最容易混】

======================  ==========================================  =========================
本文件                    game_config.py                             说明
======================  ==========================================  =========================
``DailyMissionListClass``  ``DailyMission``                          服务端状态 vs 本地配置
``misID``                ``dmID``                                    同一个数字，不同叫法
``completedTimes``       ``required_count``                          做到几次 vs 要求几次
======================  ==========================================  =========================

判断「能不能领奖」必须用**服务端状态**（本文件）：本地配置随时可能过期。

【mistypeID 是什么 —— 一个容易卡住的地方】
领奖报文 ``DMReward`` 需要 ``misID`` + ``mistypeID`` 两个参数，
但服务端返回的任务状态里**只有 misID**，``mistypeID`` 得由界面上下文决定。
它的取值空间实测就在 dump.cs 的 ``DailyMission_UIView.eType`` 里，
本文件把它挪过来成为 :class:`MissionType` ——
它是**协议参数**，不该依赖界面层。
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import Field

from models.game_packet import ProtoMessage


class MissionType(IntEnum):
    """任务类型（``DMReward.mistypeID`` 的取值）。

    来源：dump.cs 的 ``DailyMission_UIView.eType``（TypeDefIndex 14263）。

    .. note::
       数值**不连续**（缺 3、4、7~12），说明它是历史演进的枚举：
       中间那些值对应的功能可能被删掉了。因此取值时不要用
       ``range(len(...))`` 之类的循环，也不要假设「小就是早」。
    """

    #: 界面上的「无选中」，不会出现在协议里
    NONE = -1
    #: 日常任务 → 对应 ``DailyMissionRes.dmInitList``
    DAILY = 0
    #: 主线任务 → 对应 ``mmInitList``
    MAIN = 1
    #: 限时/限定任务 → 对应 ``Dic_LztRestrictMission``（需要先解锁）
    RESTRICT = 2
    #: 公会任务 → 对应 ``allianceInitList``
    ALLIANCE = 5
    #: 日常活跃度宝箱 —— 此时 ``misID`` 传的是 ``dmrID``（939000001 起）
    DAILY_REWARD_BOX = 6
    #: 指引任务 → 对应 ``guideInitList``
    GUIDE_MISSION = 13
    #: 周常任务 → 对应 ``wmInitList``
    WEEKLY = 14
    #: 周常宝箱
    WEEKLY_REWARD_BOX = 15


class DailyMissionListClass(ProtoMessage):
    """服务端返回的单条任务状态（TypeDefIndex 9062）。"""

    proto_class_name = "DailyMissionListClass"

    #: 任务 ID —— 与配置表的 ``dmID`` 是同一个数字；领奖时作为 ``DMReward.misID`` 发出
    misID: int = 0
    #: 已完成次数 —— 与配置表的 ``count`` 比较，判断是否达成
    completedTimes: int = 0
    #: 是否已领奖。实测取值 0/1，但**类型是 int 而不是 bool**，
    #: 所以判断时用 ``== 0`` 而不是 ``not``：万一将来出现其它值（如 2 = 已过期），
    #: 用 ``not`` 会把它当成「未领」，导致重复领取。
    isGotReward: int = 0
    #: 等级限制（低于该等级的任务不显示）
    levelLimit: int = 0

    def can_claim(self, required_count: int) -> bool:
        """是否满足「可以去领奖」的条件。

        :param required_count: 配置表里该任务要求的完成次数（``DailyMission.required_count``）。

        .. note::
           本方法只做**本地判断**，最终以服务端响应为准。
           它的用途是「别去发一个注定失败的请求」，而不是「保证一定能领」。
        """
        return self.completedTimes >= required_count and self.isGotReward == 0


class DailyMission(ProtoMessage):
    """拉取任务数据（消息号 6001）。

    .. note::
       请求体只有一个 ``type`` 列表，实测客户端用它指定要拉哪几组任务；
       传空列表也能拿到数据（服务端按「全都要」处理）。
    """

    proto_class_name = "DailyMission"

    #: 要拉取的任务类型列表（取值见 :class:`MissionType`）
    type: list[int] = Field(default_factory=list)


class DailyMissionRes(ProtoMessage):
    """任务数据响应（消息号 6002）。

    【为什么有些列表只保留 ``bytes``】
       ``amInitList`` / ``unlockMission`` / ``guideInitList`` 这几组
       目前 L1（领奖）与 L2（待办清单）都用不到内部结构。
       与其为了「看起来完整」而给它们建模、并在字段变化时维护，
       不如**原样保留字节**：将来真要解析时，不必重新抓包。

    命名前缀含义（实测）：
      ``dm`` = daily mission（日常）、``mm`` = main mission（主线）、
      ``wm`` = weekly mission（周常）、``am`` = activity mission（活动）。
    """

    proto_class_name = "DailyMissionRes"

    #: 日常任务
    dmInitList: list[DailyMissionListClass] = Field(default_factory=list)
    #: 主线任务
    mmInitList: list[DailyMissionListClass] = Field(default_factory=list)
    #: 活动任务（暂不解析，保留原始字节）
    amInitList: list[bytes] = Field(default_factory=list)
    #: 公会任务（暂不解析）
    allianceInitList: list[bytes] = Field(default_factory=list)
    #: 已领取的**日常活跃宝箱** ID（对应配置表 ``DailyMissionRewardData.dmrID``）
    dmrReceivedList: list[int] = Field(default_factory=list)
    #: 待解锁的限定任务（暂不解析）
    unlockMission: list[bytes] = Field(default_factory=list)
    #: 指引任务（暂不解析）
    guideInitList: list[bytes] = Field(default_factory=list)
    #: 已领取的指引奖励箱（暂不解析）
    guideReceivedList: list[bytes] = Field(default_factory=list)
    #: 周常任务
    wmInitList: list[DailyMissionListClass] = Field(default_factory=list)
    #: 已领取的**周常宝箱** ID
    wmrReceivedList: list[int] = Field(default_factory=list)


class DMReward(ProtoMessage):
    """领取单条任务奖励（消息号 6005）。

    .. warning::
       **这是有副作用的请求**：服务端会真的发奖。
       超时后**绝不能自动重试** —— 服务端可能已经处理成功，
       重试会造成重复领取的尝试甚至触发风控（详见 ``client/session.py`` 的说明）。
    """

    proto_class_name = "DMReward"

    #: 任务 ID（配置表的 ``dmID``）
    misID: int = 0
    #: 任务类型（取值见 :class:`MissionType`）。
    #: ⚠️ 服务端返回的任务状态里没有这个字段，必须由调用方根据「从哪个界面发起的」传入。
    mistypeID: int = 0

    @classmethod
    def for_mission(cls, mis_id: int, mission_type: MissionType) -> "DMReward":
        """构造「领取某条任务奖励」的请求（把类型显式化，避免传错数字）。

        用构造函数而不是直接填字段，是因为 ``mistypeID`` 传错的后果很隐蔽：
        服务端会把请求当成「另一种任务的奖励」，于是要么报错、要么领错东西。
        """
        return cls(misID=mis_id, mistypeID=int(mission_type))



# ---------------------------------------------------------------------------
# 奖励明细
# ---------------------------------------------------------------------------
class ItemClass(ProtoMessage):
    """一件奖励道具（TypeDefIndex 里与 GetObjClass 同组的明细结构）。"""

    proto_class_name = "ItemClass"

    #: 道具实例编号（同一道具 ID 的多个实例会有不同的 itemSid）
    itemSid: int = 0
    #: 道具 ID（与配置表、以及 ``RPS_Const`` 里的资源 ID 是同一套编号）
    itemID: int = 0
    #: 数量
    itemAmount: int = 0


class GetObjClass(ProtoMessage):
    """「获得了什么」的统一结构，出现在各种领奖响应里。

    【为什么有些列表用 ``bytes``】
      装备 / 宝石 / 角色 / 卡牌这些明细的字段很多，而 L1 只需要知道
      「领到了哪些道具」（``itemList``）。未建模的列表**保留原始字节**，
      将来要展示详细信息时再补类型，不必重新抓包。
    """

    proto_class_name = "GetObjClass"

    #: 获得的道具（最常用的一项）
    itemList: list[ItemClass] = Field(default_factory=list)
    #: 获得的装备（暂不解析）
    equipList: list[bytes] = Field(default_factory=list)
    #: 获得的宝石（暂不解析）
    gemList: list[bytes] = Field(default_factory=list)
    #: 获得的角色（暂不解析）
    roleList: list[bytes] = Field(default_factory=list)
    #: 获得的图鉴碎片（暂不解析）
    hcgPuzzleList: list[bytes] = Field(default_factory=list)
    #: 重复获得的角色 ID（转换为碎片等资源，服务端会另放在 itemList 里）
    repeatRoleID: list[int] = Field(default_factory=list)
    #: 获得的镜卡（暂不解析）
    mirrorCardList: list[bytes] = Field(default_factory=list)
    #: 重复获得的镜卡 ID
    repeatMirrorCardID: list[int] = Field(default_factory=list)
    #: 获得的星图（暂不解析，只保留 ID）
    starCharList: list[int] = Field(default_factory=list)
    #: 重复获得的星图 ID
    repeatStarChartID: list[int] = Field(default_factory=list)
    #: 获得的角色皮肤 ID
    roleSkinList: list[int] = Field(default_factory=list)
    #: 重复获得的角色皮肤 ID
    repeatRoleSkinID: list[int] = Field(default_factory=list)

    def item_summary(self) -> str:
        """一行文字说明拿到了什么（**仅开发者日志（DEBUG）用**）。

        :return: 形如 ``道具 200000050×500、200000001×30``；没有任何道具时返回 ``<无道具>``。

        .. warning::
           ``GetObjClass`` 出现在**多种**响应里，而 ``itemAmount`` 的语义随接口而异
           （6006 / 11010 / 37020 已证实是"操作后持有量"，见 Q-021）。
           本方法只是"把数字原样打出来给开发者看"，**不表态口径** ——
           所以禁止把它拼进用户可见文案。要展示请走
           :func:`models.reward.reward_phrase` 并显式声明 ``QuantityScope``。
        """
        if not self.itemList:
            return "<无道具>"
        parts = [f"{item.itemID}×{item.itemAmount}" for item in self.itemList]
        return "道具 " + "、".join(parts)


#: 业务成功时的 ``errorCode`` 取值。
#:
#: TODO(protocol): 目前沿用最常见的 0，**尚未经实测确认**。
#:     确认方式：抓一个成功的领奖响应看 ``errorCode`` 是多少。
#:     在确认之前，不要用它做「一定成功」的判断 —— 只用来区分
#:     「服务端明确报错」与「服务端没报错」两件事。
SUCCESS_ERROR_CODE: int = 0


class DMRewardRes(ProtoMessage):
    """领取任务奖励的响应（消息号 6006）。"""

    proto_class_name = "DMRewardRes"

    #: 业务错误码（0 = 成功，见 :data:`SUCCESS_ERROR_CODE` 的说明）
    errorCode: int = 0
    #: 本次获得的东西
    rewardList: list[GetObjClass] = Field(default_factory=list)
    #: 领奖后服务端回传的任务最新状态（**用它更新本地状态，别自己 +1**）
    missionList: list[DailyMissionListClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否业务成功。

        .. note::
           这里的「成功」只看 ``errorCode``。真实业务码尚未确认，
           所以判断失败时给出的信息会包含原始错误码，便于你对照服务端日志。
        """
        return self.errorCode == SUCCESS_ERROR_CODE


class DMActReward(ProtoMessage):
    """领取日常活跃度宝箱（消息号 6003）。

    .. warning::
       **有副作用**：会真的发奖，超时不要自动重试。

    .. note::
       ``actRewardID`` 传的是配置表 ``DailyMissionRewardData`` 里的 **dmrID**
       （实测是 939000001~939000005），**不是** ``requiredNum``（1/3/5/7/9 那个活跃点阈值）。
       两者搞反是这类接口最常见的错误，而服务端只会回一个参数错误。
    """

    proto_class_name = "DMActReward"

    #: 宝箱 ID（配置表 ``DailyMissionRewardData.dmrID``）
    actRewardID: int = 0


class DMActRewardRes(ProtoMessage):
    """领取活跃宝箱的响应（消息号 6004）。"""

    proto_class_name = "DMActRewardRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: 本次获得的东西
    rewardList: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否业务成功（判断依据见 :data:`SUCCESS_ERROR_CODE`）。"""
        return self.errorCode == SUCCESS_ERROR_CODE

