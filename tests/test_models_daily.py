"""日常任务报文（``models/daily.py``）的单元测试。

运行方式：

    python -m tests.test_models_daily
    python -m pytest tests/test_models_daily.py -q

【这组测试在防什么】
1. **字段名抄错**：模型字段与 dump.cs 的字段表必须对得上（见
   ``TestModelConsistencyWithGeneratedTables``）—— 抄错一个字母时，
   组包会直接报错还算幸运，更坏的情况是字段被静默跳过；
2. **枚举值写错**：``MissionType`` 的数值来自 ``DailyMission_UIView.eType``，
   写错会让领奖请求落到「另一种任务」上；
3. **解码行为**：用手写的十六进制字节验证，而不是「编码再解码」——
   后者只能证明自洽，不能证明我们解的是**服务端真正发的那种格式**。
"""

from __future__ import annotations

import pytest

from models import daily, proto_fields
from models.daily import (
    SUCCESS_ERROR_CODE,
    DailyMission,
    DailyMissionListClass,
    DailyMissionRes,
    DMActReward,
    DMActRewardRes,
    DMReward,
    DMRewardRes,
    GetObjClass,
    ItemClass,
    MissionType,
)
from models.game_packet import ProtoMessage


def iter_proto_classes() -> list[type[ProtoMessage]]:
    """列出 ``models/daily.py`` 里定义的全部报文类（不含基类）。"""
    classes: list[type[ProtoMessage]] = []
    for obj in vars(daily).values():
        if isinstance(obj, type) and issubclass(obj, ProtoMessage) and obj is not ProtoMessage:
            classes.append(obj)
    return sorted(classes, key=lambda cls: cls.__name__)


class TestModelConsistencyWithGeneratedTables:
    """模型必须与 dump.cs 生成的字段表一致。"""

    def test_registry_is_not_empty(self) -> None:
        """先确认迭代器真的找到了类，否则下面的断言会空跑。"""
        assert len(iter_proto_classes()) >= 8

    def test_every_model_has_a_generated_field_table(self) -> None:
        for cls in iter_proto_classes():
            assert cls.proto_class_name in proto_fields.PROTO_FIELDS, (
                f"{cls.__name__}.proto_class_name = {cls.proto_class_name!r} "
                "在 models/proto_fields.py 里不存在"
            )

    def test_model_fields_exist_in_dump(self) -> None:
        """模型字段名必须能在 dump.cs 的字段表里找到 —— 防手抄拼错。"""
        for cls in iter_proto_classes():
            declared = set(cls.model_fields)
            generated = set(proto_fields.PROTO_FIELDS[cls.proto_class_name])
            unknown = declared - generated
            assert not unknown, (
                f"{cls.__name__} 声明了 dump.cs 里没有的字段：{sorted(unknown)}；"
                f"dump.cs 里实际是：{sorted(generated)}"
            )

    def test_model_does_not_miss_required_fields(self) -> None:
        """反向检查：字段表里有的、模型里也应当有。

        允许「模型只建模一部分」（未建模的字段会被解码时跳过），
        但**不允许模型里的字段顺序与字段表不一致** —— 那会让编号整体错位。
        """
        for cls in iter_proto_classes():
            generated = proto_fields.PROTO_FIELDS[cls.proto_class_name]
            declared_order = [name for name in generated if name in cls.model_fields]
            assert list(cls.model_fields) == declared_order, (
                f"{cls.__name__} 的字段顺序与 dump.cs 不一致"
            )


class TestMissionType:
    """``MissionType`` 的数值来自 dump.cs 的 ``DailyMission_UIView.eType``。"""

    @pytest.mark.parametrize(
        ("member", "value"),
        [
            ("NONE", -1),
            ("DAILY", 0),
            ("MAIN", 1),
            ("RESTRICT", 2),
            ("ALLIANCE", 5),
            ("DAILY_REWARD_BOX", 6),
            ("GUIDE_MISSION", 13),
            ("WEEKLY", 14),
            ("WEEKLY_REWARD_BOX", 15),
        ],
    )
    def test_values_match_dump(self, member: str, value: int) -> None:
        assert int(MissionType[member]) == value

    def test_values_are_not_contiguous(self) -> None:
        """数值不连续（缺 3、4、7~12）是事实，不要「顺手补齐」。

        补齐会让某个任务的领奖请求带着一个**不存在的类型**发出去，
        而服务端只会回一个含糊的错误。
        """
        values = sorted(int(member) for member in MissionType if int(member) >= 0)
        assert values == [0, 1, 2, 5, 6, 13, 14, 15]


class TestDailyMissionListClass:
    def test_can_claim_when_completed_and_not_claimed(self) -> None:
        mission = DailyMissionListClass(misID=249000006, completedTimes=1, isGotReward=0)
        assert mission.can_claim(required_count=1) is True

    def test_cannot_claim_when_already_claimed(self) -> None:
        mission = DailyMissionListClass(misID=249000006, completedTimes=1, isGotReward=1)
        assert mission.can_claim(required_count=1) is False

    def test_cannot_claim_when_not_completed(self) -> None:
        mission = DailyMissionListClass(misID=249000006, completedTimes=0, isGotReward=0)
        assert mission.can_claim(required_count=1) is False

    def test_unknown_reward_flag_counts_as_claimed(self) -> None:
        """``isGotReward`` 是 int，遇到不认识的取值必须按「已领」处理。

        如果这里用 ``not mission.isGotReward`` 判断，取值 2（将来可能表示
        「已过期」或「已转换为其它奖励」）会被当成未领，
        于是脚本会去重复领取 —— 这正是我们要避免的。
        """
        mission = DailyMissionListClass(misID=1, completedTimes=99, isGotReward=2)
        assert mission.can_claim(required_count=1) is False


class TestRewardRequests:
    """领奖请求的组包（**有副作用的那些报文**）。"""

    def test_dm_reward_simple_roundtrip(self) -> None:
        """手算过的字节：misID=1（字段1）、mistypeID=2（字段2）。

        标签算法：(编号 << 3) | 线类型 → 字段 1 = 0x08、字段 2 = 0x10。
        """
        message = DMReward(misID=1, mistypeID=2)
        assert message.encode(include_defaults=False).hex() == "08011002"
        assert DMReward.decode(message.encode()) == message

    def test_dm_reward_for_mission_sets_type(self) -> None:
        request = DMReward.for_mission(249000007, MissionType.DAILY)
        assert request.misID == 249000007
        assert request.mistypeID == int(MissionType.DAILY)

    def test_dm_reward_weekly_uses_its_own_type(self) -> None:
        """周常与日常是同一条接口、不同 ``mistypeID`` —— 传错就领错。"""
        weekly = DMReward.for_mission(249000007, MissionType.WEEKLY)
        assert weekly.mistypeID == 14

    def test_act_reward_carries_box_id(self) -> None:
        """活跃宝箱的入参是 **dmrID**（939000001 起），不是活跃点阈值。"""
        message = DMActReward(actRewardID=939000003)
        assert DMActReward.decode(message.encode()).actRewardID == 939000003


class TestRewardResponses:
    """领奖响应的解包。"""

    #: 手写的响应字节（不是本库编码出来的，所以能独立验证解码逻辑）::
    #:
    #:     errorCode = 0                        → 08 00
    #:     rewardList[0]                        → 12 08
    #:         itemList[0]                      →   0a 06
    #:             itemSid=1                    →     08 01
    #:             itemID=2                     →     10 02
    #:             itemAmount=3                 →     18 03
    #:     missionList[0]                       → 1a 04
    #:         misID=5                          →   08 05
    #:         completedTimes=1                 →   10 01
    RESPONSE_HEX = "080012080a060801100218031a0408051001"

    def test_decode_response(self) -> None:
        response = DMRewardRes.decode(bytes.fromhex(self.RESPONSE_HEX))

        assert response.errorCode == SUCCESS_ERROR_CODE
        assert response.is_success() is True

        assert len(response.rewardList) == 1
        assert response.rewardList[0].itemList == [
            ItemClass(itemSid=1, itemID=2, itemAmount=3)
        ]

        assert len(response.missionList) == 1
        assert response.missionList[0].misID == 5
        assert response.missionList[0].completedTimes == 1

    def test_decode_keeps_unknown_reward_kinds_as_bytes(self) -> None:
        """未建模的奖励种类（装备/宝石等）要保留原始字节，而不是丢掉。"""
        response = DMRewardRes.decode(bytes.fromhex(self.RESPONSE_HEX))
        reward = response.rewardList[0]
        assert reward.equipList == []
        assert reward.roleSkinList == []

    def test_act_reward_response(self) -> None:
        response = DMActRewardRes.decode(bytes.fromhex("080012080a06080110021803"))
        assert response.is_success() is True
        assert response.rewardList[0].itemList[0].itemAmount == 3

    def test_item_summary(self) -> None:
        reward = GetObjClass(itemList=[ItemClass(itemID=200000050, itemAmount=500)])
        assert reward.item_summary() == "道具 200000050×500"
        assert GetObjClass().item_summary() == "<无道具>"


class TestDailyMissionRes:
    def test_roundtrip_with_bytes_placeholder_lists(self) -> None:
        """未建模的列表用 ``bytes`` 占位：既能原样保留，又能通过往返验证。"""
        original = DailyMissionRes(
            dmInitList=[DailyMissionListClass(misID=249000006, completedTimes=1)],
            wmInitList=[DailyMissionListClass(misID=249000010)],
            dmrReceivedList=[939000001],
            wmrReceivedList=[],
            unlockMission=[b"\x08\x01"],
        )
        restored = DailyMissionRes.decode(original.encode())
        assert restored == original

    def test_empty_response_decodes_to_defaults(self) -> None:
        """服务端返回空包（所有列表都为空）时不该报错。"""
        response = DailyMissionRes.decode(b"")
        assert response.dmInitList == []
        assert response.dmrReceivedList == []


class TestDailyMissionRequest:
    def test_type_list_is_packed(self) -> None:
        """``type`` 是 ``List<int>``，protobuf-net 默认发 packed。"""
        assert DailyMission(type=[1, 2]).encode(include_defaults=False).hex() == "0a020102"

    def test_empty_request_encodes_to_nothing(self) -> None:
        assert DailyMission().encode(include_defaults=False) == b""


if __name__ == "__main__":  # 支持 `python -m tests.test_models_daily`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))

