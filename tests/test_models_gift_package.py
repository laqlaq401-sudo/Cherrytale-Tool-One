"""特惠礼包报文（``models/gift_package.py``）的编解码单元测试。

运行方式：

    python -m pytest tests/test_models_gift_package.py -q
    python -m tests.test_models_gift_package

【本文件要钉死的两条业务规则】
1. "免费礼包"只认 ``isFree != 0``，**不能**用 ``price == "0"`` 判断
   （价格是字符串，免费礼包的 price 未必是 "0"）；
2. ``GiftInfoClass`` 的 43 个字段必须**全部声明** ——
   少一个，``ProtoMessage.encode`` 就会直接报错（见 ``models/game_packet.py``）。
"""

from __future__ import annotations

import typing

import pytest

from models.gift_package import (
    GetFreeGiftPacket,
    GetFreeGiftPacketRes,
    GetGiftListPacket,
    GetGiftListRes,
    GiftInfoClass,
    GiftType,
)
from models.proto_fields import field_tags
from protocol_samples.gift_list_samples import GIFT_LIST_SLIM_HEX


class TestFieldNumbers:
    def test_get_gift_list_packet(self) -> None:
        assert field_tags("GetGiftListPacket") == {
            "platform": 1,
            "cashType": 2,
            "channelType": 3,
            "giftType": 4,
        }

    def test_no_forward_ref_annotation(self) -> None:
        """★ 2026-10-03 回归钉子：giftList 注解必须是**已解析**的 list[GiftInfoClass]。

        曾经写成 ``list["GiftInfoClass"]``（类定义在后），pydantic 延迟构建
        让解码器拿到 ForwardRef、首次解码真实 37002 走 bytes 兜底直接崩溃
        （服务端回空列表时从不触发，潜伏至今）。这条断言在 import 后立即
        检查，与本故障的触发顺序（每进程首次解码）无关，直接钉死这类 bug。
        """
        assert GetGiftListRes.__pydantic_complete__ is True
        annotation = GetGiftListRes.model_fields["giftList"].annotation
        assert not isinstance(annotation, typing.ForwardRef)
        assert annotation is not None and typing.get_origin(annotation) is list

    def test_free_gift_packet(self) -> None:
        assert field_tags("GetFreeGiftPacket") == {"giftId": 1}
        assert field_tags("GetFreeGiftPacketRes") == {"errorCode": 1, "gotList": 2}

    def test_gift_info_key_fields(self) -> None:
        tags = field_tags("GiftInfoClass")
        assert tags["giftID"] == 2
        assert tags["buyCount"] == 5
        assert tags["buyLimit"] == 18
        assert tags["isFree"] == 26

    def test_gift_info_declares_every_field(self) -> None:
        """模型必须与字段表一一对应（缺一个就无法编码）。"""
        assert set(field_tags("GiftInfoClass")) == set(GiftInfoClass.model_fields)


class TestEncoding:
    def test_list_packet_bytes(self) -> None:
        """四个 int 字段全部发送（含 0）：手工期望字节。"""
        packet = GetGiftListPacket.for_normal_store(platform=2)
        # 08 02        → platform = 2
        # 10 00 / 18 00 / 20 00 → cashType / channelType / giftType 都是 0
        assert packet.encode() == bytes.fromhex("0802100018002000")

    def test_list_packet_bytes_matches_1003_capture(self) -> None:
        """★ 2026-10-03：10.3 安卓抓包的 37001 请求子包逐字节钉死（8 B）。

        真实客户端发 ``{platform=4, cashType=7, channelType=2, giftType=0}``，
        拿到 216 个礼包；旧组合 ``{2, 0, 0}`` 只会拿到空列表 ——
        "免费钻石/扫荡券领不到"的直接原因（见 config.GIFT_STORE_* 三个常量）。
        """
        packet = GetGiftListPacket.for_normal_store(platform=4, cash_type=7, channel_type=2)
        assert packet.encode() == bytes.fromhex("0804100718022000")

    def test_gift_type_is_normal(self) -> None:
        packet = GetGiftListPacket.for_normal_store(platform=9)
        assert packet.giftType == int(GiftType.NORMAL) == 0

    def test_free_gift_packet_bytes(self) -> None:
        assert GetFreeGiftPacket.for_gift(5).encode() == bytes.fromhex("0805")


class TestRealCaptureDecode:
    """真实 37002 响应的首次解码（slim 样本，见 fixtures/gift_list_samples.py）。"""

    def test_first_decode_succeeds_with_typed_elements(self) -> None:
        """★ 故障模式钉死：首次 decode 即成功，且每个元素都是 GiftInfoClass。

        故障发生时这条路径返回的是 raw bytes（ForwardRef 兜底），
        随后的 pydantic 校验报 ``list_type``—— 本测试在干净解码路径上直接断红。
        """
        res = GetGiftListRes.decode(bytes.fromhex(GIFT_LIST_SLIM_HEX))
        assert len(res.giftList) == 27
        assert all(isinstance(gift, GiftInfoClass) for gift in res.giftList)

    def test_first_element_matches_capture(self) -> None:
        """抓包实测首元素：sid=17、giftID=796000031、price="700"。"""
        res = GetGiftListRes.decode(bytes.fromhex(GIFT_LIST_SLIM_HEX))
        first = res.giftList[0]
        assert (first.sid, first.giftID, first.price) == (17, 796000031, "700")

    def test_free_gifts_from_capture(self) -> None:
        """slim 样本含全部 7 个免费礼包；每日免费钻石/赠礼可领（0/1）。"""
        res = GetGiftListRes.decode(bytes.fromhex(GIFT_LIST_SLIM_HEX))
        free_ids = {gift.giftID for gift in res.free_gifts()}
        assert {796010186, 796012481} <= free_ids, "每日免费赠礼 + 每日免费钻石"
        by_id = {gift.giftID: gift for gift in res.giftList}
        assert by_id[796012481].name == "每日免费钻石"
        assert by_id[796012481].buyCount == 0 and by_id[796012481].buyLimit == 1
        # 已领完的免费礼包也在列表里（isFree=1 但 1/1）
        assert by_id[796014087].buyCount == 1


class TestFreeFilter:
    def test_free_gifts_filters_by_flag(self) -> None:
        res = GetGiftListRes(
            giftList=[
                GiftInfoClass(giftID=1, isFree=0, price="6.99"),
                GiftInfoClass(giftID=2, isFree=1, name="每日免费钻石"),
                GiftInfoClass(giftID=3, isFree=1, name="每日免费扫荡券"),
            ]
        )
        assert [gift.giftID for gift in res.free_gifts()] == [2, 3]

    def test_price_zero_is_not_enough(self) -> None:
        """``price`` 为 "0" 但 ``isFree=0`` 时不算免费 —— 免费标记才算数。"""
        gift = GiftInfoClass(giftID=1, price="0", isFree=0)
        assert gift.is_free() is False


class TestClaimable:
    def test_free_and_below_limit(self) -> None:
        gift = GiftInfoClass(giftID=1, isFree=1, buyCount=0, buyLimit=1)
        assert gift.can_claim() is True

    def test_reached_limit(self) -> None:
        gift = GiftInfoClass(giftID=1, isFree=1, buyCount=1, buyLimit=1)
        assert gift.can_claim() is False

    def test_non_positive_limit_means_unlimited(self) -> None:
        gift = GiftInfoClass(giftID=1, isFree=1, buyCount=9, buyLimit=0)
        assert gift.can_claim() is True

    def test_paid_gift_is_never_claimable_here(self) -> None:
        assert GiftInfoClass(giftID=1, isFree=0, buyCount=0, buyLimit=5).can_claim() is False


class TestSummary:
    def test_free_gift_summary(self) -> None:
        text = GiftInfoClass(giftID=77, isFree=1, name="每日免费", buyCount=0, buyLimit=1).summary()
        assert "#77" in text
        assert "免费" in text
        assert "0/1" in text

    def test_paid_gift_summary_shows_price(self) -> None:
        text = GiftInfoClass(giftID=88, isFree=0, price="9.99", buyLimit=0).summary()
        assert "9.99" in text
        assert "∞" in text


class TestFreeGiftResponse:
    def test_success_and_reward_summary(self) -> None:
        from models.daily import GetObjClass, ItemClass

        res = GetFreeGiftPacketRes(
            errorCode=0,
            gotList=[
                GetObjClass(
                    itemList=[
                        ItemClass(itemID=200000001, itemAmount=60),
                        ItemClass(itemID=200000016, itemAmount=2),
                    ]
                )
            ],
        )
        assert res.is_success() is True
        text = res.reward_summary()
        assert "200000001×60" in text
        assert "200000016×2" in text

    def test_empty_summary(self) -> None:
        assert GetFreeGiftPacketRes().reward_summary() == "<无道具>"
        assert GetFreeGiftPacketRes(errorCode=3).is_success() is False


if __name__ == "__main__":  # 支持 `python -m tests.test_models_gift_package`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
