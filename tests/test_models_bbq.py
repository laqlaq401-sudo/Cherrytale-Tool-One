"""宴席报文（``models/bbq.py``）的编解码单元测试。

运行方式：

    python -m pytest tests/test_models_bbq.py -q
    python -m tests.test_models_bbq

【为什么手写期望字节而不是"编码后再解码看是否相等"】
往返测试只能证明「编解码自洽」，证明不了「与服务端一致」——
自己写错一个编号，往返依然会通过。
所以关键报文这里都给出**手工推导的十六进制期望值**：
标签（tag）= 编号 << 3 | 线类型，字段值按 protobuf 规则拼。
一旦有人改了字段顺序，这些断言会立刻红。
"""

from __future__ import annotations

import pytest

from models.bbq import (
    ActivityIntervalDataClass,
    BBQFriendsPacket,
    BBQFriendsRes,
    FriendClass,
    GetBBQDataPacket,
    GetBBQDataRes,
)
from models.proto_fields import field_tags


class TestFieldNumbers:
    """字段编号必须与 dump.cs 的声明顺序一致。"""

    def test_bbq_friends_packet(self) -> None:
        assert field_tags("BBQFriendsPacket") == {"groupID": 1, "bbqFriendList": 2}

    def test_friend_class(self) -> None:
        assert field_tags("FriendClass")["playerID"] == 1
        assert field_tags("FriendClass")["get_state"] == 9

    def test_get_bbq_data_res(self) -> None:
        assert field_tags("GetBBQDataRes") == {
            "eventDeadlineID": 1,
            "activityIntervalDataList": 2,
            "bbqFriendList": 3,
        }

    def test_bbq_friends_res(self) -> None:
        assert field_tags("BBQFriendsRes") == {
            "errorCode": 1,
            "addList": 2,
            "costList": 3,
        }


class TestEncoding:
    def test_empty_request_encodes_to_nothing(self) -> None:
        """32007 是空报文：没有字段就编出 0 字节，靠 packetID 表达意图。"""
        assert GetBBQDataPacket().encode() == b""

    def test_bbq_friends_packet_bytes(self) -> None:
        """编号 1（int）+ 编号 2（★ 非 packed：每个好友独立带 tag）。

        ★ 2026-10-03 修订：10.3 付费邀请抓包里客户端发的是连续
        ``10 <varint>``（非 packed），不是 ``12 <len> ...`` —— 已对齐。
        """
        packet = BBQFriendsPacket.for_friends(group_id=1, friend_ids=[2, 3])
        # 08 01   → 字段 1 = 1
        # 10 02   → 字段 2 = 2（独立 tag）
        # 10 03   → 字段 2 = 3（独立 tag）
        assert packet.encode() == bytes.fromhex("080110021003")

    def test_for_friends_keeps_order(self) -> None:
        packet = BBQFriendsPacket.for_friends(group_id=7, friend_ids=[5, 4, 3])
        assert packet.groupID == 7
        assert packet.bbqFriendList == [5, 4, 3]


class TestDecoding:
    def test_get_bbq_data_res_reads_group_and_friends(self) -> None:
        """嵌套结构走"编码 → 解码"往返。

        .. note::
           为什么这里不做"手写期望字节"？``GetBBQDataRes`` 里嵌着两条 repeated 消息，
           手写字节既长又容易算错作者自己的错；扁平字段的字节级验证
           已经由 :meth:`TestEncoding.test_bbq_friends_packet_bytes` 覆盖。
           本用例要确认的是：**嵌套结构与列表能原样取回**。
        """
        data = GetBBQDataRes(
            eventDeadlineID=[1],
            activityIntervalDataList=[ActivityIntervalDataClass(dataNummber=1, groupID=500000001)],
            bbqFriendList=[
                FriendClass(playerID=101, name="A", get_state=1, send_state=0),
                FriendClass(playerID=202, name="B", get_state=0, send_state=1),
            ],
        )
        decoded = GetBBQDataRes.decode(data.encode())
        assert decoded.eventDeadlineID == [1]
        assert decoded.group_ids() == [500000001]
        assert decoded.friend_ids() == [101, 202]
        assert decoded.bbqFriendList[0].name == "A"

    def test_friend_state_hint_shows_raw_values(self) -> None:
        """状态含义未确认前，只允许原样展示，不允许解释。"""
        friend = FriendClass(playerID=1, send_state=2, get_state=3)
        assert friend.state_hint() == "send_state=2, get_state=3"


class TestResponseHelpers:
    def test_is_success_uses_error_code_zero(self) -> None:
        assert BBQFriendsRes(errorCode=0).is_success() is True
        assert BBQFriendsRes(errorCode=5).is_success() is False

    def test_reward_and_cost_summary(self) -> None:
        from models.daily import GetObjClass, ItemClass

        res = BBQFriendsRes(
            errorCode=0,
            addList=[GetObjClass(itemList=[ItemClass(itemID=200000002, itemAmount=30)])],
            costList=[GetObjClass(itemList=[ItemClass(itemID=200000001, itemAmount=7)])],
        )
        assert "200000002×30" in res.reward_summary()
        assert "200000001×7" in res.cost_summary()

    def test_empty_summaries(self) -> None:
        res = BBQFriendsRes()
        assert res.reward_summary() == "<无奖励>"
        assert res.cost_summary() == "<无消耗>"


class TestDataSummary:
    def test_summary_counts(self) -> None:
        data = GetBBQDataRes(
            activityIntervalDataList=[ActivityIntervalDataClass(groupID=1)],
            bbqFriendList=[FriendClass(playerID=9)],
        )
        text = data.summary()
        assert "1 段" in text
        assert "1 人" in text


if __name__ == "__main__":  # 支持 `python -m tests.test_models_bbq`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
