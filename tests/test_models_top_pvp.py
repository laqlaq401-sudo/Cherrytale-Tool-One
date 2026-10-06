"""荣耀之巅报文（``models/top_pvp.py``）的编解码单元测试。

运行方式：

    python -m pytest tests/test_models_top_pvp.py -q
    python -m tests.test_models_top_pvp

【本文件钉死的三件事】

1. **字段编号 = dump.cs 的声明顺序**（``isSkipBattle`` 必须是第 16 个字段 ——
   它一旦错位，"跳过战斗演出"开关就会被写到别的字段上，而服务端只会回一句
   "数据错误"，极难排查）；
2. **空请求真的是 0 字节**（39001 / 39007 靠 ``RootPacket.packetID`` 表达意图）；
3. **禁止发出的消息号不在白名单里**（``39005`` 花钻石买次数 —— 本项目的红线）。
"""

from __future__ import annotations

import pytest

from models.packet_ids import packet_id
from models.proto_fields import field_tags
from models.protobuf_wire import encode_int, encode_string
from models.top_pvp import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    TOP_PVP_ERROR_NO_CHALLENGE_TIMES,
    GetTopPvpHomePacket,
    GetTopPvpHomeRes,
    TopPvpChallengePacket,
    TopPvpChallengeRes,
    TopPvpSetSkipBattleTogglePacket,
    TopPvpSetSkipBattleToggleRes,
    describe_error_code,
)


# ---------------------------------------------------------------------------
# ① 字段编号
# ---------------------------------------------------------------------------
class TestFieldNumbers:
    """字段编号必须与 dump.cs 的声明顺序一致（其余一切结论都建立在这上面）。"""

    def test_home_res_field_order(self) -> None:
        tags = field_tags("GetTopPvpHomeRes")
        assert tags["errorCode"] == 1
        assert tags["buyChallengeCount"] == 2
        assert tags["classRankClassList"] == 4
        assert tags["allRankClassList"] == 5
        # ★ 最关键的一条：跳过战斗演出开关就是第 16 个字段
        assert tags["isSkipBattle"] == 16
        assert len(tags) == 16

    def test_rank_class_field_order(self) -> None:
        assert field_tags("TopPvpRankClass") == {
            "ranking": 1,
            "iconData": 2,
            "rankPoing": 3,
            "rankClassID": 4,
            "isMy": 5,
        }

    def test_versus_info_field_order(self) -> None:
        assert field_tags("TopPvpVersusInfoClass") == {
            "point": 1,
            "mirrorBuffID": 2,
            "flag": 3,
            "beforeClassID": 4,
            "afterClassID": 5,
            "pointChange": 6,
            "winningStreakPoint": 7,
        }

    def test_toggle_packet_has_single_boolean(self) -> None:
        assert field_tags("TopPvpSetSkipBattleTogglePacket") == {"isSkipBattle": 1}

    def test_challenge_res_field_order(self) -> None:
        tags = field_tags("TopPvpChallengeRes")
        assert tags["errorCode"] == 1
        assert tags["gotList"] == 2
        assert tags["rewardList"] == 3
        assert tags["versusInfo_Me"] == 4
        # 注意大小写：协议里就是小写 i 的 versusinfo_Enemy
        assert tags["versusinfo_Enemy"] == 5
        assert tags["replayResult"] == 6

    def test_empty_requests_have_no_fields(self) -> None:
        """39001 / 39007 都是空报文（请求体 0 字节）。"""
        assert field_tags("GetTopPvpHomePacket") == {}
        assert field_tags("TopPvpChallengePacket") == {}
        assert field_tags("TopPvpSetSkipBattleToggleRes") == {"errorCode": 1}

    def test_team_edit_info_field_order(self) -> None:
        """39011/39012：挑战前必发的那一步（实测 sectionID=726010017, actionType=0）。"""
        assert field_tags("TopTeamEditInfoPacket") == {"sectionID": 1, "actionType": 2}
        assert field_tags("TopTeamEditInfoRes") == {
            "roleRankList": 1,
            "lastUpdateTime": 2,
            "defTeamClass": 3,
        }


# ---------------------------------------------------------------------------
# ② 编码
# ---------------------------------------------------------------------------
class TestEncoding:
    def test_empty_requests_encode_to_nothing(self) -> None:
        """空报文编码成 0 字节 —— 服务端靠 packetID 识别意图。"""
        assert GetTopPvpHomePacket().encode() == b""
        assert TopPvpChallengePacket().encode() == b""

    def test_toggle_encodes_as_field1_true(self) -> None:
        """39019 的字节只有一个字段：``08 01``（字段 1 = varint 1）。"""
        assert TopPvpSetSkipBattleTogglePacket.enable().encode() == bytes.fromhex("0801")

    def test_toggle_default_is_enabled(self) -> None:
        """默认值就是"开启跳过演出" —— 需求要求默认开启。"""
        assert TopPvpSetSkipBattleTogglePacket().isSkipBattle is True


# ---------------------------------------------------------------------------
# ③ 解码
# ---------------------------------------------------------------------------
class TestDecoding:
    def test_home_res_reads_skip_battle_flag(self) -> None:
        """手工拼的 39002：errorCode=0、isSkipBattle=1 → 两者都要能读出来。"""
        payload = encode_int(1, 0) + encode_int(16, 1)
        res = GetTopPvpHomeRes.decode(payload)
        assert res.errorCode == 0
        assert res.isSkipBattle is True
        assert res.is_success() is True

    def test_home_res_defaults_when_field_missing(self) -> None:
        """服务端省略字段是常态：缺字段应回落到默认值（而不是报错）。"""
        res = GetTopPvpHomeRes.decode(encode_int(1, 0))
        assert res.isSkipBattle is False
        assert res.classRankClassList == []
        assert res.my_rank() is None
        assert "已购次数" in res.summary()

    def test_challenge_res_parses_replay_summary(self) -> None:
        """39008 的战报是 46KB JSON —— 只取几个键，且要能拿到胜负。"""
        replay = (
            '{"elapsed":1,"step":418,"winOrFail":true,'
            '"replay_file_name":"A_B","isNpc":false,"error":false,"big":"'
            + "x" * 500
            + '"}'
        )
        res = TopPvpChallengeRes.decode(encode_int(1, 0) + encode_string(6, replay))
        summary = res.battle_summary()
        assert summary["winOrFail"] is True
        assert summary["step"] == 418
        assert summary["replay_file_name"] == "A_B"
        # 大字段不该被带进摘要（避免日志被 46KB 撑爆）
        assert "big" not in summary
        assert "本场=胜" in res.summary()

    def test_broken_replay_json_does_not_raise(self) -> None:
        """战报解不动只是"展示信息缺失"，绝不能把整场判成失败。"""
        res = TopPvpChallengeRes.decode(
            encode_int(1, 0) + encode_string(6, "{不是合法 JSON")
        )
        assert res.battle_summary() == {}
        assert "本场=未知" in res.summary()


# ---------------------------------------------------------------------------
# ④ 白名单 / 禁发清单（"绝不买次数"的第一道防线）
# ---------------------------------------------------------------------------
class TestPacketWhitelist:
    def test_allowed_ids(self) -> None:
        assert ALLOWED_PACKET_IDS == {
            "GetTopPvpHomePacket": 39001,
            "TopPvpSetSkipBattleTogglePacket": 39019,
            "TopTeamEditInfoPacket": 39011,
            "TopPvpChallengePacket": 39007,
            "ReceiveTopPvpRewardPacket": 39003,
        }

    def test_buy_packet_is_forbidden(self) -> None:
        assert FORBIDDEN_PACKET_IDS["TopPvpBuyChallengeCountPacket"] == 39005

    def test_whitelist_and_forbidden_do_not_overlap(self) -> None:
        """两个集合若有交集，说明白名单写错了 —— 那是"会买次数"的隐患。"""
        overlap = set(ALLOWED_PACKET_IDS) & set(FORBIDDEN_PACKET_IDS)
        assert overlap == set()

    def test_whitelist_matches_generated_packet_ids(self) -> None:
        """白名单里的名字必须真的存在于消息号表，且号码一致。"""
        for name, number in ALLOWED_PACKET_IDS.items():
            assert packet_id(name) == number


# ---------------------------------------------------------------------------
# ⑤ 错误码说明
# ---------------------------------------------------------------------------
class TestErrorCodes:
    def test_challenge_times_not_enough_is_minus_one(self) -> None:
        """-1 是"次数不足"，是循环的**正常终止条件**（不是异常）。"""
        assert TOP_PVP_ERROR_NO_CHALLENGE_TIMES == -1
        assert "绝不购买" in describe_error_code(-1)

    def test_unknown_code_is_reported_as_is(self) -> None:
        assert "777" in describe_error_code(777)


if __name__ == "__main__":  # pragma: no cover - 便于直接运行本文件
    raise SystemExit(pytest.main([__file__, "-q"]))