"""特惠礼包（GiftPackage）报文（消息号 37001/37002 与 37019/37020）。

【这个系统长什么样】
首页的「特惠礼包」面板会一次拉出整页礼包，其中既有要花钱买的，也有**每日免费**的：
``GiftInfoClass.isFree`` 就是"免费"标记。免费的那些用 37019 ``GetFreeGiftPacket``
直接领走，**完全不需要走任何支付渠道** —— 这正是本项目唯一会碰的部分。

【两条链路】
======================================================  ===============================
37001 GetGiftListPacket{platform, cashType, channelType, giftType}
   → 37002 GetGiftListRes{giftList}
37019 GetFreeGiftPacket{giftId}
   → 37020 GetFreeGiftPacketRes{errorCode, gotList}
======================================================  ===============================

【"每日免费钻石"和"每日免费扫荡券"是怎么定位的】
**不靠猜名字**，而是靠"领完之后看拿到了什么"：

============================  =============
资源                          常量与取值
============================  =============
钻石                          ``DIAMOND_ID`` = 200000001
扫荡券                        ``SWEEPTICKET_ID`` = 200000016
============================  =============

两个常量都在 ``models/const_ids.py`` 里（来自客户端 ``RPS_Const``）。
所以本模块只负责"把免费礼包挑出来 + 领"，**识别交给任务层的日志汇总** ——
这样将来活动换成别的免费道具，这里一行都不用改。

【实测状态】
1. ``GetGiftListPacket`` 的四个参数**已于 2026-10-03 抓包实测定案**
   （``platform=4 / cashType=7 / channelType=2 / giftType=0``，
   见 ``config.GIFT_STORE_*`` 与下方类注释）；
2. 每日重置的判定：``buyCount``（已领次数）与 ``buyLimit``（上限）谁是谁只能靠实测响应确认。
   因此任务层把本地预判只当作"少发无效请求"的辅助，**不作为唯一依据**。
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import Field

from models.daily import GetObjClass, SUCCESS_ERROR_CODE
from models.game_packet import ProtoMessage


class GiftType(IntEnum):
    """``GetGiftListPacket.giftType`` 的取值（客户端 ``GiftPackageNetModule.eGiftType``）。

    .. note::
       这就是"要拉哪一类礼包"的筛选参数。本项目只关心 :attr:`NORMAL`
       （首页特惠礼包）；:attr:`RETURN_REWARD` 是回流玩家的专属礼包。
    """

    #: 普通礼包（首页特惠礼包）
    NORMAL = 0
    #: 回流奖励礼包
    RETURN_REWARD = 1


class GetGiftListPacket(ProtoMessage):
    """拉取礼包列表（消息号 37001）。

    :param platform: 礼包商店的**平台筛选**（10.3 安卓抓包实测 = 4；
        与握手/登录用的 ``channelPlatformType``（EL = 2）不是一回事）。
    :param cashType: 支付类型筛选（10.3 安卓抓包实测 = 7）。
    :param channelType: 渠道筛选（10.3 安卓抓包实测 = 2）。
    :param giftType: 礼包类别，见 :class:`GiftType`。

    .. warning::
       上面三个实测值是"能拿到完整礼包列表（216 个）"的组合；
       历史上用 ``{2, 0, 0}`` 服务端只回**空列表**。改任何一项前先抓包对照。
    """

    proto_class_name = "GetGiftListPacket"

    #: 礼包商店的平台筛选（安卓客户端 = 4）
    platform: int = 0
    #: 支付类型筛选（实测 7）
    cashType: int = 0
    #: 渠道筛选（实测 2）
    channelType: int = 0
    #: 礼包类别（见 :class:`GiftType`）
    giftType: int = 0

    @classmethod
    def for_normal_store(
        cls, *, platform: int, cash_type: int = 0, channel_type: int = 0
    ) -> "GetGiftListPacket":
        """构造"拉首页特惠礼包列表"的请求（``giftType`` 固定为普通）。

        :param platform: 商店平台筛选（用 ``config.GIFT_STORE_PLATFORM``，实测 4）。
        :param cash_type: 支付类型筛选（用 ``config.GIFT_STORE_CASH_TYPE``，实测 7）。
        :param channel_type: 渠道筛选（用 ``config.GIFT_STORE_CHANNEL_TYPE``，实测 2）。

        【为什么把 giftType 固定在这里】
        它是本项目唯一的筛选意图；分散到各调用点去传，早晚有人传成
        ``RETURN_REWARD`` 而拿到"回流礼包"的列表 —— 那种错误不会报错，
        只会让人困惑"为什么免费礼包一个都没有"。
        """
        return cls(
            platform=platform,
            cashType=cash_type,
            channelType=channel_type,
            giftType=int(GiftType.NORMAL),
        )


class GiftInfoClass(ProtoMessage):
    """一个礼包（``GetGiftListRes.giftList`` 的元素，43 个字段）。

    .. note::
       字段很多，但本项目只用到其中几个（``giftID`` / ``isFree`` /
       ``buyCount`` / ``buyLimit`` / ``name`` / ``endTime``）。
       **仍然把 43 个字段全部声明**，原因是 ``ProtoMessage.encode`` 会检查
       "字段表里有、模型里没有"的字段并直接报错（见 ``models/game_packet.py``）——
       少声明一个就会让整个类无法编码，而不是悄悄少发一个字段。

    .. warning::
       ``groupMain`` / ``groupSub`` / ``customItemList`` / ``characterSelectInfo``
       这几个嵌套结构在本次任务里用不到，按项目惯例**保留原始字节**
       （``bytes`` / ``list[bytes]``）。将来要展示"可选礼包内容"时再建模，
       不需要重新抓包。
    """

    proto_class_name = "GiftInfoClass"

    #: 记录 ID
    sid: int = 0
    #: **礼包 ID** —— 领免费礼包时作为 ``GetFreeGiftPacket.giftId``
    giftID: int = 0
    #: 价格（字符串，可能带货币符号或分档标记）
    price: str = ""
    #: 购买条件
    condition: str = ""
    #: 已购买/已领取次数
    buyCount: int = 0
    #: 开始时间（Unix 时间戳，秒）
    startTime: int = 0
    #: 结束时间（Unix 时间戳，秒）
    endTime: int = 0
    #: 礼包内容
    goodsList: list[GetObjClass] = Field(default_factory=list)
    #: 主推商品
    mainProductList: list[GetObjClass] = Field(default_factory=list)
    #: 渠道商品标识（用于向 IAP 平台下单）
    identifier: str = ""
    #: 价格（NT 币种）
    price_nt: str = ""
    #: 图标 ID 列表
    iconList: list[int] = Field(default_factory=list)
    #: 礼包分类（字符串，例如按页签分组）
    type: str = ""
    #: 排序
    order: int = 0
    #: 礼包名
    name: str = ""
    #: 说明文字
    explain: str = ""
    #: 横幅图
    banner: str = ""
    #: 购买/领取上限（``0`` 或负数表示不限，待实测确认）
    buyLimit: int = 0
    #: 礼包组 ID
    giftGroupID: int = 0
    #: 组主框（未建模，保留原始字节）
    groupMain: bytes = b""
    #: 组子框（未建模）
    groupSub: list[bytes] = Field(default_factory=list)
    #: 是否可自选内容
    isChoose: int = 0
    #: 折扣文案
    discount: str = ""
    #: 边框样式
    frameType: str = ""
    #: 原价文案
    originalPrice: str = ""
    #: **是否免费**（``!= 0`` 即免费礼包 —— 本项目要领的就是这些）
    isFree: int = 0
    #: 人民币价格档位
    price_cny: list[str] = Field(default_factory=list)
    #: 港币价格档位
    price_hkd: list[str] = Field(default_factory=list)
    #: 购买附带的 VIP 经验
    vip_exp: int = 0
    #: 渠道价格档位（Emma 渠道）
    price_emma: list[str] = Field(default_factory=list)
    #: 前置礼包 ID（买了它才解锁本礼包）
    prePackageSid: int = 0
    #: 关联的抽卡机分组
    drawMachineGroupID: list[int] = Field(default_factory=list)
    #: 打包价
    package_price: str = ""
    #: 捆绑包 ID
    bundlesID: int = 0
    #: 组中框（未建模，保留原始字节）
    groupMiddle: bytes = b""
    #: 外框样式
    frame: str = ""
    #: 最多可选数量
    maxSelectable: int = 0
    #: 自选内容列表（未建模，保留原始字节）
    customItemList: list[bytes] = Field(default_factory=list)
    #: 角色选择信息（未建模）
    characterSelectInfo: list[bytes] = Field(default_factory=list)
    #: 是否回流玩家专属礼包
    isReturnPackage: bool = False
    #: 是否重置类礼包（每日重置的礼包常带这一位 —— **待实测确认**）
    isReset: bool = False
    #: 展示用立绘 ID
    roleSpine: int = 0
    #: 新版横幅图
    newBanner: str = ""

    # ------------------------------------------------------------------
    # 业务判断
    # ------------------------------------------------------------------
    def is_free(self) -> bool:
        """是否免费礼包（依据 ``isFree != 0``，而不是看 ``price`` 文本）。"""
        return self.isFree != 0

    def can_claim(self) -> bool:
        """本地预判"这个免费礼包现在还能领吗"。

        :return: 免费、且未达上限时返回 ``True``。

        .. note::
           只是**预判**，用于少发注定失败的请求。
           真实可领状态以服务端响应为准 —— 本地数据可能过期，
           完全依赖它来跳过会造成漏领。
        """
        if not self.is_free():
            return False
        if self.buyLimit > 0 and self.buyCount >= self.buyLimit:
            return False
        return True

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        price = self.price or "0"
        flag = "免费" if self.is_free() else f"售价 {price}"
        return (
            f"#{self.giftID} {self.name or '<无名>'}（{flag}，"
            f"已领 {self.buyCount}/{self.buyLimit if self.buyLimit > 0 else '∞'}）"
        )


class GetGiftListRes(ProtoMessage):
    """礼包列表响应（消息号 37002）。

    .. warning::
       ``GiftInfoClass`` **必须定义在本类之前**（2026-10-03 修过的坑）：
       项目模型的惯例是"元素类先定义"。曾经把本类写在前面并用字符串前向
       引用 ``list["GiftInfoClass"]``，结果 pydantic 延迟构建让解码器拿到
       未解析的 ``ForwardRef``，首次解码真实 37002 响应（216 个礼包）时
       走了 bytes 兜底分支直接崩溃（服务端回空列表时从不触发，所以潜伏了
       很久）。同类问题已由 ``models/game_packet.py`` 的
       ``__pydantic_complete__`` 预检兜底，但这里仍按惯例排序，不留隐患。
    """

    proto_class_name = "GetGiftListRes"

    #: 全部礼包
    giftList: list[GiftInfoClass] = Field(default_factory=list)

    def free_gifts(self) -> list[GiftInfoClass]:
        """挑出**免费**礼包（``isFree != 0``），按服务端顺序返回。

        为什么这里按 ``isFree`` 挑、而不是按 ``price == "0"``：
        价格是字符串（含货币符号/分档），且免费礼包的 ``price`` 未必是 "0"；
        ``isFree`` 是客户端专门用来标记免费的字段，语义最直接。
        """
        return [gift for gift in self.giftList if gift.is_free()]


class GetFreeGiftPacket(ProtoMessage):
    """领取**免费**礼包（消息号 37019）。

    :param giftId: 礼包 ID —— 取自 ``GiftInfoClass.giftID``（不是记录 ID ``sid``）。

    .. warning::
       **有副作用**：服务端会真的发奖，且免费礼包通常**每日或每期限领一次**。
       超时后**不要自动重试**（与 ``DMReward`` 同一原则）：服务端可能已经处理成功，
       重试只会拿到"已达上限"的错误，还可能触发风控。每天重跑一次这个任务
       就是正确的复查方式。
    """

    proto_class_name = "GetFreeGiftPacket"

    #: 礼包 ID
    giftId: int = 0

    @classmethod
    def for_gift(cls, gift_id: int) -> "GetFreeGiftPacket":
        """按礼包 ID 构造请求。

        .. note::
           参数名 ``giftId`` 与请求里的字段名 ``giftID`` 只差一个大小写，
           这是协议里就存在的写法差异（``GetFreeGiftPacket.giftId`` vs
           ``GiftInfoClass.giftID``）。用本工厂方法可以避免手抄错，
           也让"这里传的是礼包 ID、不是记录 ID"在调用处一目了然。
        """
        return cls(giftId=gift_id)


class GetFreeGiftPacketRes(ProtoMessage):
    """领取免费礼包的响应（消息号 37020）。"""

    proto_class_name = "GetFreeGiftPacketRes"

    #: 业务错误码（0 = 成功，见 :data:`models.daily.SUCCESS_ERROR_CODE`）
    errorCode: int = 0
    #: 本次获得的东西
    gotList: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否业务成功（判断依据见 :data:`models.daily.SUCCESS_ERROR_CODE`）。"""
        return self.errorCode == SUCCESS_ERROR_CODE

    def reward_summary(self) -> str:
        """把 ``gotList`` 汇总成一行（**仅开发者日志（DEBUG）用**）。

        :return: 形如 ``道具 200000001×60、200000016×2``；没有道具时返回 ``<无道具>``。

        .. warning::
           ``itemAmount`` 是**领取后的持有量**（Q-021 同源），不是本次增量 ——
           禁止拼进用户可见文案。任务层用
           :func:`models.reward.reward_phrase`（``scope=HELD``）来展示。
        """
        items: list[str] = []
        for got in self.gotList:
            items.extend(f"{item.itemID}×{item.itemAmount}" for item in got.itemList)
        return "道具 " + "、".join(items) if items else "<无道具>"


