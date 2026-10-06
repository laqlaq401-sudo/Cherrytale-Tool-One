"""背包 / 货币报文（消息号 2007 → 2008）：**只读**取金币与钻石数量。

【为什么"货币"要从背包里读】
游戏把资源都做成了道具：``models/const_ids.py`` 里写着
``COINS_ID = 200000050``（金币）、``DIAMOND_ID = 200000001``（钻石）、
体力 ``ENERGY_ID = 200000002``。所以"我有多少金币/钻石"= 背包里这两个 itemID
的 ``itemAmount``。

【为什么能直接复用 ``ItemClass``】
``GetAllItemRes(2008)`` 只有一个字段 ``itemList``，元素结构与
``GetObjClass.itemList`` 同型（``itemSid`` / ``itemID`` / ``itemAmount``）。
所以这里**复用** :class:`models.daily.ItemClass`，不再猜一遍字段编号 ——
"同一个结构建两份模型"正是字段错位的经典来源。

.. warning::
   本模块只建模**只读**的 2007 / 2008。以下报文刻意不建模，让"改背包 / 花资源"
   在类型层面就不存在：

   - ``2001 UseItemPacket``（使用道具）
   - ``2005 MixItemsPacket``（合成道具）
   - ``2011 HarvestDreamStonePacket``（挖钻石，属于有次数的产出行为）
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from models.const_ids import CONSTS
from models.daily import ItemClass
from models.game_packet import ProtoMessage

#: 本任务允许发出的报文（类名 → 消息号）；``guarded_send`` 用它做白名单校验。
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "GetAllItemPacket": 2007,
}

#: 明确禁发：任何会改动背包的包。留在这里是为了让"越界"在**运行期**立刻报错，
#: 而不是等某天有人顺手 import 了一个写包才被发现。
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "UseItemPacket": 2001,  # 使用道具
    "MixItemsPacket": 2005,  # 合成道具
    "HarvestDreamStonePacket": 2011,  # 挖钻石
}

#: ``{消息号: 说明}``（会写进 DEBUG 日志与演练输出，排查时一眼看清发了什么）
PACKET_PURPOSE: Final[dict[int, str]] = {
    2007: "拉取背包全量列表（只读；金币/钻石/体力都在里面）",
    2008: "背包全量响应",
}


class GetAllItemPacket(ProtoMessage):
    """请求背包全量列表（**空报文**，只读，不消耗任何东西）。"""

    proto_class_name = "GetAllItemPacket"


class GetAllItemRes(ProtoMessage):
    """背包全量列表（响应）。"""

    proto_class_name = "GetAllItemRes"

    #: 全部道具（含金币 200000050 / 钻石 200000001 / 体力 200000002）
    itemList: list[ItemClass] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # 取值辅助（一律按 itemID 找，不依赖列表顺序）
    # ------------------------------------------------------------------
    def amount_of(self, item_id: int) -> int:
        """某个 ``itemID`` 的数量；背包里没有它时返回 ``0``。

        【为什么用 ``sum`` 而不是取第一条命中】
        同一 itemID 在背包里可能有多条（例如不同 ``itemSid`` 的实例）。
        只取第一条会**少算**，而"少算资源"会让用户以为自己的钻石凭空不见了。
        """
        wanted = int(item_id)
        return sum(
            int(item.itemAmount) for item in self.itemList if int(item.itemID) == wanted
        )

    def coins(self) -> int:
        """金币（``COINS_ID`` = 200000050）。"""
        return self.amount_of(int(CONSTS["COINS_ID"]))

    def diamonds(self) -> int:
        """钻石（``DIAMOND_ID`` = 200000001）。"""
        return self.amount_of(int(CONSTS["DIAMOND_ID"]))

    def stamina(self) -> int:
        """体力（``ENERGY_ID`` = 200000002）。"""
        return self.amount_of(int(CONSTS["ENERGY_ID"]))

    def summary(self) -> str:
        """一行摘要（日志与界面共用同一句措辞，避免两处说法不一致）。"""
        return (
            f"金币 {self.coins()}，钻石 {self.diamonds()}，体力 {self.stamina()}"
            f"（背包共 {len(self.itemList)} 条）"
        )
