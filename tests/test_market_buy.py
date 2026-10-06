"""一般市集购买任务（``tasks/market_buy.py`` + ``models/market.py``）的单元测试。

运行方式：

    python -m pytest tests/test_market_buy.py -q
    python -m tests.test_market_buy

【本文件守住的五条底线】

1. **默认只对用户勾选的道具下单**（4 个开关默认全开；关掉的绝不出现在 14003 里）；
2. **买满即跳过**：``nowBuyCount >= maxBuyCount`` 时只读拉货架、不发购买请求；
3. **演练模式一个包都不发**（``--dry-run`` 的承诺是"只组装、不发送"）；
4. **出站消息号 ⊆ {14001, 14003}**：从请求字节里解出 packetID 再核对，
   而不是"相信代码写对了"；
5. **失败即停、不重试**：服务端回了非 0 错误码时，后面几种道具一次都不许再发。

响应字节全部**手工编码**（没有实测抓包），所以这里断言的是"任务自己的行为"
（发什么、发几次、怎么解读响应），不是"服务端真的会这么回"。
"""

from __future__ import annotations

import pytest

from client.game_client import GameClient
from models.envelope import parse_envelope
from models.market import (
    ALLOWED_PACKET_IDS,
    MARKET_CURRENCY_ITEM_ID,
    MARKET_ITEMS,
    MARKET_STORE_TYPE,
    BuyProductPacket,
    GetStoreListPacket,
)
from models.protobuf_wire import encode_int, encode_message
from tasks.base import create_task
from tests.test_game_client import FakeResponse
from tests.test_tasks_top_pvp import envelope_bytes, make_session, sent_packet_ids

import tasks.market_buy as market_task  # noqa: F401  导入即注册

#: 两个消息号（写死成常量，任何改动都会让测试红）
STORE_LIST_ID = 14001
BUY_PRODUCT_ID = 14003

#: 本任务允许出现的出站消息号（安全边界）
ALLOWED_IDS = set(ALLOWED_PACKET_IDS.values())

#: 四种道具（顺序与 ``MARKET_ITEMS`` 一致）
BLOOD, AMBER, CHOCOLATE, FRAGRANCE = tuple(MARKET_ITEMS)


# ---------------------------------------------------------------------------
# 离线替身与字节构造
# ---------------------------------------------------------------------------
def patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成假的，记录每个出站请求体。

    :param handler: ``(消息号, 请求体) -> 响应字节``。**故意不给默认实现**：
        真发一条没被预期的包时应当直接炸掉（``AssertionError``）。
    """
    recorded: list[bytes] = []

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        recorded.append(body)
        number = parse_envelope(body).packet_id
        return FakeResponse(handler(number, body))

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def product_payload(
    product_sid: int, item_id: int, now: int, top: int, price: int = 500
) -> bytes:
    """``ProductClass`` 的子字节（编号来自 ``models/proto_fields.py``）。

    ``productSID``(1) / ``itemID``(2) / ``nowBuyCount``(4) / ``maxBuyCount``(5) /
    ``currencyType``(6) / ``price``(7)
    """
    return (
        encode_int(1, product_sid)
        + encode_int(2, item_id)
        + encode_int(4, now)
        + encode_int(5, top)
        + encode_int(6, MARKET_CURRENCY_ITEM_ID)
        + encode_int(7, price)
    )


def store_payload(products: list[bytes], error_code: int = 0) -> bytes:
    """``GetStoreListRes`` 的子字节：``errorCode``(1) + ``productList``(2) 重复。"""
    payload = encode_int(1, error_code)
    for item in products:
        payload += encode_message(2, item)
    return payload


def buy_payload(
    error_code: int = 0, gained_item: int = 0, gained_amount: int = 0, now: int = 0
) -> bytes:
    """``BuyProductRes`` 子字节：``errorCode``(1) + ``itemList``(2) + ``nowBuyCount``(4)。"""
    payload = encode_int(1, error_code)
    if gained_item:
        inner = (
            encode_int(1, 0)
            + encode_int(2, gained_item)
            + encode_int(3, gained_amount)
        )
        payload += encode_message(2, encode_message(1, inner))
    payload += encode_int(4, now)
    return payload


def default_shelf(*, now: int = 0, top: int = 5) -> list[bytes]:
    """默认货架：四种道具都在架上（默认今日 0/5，都需要补货）。"""
    return [
        product_payload(399000101 + index, item_id, now, top)
        for index, item_id in enumerate(MARKET_ITEMS)
    ]


def make_handler(
    *,
    shelf: list[bytes] | None = None,
    store_error: int = 0,
    buy_error: int = 0,
    buy_now: int = 5,
):
    """构造一个"像真服务端"的响应分发器（不认识的包会 AssertionError）。"""
    products = default_shelf() if shelf is None else shelf
    buys: list[BuyProductPacket] = []

    def handler(number: int, body: bytes) -> bytes:
        if number == STORE_LIST_ID:
            return envelope_bytes(14002, store_payload(products, store_error))
        if number == BUY_PRODUCT_ID:
            request = BuyProductPacket.decode(parse_envelope(body).sub_packet_bytes)
            buys.append(request)
            return envelope_bytes(
                14004,
                buy_payload(
                    error_code=buy_error,
                    gained_item=request.productSID,
                    gained_amount=request.count,
                    now=buy_now,
                ),
            )
        raise AssertionError(f"任务发了一个不该发的包：{number}")

    handler.buys = buys  # type: ignore[attr-defined]
    return handler


def make_task(**kwargs: object):
    """构造市集购买任务（四个开关默认全开，与规格表一致）。"""
    return create_task("market_buy", make_session(), **kwargs)


def buy_requests(recorded: list[bytes]) -> list[BuyProductPacket]:
    """从记录下来的请求里挑出 ``14003``，并解成模型（断言商品与数量用）。"""
    packets: list[BuyProductPacket] = []
    for body in recorded:
        view = parse_envelope(body)
        if view.packet_id == BUY_PRODUCT_ID:
            packets.append(BuyProductPacket.decode(view.sub_packet_bytes))
    return packets


# ---------------------------------------------------------------------------
# ① 只读：拉货架
# ---------------------------------------------------------------------------
class TestStoreList:
    def test_request_carries_market_store_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """14001 里的 ``storeType`` 必须是一般市集 398000015（**不是** 398000012）。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task().run()

        assert result.ok is True
        first = parse_envelope(recorded[0])
        assert first.packet_id == STORE_LIST_ID
        request = GetStoreListPacket.decode(first.sub_packet_bytes)
        assert request.storeType == MARKET_STORE_TYPE == 398000015

    def test_store_error_is_reported_without_buying(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """拉货架失败（errorCode != 0）→ 直接失败，且**一次 14003 都不发**。

        失败信息必须**可定位**：带上正在请求的 ``storeType`` 与错误码的人话翻译
        —— 2026-10-03 那次排查就是被一个裸 ``-1`` 卡住的（真实的铺子号写错了，
        服务端回 ``dataError`` 而不是 ``notExist``）。
        """
        recorded = patch_transport(monkeypatch, make_handler(store_error=-2))
        result = make_task().run()

        assert result.ok is False
        assert "errorCode=-2" in result.message
        assert f"storeType={MARKET_STORE_TYPE}" in result.message
        assert "notExist" in result.message
        assert result.data["error_text"] == "商铺不存在（notExist）"
        assert result.data["store_type"] == MARKET_STORE_TYPE
        assert sent_packet_ids(recorded) == [STORE_LIST_ID]

    def test_store_data_error_says_old_store_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``-1``（``dataError``）必须翻译成"旧商铺号"的提示，而不是含糊的失败。

        这是**回归护栏**：官方客户端用 398000015 能拉到货架，而 398000012
        只会拿到 ``-1``；一旦有人把常量改回旧号，日志里要能立刻看出来。
        """
        recorded = patch_transport(monkeypatch, make_handler(store_error=-1))
        result = make_task().run()

        assert result.ok is False
        assert "dataError" in result.message
        assert "398000012" in result.message  # 提示里点名那个典型旧号
        assert sent_packet_ids(recorded) == [STORE_LIST_ID]


# ---------------------------------------------------------------------------
# ② 购买：剩余量 / 买满跳过 / 开关 / 失败即停
# ---------------------------------------------------------------------------
class TestBuying:
    def test_buys_all_four_remaining(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认（全勾选、都没买满）→ 四种各发一次 14003，数量 = 剩余可购量。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task().run()

        assert result.ok is True
        assert sent_packet_ids(recorded) == [STORE_LIST_ID] + [BUY_PRODUCT_ID] * 4
        assert [packet.count for packet in buy_requests(recorded)] == [5, 5, 5, 5]
        assert result.data["mode"] == "buy"

    def test_remaining_is_top_minus_now(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """剩余可购量 = ``maxBuyCount - nowBuyCount``（今日买过 3 件 → 只补 2 件）。"""
        shelf = [product_payload(399000101, BLOOD, 3, 5)]
        recorded = patch_transport(monkeypatch, make_handler(shelf=shelf))
        result = make_task(
            buy_amber=False, buy_chocolate=False, buy_fragrance=False
        ).run()

        assert result.ok is True
        assert [packet.count for packet in buy_requests(recorded)] == [2]

    def test_sold_out_items_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """已买满（now == max）→ 只读拉货架，一个 14003 都没有。"""
        shelf = [product_payload(399000101, BLOOD, 5, 5)]
        recorded = patch_transport(monkeypatch, make_handler(shelf=shelf))
        result = make_task(
            buy_amber=False, buy_chocolate=False, buy_fragrance=False
        ).run()

        assert result.ok is True
        assert result.data["mode"] == "nothing_to_buy"
        assert sent_packet_ids(recorded) == [STORE_LIST_ID]

    def test_unchecked_items_are_never_bought(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只勾巧克力：另外三种一次都不许出现在 14003 里。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(
            buy_blood_diamond=False, buy_amber=False, buy_fragrance=False
        ).run()

        assert result.ok is True
        requests = buy_requests(recorded)
        assert len(requests) == 1
        assert requests[0].count == 5
        # 巧克力在货架上的 productSID 是 399000103（default_shelf 的第三个）
        assert requests[0].productSID == 399000103

    def test_all_switches_off_is_read_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """四个开关全关 → 只拉货架（只读），并把货架现状报出来。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(
            buy_blood_diamond=False,
            buy_amber=False,
            buy_chocolate=False,
            buy_fragrance=False,
        ).run()

        assert result.ok is True
        assert result.data["mode"] == "read_only"
        assert len(result.data["shelf"]) == len(MARKET_ITEMS)
        assert sent_packet_ids(recorded) == [STORE_LIST_ID]

    def test_missing_product_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """货架上没有这件商品（被轮换下架）→ 跳过它，其余照常买。"""
        shelf = [product_payload(399000101, BLOOD, 0, 5)]
        recorded = patch_transport(monkeypatch, make_handler(shelf=shelf))
        result = make_task().run()

        assert result.ok is True
        # 只有血钻在架子上 → 只买血钻
        assert [packet.productSID for packet in buy_requests(recorded)] == [399000101]

    def test_server_error_stops_and_does_not_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """14004 报错 → 立刻停手：后面几种道具一次都不许再发（有副作用的包绝不重试）。"""
        recorded = patch_transport(monkeypatch, make_handler(buy_error=-3))
        result = make_task().run()

        assert result.ok is False
        assert "moneyNotEnough" in result.message
        assert sent_packet_ids(recorded) == [STORE_LIST_ID, BUY_PRODUCT_ID]

    def test_buy_request_has_exactly_two_fields(self) -> None:
        """``BuyProductPacket`` 的字段就是 ``productSID`` + ``count``，别的一律没有。"""
        packet = BuyProductPacket(productSID=399000101, count=3)
        assert packet.encode(include_defaults=False) == (
            encode_int(1, 399000101) + encode_int(2, 3)
        )



# ---------------------------------------------------------------------------
# ③ 演练：一个包都不发
# ---------------------------------------------------------------------------
class TestDryRun:
    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--dry-run`` 只打印计划：**联网的请求一个都没有**。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(dry_run=True).run()

        assert recorded == []
        assert result.ok is True
        assert result.data["mode"] == "dry_run"
        assert result.data["sent_packets"] == ()
        assert result.data["store_type"] == 398000015
        # 默认四个开关全开 → 演练应列出四件目标
        assert len(result.data["enabled_items"]) == 4

    def test_dry_run_respects_switches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """演练也要如实反映勾选：只勾琥珀时只列一件。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(
            dry_run=True, buy_blood_diamond=False, buy_chocolate=False, buy_fragrance=False
        ).run()

        assert recorded == []
        assert [item["item_id"] for item in result.data["enabled_items"]] == [200200003]


# ---------------------------------------------------------------------------
# ④ 安全边界：白名单与常量
# ---------------------------------------------------------------------------
def test_allowed_packet_ids_are_exactly_two() -> None:
    """白名单只有"拉货架"与"购买"两个包；刷新货架（14007）在禁发清单里。"""
    from models.market import FORBIDDEN_PACKET_IDS

    assert ALLOWED_PACKET_IDS == {
        "GetStoreListPacket": 14001,
        "BuyProductPacket": 14003,
    }
    assert FORBIDDEN_PACKET_IDS["StoreResetPacket"] == 14007
    assert set(ALLOWED_PACKET_IDS) & set(FORBIDDEN_PACKET_IDS) == set()


def test_currency_is_coins_not_diamonds() -> None:
    """一般市集用金币结算（``StoreSettingsData.currencyIcon`` = 200000050）。"""
    assert MARKET_CURRENCY_ITEM_ID == 200000050


def test_packets_stay_inside_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    """真跑时发出的每一个包都必须在白名单里（安全边界）。"""
    recorded = patch_transport(monkeypatch, make_handler())
    make_task().run()
    assert set(sent_packet_ids(recorded)) <= ALLOWED_IDS


def test_item_names_match_config_table() -> None:
    """四种道具的 itemID ↔ 名字写死成回归断言（改错编号会在这里红掉）。"""
    assert MARKET_ITEMS == {
        200200000: "血钻",
        200200003: "琥珀",
        200600001: "爱心巧克力",
        200600002: "幸福香氛",
    }

