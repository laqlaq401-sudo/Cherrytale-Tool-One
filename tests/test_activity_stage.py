"""降临／活动关卡报文（``models/activity_stage.py``）的字节级验证。

运行方式：

    python -m pytest tests/test_activity_stage.py -q
    python -m tests.test_activity_stage

【为什么必须用真实抓包字节】
本板块的 ``11009`` **会扣玩家真实资源**（体力 + 扫荡券）。字段编号错一位的后果是
"扫错关卡/扫错次数/扣错资源"，而且服务端不会报错。所以这里的断言全部拿
``captures/降临扫荡抓包1.saz`` 的原始子包字节来核对（见 ``protocol_samples/sweep_samples.py``）。

【本文件同时钉住三条"容易悄悄改错"的事实】
1. ``11010.rewardList`` / ``costList`` 里是**操作后的持有量**（不是本次变动量）；
2. ``ActivityAreaStateClass.receiveStarReward`` 与 ``SectionStateClass.starCount`` /
   ``limitItemCount`` 在线上是 **repeated**（同一编号出现多次），模型必须是 ``list[int]``；
3. 模型字段集合必须与生成表 ``models/proto_fields.py`` **逐字一致**
   （多一个/少一个都会让解码静默丢字段）。
"""

from __future__ import annotations

import pytest

from models.activity_stage import (
    ACTIVITY_STAGE_SUCCESS,
    ALLOWED_PACKET_IDS,
    ENERGY_ITEM_ID,
    ERROR_NOTES,
    FORBIDDEN_PACKET_IDS,
    SWEEP_TICKET_ITEM_ID,
    SWEEP_TIME_CHOICES,
    SWEEP_UNTIL_EMPTY,
    ActivityAreaStateClass,
    GetAllActivityStage,
    GetAllActivityStageRes,
    GetSectionByAreaPacket,
    GetSectionByAreaRes,
    SectionStateClass,
    SweepSectionPacket,
    SweepSectionRes,
    describe_error_code,
    normalize_sweep_times,
    timestamp_to_text,
)
from models.proto_fields import field_tags
from protocol_samples.sweep_samples import (
    AREA_ELEMENT_HEX,
    SECTION_ELEMENT_HEX,
    SWEEP_REQUEST_FOUR_HEX,
    SWEEP_REQUEST_ONCE_HEX,
    SWEEP_RESPONSE_FOUR_HEX,
    SWEEP_RESPONSE_ONCE_HEX,
)

#: 抓包里的关卡（第 9 关，体力 40）
SECTION_ID = 129110539
#: 抓包里的区域（"可疑的工作"）
AREA_ID = 128110053


def decode_sweep_request(hex_text: str) -> SweepSectionPacket:
    """把 fixture 里的请求字节解成模型。"""
    return SweepSectionPacket.decode(bytes.fromhex(hex_text))


# ---------------------------------------------------------------------------
# 11009 请求：字段编号与实测逐字节一致
# ---------------------------------------------------------------------------
def test_request_once_matches_capture() -> None:
    """扫荡 1 次的请求：``08 8ba4c83d | 10 01 | 18 01`` → sectionID/times/useSweepTicket。"""
    packet = decode_sweep_request(SWEEP_REQUEST_ONCE_HEX)

    assert packet.sectionID == SECTION_ID
    assert packet.times == 1
    assert packet.useSweepTicket == 1


def test_request_four_is_the_client_trimmed_value() -> None:
    """抓包里"点 5 次"实际发出的是 ``times=4``（客户端按体力削过）。"""
    packet = decode_sweep_request(SWEEP_REQUEST_FOUR_HEX)

    assert packet.sectionID == SECTION_ID
    assert packet.times == 4


def test_request_encodes_back_to_original_bytes() -> None:
    """本板块三个请求字段都非零 → 组包必须能**原样**还原抓包字节。

    这条断言的价值：一旦字段顺序/类型被改动，字节会立刻不同（而服务端只会"行为奇怪"）。
    """
    for hex_text in (SWEEP_REQUEST_ONCE_HEX, SWEEP_REQUEST_FOUR_HEX):
        packet = decode_sweep_request(hex_text)
        assert packet.encode(include_defaults=True).hex() == hex_text


# ---------------------------------------------------------------------------
# 11010 响应：数量是"操作后的持有量"
# ---------------------------------------------------------------------------
def test_response_once_reads_holdings_not_deltas() -> None:
    """1 次扫荡的响应：体力剩 160、扫荡券剩 893 —— 都是**持有量**。"""
    res = SweepSectionRes.decode(bytes.fromhex(SWEEP_RESPONSE_ONCE_HEX))

    assert res.errorCode == ACTIVITY_STAGE_SUCCESS
    assert res.is_success
    assert res.energy_left == 160
    assert res.sweep_ticket_left == 893
    # 实测：1 次扫荡 → 1 个非空明细 + 末尾 1 个空占位
    assert len(res.rewardList) == 2
    assert res.rewardList[-1].itemList == []

    items = res.settlement_items()
    assert [item.itemID for item in items] == [200300339, 200000050, 200000017]
    assert [item.itemAmount for item in items] == [1050, 20198609, 1446205]


def test_response_four_times_shows_decreasing_holdings() -> None:
    """4 次扫荡：体力 0、券 889（893-889=4=次数），奖励明细逐次递增。"""
    res = SweepSectionRes.decode(bytes.fromhex(SWEEP_RESPONSE_FOUR_HEX))

    assert res.energy_left == 0
    assert res.sweep_ticket_left == 889
    assert len(res.rewardList) == 5
    assert res.rewardList[-1].itemList == []

    items = res.settlement_items()
    assert [item.itemAmount for item in items] == [2650, 20206609, 1446205]
    # 活动币每次 +400、金币每次 +2000（见 notes/sweep_capture.md 第四节）
    assert res.cost_summary() == "体力剩 0；扫荡券剩 889"
    assert "200000050" in res.reward_summary()


# ---------------------------------------------------------------------------
# 11026 / 11004 的元素：repeated 字段必须被正确聚合成 list
# ---------------------------------------------------------------------------
def test_area_element_repeated_field_is_a_list() -> None:
    """区域元素：``receiveStarReward`` 在线上出现 3 次 —— 模型必须是 ``list[int]``。

    这条断言是"用实测字节决定类型"的样板：如果按 dump.cs 只看到属性名就写成
    ``int``，解码会**静默丢掉前两个值**。
    """
    area = ActivityAreaStateClass.decode(bytes.fromhex(AREA_ELEMENT_HEX))

    assert area.areaID == 128010023
    assert area.passedCount == 0
    assert area.areaStars == 15
    assert area.receiveStarReward == [1, 1, 1]
    # ⚠️ 实测 weeks 是 -1（不是"2 周"）—— 两周一期只能从起止时间看出来
    assert area.weeks == -1
    assert area.sectionState == 2
    assert area.progressStr == "5/5"
    assert area.startTime == 1788915600000
    assert area.endTime == 1790125200000
    assert area.isMemoryUnlock is False
    assert area.start_text == "2026-09-09 09:00"
    assert area.end_text == "2026-09-23 09:00"


def test_area_element_extra_coin_is_negative_placeholder() -> None:
    """``extraChallengeCoin`` 实测是 ``ItemClass{itemSid:-1, itemID:-1, itemAmount:-1}``。

    也就是说"没有额外挑战币"用的是 **-1 占位**，不是空消息 ——
    将来若要判断"有没有币"，不能只看 ``itemList`` 是否为空。
    """
    area = ActivityAreaStateClass.decode(bytes.fromhex(AREA_ELEMENT_HEX))

    assert area.extraChallengeCoin is not None
    assert area.extraChallengeCoin.itemID == -1
    assert area.extraChallengeCoin.itemAmount == -1


def test_section_element_repeated_fields_are_lists() -> None:
    """关卡元素：``starCount`` / ``limitItemCount`` 都是 repeated。"""
    section = SectionStateClass.decode(bytes.fromhex(SECTION_ELEMENT_HEX))

    assert section.sectionID == 129110531
    assert section.sectionState == 2
    assert section.starCount == [1, 1, 1]
    assert section.limitItemCount == [0]
    assert section.mapBonusState == 1
    assert section.colorEgg == -2
    assert section.weekDay == -1  # -1 = 每天都开
    assert section.stageDistributeID == -1


# ---------------------------------------------------------------------------
# 辅助方法
# ---------------------------------------------------------------------------
def test_section_ids_keep_server_order() -> None:
    """``section_ids()`` 必须保持服务端顺序（扫荡顺序由它决定）。"""
    res = GetSectionByAreaRes(
        sectionStateList=[
            SectionStateClass(sectionID=129110531),
            SectionStateClass(sectionID=129110539),
        ]
    )

    assert res.section_ids() == [129110531, 129110539]


def test_find_area_searches_all_three_lists() -> None:
    """``find_area`` 三个列表都要找（常驻区在第三个列表里）。"""
    memory = ActivityAreaStateClass(areaID=42)
    permanent = ActivityAreaStateClass(areaID=43)
    res = GetAllActivityStageRes(
        memoryAreaStateList=[memory], permanentAreaStateList=[permanent]
    )

    assert res.find_area(42) is memory
    assert res.find_area(43) is permanent
    assert res.find_area(7) is None
    assert [area.areaID for area in res.all_areas()] == [42, 43]


def test_area_describe_uses_config_name_when_available() -> None:
    """``describe`` 带配置表名字时更可读（配置表查不到也不该崩）。"""
    area = ActivityAreaStateClass(
        areaID=AREA_ID, progressStr="5/5", startTime=1788915600000, endTime=1790125200000
    )

    assert "可疑的工作" in area.describe("可疑的工作")
    assert str(AREA_ID) in area.describe()


# ---------------------------------------------------------------------------
# 常量与白名单（本板块的安全边界）
# ---------------------------------------------------------------------------
def test_allowed_and_forbidden_packets() -> None:
    """白名单包含基础扫荡包与简单首通战斗包；买次数（11027）必须在禁发清单里，且两者不相交。"""
    assert ALLOWED_PACKET_IDS == {
        "GetAllActivityStage": 11025,
        "GetSectionByAreaPacket": 11003,
        "SweepSectionPacket": 11009,
        "FightingRequestPacket": 11015,
        "FightingResultPacket": 11017,
        # ★ 2026-10-02：扫荡前要先用只读的 2007 数扫荡券（零券则一个 11009 都不发）
        "GetAllItemPacket": 2007,
    }
    assert FORBIDDEN_PACKET_IDS["BuyActivityStagePacket"] == 11027
    assert set(ALLOWED_PACKET_IDS) & set(FORBIDDEN_PACKET_IDS) == set()


def test_item_ids_come_from_rps_const() -> None:
    """体力/扫荡券 ID 取自 ``RPS_Const``（写死这两个数字是刻意的回归断言）。"""
    assert ENERGY_ITEM_ID == 200000002
    assert SWEEP_TICKET_ITEM_ID == 200000016


def test_error_code_helper_is_honest_about_unknown() -> None:
    """只把实测见过的 0 翻译成"成功"；未知码原样报出来，不编含义。"""
    assert describe_error_code(0) == "成功"
    assert "未知错误码" in describe_error_code(-99)


def test_timestamp_helper_handles_unset_values() -> None:
    """``0`` / ``-1``（以 2^64-1 出现）都视为"未设置"。"""
    assert timestamp_to_text(0) == "<未设置>"
    assert timestamp_to_text((1 << 64) - 1) == "<未设置>"
    assert timestamp_to_text(1790125200000) == "2026-09-23 09:00"


def test_models_match_generated_field_table() -> None:
    """模型字段集合必须与生成表**逐字一致**（多/少一个都会静默丢字段）。"""
    for model in (
        SweepSectionPacket,
        SweepSectionRes,
        GetSectionByAreaPacket,
        GetSectionByAreaRes,
        GetAllActivityStage,
        GetAllActivityStageRes,
        ActivityAreaStateClass,
        SectionStateClass,
    ):
        assert set(model.model_fields) == set(field_tags(model.proto_class_name)), (
            model.__name__
        )


# ---------------------------------------------------------------------------
# 扫荡档位（Q-018）：客户端只有 1 / 5 / 10 三档
# ---------------------------------------------------------------------------
def test_sweep_time_choices_match_client_enum() -> None:
    """档位来自 ``StageModule.eSweepType``（dump.cs L530313）：One=1 / five=5 / Ten=10。"""
    assert SWEEP_TIME_CHOICES == (1, 5, 10)


def test_until_empty_sentinel_is_not_a_wire_value() -> None:
    """★ 2026-10-04：``SWEEP_UNTIL_EMPTY`` 只是**界面哨兵**，绝不进 ``times`` 白名单。

    它是"扫荡到清空体力 / 次数耗尽"的 UI 值；真正发包时每轮发的是 5
    （或被体力削出的其它值）。所以它必须**不在** ``SWEEP_TIME_CHOICES`` 里 ——
    否则回归断言 ``req.times in SWEEP_TIME_CHOICES`` 就拦不住"哨兵被误发上网"。
    """
    assert SWEEP_UNTIL_EMPTY == -1
    assert SWEEP_UNTIL_EMPTY not in SWEEP_TIME_CHOICES


@pytest.mark.parametrize(
    ("requested", "expected", "has_note"),
    [
        (0, 0, False),  # 只读
        (-3, 0, False),  # 负数按"不扫"处理
        (1, 1, False),  # 档位内 → 原样
        (5, 5, False),
        (10, 10, False),
        (7, 5, True),  # 档位外的值 → 向下归
        (9, 5, True),
        (11, 10, True),  # 超过最高档 → 收到 10
        (999, 10, True),
    ],
)
def test_normalize_sweep_times(requested: int, expected: int, has_note: bool) -> None:
    """当归一/截断发生时**必须**给出说明，否则用户不知道"为什么只扫了 5 次"。"""
    times, note = normalize_sweep_times(requested)

    assert times == expected
    assert bool(note) is has_note


# ---------------------------------------------------------------------------
# 错误码（Q-022）：逐条来自客户端枚举 StageModule.eSweepErrorCode
# ---------------------------------------------------------------------------
def test_error_notes_cover_client_enum() -> None:
    """错误码表必须覆盖 ``StageModule.eSweepErrorCode`` 的全部取值（缺一个就会显示"未知"）。"""
    client_enum = {0, -1, -2, -3, -4, -6, -7, -8, -9, -10, -11}

    assert set(ERROR_NOTES) == client_enum
    assert "体力不足" in describe_error_code(-2)
    assert "未通关" in describe_error_code(-1)
    assert "VIP" in describe_error_code(-9)
    assert "扫荡券不足" in describe_error_code(-3)

