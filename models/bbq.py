"""宴席（BBQ）报文（消息号 32007/32008 与 15003/15004）。

【这个系统和"体力"的关系】
本地配置表 ``ExtraEnergyData`` 的 ``assetID`` 直接叫 ``Activity_Eat_food1..5``，
``menuID`` = 500000001~500000005，``itemAmount`` = 0 / 7 / 15 / 24 / 31：

    menuID      number  assetID               useItemID  itemAmount
    500000001   1       Activity_Eat_food1    NULL       0     ← 免费档
    500000002   2       Activity_Eat_food2    NULL       7
    500000003   3       Activity_Eat_food3    NULL       15
    500000004   4       Activity_Eat_food4    NULL       24
    500000005   5       Activity_Eat_food5    NULL       31

``useItemID`` 为 ``NULL`` 表示消耗的是**默认货币（钻石）**，而第 1 档为 0 ——
这正是"可以选择是否花钻石邀请好友"的数据来源。**因此本模块的调用方必须过消费闸门**
（``tasks/base.py`` 的 ``BaseTask.request_diamond_spend``）。

【两张表的分工】
=======================================  ==================================================
32007 GetBBQDataPacket（空请求）           拉数据：活动组 groupID、好友列表与各自状态
   → 32008 GetBBQDataRes
15003 BBQFriendsPacket{groupID, 好友列表}  真正"吃"的动作
   → 15004 BBQFriendsRes{errorCode, addList, costList}
=======================================  ==================================================

【⚠️ 尚未实测确认的三件事（本模块刻意不臆造逻辑）】
1. ``groupID`` 与 ``ExtraEnergyData.menuID`` 的对应关系（是否就是同一个编号）；
2. ``FriendClass.send_state`` / ``get_state`` 的取值含义
   （哪个值表示"今天还能吃"）—— 所以这里只提供 :meth:`FriendClass.state_hint`
   原样展示，**没有**写 ``can_eat()`` 之类的判断，避免把猜测固化成逻辑；
3. 每日次数上限（``ExtraEnergyData.number`` 是否就是上限）。

真机跑通一次后，把真实响应存进 ``protocol_samples/`` 再回来补这些语义。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from pydantic import Field

from models.daily import GetObjClass, SUCCESS_ERROR_CODE
from models.game_packet import ProtoMessage


class GetBBQDataPacket(ProtoMessage):
    """宴席数据请求（消息号 32007）。

    .. note::
       空报文（dump.cs 里它没有任何 ``ProtoMember`` 字段），
       所以编码结果就是 ``b""`` —— 服务端靠 ``RootPacket.packetID`` 识别意图。
    """

    proto_class_name = "GetBBQDataPacket"


class ActivityIntervalDataClass(ProtoMessage):
    """一段活动期间数据（``GetBBQDataRes`` 的列表元素）。

    ``groupID`` 就是要回填进 :class:`BBQFriendsPacket` 的那个参数 ——
    换句话说「今天开的是哪一期宴席」由服务端决定，不能本地写死。
    """

    proto_class_name = "ActivityIntervalDataClass"

    #: 数据条数（与 ``ActivityIntervalRelationData`` 表的 ``dataNummber`` 同源）
    dataNummber: int = 0
    #: 活动组 ID —— **BBQFriendsPacket.groupID 的取值来源**
    groupID: int = 0
    #: 开始时间（Unix 时间戳，**毫秒** —— 10.3 抓包实测为 13 位数）
    startDateTime: int = 0
    #: 结束时间（Unix 时间戳，**毫秒**）
    endDateTime: int = 0
    #: 周期（周数）。10.3 抓包里两期分别为 0 和 2 —— 免费吃锚定 ``weeks == 0``
    #: 的那一期（见 ``tasks.bbq.BBQEnergyTask._current_group_id``）
    weeks: int = 0
    #: 活动入口按钮（结构复杂且本流程用不到，保留原始字节）
    inGameBannerBtnClz: list[bytes] = Field(default_factory=list)


class FriendClass(ProtoMessage):
    """一个好友（``GetBBQDataRes.bbqFriendList`` 的元素）。

    .. note::
       ``icon`` 在协议里是 ``IconClass``，本次没有把它加进字段表，
       因此按项目的既有做法**保留原始字节**（见 ``models/daily.py`` 的同类处理）：
       将来要展示头像时再建模，不需要重新抓包。
    """

    proto_class_name = "FriendClass"

    #: 玩家 ID —— 就是 ``BBQFriendsPacket.bbqFriendList`` 里要填的值
    playerID: int = 0
    #: 头像（未建模，保留原始字节）
    icon: bytes = b""
    #: 昵称
    name: str = ""
    #: 等级
    player_lv: int = 0
    #: VIP 等级
    vip_lv: int = 0
    #: 战力
    fighting_force: int = 0
    #: 上次登录时间（Unix 时间戳，秒）
    last_login_time: int = 0
    #: 送出状态（含义待实测，见模块文档第 2 条）
    send_state: int = 0
    #: 获取状态（含义待实测）
    get_state: int = 0

    def state_hint(self) -> str:
        """把两个状态字段**原样**展示出来（供日志与实测对照用）。

        为什么不直接写 ``can_eat() -> bool``？
        因为"哪个值代表还能吃"目前只是推测。把它写成函数会被后续代码当成事实使用，
        而一旦猜反，现象是"该吃的没吃、不该吃的发了包"—— 极难回溯。
        实测确认后，再把判断逻辑加到本类里，并补相应单测。
        """
        return f"send_state={self.send_state}, get_state={self.get_state}"


class GetBBQDataRes(ProtoMessage):
    """宴席数据响应（消息号 32008）。"""

    proto_class_name = "GetBBQDataRes"

    #: 活动截止 ID 列表
    eventDeadlineID: list[int] = Field(default_factory=list)
    #: 活动期间数据（``groupID`` 在这里）
    activityIntervalDataList: list[ActivityIntervalDataClass] = Field(default_factory=list)
    #: 好友列表（含各自状态）
    bbqFriendList: list[FriendClass] = Field(default_factory=list)

    def group_ids(self) -> list[int]:
        """取出所有候选 ``groupID``（顺序与服务端一致）。"""
        return [item.groupID for item in self.activityIntervalDataList]

    def friend_ids(self) -> list[int]:
        """取出好友 ID 列表（顺序与服务端一致）。"""
        return [friend.playerID for friend in self.bbqFriendList]

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        return (
            f"活动期间 {len(self.activityIntervalDataList)} 段"
            f"（groupID={self.group_ids()}）、好友 {len(self.bbqFriendList)} 人"
        )


class BBQFriendsPacket(ProtoMessage):
    """"吃"好友的宴席（消息号 15003）。

    :param groupID: 要参与的那期宴席 —— 取自
        :class:`GetBBQDataRes` 的 ``activityIntervalDataList[].groupID``。
    :param bbqFriendList: 选中的好友 ID 列表（UI 上是多选，
        见 dump.cs 的 ``BBQ_Main_UIView_New.EatFriendData``）。

    .. warning::
       **本请求可能真的花钻石**。档位越高、选中的好友越多，消耗越大
       （数据来源见模块文档里的 ``ExtraEnergyData`` 表）。
       调用方必须在组包之前调用 ``BaseTask.request_diamond_spend()`` ——
       本模型只负责编解码，**不做任何权限判断**，因为它拿不到策略对象。
    """

    proto_class_name = "BBQFriendsPacket"

    #: ``bbqFriendList`` 用**非 packed**（每个好友独立带 tag）编码：
    #: 10.3 抓包（付费邀请 raw/09）里客户端发的是 ``10 <varint>`` 连续四个，
    #: 不是 packed —— 与官方客户端逐字节对齐（见 tests/test_capture_1003_regression.py）。
    unpacked_fields: ClassVar[frozenset[str]] = frozenset({"bbqFriendList"})

    #: 宴席组 ID（来自服务端）
    groupID: int = 0
    #: 选中的好友玩家 ID 列表
    bbqFriendList: list[int] = Field(default_factory=list)

    @classmethod
    def for_friends(
        cls, group_id: int, friend_ids: Sequence[int]
    ) -> "BBQFriendsPacket":
        """构造「和这些好友一起吃」的请求。

        :param group_id: :class:`GetBBQDataRes` 里取出的 ``groupID``。
        :param friend_ids: 好友玩家 ID（``FriendClass.playerID``）。

        【为什么要这个工厂方法】
        ``bbqFriendList`` 与 ``FriendClass`` 同名但类型完全不同（这边是 ``int``，
        那边是对象）。写错时 protobuf 编码会直接报类型错误，但报错信息只会说
        "字段 bbqFriendList 类型不对"，很难一眼看出是"传了对象列表"——
        用一个签名明确的工厂方法，把这个坑挡在调用处。
        """
        return cls(groupID=group_id, bbqFriendList=list(friend_ids))


class BBQFriendsRes(ProtoMessage):
    """"吃"好友宴席的响应（消息号 15004）。"""

    proto_class_name = "BBQFriendsRes"

    #: 业务错误码（0 = 成功，见 :data:`models.daily.SUCCESS_ERROR_CODE` 的说明）
    errorCode: int = 0
    #: 得到的东西（体力通常在这里）
    addList: list[GetObjClass] = Field(default_factory=list)
    #: 消耗掉的东西（花钻石时可能出现在这里，便于对账）
    costList: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否业务成功（判断依据见 :data:`models.daily.SUCCESS_ERROR_CODE`）。"""
        return self.errorCode == SUCCESS_ERROR_CODE

    def reward_summary(self) -> str:
        """把 ``addList`` 里的道具汇总成一行（**仅开发者日志（DEBUG）用**）。

        :return: 形如 ``道具 200000002×30``；什么都没有时返回 ``<无奖励>``。

        .. warning::
           15004 的 ``itemAmount`` 数量口径**尚未实证** —— 禁止拼进用户可见文案；
           任务层用 :func:`models.reward.reward_phrase`（``scope=UNKNOWN``，
           只报件数）来展示。
        """
        items: list[str] = []
        for got in self.addList:
            items.extend(f"{item.itemID}×{item.itemAmount}" for item in got.itemList)
        return "道具 " + "、".join(items) if items else "<无奖励>"

    def cost_summary(self) -> str:
        """把 ``costList`` 里的道具汇总成一行（**仅开发者日志（DEBUG）用**）。

        口径警告同 :meth:`reward_summary`：15004 的 ``itemAmount`` 语义未实证，
        不能当"消耗"写进用户可见文案。
        """
        items: list[str] = []
        for cost in self.costList:
            items.extend(f"{item.itemID}×{item.itemAmount}" for item in cost.itemList)
        return "消耗 " + "、".join(items) if items else "<无消耗>"

