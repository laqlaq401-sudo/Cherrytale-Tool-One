"""工会（Alliance）报文 —— 签到、个人/团体挖矿、金币捐献（消息号 21001–21062 段）。

【游戏内命名】
"工会"在本游戏协议与客户端代码里叫 **Alliance**（没有 guild/union 等变体）；
消息号集中在 21001–21062（奇数=请求、偶数=响应，见 ``models/packet_ids.py:301``）。

【本模块覆盖的三条链路】
=============================================================  ===============================
签到（Bingo 面板）                                              21055 → 21057
   21055 ``AllianceBingoHomePacket``（空）→ ``AllianceBingoHomeRes``   取 bingoData（格子清单）
   21057 ``AllianceBingoSignInPacket{pos}`` → ``AllianceBingoSignInRes``  在选中的格子上签到
挖矿（新版 NewPit，个人 + 团体同源）                             21039 → 21047 / 21043 / 21045 / 21049
   21039 ``AllianceNewPitPacket{teachingID}`` → ``AllianceNewPitRes``   个人/团体矿点状态总表
   21047 ``AllianceNewPitOneKeyStartPacket``                            个人矿一键开矿
   21043 ``AllianceNewPitSetRolePacket``                                团体矿进出槽位
   21045 ``AllianceNewPitStartPacket``                                  开矿（团体 / 指定角色）
   21049 ``AllianceNewPitRewardPacket{pitSid}``                         收已完成矿的奖励
捐献                                                            21009 → 21017
   21009 ``AllianceHomePacket``（空）→ ``AllianceHomeRes``              是否入会 + 捐献计数
   21017 ``DonatePacket{actionType}`` → ``DonateRes``                   档位捐献
=============================================================  ===============================

【捐献档位：金币档是唯一可构造路径（硬保证）】
``DonatePacket.actionType`` 的取值 = 配置表 ``AllianceDonateData`` 行首的
``donateID`` —— 这不是猜测：dump.cs 里 ``DonateReq..ctor(AllianceDonateData)``
的反汇编第一条有效指令就是 ``packet.actionType = donateSetting.donateID``。
三档配置（``Cherrytale Asset/TextAsset/AllianceDonateData``）：

=====================  ===========  ==========  =========
档位                   donateID     消耗         贡献
=====================  ===========  ==========  =========
初級（金币）           693000001    5000 金币    10
中級（钻石）           693000002    50 钻石      50
高級（钻石）           693000003    200 钻石     200
=====================  ===========  ==========  =========

本模块只暴露 :meth:`DonatePacket.for_gold_tier`（``actionType`` 恒为
``GOLD_TIER_DONATE_ID``）；钻石档（中級/高級）**没有任何可构造路径** ——
这是"不打通钻石捐献通道"的模型层硬保证，另有测试钉死。

【⚠️ 尚未实测确认】
1. **成功码**：``AllianceNetWorkModule.eResErrCode`` 枚举是 ``None=0, Success=1,
   SuccessApply=2``，错误从 3 起 —— 与其它系统"0=成功"不同。本项目按
   ``{0, 1}`` 判成功并**总是同时校验响应内容**（如捐献看 ``costList``、
   开矿看 ``endTime``），首次真机返回后定案（见 notes/open_questions.md）。
2. ``AllianceBingoSignInPacket.pos`` 的合法范围与"格子是否已签"的判定：
   客户端是按玩家点击的格子发（``OnClick_SignIn(int iPos)``），格子占用情况
   从 21055 的 ``bingoData``（``iconData`` 是否有值）读 —— 首次真机返回后核对。
3. 一键开矿 21047 的 ``oneKeyPersonalList`` 元素里 ``roleSidList`` 是否允许
   空（客户端会自己算满足条件的角色再发；服务端是否接受空列表 = 让服务端
   自选，需真机验证）。→ **已解（2026-10-03 抓包）**：空列表不可用，每坑须填
   ``roleMaxCondition`` 个角色，见 ``tasks/alliance_mining.py``。
4. ``AllianceNewPitRes.personalLimit`` / ``teamLimit`` 都是**今日次数上限**
   （客户端 ``m_personalLimitCount`` / ``m_teamLimitCount``）。
   10.3 抓包实测 ``personalLimit=8``、``teamLimit=4``。
   ⚠️ 白天那一版把 ``teamLimit`` 判成"按坑的限制、不能当配额用"是**误读**，
   据此删掉闸门后真机连吃 15 个 ``-8`` —— 详见
   :attr:`AllianceNewPitRes.teamLimit` 的说明（**别再删那条闸门**）。
"""

from __future__ import annotations

from typing import ClassVar, Final

from pydantic import Field

from models.daily import GetObjClass
from models.game_packet import ProtoMessage

# ---------------------------------------------------------------------------
# 错误码（来自 dump.cs 的 AllianceNetWorkModule.eResErrCode 枚举，值未实测）
# ---------------------------------------------------------------------------
#: 已知的错误码 → 可读名（只收录任务层会用到的；完整枚举见 dump.cs:536953）
ERROR_CODE_NAMES: Final[dict[int, str]] = {
    0: "None（未填）",
    1: "Success（成功）",
    2: "SuccessApply（申请成功）",
    7: "NonAchieve_CoinRequest（金币不足）",
    10: "NonAchieve_EnoughDonateCount（今日捐献次数已用完）",
    19: "HaveNotBeen_ExistedMyAlliance（未加入工会）",
    31: "HaveBeen_Receive（已经领取过）",
    34: "Re_Request_MiningPit（重复开矿请求）",
    35: "Error_On_Countdown_Time（冷却中）",
}

#: 按"成功族"判定的取值集合（⚠️ 待真机定案，见模块 docstring）
SUCCESS_ERROR_CODES: Final[frozenset[int]] = frozenset({0, 1})

#: 未加入工会的错误码（签到 / 挖矿 / 捐献通用跳过信号；dump.cs 枚举
#: ``HaveNotBeen_ExistedMyAlliance``）
ERR_NOT_IN_ALLIANCE: Final[int] = 19


def is_not_in_alliance(error_code: int) -> bool:
    """判断错误码是否表达"未加入工会"。

    【2026-10-02 真机实测修正】
    工会主页 21009 对未入会账号实测回 ``errorCode=-1``，且 ``myAlliance``
    全字段为 -1 哨兵（allianceSid=-1 等）—— dump.cs 里的 19 在这条链路上
    没有出现。签到（21055）/ 挖矿（21039）面板从 dump.cs 推的是 19，
    真机行为尚未验证，因此两个码都按"未入会"处理。

    .. warning::
       ``-1`` 在其他错误码表里没有名字，理论上也可能是别的失败；但在
       这三个工会任务的分支语境里，把它当"未入会"跳过（``ok=True``）
       比"任务失败"更接近真实语义，代价可控。
    """
    return error_code == ERR_NOT_IN_ALLIANCE or error_code == -1


def error_code_name(code: int) -> str:
    """把错误码翻译成可读名（未知码原样返回数字，不编造语义）。"""
    return ERROR_CODE_NAMES.get(code, f"未知错误码 {code}")


# ---------------------------------------------------------------------------
# 白名单 / 禁发清单（guarded_send 三道检查的数据源，按链路分组）
# ---------------------------------------------------------------------------
#: 工会主页（判是否入会、读捐献计数；只读）
HOME_ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "AllianceHomePacket": 21009,
}
#: 签到链路：21055 取格子 → 21057 签到 → 21061 领连线奖励。
#: 【★ 2026-10-03】21061 由"不做"改为接入：10.3 抓包
#: （``captures/工会签到+个人挖矿+团队挖矿占坑位全流程.saz``）证明签到完成后
#: 立即发空包 21061 即可当场领到连线奖励（旧假设"未领的次日转邮箱"不成立）。
SIGN_IN_ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "AllianceBingoHomePacket": 21055,
    "AllianceBingoSignInPacket": 21057,
    "AllianceBingoRewardPacket": 21061,
}
#: 挖矿链路：21039 状态总表 + 开矿三件套 + 收矿；21041 刷新 / 21051 邀请**不做**
MINING_ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "AllianceNewPitPacket": 21039,
    "AllianceNewPitOneKeyStartPacket": 21047,
    "AllianceNewPitSetRolePacket": 21043,
    "AllianceNewPitStartPacket": 21045,
    "AllianceNewPitRewardPacket": 21049,
}
#: 捐献链路：21009 读状态 → 21017 捐献。**21017 的 actionType 只允许金币档**
#: （钻石档在模型层就没有构造路径，见 DonatePacket）。
DONATE_ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "AllianceHomePacket": 21009,
    "DonatePacket": 21017,
}
#: 名册（挖矿挑角色用；定义在 models/role.py，这里引用消息号做用途说明）
ROLE_ROSTER_ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "GetAllRolePacket": 5011,
}

#: 工会段内明确**不做**的消耗/社交类报文（防越界发包的最后一道声明）
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "CreateAlliancePacket": 21001,  # 建会
    "AllianceOperationPacket": 21005,  # 退出/踢人等操作
    "AllianceEditPacket": 21011,  # 改公告/设置
    "MemberAuditPacket": 21021,  # 审批
    "MemberEditPacket": 21023,  # 职位编辑
    "AlliacneBoxPacket": 21027,  # 宝箱
    "ReceiveBoxPacket": 21029,  # 领宝箱
    "PitEditPacket": 21033,  # 旧版矿点编辑
    "PitInvitePacket": 21035,  # 旧版矿点邀请
    "SendBoxPacket": 21037,  # 送宝箱
    "AllianceNewPitRefreshPacket": 21041,  # 刷新矿点（消耗类，语义未确认）
    "AllianceNewPitInvitePacket": 21051,  # 组队邀请
    "AllianceDonateRewardsPacket": 21053,  # 捐献进度奖励（用户决策：不做）
    "AllianceBingoRecordPacket": 21059,  # 签到记录（只读但非必需）
}

PACKET_PURPOSE: Final[dict[int, str]] = {
    21009: "工会主页（只读：是否入会 + 捐献计数）",
    21017: "工会捐献请求（金币档）",
    21039: "新版挖矿状态总表（个人 + 团体）",
    21043: "团体矿槽位进出",
    21045: "开矿请求",
    21047: "个人矿一键开矿",
    21049: "收已完成矿的奖励",
    21055: "签到 Bingo 面板（只读：取格子清单）",
    21057: "工会签到请求",
    21061: "领取 Bingo 连线奖励（纯领取，零消耗；10.3 抓包实证可当场领）",
    5011: "角色名册（只读：挖矿挑角色用）",
}

# ---------------------------------------------------------------------------
# 捐献档位常量
# ---------------------------------------------------------------------------
#: 金币档的 ``donateID``（= ``DonatePacket.actionType``，证据见模块 docstring）
GOLD_TIER_DONATE_ID: Final[int] = 693000001
#: 金币档单次消耗（``AllianceDonateData`` 表 693000001 行 costAmount）
GOLD_TIER_COST: Final[int] = 5000
#: 金币档单次产出贡献值（同表 creditAmount）
GOLD_TIER_CREDIT: Final[int] = 10
#: 钻石档 donateID —— **仅用于文档与测试断言"永不发出"**，模型不提供构造路径
DIAMOND_TIER_DONATE_IDS: Final[tuple[int, ...]] = (693000002, 693000003)


# ---------------------------------------------------------------------------
# 工会主页
# ---------------------------------------------------------------------------
class AllianceHomePacket(ProtoMessage):
    """拉取工会主页（消息号 21009，空包，只读）。"""

    proto_class_name = "AllianceHomePacket"


class AllianceClass(ProtoMessage):
    """一个工会（``AllianceHomeRes.myAlliance`` 的类型）。

    .. note::
       ``leaderIcon`` 是嵌套的 ``IconClass``，本模块用不到，保留原始字节。
    """

    proto_class_name = "AllianceClass"

    #: 工会 ID
    allianceSid: int = 0
    #: 工会名
    name: str = ""
    #: 工会图标 ID
    icon: int = 0
    #: 工会等级
    level: int = 0
    #: 工会经验
    exp: int = 0
    #: 会长玩家 ID
    leaderPlayerID: int = 0
    #: 会长头像（未建模，保留原始字节）
    leaderIcon: bytes = b""
    #: 公告
    bulletin: str = ""
    #: 成员数
    memberAmount: int = 0
    #: 申请设置
    apply_setting: int = 0
    #: 入会等级门槛
    lv_limit: int = 0
    #: 累计捐献值
    allDonateAmount: int = 0
    #: 今日捐献值
    dailyDonateAmount: int = 0
    #: 工会斧头数（捐献产出物之一，语义待实测）
    allianceAxe: int = 0
    #: 工会称号
    allianceTitle: int = 0


class AllianceHomeRes(ProtoMessage):
    """工会主页响应（消息号 21010）。

    :ivar myAlliance: 未入会时缺省（解码后为 ``None``）—— 判"是否入会"的主依据。
    """

    proto_class_name = "AllianceHomeRes"

    #: 业务错误码（⚠️ 成功族取值待定案，见 ERROR_CODE_NAMES）
    errorCode: int = 0
    #: 自己的工会（未入会时服务端不填）
    myAlliance: AllianceClass | None = None
    #: 今日已捐献次数
    donateCount: int = 0
    #: 是否重置（语义待实测）
    isReset: int = 0
    #: 工会排名
    alliance_ranking: int = 0
    #: 职位列表
    position: list[int] = Field(default_factory=list)
    #: 公告文本
    bulletin: str = ""
    #: 是否有变更
    isChange: int = 0
    #: 每日刷新时间（Unix 秒）
    dailyRefreshTime: int = 0
    #: 已领取的捐献进度奖励 ID（本模块不领，见 FORBIDDEN_PACKET_IDS）
    receivedDonateRewardID: list[int] = Field(default_factory=list)
    #: 点赞人数上限（语义待实测）
    praisePeopleMax: int = 0
    #: 签到面板红点（!=0 表示今天还能签）
    bingoRedDot: int = 0
    #: 签到面板下次重置时间（Unix 秒）
    bingoNextResetTime: int = 0
    #: 湖之女神红点（另一玩法，本项目不做）
    lakeGoddessRedDot: int = 0

    def is_success(self) -> bool:
        """是否业务成功（成功族取值待真机定案，见模块 docstring 第 1 条）。"""
        return self.errorCode in SUCCESS_ERROR_CODES

    def in_alliance(self) -> bool:
        """是否已在工会中（``myAlliance`` 有值即算；本地预判，服务端为准）。"""
        return self.myAlliance is not None


# ---------------------------------------------------------------------------
# 捐献
# ---------------------------------------------------------------------------
class DonatePacket(ProtoMessage):
    """工会捐献请求（消息号 21017）。

    .. warning::
       **唯一合法的构造方式是** :meth:`for_gold_tier`：``actionType`` 直接决定
       服务端扣金币还是扣钻石，所以模型不提供自由设 ``actionType`` 的公开
       途径之外的档位 —— 钻石捐献通道（中級/高級）在本项目**不打通**。

    :param actionType: 档位 ID（= ``AllianceDonateData.donateID``，
        见模块 docstring 的反汇编证据）。
    """

    proto_class_name = "DonatePacket"

    #: 档位 ID —— 外部代码请用 :meth:`for_gold_tier`，不要直接改这个字段
    actionType: int = GOLD_TIER_DONATE_ID

    @classmethod
    def for_gold_tier(cls) -> "DonatePacket":
        """构造**金币档**捐献请求（5000 金币 → 10 贡献）。"""
        return cls(actionType=GOLD_TIER_DONATE_ID)


class DonateRes(ProtoMessage):
    """捐献响应（消息号 21018）。"""

    proto_class_name = "DonateRes"

    #: 业务错误码
    errorCode: int = 0
    #: 本次获得的东西（贡献值不在道具里，gotList 是附带的其它产出）
    gotList: list[GetObjClass] = Field(default_factory=list)
    #: 今日已捐献次数（捐完后的最新值）
    donateCount: int = 0
    #: 工会当前经验（捐完后的最新值）
    nowExp: int = 0
    #: 工会当前等级
    nowLv: int = 0
    #: 本次扣除的道具（含金币 —— 对账依据）
    costList: list[GetObjClass] = Field(default_factory=list)
    #: 工会累计捐献值
    allDonateAmount: int = 0
    #: 今日累计捐献值
    dailyDonateAmount: int = 0
    #: 工会斧头数（捐献产出物之一）
    allianceAxe: int = 0

    def is_success(self) -> bool:
        """是否业务成功。"""
        return self.errorCode in SUCCESS_ERROR_CODES

    def gold_spent(self, gold_item_id: int) -> int:
        """``costList`` 里某个道具的 ``itemAmount`` 之和。

        .. warning::
           **数量口径尚未实证**。21017 的 ``costList`` 与 6006 / 11010 / 37020
           同源（都是 ``GetObjClass``），而那些已被证实回的是"**操作后持有量**"
           （Q-021）—— 也就是说本方法返回的**很可能不是本次扣除量**。

           因此**禁止**把返回值当"消耗"写进用户可见文案（把 5 次捐献的余额
           累加会得到荒谬的总额）。需要展示消耗量时，用本地配置表的单价推算，
           例如 ``tasks/alliance_donate.py`` 的 ``GOLD_TIER_COST * 成功次数``。
        """
        total = 0
        for obj in self.costList:
            for item in obj.itemList:
                if int(item.itemID) == gold_item_id:
                    total += int(item.itemAmount)
        return total


# ---------------------------------------------------------------------------
# 签到（Bingo 面板）
# ---------------------------------------------------------------------------
class GeneralRewardInfoClass(ProtoMessage):
    """一条进度型奖励（签到 Bingo 奖励进度；本模块只读不领）。"""

    proto_class_name = "GeneralRewardInfoClass"

    #: 奖励 ID
    rewardID: int = 0
    #: 已完成次数
    completedTimes: int = 0
    #: 是否已领
    isGotReward: int = 0
    #: 等级门槛
    levelLimit: int = 0


class AllianceBingoClass(ProtoMessage):
    """签到 Bingo 面板上的一个格子。

    【"格子被签了"怎么判】
    ``iconData`` 是占这个格子的玩家头像信息：有值 = 已被签。
    这是客户端 ``IsSignIn`` / ``IsFinishSignIn`` 谓词的判法
    （反汇编：读 ``iconData`` 内的玩家 ID 与自己比对），待真机核对。
    """

    proto_class_name = "AllianceBingoClass"

    #: 签到行 ID（对应配置表 ``AllianceBingoSignInData.allianceSignInID``）
    SignInID: int = 0
    #: 格子位置（玩家点哪个格子就发哪个 pos）
    pos: int = 0
    #: 占位玩家头像（未建模，保留原始字节；**有值 = 该格已被签**）
    iconData: bytes = b""
    #: 是否构成连线（Bingo 判定，服务端算）
    isBingo: bool = False


class AllianceBingoHomePacket(ProtoMessage):
    """拉取签到 Bingo 面板（消息号 21055，空包，只读）。"""

    proto_class_name = "AllianceBingoHomePacket"


class AllianceBingoHomeRes(ProtoMessage):
    """签到面板响应（消息号 21056）。

    :ivar bingoData: 全部格子的占用情况 —— "挑一个没被签的格子"的数据来源。
    """

    proto_class_name = "AllianceBingoHomeRes"

    #: 业务错误码
    errorCode: int = 0
    #: 格子清单
    bingoData: list[AllianceBingoClass] = Field(default_factory=list)
    #: 签到次数进度奖励（本模块不领，仅随响应解出）
    bingoRewardData: list[GeneralRewardInfoClass] = Field(default_factory=list)
    #: 面板下次重置时间（Unix 秒）
    bingoNextResetTime: int = 0

    def is_success(self) -> bool:
        """是否业务成功。"""
        return self.errorCode in SUCCESS_ERROR_CODES

    def next_sign_position(self) -> int | None:
        """推断下一个可签的格子位置；推断不出时返回 ``None``。

        【★ 2026-10-03 按 10.3 抓包重写（旧实现是错的）】
        21056 的 ``bingoData`` 只包含**已被占**的格子 —— 抓包里 10 条记录
        全部带头像字节（pos 0~7、13、19），未签的格子根本不在响应里。
        旧实现"取 ``iconData`` 为空的格子"永远返回空列表，任务因此恒报
        "都已签过"、从未真正签过一次。

        真实客户端在上述占用状态下签了 ``pos=25``（服务端 errorCode=0 接受），
        证明**未被占用的位置可以自由选择**。这里取 ``max(已占 pos) + 1``
        （空面板取 0）：保证不与已占位置重复，也必然落在已验证可行的
        编号范围内。棋盘全集（几行几列）没有可靠的静态来源，若服务端
        对某个位置回错误码，任务会原样上报 —— 那正是补齐认知的途径
        （见 notes/open_questions.md 的 pos 全集条目）。
        """
        occupied = [cell.pos for cell in self.bingoData]
        if not occupied:
            return 0
        return max(occupied) + 1

    def all_signed(self) -> bool:
        """是否全部格子都已签。

        .. warning::
           本地**无法**得知棋盘全集（``bingoData`` 只含已占格子，见
           :meth:`next_sign_position`），所以这个谓词恒为 ``False``；
           "今天还能不能签"只能由服务端裁决（21057 的错误码）。
        """
        return False


class AllianceBingoSignInPacket(ProtoMessage):
    """工会签到请求（消息号 21057）。

    :param pos: 要签的格子位置，**必须取自** 21055 响应的
        :meth:`AllianceBingoHomeRes.unsigned_positions` —— 玩家在真实客户端里
        也是"点一个格子"，这里照搬同一语义，不发明位置。

    .. warning::
       有副作用（每日一次）：成功后当天不能再签；**不要自动重试**。
    """

    proto_class_name = "AllianceBingoSignInPacket"

    #: 要签的格子位置
    pos: int = 0

    @classmethod
    def at_position(cls, pos: int) -> "AllianceBingoSignInPacket":
        """按格子位置构造签到请求（与直接赋值等价，为可读性保留）。"""
        return cls(pos=pos)


class AllianceBingoSignInRes(ProtoMessage):
    """签到响应（消息号 21058）。"""

    proto_class_name = "AllianceBingoSignInRes"

    #: 业务错误码
    errorCode: int = 0
    #: 签完后的格子清单（本次签的格子 ``iconData`` 应已有值）
    bingoData: list[AllianceBingoClass] = Field(default_factory=list)
    #: 签到次数进度奖励
    bingoRewardData: list[GeneralRewardInfoClass] = Field(default_factory=list)
    #: 本次签到直接获得的道具（配置表 rewardType=1 的格子给工会代币等）
    getObjClass: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否业务成功。"""
        return self.errorCode in SUCCESS_ERROR_CODES


class AllianceBingoRewardPacket(ProtoMessage):
    """领取 Bingo 连线奖励（消息号 21061）。空请求体。

    【★ 2026-10-03 接入依据】10.3 抓包：签到（21057）成功后，真实客户端
    立即发这个空包，服务端当场发连线奖励（金币）—— 旧决策"不领、等次日
    转邮箱"与实测不符。
    """

    proto_class_name = "AllianceBingoRewardPacket"


class AllianceBingoRewardRes(ProtoMessage):
    """Bingo 连线奖励响应（消息号 21062）。

    字段定义来自 dump.cs ``AllianceBingoRewardRes``（TypeDefIndex 8446），
    与 10.3 抓包逐字段核对一致。
    """

    proto_class_name = "AllianceBingoRewardRes"

    #: 业务错误码
    errorCode: int = 0
    #: 本次领到的奖励
    getObjClass: list[GetObjClass] = Field(default_factory=list)
    #: 领奖后的格子清单
    bingoData: list[AllianceBingoClass] = Field(default_factory=list)
    #: 连线进度奖励状态
    bingoRewardData: list[GeneralRewardInfoClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否业务成功。"""
        return self.errorCode in SUCCESS_ERROR_CODES


# ---------------------------------------------------------------------------
# 挖矿（新版 NewPit）
# ---------------------------------------------------------------------------
class AlliancePersonalPitClass(ProtoMessage):
    """一个**个人**矿点（``AllianceNewPitRes.personalList`` 的元素）。

    【状态怎么读】（★ 2026-10-03 按 10.3 抓包实测修订；旧表"0 = 空闲"有误）
    ============  =====================================================
    空闲            ``endTime == -1``（抓包里空闲坑全是 -1；``<= 0`` 兜底）
    挖掘中          ``endTime > 当前毫秒时间戳``
    已完成可收      ``0 < endTime <= 当前毫秒时间戳`` 且 ``isReceived == 0``
    已收            ``isReceived != 0``
    ============  =====================================================

    【⚠️ 时间单位是毫秒】抓包里开矿后 ``endTime`` 为 13 位数（毫秒），
    旧注释"Unix 秒"是错的 —— 旧任务拿 ``time.time()``（秒）去比，
    ``needs_harvest`` 永远不成立，收矿从未触发过。
    """

    proto_class_name = "AlliancePersonalPitClass"

    #: ``roleSidList`` 用**非 packed**编码（每个角色独立带 tag）：
    #: 10.3 抓包（21047 一键开矿）里客户端发的是连续 ``18 <varint>``，
    #: 人数与模板表 ``roleMaxCondition`` 相等 —— 见
    #: ``tasks.alliance_mining`` 的分配函数与 tests/test_capture_1003_regression.py。
    unpacked_fields: ClassVar[frozenset[str]] = frozenset({"roleSidList"})

    #: 矿点实例 ID（收矿 / 开矿都以它为准）
    sid: int = 0
    #: 矿点模板 ID（对应配置表 ``AlliancePersonalPitData.personalPitID``）
    id: int = 0
    #: 已指派的角色实例 ID（一键开矿 21047 请求里必须**非空**，
    #: 人数 = 模板表 ``roleMaxCondition`` —— 10.3 抓包实证，见
    #: ``tasks.alliance_mining``）
    roleSidList: list[int] = Field(default_factory=list)
    #: 挖矿结束时间（Unix **毫秒**；-1 = 未开矿）
    endTime: int = 0
    #: 奖励是否已收
    isReceived: int = 0

    def is_idle(self) -> bool:
        """是否空闲（未开矿；抓包实测空闲坑 ``endTime == -1``）。"""
        return self.endTime <= 0

    def is_mining(self, now: int) -> bool:
        """是否挖掘中（``now`` 为当前 Unix **毫秒**时间戳）。"""
        return self.endTime > now

    def needs_harvest(self, now: int) -> bool:
        """是否已完成且奖励未收（``now`` 为当前 Unix **毫秒**时间戳）。"""
        return 0 < self.endTime <= now and self.isReceived == 0


#: 团体矿槽位**无人认领**时 ``memberPid`` 的哨兵值。
#:
#: 【★ 2026-10-05 抓包实证（此前记的 "0" 是错的）】
#: 10.3 抓包（``captures/工会签到+个人挖矿+团队挖矿占坑位全流程.saz`` 的
#: raw/23 = 21040 响应）里 18 个团体矿点共 68 个槽位，``memberPid`` 取值只有
#: 两类：6 个队友的真实 playerID（共 61 个）+ **-1**（共 7 个）。7 个 -1
#: 全部落在 ``endTime == -1`` 的未开矿坑位里 —— 即"客户端显示 ＋ 号、可以占"
#: 的槽位。
#:
#: **闭环佐证**：raw/84 占了 ``(pitSid=205294527, teamPitIndex=2)``、
#: raw/86 占了 ``(pitSid=205294519, teamPitIndex=1)`` —— 这两个槽位在 raw/23
#: 的状态里恰好都是 ``memberPid == -1``；占坑成功后它们变成 ``10000006``（自己）。
#: 也就是说"点 ＋ 号"在协议上 = 对 ``memberPid == -1`` 的槽位发 21043。
#:
#: **为什么先前会以为空槽位"根本没有 memberPid"**：槽位记录里 ``member``
#: 字段并非缺失，而是一个 306 B 的"默认头像模板"（已占槽位只有 153~165 B），
#: 其 ``IconClass.playerID`` 同样是 -1。整条记录是哨兵填充，不是空缺。
#:
#: **为什么判定写成 ``<= 0`` 而不是 ``== -1``**：哨兵约定只要"非正数"这一
#: 语义就够；万一服务端版本改用 0，两种都能覆盖，而 ``> 0`` 判"已占"在两种
#: 约定下都成立。
NO_MEMBER_PID: Final[int] = -1


class AllianceTeamPitDetailClass(ProtoMessage):
    """团体矿的一个**槽位**（``AllianceTeamPitClass.detaliClassList`` 的元素）。

    :ivar needRoleInfoID: 这个槽位要求的角色 **RoleInfoID（133xxxxxx 段）**。
        ⚠️ **不能拿它跟名册里的 ``RoleClass.roleID`` 直接比** —— 后者是
        RoleMainID（130xxxxxx），是两个不同的 ID 空间。必须先换算，见
        :meth:`models.game_config.RoleIdMap.matches`（这也是 2026-10-05
        "团体矿永远报『名册中没有可用的匹配』"的根因）。
    :ivar memberPid: 认领这个槽位的成员玩家 ID；**``-1`` = 待认领（可占）**，
        见 :data:`NO_MEMBER_PID` 的实证说明。判空请用 :meth:`is_empty`。
    """

    proto_class_name = "AllianceTeamPitDetailClass"

    #: 这个槽位要求的角色 **RoleInfoID**（133xxxxxx 段；**不是** RoleMainID，
    #: 与名册 ``roleID`` 比对前必须经 ``RoleIdMap`` 换算）
    needRoleInfoID: int = 0
    #: 这个槽位要求的角色等级（与名册 ``RoleClass.roleLV`` 直接比即可）
    needRoleLv: int = 0
    #: 认领者的头像信息（未建模，保留原始字节；空槽位是 306 B 的哨兵模板）
    member: bytes = b""
    #: 认领者的玩家 ID（``-1`` = 无人认领，见 :data:`NO_MEMBER_PID`）
    memberPid: int = 0

    def is_empty(self) -> bool:
        """该槽位是否**无人认领**（即客户端会显示 ＋ 号、可以占）。

        【★ 2026-10-05：这是"团体矿永远显示没有可占的坑位"的根因所在】
        旧判据是 ``not memberPid``（等价 ``== 0``），而哨兵值是 ``-1``
        （truthy）→ 恒判"没人占的槽位不存在" → 任务恒无候选。详见
        :data:`NO_MEMBER_PID`。
        """
        return self.memberPid <= 0


class AllianceTeamPitClass(ProtoMessage):
    """一个**团体**矿点（``AllianceNewPitRes.teamList`` 的元素）。"""

    proto_class_name = "AllianceTeamPitClass"

    #: 矿点实例 ID
    sid: int = 0
    #: 矿点模板 ID（对应配置表 ``AllianceTeamPitData.teamPitID``）
    id: int = 0
    #: 槽位清单（每槽要求一个指定角色）
    detaliClassList: list[AllianceTeamPitDetailClass] = Field(default_factory=list)
    #: 挖矿结束时间（Unix **毫秒**；-1 = 未开矿 —— 与个人矿同一约定）
    endTime: int = 0

    def is_idle(self) -> bool:
        """是否未开矿。"""
        return self.endTime <= 0

    def is_mining(self, now: int) -> bool:
        """是否挖掘中（``now`` 为当前 Unix **毫秒**时间戳）。"""
        return self.endTime > now

    def empty_slot_indices(self) -> list[int]:
        """还没被任何人认领的槽位下标（客户端显示 ＋ 号的那些）。

        判据收口在 :meth:`AllianceTeamPitDetailClass.is_empty` —— 哨兵值是
        ``-1`` 不是 ``0``，见 :data:`NO_MEMBER_PID` 的实证。
        """
        return [
            index
            for index, slot in enumerate(self.detaliClassList)
            if slot.is_empty()
        ]

    def slots_for_member(self, player_id: int) -> list[int]:
        """分给指定玩家的槽位下标（``memberPid`` 匹配；本地预判，服务端为准）。

        .. note::
           用 ``==`` 而非 ``<=``：``player_id`` 理论上可以是 0（未登录态），
           那时不该把空槽位误判成"自己的"。
        """
        return [
            index
            for index, slot in enumerate(self.detaliClassList)
            if slot.memberPid == player_id
        ]


class AllianceNewPitPacket(ProtoMessage):
    """拉取新版挖矿状态总表（消息号 21039）。

    :param teachingID: 教学引导 ID。★ 10.3 抓包实测：正常请求传 **-1**
        （不是 0）—— 真实客户端非引导状态下发的是 -1，照抄即可。
    """

    proto_class_name = "AllianceNewPitPacket"

    #: 教学引导 ID（-1 = 非引导请求，10.3 抓包实测）
    teachingID: int = -1


class AllianceNewPitRes(ProtoMessage):
    """挖矿状态总表响应（消息号 21040）。"""

    proto_class_name = "AllianceNewPitRes"

    #: 业务错误码
    errorCode: int = 0
    #: 全部个人矿点
    personalList: list[AlliancePersonalPitClass] = Field(default_factory=list)
    #: 全部团体矿点
    teamList: list[AllianceTeamPitClass] = Field(default_factory=list)
    #: 个人矿今日可开次数上限（语义待实测）
    personalLimit: int = 0
    #: 团体矿**今日可占次数上限**（= 客户端 ``m_teamLimitCount``，字段偏移 0x94）。
    #:
    #: 【★★ 2026-10-05 晚：这段注释曾被写错并造成真机故障，务必读完再改】
    #:
    #: 白天那一版写的是"它是**按坑**的限制、**不可**当'我还能占几个'用"，
    #: 据此删掉了 ``teamLimit − 我已占`` 这条闸门。**那是误读**，后果是真机
    #: 3 次占坑成功后连吃 **15 个 errorCode=-8**（全是白发请求）。证据：
    #:
    #: - ``AllianceNewPitRes`` **只有 5 个字段**（errorCode / personalList /
    #:   teamList / personalLimit / teamLimit）—— ``teamLimit`` 就是**一个 int**；
    #: - 被误当成"按坑配额表"的 ``m_lzt_newPitLimit_Team`` 其实是**坑的槽位列表**：
    #:   ``NewPitTeamLimit`` 这个类**只包了一个 ``AllianceTeamPitDetailClass``**
    #:   （getter 是 NeedRoleID / NeedRoleLv / Member / MemberPid），**与配额无关**；
    #: - 真正的配额是 ``m_teamLimitCount``(0x94) vs ``m_teamCount``(0xA4)：
    #:   ``PitRoleIcon.IsTeamCountMax()`` 比的正是这两个，
    #:   ``UpdatePitTeamCountText()`` 用 ``TeamRemainCount`` + ``TeamLimitCount``
    #:   渲染「剩余/上限」——**这是一个每日额度**。
    #:
    #: ⇒ 用法：``allowance = max(teamLimit - 我已占槽位数, 0)``。
    #: 见 ``tasks/alliance_mining.py`` 的 ``_team_allowance``（**别再删它**）。
    #: ``teamLimit <= 0``（服务端没下发）时**不要**当成 0，退化为服务端裁决。
    teamLimit: int = 0

    def is_success(self) -> bool:
        """是否业务成功。"""
        return self.errorCode in SUCCESS_ERROR_CODES


class AllianceNewPitOneKeyStartPacket(ProtoMessage):
    """个人矿**一键开矿**（消息号 21047）。

    :param one_key_personal_list: 要开的矿点（``sid``/``id`` 必填，
        ``roleSidList`` 取自 :meth:`~models.role.GetAllRoleRes.by_role_id`
        等本地匹配 —— 空列表语义待真机验证，见模块 docstring 第 3 条）。
    :param update_list: 要**改派角色**的已开矿点（本模块不用，留空）。

    .. warning::
       有副作用（消耗今日开矿次数）：**不要自动重试**。
    """

    proto_class_name = "AllianceNewPitOneKeyStartPacket"

    #: 要开的个人矿点
    oneKeyPersonalList: list[AlliancePersonalPitClass] = Field(default_factory=list)
    #: 要改派角色的矿点（本模块不用）
    updateList: list[AlliancePersonalPitClass] = Field(default_factory=list)

    @classmethod
    def for_pits(
        cls, pits: list[AlliancePersonalPitClass]
    ) -> "AllianceNewPitOneKeyStartPacket":
        """按要开的矿点清单构造一键开矿请求。"""
        return cls(oneKeyPersonalList=list(pits), updateList=[])


class AllianceNewPitOneKeyStartRes(ProtoMessage):
    """一键开矿响应（消息号 21048）。"""

    proto_class_name = "AllianceNewPitOneKeyStartRes"

    #: 业务错误码
    errorCode: int = 0
    #: 开矿后的最新矿点状态
    personalList: list[AlliancePersonalPitClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否业务成功。"""
        return self.errorCode in SUCCESS_ERROR_CODES


class PitSetRoleType:
    """``AllianceNewPitSetRolePacket.type`` 的取值
    （``AllianceNetWorkModule.ePitSetRolePacketType`` 枚举）。"""

    #: 个人矿改派角色
    PERSONAL_EDIT: Final[int] = 0
    #: 团体矿进入槽位
    TEAM_IN: Final[int] = 1
    #: 团体矿退出槽位
    TEAM_OUT: Final[int] = 2


class AllianceNewPitSetRolePacket(ProtoMessage):
    """矿点槽位角色设置（消息号 21043；个人改派 / 团体进出共用）。

    ``roleSidList`` 用**非 packed**编码：10.3 抓包（raw/84）里客户端发的是
    单个 ``18 <varint>``（逐元素带 tag），与官方客户端逐字节对齐。
    """

    proto_class_name = "AllianceNewPitSetRolePacket"

    unpacked_fields: ClassVar[frozenset[str]] = frozenset({"roleSidList"})

    #: 操作类型（见 :class:`PitSetRoleType`）
    type: int = PitSetRoleType.PERSONAL_EDIT
    #: 矿点实例 ID
    pitSid: int = 0
    #: 要指派的角色实例 ID
    roleSidList: list[int] = Field(default_factory=list)
    #: 团体矿槽位下标（个人矿不填）
    teamPitIndex: int = 0


class AllianceNewPitSetRoleRes(ProtoMessage):
    """槽位设置响应（消息号 21044）。"""

    proto_class_name = "AllianceNewPitSetRoleRes"

    #: 业务错误码
    errorCode: int = 0
    #: 最新个人矿点状态
    personalList: list[AlliancePersonalPitClass] = Field(default_factory=list)
    #: 最新团体矿点状态
    teamList: list[AllianceTeamPitClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否业务成功。"""
        return self.errorCode in SUCCESS_ERROR_CODES


class AllianceNewPitStartPacket(ProtoMessage):
    """开矿请求（消息号 21045；团体矿 / 指定角色开矿用）。

    .. warning::
       有副作用（消耗今日开矿次数）：**不要自动重试**。
    """

    proto_class_name = "AllianceNewPitStartPacket"

    unpacked_fields: ClassVar[frozenset[str]] = frozenset({"roleSidList"})

    #: 矿点实例 ID
    pitSid: int = 0
    #: 本次开矿指派的角色实例 ID
    roleSidList: list[int] = Field(default_factory=list)


class AllianceNewPitStartRes(ProtoMessage):
    """开矿响应（消息号 21046）。"""

    proto_class_name = "AllianceNewPitStartRes"

    #: 业务错误码
    errorCode: int = 0
    #: 挖矿结束时间（Unix 秒；成功时 > 当前时间）
    endTime: int = 0

    def is_success(self) -> bool:
        """是否业务成功。"""
        return self.errorCode in SUCCESS_ERROR_CODES


class AllianceNewPitRewardPacket(ProtoMessage):
    """收已完成矿的奖励（消息号 21049）。

    .. warning::
       有副作用（一次性领取）：**不要自动重试** —— 服务端可能已处理成功。
    """

    proto_class_name = "AllianceNewPitRewardPacket"

    #: 矿点实例 ID
    pitSid: int = 0


class AllianceNewPitRewardRes(ProtoMessage):
    """收矿响应（消息号 21050）。"""

    proto_class_name = "AllianceNewPitRewardRes"

    #: 业务错误码
    errorCode: int = 0
    #: 收矿后的最新矿点状态
    personalList: list[AlliancePersonalPitClass] = Field(default_factory=list)
    #: 本次获得的道具
    gotList: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否业务成功。"""
        return self.errorCode in SUCCESS_ERROR_CODES

    def reward_summary(self) -> str:
        """把 ``gotList`` 汇总成一行（**仅开发者日志（DEBUG）用**）。

        .. warning::
           21050 的 ``itemAmount`` 数量口径**尚未实证**（同源 6006/11010/37020
           已证实是"操作后持有量"）—— 禁止拼进用户可见文案；任务层用
           :func:`models.reward.reward_phrase`（``scope=UNKNOWN``）展示。
        """
        items: list[str] = []
        for got in self.gotList:
            items.extend(f"{item.itemID}×{item.itemAmount}" for item in got.itemList)
        return "道具 " + "、".join(items) if items else "<无道具>"
