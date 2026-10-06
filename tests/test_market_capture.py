"""市集购买「真机抓包」的字节级回归（数据来自 ``captures/市集购买.saz``）。

【这份测试守什么】
2026-10-03 的故障现象：任务拉货架永远回 ``errorCode=-1``，四件道具一件都买不到、
连一个购买请求都不会发。根因是 ``MARKET_STORE_TYPE`` 用了**已被服务端弃用的
旧商铺号 ``398000012``**（本地 ``StoreSettingsData`` 里有两行同名「一般市集」），
而官方客户端用的是 ``398000015``。

当时手里只有"客户端错误码枚举 + 本地配置表"这两条**间接**证据，结论是靠推断
给出的；现在有了官方客户端的完整购买抓包，就把结论固化成**字节级断言** ——
而不是留在某一轮对话里。

【为什么既断言"从字节解出来的值"，又断言"我们组出来的字节"】
- ``MARKET_STORE_TYPE == 398000015``：只能证明"常量写对了"；
- ``GetStoreListPacket.for_market().encode() == 抓包请求字节``：才能证明
  **我们发出去的字节与官方客户端一模一样**（防的是字段编号/编码方式走偏）。

两者都要有，缺一个都会留下"看着对、其实不对"的空间。

【数据来源与再生成】
``protocol_samples/market_samples.py``（由 ``tools/export_saz_samples.py`` 生成，
文件头里写着原样重跑的命令）。
"""

from __future__ import annotations

import pytest

from models.market import (
    MARKET_CURRENCY_ITEM_ID,
    MARKET_DAILY_BUY_LIMIT,
    MARKET_ITEMS,
    MARKET_STORE_TYPE,
    BuyProductPacket,
    BuyProductRes,
    GetStoreListPacket,
    GetStoreListRes,
)
from models.protobuf_wire import Field, fields_by_number
from protocol_samples.market_samples import (
    MARKET04_REQUEST_HEX,
    MARKET04_RESPONSE_HEX,
    MARKET06_REQUEST_HEX,
    MARKET06_RESPONSE_HEX,
    MARKET09_REQUEST_HEX,
    MARKET09_RESPONSE_HEX,
)

#: 抓包实测值（**写死成数字**：模型解析一旦错位，这些断言立刻变红）
CAPTURED_STORE_TYPE = 398000015
CAPTURED_OLD_STORE_TYPE = 398000012
CAPTURED_PRODUCT_SID = 399001531
CAPTURED_BUY_COUNT = 5
CAPTURED_SHELF_SIZE = 36
CAPTURED_TYPE_LIST_SIZE = 28


def sub_bytes(hex_text: str) -> bytes:
    """把夹具里的十六进制字符串转成字节。"""
    return bytes.fromhex(hex_text)


def varint_field(field: Field) -> int:
    """读一条 varint 字段的数值（这里的编号都小于 2^31）。"""
    return int(field.as_int32())


class TestStoreListRequest:
    """14001 请求：我们发出去的字节必须与官方客户端**逐字节相同**。"""

    def test_constant_is_the_official_store_id(self) -> None:
        """常量必须是 398000015（官方客户端用的那个），不是旧号 398000012。"""
        assert MARKET_STORE_TYPE == CAPTURED_STORE_TYPE
        assert MARKET_STORE_TYPE != CAPTURED_OLD_STORE_TYPE

    def test_our_request_bytes_match_the_capture(self) -> None:
        """``for_market()`` 组出来的字节 == 抓包里官方客户端的请求字节。"""
        assert GetStoreListPacket.for_market().encode().hex() == MARKET06_REQUEST_HEX

    def test_capture_decodes_to_the_same_store_type(self) -> None:
        """反向也成立：解抓包字节 → ``storeType`` 就是我们的常量。"""
        request = GetStoreListPacket.decode(sub_bytes(MARKET06_REQUEST_HEX))
        assert request.storeType == MARKET_STORE_TYPE == CAPTURED_STORE_TYPE


class TestStoreTypeList:
    """14011/14012：服务端"此刻开放的商铺号"清单 —— 根因的**最终证据**。"""

    def test_request_is_an_empty_packet(self) -> None:
        """``14011 GetStoreTypeListPacket`` 没有字段：请求子包必须是空串。"""
        assert MARKET04_REQUEST_HEX == ""

    def _open_store_ids(self) -> list[int]:
        """从 14012 子包里解出全部 ``storeType``。

        元素结构 = ``{#1 商铺号, #2 int64, #3 int}``（dump.cs 的
        ``GetStoreTypeListRes`` 元素类未建模，这里用通用字段读取器取 ``#1``）。
        """
        ids: list[int] = []
        for entry in fields_by_number(sub_bytes(MARKET04_RESPONSE_HEX))[1]:
            inner = fields_by_number(entry.as_bytes())
            ids.append(varint_field(inner[1][0]))
        return ids

    def test_open_list_has_new_id_and_not_old_one(self) -> None:
        """开放清单里有 398000015、**没有** 398000012 —— 这正是当初 ``-1`` 的来源。"""
        ids = self._open_store_ids()
        assert len(ids) == CAPTURED_TYPE_LIST_SIZE
        assert CAPTURED_STORE_TYPE in ids
        assert CAPTURED_OLD_STORE_TYPE not in ids


class TestShelf:
    """14002：解真实货架字节（36 件商品），确认四件目标道具确实在架。"""

    @pytest.fixture()
    def shelf(self) -> GetStoreListRes:
        """抓包里那条**成功**的货架响应（官方客户端用 398000015 拉的）。"""
        return GetStoreListRes.decode(sub_bytes(MARKET06_RESPONSE_HEX))

    def test_error_code_is_success(self, shelf: GetStoreListRes) -> None:
        """官方客户端这条 ``errorCode=0`` —— "服务端不让买"的说法并不成立。"""
        assert shelf.errorCode == 0
        assert shelf.is_success() is True
        assert shelf.describe_error() == "成功"

    def test_shelf_shape_matches_capture(self, shelf: GetStoreListRes) -> None:
        """36 件商品、今日刷新 0 次（与抓包一致）。"""
        assert len(shelf.productList) == CAPTURED_SHELF_SIZE
        assert shelf.resetCount == 0

    def test_four_target_items_are_on_the_shelf(self, shelf: GetStoreListRes) -> None:
        """四件目标道具都在架，且全部用**金币**结算。"""
        for item_id, name in MARKET_ITEMS.items():
            product = shelf.find_product(item_id)
            assert product is not None, f"货架上没有 {name}（{item_id}）"
            assert product.currencyType == MARKET_CURRENCY_ITEM_ID
            assert 0 <= product.nowBuyCount <= product.maxBuyCount

    def test_daily_limit_matches_our_constant(self, shelf: GetStoreListRes) -> None:
        """每日限购 = 5（= ``MARKET_DAILY_BUY_LIMIT``）。

        这是"用真实字节验证本地常量"：官方客户端的购买请求里 ``count`` 正好
        等于限购上限，说明"一次买满"本身就是官方行为，而不是我们的发明。
        """
        for item_id, name in MARKET_ITEMS.items():
            product = shelf.find_product(item_id)
            assert product is not None, name
            assert product.maxBuyCount == MARKET_DAILY_BUY_LIMIT

    def test_error_text_for_old_store_id(self) -> None:
        """``-1`` 的翻译必须点名"旧商铺号"（排查时最需要的那句话）。"""
        failed = GetStoreListRes(errorCode=-1)
        assert failed.is_success() is False
        assert "dataError" in failed.describe_error()
        assert str(CAPTURED_OLD_STORE_TYPE) in failed.describe_error()


class TestBuyProduct:
    """14003/14004：官方客户端**一次买 5 件**（``count`` 允许 >1）。"""

    def test_our_request_bytes_match_the_capture(self) -> None:
        """我们组出来的购买字节 == 抓包里官方客户端的购买字节。"""
        packet = BuyProductPacket(
            productSID=CAPTURED_PRODUCT_SID, count=CAPTURED_BUY_COUNT
        )
        assert packet.encode().hex() == MARKET09_REQUEST_HEX

    def test_capture_decodes_to_product_and_count(self) -> None:
        """解抓包请求：``productSID`` 是服务端发的流水号，``count`` = 5。"""
        request = BuyProductPacket.decode(sub_bytes(MARKET09_REQUEST_HEX))
        assert request.productSID == CAPTURED_PRODUCT_SID
        assert request.count == CAPTURED_BUY_COUNT == MARKET_DAILY_BUY_LIMIT

    def test_response_reports_success_and_progress(self) -> None:
        """购买响应：成功 + 买到东西 + 花了钱 + 今日已购 5 件。"""
        response = BuyProductRes.decode(sub_bytes(MARKET09_RESPONSE_HEX))
        assert response.errorCode == 0
        assert response.describe_error() == "成功"
        assert response.nowBuyCount == CAPTURED_BUY_COUNT
        assert response.gained_items(), "抓包里应当有获得物明细"
        assert response.cost_items(), "抓包里应当有花费明细"
