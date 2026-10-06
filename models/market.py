"""常驻市集报文（消息号 14001/14002 与 14003/14004）。

【这个系统是什么】
游戏首页「市集」里的**常驻商铺**。``StoreSettingsData`` 给每个商铺一个
``storeID``（实测 ``398000001`` 竞技市集 … ``398000015`` **一般市集**），
``StoreNetWorkModule`` 用同一个 ``14001`` 报文按 ``storeType`` 拉不同商铺的货架：

============================================  ======================================
14001 ``GetStoreListPacket{storeType}``       只读拉货架
  → 14002 ``GetStoreListRes{errorCode, productList, resetCount}``
14003 ``BuyProductPacket{productSID, count}`` **真的买**（扣货币）
  → 14004 ``BuyProductRes{errorCode, itemList, costList, nowBuyCount}``
============================================  ======================================

【"一般市集"是哪一间、用什么货币买】
``StoreSettingsData`` 里有**两行都叫「一般市集」**（同名不同号）：

============================================  ===============  ==================
``storeID``（= 请求里的 ``storeType``）         ``pageOrder``    服务端是否开放
============================================  ===============  ==================
``398000012``                                 9998（第 13 行）  ✘ 已弃用
``398000015``                                 9001（第 16 行）  ✔ 实际开放
============================================  ===============  ==================

【为什么确定是 015 而不是 012】（真实抓包，可复现）
``captures/市集购买.saz``（2026-10-03 官方客户端实测，整条购买链路）：

::

    raw/04  14011 GetStoreTypeListPacket（空包）→ 14012（服务端开放的商铺号清单）
    raw/06  14001 GetStoreListPacket{storeType=398000015}
              → 14002 errorCode=0、productList 36 件   ✅
            （向旧号 398000012 请求则回 errorCode=-1 = dataError，不是 -2 notExist）

复现命令：

.. code-block:: bash

   PYTHONIOENCODING=utf-8 python tools/extract_saz_posts.py --match 市集 --decode raw/06

⇒ 本地表里的 ``398000012`` 是**旧版遗留行**：两行同名、货币列也相同（都是
  ``200000050``），但服务端只认 ``398000015``。写错号的现象是 14002 回 ``-1``
  （``dataError``）而不是 ``-2``（``notExist``）—— 很容易被误判成"账号/等级不够"
  之类的别的问题，所以编号改对之后，仍保留 :meth:`GetStoreListRes.describe_error`
  把错误码翻成人话。

货币：015 那一行的 ``currencyIcon`` 列同样是 ``200000050``，即
``models.const_ids.CONSTS["COINS_ID"]``（**金币**）。所以本商铺**不花钻石**，
扣的是金币（与"买体力"那种花钻石的行为是两回事）。

.. note::
   ``DailyMissionData`` 里两条"好貨等你買"任务的参数列写的是旧号 ``398000012``
   （"市集購買{0}件商品"）。任务进度由服务端判定，不取决于我们请求哪个号；
   这里记一笔，是为了下次再看到旧号时知道它是从哪来的。

【字段编号与类型的来源】
- 编号：``models/proto_fields.py``（由 dump.cs 生成）里 ``ProductClass`` 的字段顺序；
- 类型：dump.cs **L435765** ``public class ProductClass`` 的字段声明
  （``productSID/itemID/itemSet/nowBuyCount/maxBuyCount/currencyType/price`` 都是
  ``int``，``condition/productPromo`` 是 ``string``，``refreshTime`` 是 ``long``）。
  类型写错的表现是服务端"含糊拒绝"，所以这里照抄 dump 而不是按名字猜。

【本模块刻意**不建模**的包（与 ``models/top_pvp.py`` 同一哲学）】
==============================  ====================================================
14005 ``GetStoreVersionPacket`` 货架版本号（本任务不需要，少一个包少一分风险）
14007 ``StoreResetPacket``       刷新货架（**花钻石**）—— 连模型都不建，
                                 类型层面就不存在"手滑刷新"这条路
==============================  ====================================================

.. warning::
   14003 ``BuyProductPacket`` **有副作用**（扣金币）。它在白名单里，但任务层
   （``tasks/market_buy.py``）只对"用户勾选的 4 种道具 + 服务端给的剩余可购量"下单，
   并且绝不重试。
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from models.const_ids import int_const
from models.daily import GetObjClass, ItemClass, SUCCESS_ERROR_CODE
from models.game_packet import ProtoMessage
from models.packet_ids import packet_id
from models.reward import item_name

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 业务成功码（与其它模块共用同一个 0，见 :data:`models.daily.SUCCESS_ERROR_CODE`）
MARKET_SUCCESS: Final[int] = SUCCESS_ERROR_CODE

#: 「一般市集」的 ``storeType``（= ``StoreSettingsData.storeID``）。
#:
#: 【为什么写死在模型层，而不是让调用方自由传】
#: 需求只针对这一间常驻商铺；做成参数就会出现"传错一间铺子"的可能，
#: 而那种错误不会报错，只会买回来一堆别的东西。要买别的铺子时显式改这一行，
#: 并顺手补一条实测证据 —— 这正是本项目"宁可改常量，也不留自由参数"的原则。
#:
#: 【为什么是 398000015 而不是 398000012】（2026-10-03 真机抓包结案）
#: ``StoreSettingsData`` 里有两行同名「一般市集」：``398000012``（旧版遗留、已弃用）
#: 与 ``398000015``（服务端实际开放）。向 012 请求 14001 只会拿到
#: ``14002 errorCode=-1``（``dataError``，**不是**"商铺不存在"），任务因此在
#: "只读拉货架"这一步就中止 —— 一件都买不到，且一个购买请求都不会发。
#: 证据：``captures/市集购买.saz`` 的 ``raw/06`` —— 官方客户端发的是 015，
#: 响应 ``errorCode=0`` + 36 件商品。复现：
#: ``PYTHONIOENCODING=utf-8 python tools/extract_saz_posts.py --match 市集 --decode raw/06``
MARKET_STORE_TYPE: Final[int] = 398000015

#: 商铺中文名（日志与界面共用同一句话，避免两处说法不一致）
MARKET_STORE_NAME: Final[str] = "一般市集"

#: 本商铺的结算货币（``StoreSettingsData.currencyIcon`` = 200000050 = 金币）。
#: 用途只有一个：让日志说清"扣的是金币"，并给"这是什么资源"一个可核对的名字。
MARKET_CURRENCY_ITEM_ID: Final[int] = int_const("COINS_ID")

#: 每件商品的**每日限购次数**（需求：每日限购 5 次，买满则跳过）。
#:
#: .. note::
#:    这只是"日志里对照用的参考值"，**真正的判据永远是服务端回的**
#:    ``ProductClass.nowBuyCount / maxBuyCount`` —— 服务端一改活动，
#:    写死的数字就会过期，而响应里的两个字段不会。
MARKET_DAILY_BUY_LIMIT: Final[int] = 5

#: 目标道具：``itemID → 中文名``（顺序 = 界面/日志里的展示顺序）。
#:
#: 【名字来源】
#: ``Cherrytale Asset/TextAsset/ItemData``：
#: ``200200000|血鑽``、``200200003|琥珀``、``200600001|愛心巧克力``、
#: ``200600002|幸福香氛``。
MARKET_ITEMS: Final[dict[int, str]] = {
    200200000: "血钻",
    200200003: "琥珀",
    200600001: "爱心巧克力",
    200600002: "幸福香氛",
}

#: 便于其它模块一次性拿到"目标道具 ID 列表"（顺序 = :data:`MARKET_ITEMS`）
MARKET_ITEM_IDS: Final[tuple[int, ...]] = tuple(MARKET_ITEMS)

#: 本模块允许发出的报文（``tasks/market_buy.py`` 用它做白名单）。
ALLOWED_PACKET_NAMES: Final[tuple[str, ...]] = (
    "GetStoreListPacket",  # 14001 只读拉货架
    "BuyProductPacket",  # 14003 购买（扣金币）
)

#: ``{类名: 消息号}``（供 ``tasks/packet_guard.guarded_send`` 使用）
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in ALLOWED_PACKET_NAMES
}

#: 明确**禁发**的报文：命中即抛错，绝不发送。
FORBIDDEN_PACKET_NAMES: Final[tuple[str, ...]] = (
    "StoreResetPacket",  # 14007 刷新货架（花钻石）
)

#: ``{类名: 消息号}``
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in FORBIDDEN_PACKET_NAMES
}

#: ``{消息号: 一句话说明}``（写进 DEBUG 日志，便于一眼看懂在发什么）
PACKET_PURPOSE: Final[dict[int, str]] = {
    14001: "拉取商铺货架（只读；本任务只用于「一般市集」398000015）",
    14002: "商铺货架响应",
    14003: "购买一件商品（**扣金币**；数量由服务端 maxBuyCount 兜底）",
    14004: "购买响应（返回获得物与花费明细）",
}



# ---------------------------------------------------------------------------
# 货架结构
# ---------------------------------------------------------------------------
class ProductClass(ProtoMessage):
    """货架上的一件商品（``StoreNetWorkModule.AddStoreListRes(List<ProductClass> …)``）。

    字段与编号一一对应 ``models/proto_fields.py::ProductClass``；
    类型照抄 dump.cs L435765（**不是**按名字猜的）。
    """

    proto_class_name = "ProductClass"

    #: 商品唯一编号（购买时回填给 14003 的 ``productSID``）
    productSID: int = 0
    #: 这件商品卖的道具 ID（与 :data:`MARKET_ITEMS` 的键比对）
    itemID: int = 0
    #: 商品打包内容（本任务不用：我们只按 ``itemID`` 认道具）
    itemSet: int = 0
    #: **今日已购次数**（买满判据之一）
    nowBuyCount: int = 0
    #: 每日限购上限（买满判据之二）
    maxBuyCount: int = 0
    #: 结算货币类型（``398000015`` 实测为金币 ``200000050``）
    currencyType: int = 0
    #: 单价（单位 = :attr:`currencyType`）
    price: int = 0
    #: 购买条件（如等级 / 前置任务；字符串描述，本任务只展示）
    condition: str = ""
    #: 货架分类
    shelfType: int = 0
    #: 促销标记
    productPromo: str = ""
    #: 下次刷新时间（毫秒时间戳；``long`` → Python ``int``）
    refreshTime: int = 0

    # ------------------------------------------------------------------
    # 取值辅助
    # ------------------------------------------------------------------
    def remaining(self) -> int:
        """**今天还能买几件**。

        【为什么用 ``max(0, …)`` 而不是先比较】
        ``maxBuyCount`` 为 ``0`` 时（未配置 / 异常值）会算出负数；负数传给服务端
        就是一次"必然被拒"的请求，等于白送一次风控暴露。夹到 ``0`` 之后，
        "是否要下单"只需判断一次 ``> 0``。
        """
        return max(0, int(self.maxBuyCount) - int(self.nowBuyCount))

    def is_sold_out(self) -> bool:
        """今日是否已买满（剩余可购量为 0）。"""
        return self.remaining() <= 0

    def display_name(self) -> str:
        """道具中文名（表里没有的 itemID 回退成编号，免得日志出现空字符串）。"""
        return MARKET_ITEMS.get(int(self.itemID), f"itemID={self.itemID}")

    def currency_name(self) -> str:
        """结算货币的**中文名**（认不出时回退成可读形式）。

        【★ 2026-10-04 为什么要翻名字】
        ``currencyType`` 是一个**道具 ID**（本铺实测 = 金币 200000050）。
        直接把它印成"货币 200000050"，用户会把它读成价格 ——
        例如「单价 500（货币 200000050）」看起来像"要花两亿"。
        """
        name = item_name(self.currencyType)
        return name if name else f"未知货币（{self.currencyType}）"

    def describe(self) -> str:
        """一行摘要：``血钻：今日 3/5，剩 2 件，单价 500 金币``。

        【★ 2026-10-04 修复】
        本方法此前**只有 docstring、没有函数体**（返回 ``None``），
        而被误并进 :meth:`GetStoreListRes.summary` 的那段 ``return`` 正是它的实现 ——
        后果是 ``tasks/market_buy.py`` 打出的货架清单全是 ``None``、
        ``data["shelf"]`` 也全是 ``None``（测试只断言了长度，所以一直没红）。

        【为什么保留这些字段】
        本方法是**货架清单**（``_list_catalog``）用的：用户要照着它挑道具，
        所以"今日已购 / 限购 / 剩几件 / 单价"都得留着。
        ``productSID`` 与 ``itemID`` 不进这里 —— 那是开发者日志的内容。
        """
        return (
            f"{self.display_name()}：今日 {self.nowBuyCount}/{self.maxBuyCount}，"
            f"剩 {self.remaining()} 件，单价 {self.price} {self.currency_name()}"
        )


class GetStoreListPacket(ProtoMessage):
    """拉取商铺货架（消息号 14001，**只读**）。

    :param storeType: 商铺编号（``StoreSettingsData.storeID``）。
    """

    proto_class_name = "GetStoreListPacket"

    #: 商铺编号（本任务恒为 :data:`MARKET_STORE_TYPE`）
    storeType: int = 0

    @classmethod
    def for_market(cls, store_type: int = MARKET_STORE_TYPE) -> "GetStoreListPacket":
        """构造"拉一般市集货架"的请求（默认就是 398000015）。

        【为什么要留一个 classmethod，而不是各处手写 ``storeType=398000015``】
        编号只写在模型层一处，任务层调用它即"不可能填错铺子"；
        将来要换成别的常驻商铺，改默认值即可，任务代码一行不用动。
        """
        return cls(storeType=int(store_type))


class GetStoreListRes(ProtoMessage):
    """货架响应（消息号 14002）。"""

    proto_class_name = "GetStoreListRes"

    #: 业务错误码（``StoreNetWorkModule.eStoreListResErrorCode``：
    #: ``complete=0`` / ``dataError=-1`` / ``notExist=-2``）
    errorCode: int = 0
    #: 货架上的全部商品
    productList: list[ProductClass] = Field(default_factory=list)
    #: 今日刷新次数（本任务只展示）
    resetCount: int = 0

    def is_success(self) -> bool:
        """服务端是否明确报成功（唯一可靠取值是 ``0``）。"""
        return int(self.errorCode) == MARKET_SUCCESS

    def describe_error(self) -> str:
        """把 ``errorCode`` 翻成人话（未知码原样报出来，**不编含义**）。

        来源是客户端枚举 ``StoreNetWorkModule.eStoreListResErrorCode``
        （dump.cs L551695 起），逐条抄下来：

        - ``0``  = ``complete``  成功；
        - ``-1`` = ``dataError`` 商铺数据异常 —— 实测**旧商铺号**
          （如已弃用的 ``398000012``）就回这个码，**而不是** ``-2``，
          所以别把它当成"这间铺子不存在"；
        - ``-2`` = ``notExist``  商铺号不存在（本地表里查无此号）。

        【为什么要单独写一个方法】
        这两个负数写法上长得几乎一样，含义却完全不同（一个换编号就能救、
        一个要查本地表）。把它们固化成文字，日志里就不会再出现裸 ``-1``
        让人去猜 —— 这正是本次「拉货架失败」排查耗时的直接原因。
        """
        notes = {
            0: "成功",
            -1: "商铺数据异常（dataError：多为已下线的旧商铺号，如 398000012）",
            -2: "商铺不存在（notExist）",
        }
        return notes.get(int(self.errorCode), f"未知错误码 {self.errorCode}")

    def find_product(self, item_id: int) -> ProductClass | None:
        """按 ``itemID`` 找货架上的商品；没有就返回 ``None``。

        【为什么按 itemID 找、而不是按 productSID】
        ``productSID`` 是服务端随时会换的编号（``StoreProductListData`` 里是
        3990001xx 这类流水号），而用户要的是"血钻 / 琥珀"这四样**道具**。
        跟着道具 ID 走，商品改编号也不会买错东西。
        """
        wanted = int(item_id)
        for product in self.productList:
            if int(product.itemID) == wanted:
                return product
        return None

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        left = sum(product.remaining() for product in self.productList)
        return (
            f"货架共 {len(self.productList)} 件商品，累计剩 {left} 件可购"
            f"（今日刷新 {self.resetCount} 次）"
        )


class BuyProductPacket(ProtoMessage):
    """购买一件商品（消息号 14003，**扣货币**）。

    :param productSID: 商品编号（来自 14002 的 ``ProductClass.productSID``）。
    :param count: 购买数量（本任务恒为服务端给的"剩余可购量"）。
    """

    proto_class_name = "BuyProductPacket"

    #: 商品编号
    productSID: int = 0
    #: 购买数量
    count: int = 0


class BuyProductRes(ProtoMessage):
    """购买响应（消息号 14004）。

    【``itemList`` / ``costList`` 的读法】
    与 ``11010 SweepSectionRes`` 同一套约定：``costList`` 里是**本次花费**，
    ``itemList`` 是**本次获得**（两者都是 ``GetObjClass``，真正的明细在它的
    ``itemList`` 里）。这里只做一行摘要，具体汇总交给任务层。
    """

    proto_class_name = "BuyProductRes"

    #: 业务错误码（``StoreNetWorkModule.eStoreBuyProductResErrorCode``：
    #: ``complete=0`` / ``notExist=-1`` / ``buyLvLimit=-2`` / ``moneyNotEnough=-3``）
    errorCode: int = 0
    #: 本次获得（含 ``itemList`` 里的道具明细）
    itemList: list[GetObjClass] = Field(default_factory=list)
    #: 本次花费（货币明细）
    costList: list[GetObjClass] = Field(default_factory=list)
    #: 购买后该商品今日的累计购买次数（服务端回传，可用于滚动判断"买满了没"）
    nowBuyCount: int = 0

    def is_success(self) -> bool:
        """服务端是否明确报成功（唯一可靠取值是 ``0``）。"""
        return int(self.errorCode) == MARKET_SUCCESS

    @staticmethod
    def _flatten(groups: list[GetObjClass]) -> list[ItemClass]:
        """把 ``list[GetObjClass]`` 摊平成"一行一个道具"。"""
        items: list[ItemClass] = []
        for group in groups:
            items.extend(group.itemList)
        return items

    def gained_items(self) -> list[ItemClass]:
        """本次**获得**的道具明细。"""
        return self._flatten(self.itemList)

    def cost_items(self) -> list[ItemClass]:
        """本次**花费**的货币明细。"""
        return self._flatten(self.costList)

    def item_summary(self) -> str:
        """一行文字说明买到了什么（**仅开发者日志用**）。

        .. warning::
           14004 的 ``itemList`` 数量语义**尚未实证** —— 同源的 6006 / 11010 /
           37020 都已证实回的是"操作后持有量"（Q-021），所以这里的
           ``itemAmount`` 很可能是**余额而非本次获得量**。
           禁止把它当"获得"写进用户可见文案；要展示请走
           :func:`models.reward.reward_phrase` 并显式给出 ``QuantityScope``。
        """
        items = self.gained_items()
        if not items:
            return "<无道具>"
        return "道具 " + "、".join(f"{item.itemID}×{item.itemAmount}" for item in items)

    def cost_summary(self) -> str:
        """一行文字说明花了什么（**仅开发者日志用**，口径警告同 :meth:`item_summary`）。"""
        costs = self.cost_items()
        if not costs:
            return "<无花费明细>"
        return "花费 " + "、".join(f"{item.itemID}×{item.itemAmount}" for item in costs)

    def describe_error(self) -> str:
        """把 ``errorCode`` 翻成人话（未知码原样报出来，**不编含义**）。

        来源是客户端枚举 ``StoreNetWorkModule.eStoreBuyProductResErrorCode``
        （协议档案卡 ``BuyProductRes`` 一节，dump.cs L551705 起），逐条抄下来。
        """
        notes = {
            0: "成功",
            -1: "商品不存在（notExist：编号过期 / 已下架）",
            -2: "等级不足（buyLvLimit）",
            -3: "货币不足（moneyNotEnough）",
        }
        return notes.get(int(self.errorCode), f"未知错误码 {self.errorCode}")


__all__ = [
    "ALLOWED_PACKET_IDS",
    "ALLOWED_PACKET_NAMES",
    "BuyProductPacket",
    "BuyProductRes",
    "FORBIDDEN_PACKET_IDS",
    "FORBIDDEN_PACKET_NAMES",
    "GetObjClass",
    "GetStoreListPacket",
    "GetStoreListRes",
    "ItemClass",
    "MARKET_CURRENCY_ITEM_ID",
    "MARKET_DAILY_BUY_LIMIT",
    "MARKET_ITEMS",
    "MARKET_ITEM_IDS",
    "MARKET_STORE_NAME",
    "MARKET_STORE_TYPE",
    "MARKET_SUCCESS",
    "PACKET_PURPOSE",
    "ProductClass",
]

