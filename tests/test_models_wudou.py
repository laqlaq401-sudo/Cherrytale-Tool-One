"""天命对决报文（``models/wudou.py``）的编解码与安全红线单元测试。

运行方式：

    python -m pytest tests/test_models_wudou.py -q
    python -m tests.test_models_wudou
"""

from __future__ import annotations

import pytest

from models.packet_ids import packet_id
from models.proto_fields import field_tags
from models.wudou import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    WUDOU_ERROR_NO_CHALLENGE_TIMES,
    WUDOU_SUCCESS,
    PVPListClass,
    WuDouChallengeHome,
    WuDouChallengeHomeRes,
    WuDouHome,
    WuDouHomeRes,
    WuDouRankPacket,
    WuDouRankRes,
    WuDouReplayPacket,
    WuDouReplayRes,
    describe_error_code,
)


class TestFieldNumbers:
    """字段编号必须与 dump.cs 的声明顺序一致。"""

    def test_wudou_home_fields(self) -> None:
        assert field_tags("WuDouHome") == {"groupID": 1}

    def test_wudou_home_res_fields(self) -> None:
        tags = field_tags("WuDouHomeRes")
        assert tags["weekRanking"] == 1
        assert tags["team"] == 2
        assert tags["topList"] == 3
        assert tags["wudouGroupListClz"] == 4
        assert tags["myGroupID"] == 5
        assert tags["challenge_times"] == 6

    def test_wudou_challenge_home_fields(self) -> None:
        assert field_tags("WuDouChallengeHome") == {"openTest": 1}

    def test_wudou_challenge_home_res_fields(self) -> None:
        tags = field_tags("WuDouChallengeHomeRes")
        assert tags["myRanking"] == 1
        assert tags["challengeList"] == 2
        assert tags["reflash_time"] == 3
        assert tags["challenge_time"] == 4
        assert tags["npc_times"] == 5
        assert tags["challenge_times"] == 6
        assert tags["buyedCount"] == 7
        assert tags["settlement_time"] == 8

    def test_pvp_list_class_fields(self) -> None:
        tags = field_tags("PVPListClass")
        assert tags["playerID"] == 1
        assert tags["ranking"] == 2
        assert tags["icon"] == 3
        assert tags["name"] == 4
        assert tags["fighting"] == 5
        assert tags["integral"] == 6
        assert tags["allianceName"] == 7
        assert tags["worship"] == 8
        assert tags["allianceTitle"] == 9

    def test_wudou_rank_packet_fields(self) -> None:
        tags = field_tags("WuDouRankPacket")
        assert tags["targetPid"] == 1
        assert tags["myRanking"] == 2
        assert tags["targetRanking"] == 3
        assert tags["isBlock"] == 4

    def test_wudou_rank_res_fields(self) -> None:
        assert field_tags("WuDouRankRes") == {"errorCode": 1}

    def test_wudou_replay_packet_fields(self) -> None:
        tags = field_tags("WuDouReplayPacket")
        assert tags["targetPid"] == 1
        assert tags["teamID"] == 2
        assert tags["myRanking"] == 3
        assert tags["targetRanking"] == 4

    def test_wudou_replay_res_fields(self) -> None:
        tags = field_tags("WuDouReplayRes")
        assert tags["errorCode"] == 1
        assert tags["replayResult"] == 2
        assert tags["challenge_time"] == 3
        assert tags["myRanking"] == 4
        assert tags["npc_times"] == 5
        assert tags["challenge_times"] == 6
        assert tags["gotList"] == 7
        assert tags["winRewardList"] == 8


class TestSafetyLists:
    """白名单、禁发清单与消息号校验。"""

    def test_allowed_packets_exist_in_dump(self) -> None:
        """白名单中的类名必须存在于 dump 生成的消息号表中，且号完全一致。"""
        for cls_name, expected_id in ALLOWED_PACKET_IDS.items():
            actual_id = packet_id(cls_name)
            assert actual_id == expected_id, f"{cls_name}: 期望 {expected_id} 实际 {actual_id}"

    def test_forbidden_packets_exist_in_dump(self) -> None:
        """禁发清单中的类名与消息号必须与生成表完全一致。"""
        for cls_name, expected_id in FORBIDDEN_PACKET_IDS.items():
            actual_id = packet_id(cls_name)
            assert actual_id == expected_id, f"{cls_name}: 期望 {expected_id} 实际 {actual_id}"

    def test_forbidden_disjoint_from_allowed(self) -> None:
        """白名单与禁发清单交集必须为空。"""
        overlap_names = set(ALLOWED_PACKET_IDS).intersection(FORBIDDEN_PACKET_IDS)
        overlap_ids = set(ALLOWED_PACKET_IDS.values()).intersection(FORBIDDEN_PACKET_IDS.values())
        assert not overlap_names
        assert not overlap_ids

    def test_buy_challenge_26047_strictly_forbidden(self) -> None:
        """红线：购买次数包 26047 必须在禁发清单中，绝不在白名单中。"""
        assert 26047 in FORBIDDEN_PACKET_IDS.values()
        assert 26047 not in ALLOWED_PACKET_IDS.values()
        assert "WuDouBuyChallenge" in FORBIDDEN_PACKET_IDS

    def test_obsolete_packets_strictly_forbidden(self) -> None:
        """红线：旧版废弃包 26037 和 26039 必须在禁发清单中。"""
        assert 26037 in FORBIDDEN_PACKET_IDS.values()
        assert 26039 in FORBIDDEN_PACKET_IDS.values()
        assert 26037 not in ALLOWED_PACKET_IDS.values()
        assert 26039 not in ALLOWED_PACKET_IDS.values()


class TestPVPListClass:
    """对手模型与 NPC 识别。"""

    def test_npc_identification_by_negative_pid(self) -> None:
        player = PVPListClass(playerID=-101, name="新手训练员", fighting=50000, integral=100)
        assert player.is_npc is True

    def test_npc_identification_by_name(self) -> None:
        player = PVPListClass(playerID=12345, name="NPC_Trainer_01", fighting=50000, integral=100)
        assert player.is_npc is True

    def test_real_player_with_zero_integral_not_npc(self) -> None:
        player = PVPListClass(playerID=12345, name="玩家测试", fighting=50000, integral=0)
        assert player.is_npc is False

    def test_real_player_not_npc(self) -> None:
        player = PVPListClass(playerID=10001, name="真大佬", fighting=800000, integral=1500)
        assert player.is_npc is False


class TestWuDouReplayRes:
    """战报 JSON 解析与摘要输出。"""

    def test_battle_summary_parsing(self) -> None:
        json_data = '{"winOrFail": true, "elapsed": 3.5, "step": 12, "replay_file_name": "rp_123.bin"}'
        res = WuDouReplayRes(
            errorCode=0,
            replayResult=json_data,
            myRanking=1050,
            challenge_times=4,
        )
        summary = res.battle_summary()
        assert summary["winOrFail"] is True
        assert summary["elapsed"] == 3.5
        assert summary["step"] == 12

        assert "本场胜" in res.summary()
        assert "最新名次 1050" in res.summary()

    def test_battle_summary_corrupted_json_does_not_raise(self) -> None:
        res = WuDouReplayRes(
            errorCode=0,
            replayResult="corrupted json {",
            myRanking=1050,
            challenge_times=4,
        )
        assert res.battle_summary() == {}
        assert "本场未知" in res.summary()

    def test_error_code_description(self) -> None:
        assert describe_error_code(0) == "成功"
        assert "挑战次数不足" in describe_error_code(WUDOU_ERROR_NO_CHALLENGE_TIMES)
        assert "目标排名已变更" in describe_error_code(-3)
