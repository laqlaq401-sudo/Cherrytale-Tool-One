"""登录签到（每日循环登入奖励 + 活动登入奖励，消息号 33055/33056）。

【这条链路是什么 —— 2026-10-06 抓包定论】
登录后客户端自动发一个**空包** 33055，服务端把三类"登入即结算"的奖励
**搭在同一个响应里**下发，没有任何独立的"领取"包（33051~33062 段全量扫描
过：除查询外只有通行证的 Buy/Receive）：

======================  ==========================================  ==================
响应字段                 游戏里的叫法                                  入账位置
======================  ==========================================  ==================
``dailyAddList``        每日签到（循环登入奖励 LoginReWardLoop）      本次已自动入账
``newPlayerAddList``    新手 7 日登入（LoginReWardNewPlayer）         本次已自动入账
``activityData[].addList``  活动登入奖励（"七天登入可領取獎勵"）      本次已自动入账
======================  ==========================================  ==================

【证据链（为什么"每日签到"在这个包里）】
- ``captures/登录后自动签到（每日+活动）.saz`` raw/17：33055（0 B）→ 33056，
  其中 ``dailyAddList`` = 高級寶石袋(200707002)×2，与游戏配置表
  ``Cherrytale Asset/TextAsset/LoginReWardLoopData`` 第 11 天的
  ``11|200707002|2`` **逐字节吻合**；
- ``GetLoginRewardRes``(33002) 只有一个 ``days`` 计数字段、不含道具，
  且 33001 只在打开登入奖励 UI 时才发 —— 它不是入账通道；
- 通行证（33057/33058，领奖 33061）是**另一个系统**：4 张卡的标题全是
  「○○通行證」，奖励要显式领取，与"查询即入账"的签到相反。

【重复发送有没有负面影响 —— 没有】
33055 是幂等查询：游戏客户端自己每次登录都发；服务端按日结算一次，
重复查询只会得到空的 addList。所以"空 addList"就是"今日已结算"
（可能在游戏客户端里已经登过）的信号，本项目的钩子据此记账与汇报。

【数量口径（Q-021 纪律）】
- ``dailyAddList``：**GAINED**（与 LoginReWardLoopData 当日配置逐字节吻合，
  "Add"即增量）；
- ``newPlayerAddList`` / ``activityData[].addList``：**UNKNOWN** —— 名字同属
  "Add"家族，但活动侧的数值（如 13/23/33）与 7 天展示表对不上、未经实证，
  文案禁止报数字（见 ``models/reward.py`` 的口径纪律）。

【字段来源与编号依据】
- 字段名与类型：``dump.cs`` 的 ``GetActivityLoginRewards``(416844) /
  ``GetActivityLoginRewardsRes``(416861) / ``ActivityLoginRewardsData``(430712)；
- 字段编号：``models/proto_fields.py``（2026-10-06 重生成收录）；
- 真实字节样本：``protocol_samples/login_reward_samples.py``。
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from models.daily import GetObjClass, ItemClass
from models.game_packet import ProtoMessage

# ---------------------------------------------------------------------------
# 请求 / 响应
# ---------------------------------------------------------------------------
class GetActivityLoginRewards(ProtoMessage):
    """拉取全部登入奖励并触发服务端结算（消息号 33055，**空报文**）。

    .. note::
       类名**没有** ``Packet`` 后缀 —— dump.cs 里就叫这个名字
       （``GetActivityLoginRewardsPacket`` 不在 ePacketFormat_RPS 枚举里，
       见 ``tools/extract_proto_fields.py`` 的收录注释）。
       空包：与 SyncDataPacket(99023) 同款，信封里子包槽位为 0 字节。
    """

    proto_class_name = "GetActivityLoginRewards"


class ActivityLoginRewardsData(ProtoMessage):
    """一条活动登入奖励（"七天登入可領取獎勵"）。

    .. warning::
       ``addList`` 是"本次查询时服务端已自动入账"的记录；它的 ``itemAmount``
       （实测 13/23/33）与 ``rewards`` 七天展示表对不上、语义未实证 ——
       展示时必须用 ``QuantityScope.UNKNOWN``，禁止报数字。
    """

    proto_class_name = "ActivityLoginRewardsData"

    #: 活动 ID（如 607000083）
    sid: int = 0
    #: 活动开始时间（毫秒时间戳）
    startDate: int = 0
    #: 活动结束时间（毫秒时间戳）
    endDate: int = 0
    #: 入口按钮文案（如「間宮麻理繪登入獎勵」）
    buttonTitle: str = ""
    #: 活动标题（与 buttonTitle 同文本）
    title: str = ""
    #: 活动描述（如「七天登入可領取獎勵。」）
    description: str = ""
    #: 背景图资源名（如 ``Login_Reward_BG_01``）
    background: str = ""
    #: 7 天奖励**展示表**（第 1..7 天各是什么；不含领取状态）
    rewards: list[ItemClass] = Field(default_factory=list)
    #: 已领到第几天（实测为 1；语义未经真机二验，只作展示）
    rewardIndex: int = 0
    #: 领取状态（实测为 2；枚举语义未实证，只作展示）
    getStatus: int = 0
    #: 立绘 CG 资源名（如 ``ct087_Cg_Spine``）
    cgAssetID: str = ""
    #: 本次查询时**已自动入账**的物件（口径未实证，见类文档警告）
    addList: list[GetObjClass] = Field(default_factory=list)


class GetActivityLoginRewardsRes(ProtoMessage):
    """登入奖励总响应（消息号 33056）——每日/新手/活动三路都在这里。"""

    proto_class_name = "GetActivityLoginRewardsRes"

    #: 活动登入奖励（每条一个七天活动）
    activityData: list[ActivityLoginRewardsData] = Field(default_factory=list)
    #: **每日签到**（循环登入奖励）本次已自动入账的物件（口径 GAINED）
    dailyAddList: list[GetObjClass] = Field(default_factory=list)
    #: 新手 7 日登入本次已自动入账的物件（新手期结束后恒为空）
    newPlayerAddList: list[GetObjClass] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 发包白名单 / 禁发表（交给 tasks/packet_guard.guarded_send 用）
# ---------------------------------------------------------------------------
#: 登录签到链路允许发出的包：只有这一次空查询。
SIGNIN_ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "GetActivityLoginRewards": 33055,
}

#: 明确禁发：33xxx 段里的**通行证**三件套（拉状态/购买/领取）。
#:
#: 【为什么单独立禁发表】通行证与签到是两个系统（33058 的卡片标题全是
#: 「○○通行證」，奖励要显式领取）。签到功能不需要它们；把 33057 也禁掉
#: 是为了防止将来有人把"拉通行证状态"顺手塞进这条链路 —— 通行证若要做，
#: 必须带着自己的抓包（领取动作 33061 从未被抓包实证）另立链路。
SIGNIN_FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "GetLoginPassPacket": 33057,
    "BuyLoginPassPacket": 33059,
    "ReceiveLoginPassRewardPacket": 33061,
}
