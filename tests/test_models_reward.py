"""``models/reward.py``（数量口径层）与「运行视图禁裸标识符」的回归守卫。

【这一组测试在守什么】

2026-10-04 的全方位整改把"数量口径"从"写在 docstring 里的约定"提升成了**类型**。
本模块钉死三件事：

1. ``QuantityScope`` 的三种口径各自产出什么文案（尤其是 ``UNKNOWN`` **不报数量**）；
2. 件数 / 种数 / 组数三个量词不能混用（``count_items`` vs ``count_item_kinds``）；
3. 运行视图文案里不出现裸标识符 —— 判据见 ``tests/reward_guard.py`` 的模块文档
   （**按语义判，不按数字位数判**，因为金币可能过亿）。
"""

from __future__ import annotations

import pytest

from models.daily import GetObjClass, ItemClass
from models.reward import (
    QuantityScope,
    cost_phrase,
    count_item_kinds,
    count_items,
    item_name,
    reward_phrase,
    reward_text,
)
from tests.reward_guard import (
    assert_no_bare_identifiers,
    bare_identifiers,
)

#: 一份"混装"的奖励：认识名字的 + 不认识的 + 重复 itemID 的。
MIXED_GROUPS = [
    GetObjClass(
        itemList=[
            ItemClass(itemID=200000050, itemAmount=36610000),  # 金币（认识）
            ItemClass(itemID=200000001, itemAmount=60),  # 钻石（认识）
            ItemClass(itemID=200000050, itemAmount=1000),  # 金币再来一条（重复 ID）
            ItemClass(itemID=200300339, itemAmount=1050),  # 不认识（活动币）
        ]
    )
]


class TestQuantityScope:
    """三种口径的文案形态。"""

    def test_gained_uses_obtain_verb(self) -> None:
        text = reward_phrase(MIXED_GROUPS, scope=QuantityScope.GAINED)
        assert text == "获得：金币×36610000、钻石×60、金币×1000"
        assert "当前持有" not in text

    def test_held_uses_holding_verb(self) -> None:
        """Q-021：6006/11010/37020 回的是"操作后持有量"，措辞必须是"当前持有"。"""
        text = reward_phrase(MIXED_GROUPS, scope=QuantityScope.HELD)
        assert text.startswith("当前持有：")
        assert "获得" not in text

    def test_unknown_shows_no_amounts_at_all(self) -> None:
        """★ 核心规则：口径未实证 ⇒ 只报件数，一个数量都不许出现。

        这里刻意用"金币可能过亿"的场景：如果实现按"数字位数"过滤，
        36610000 会被误杀；正确做法是**整条数量都不展示**。
        """
        text = reward_phrase(MIXED_GROUPS, scope=QuantityScope.UNKNOWN)
        assert text == "获得 4 件道具"
        for leak in ("36610000", "60", "1000", "金币", "钻石"):
            assert leak not in text, f"UNKNOWN 口径泄露了 {leak!r}：{text!r}"

    def test_cost_phrase_is_mirror_of_reward_phrase(self) -> None:
        assert cost_phrase(MIXED_GROUPS, scope=QuantityScope.GAINED).startswith("消耗：")
        assert cost_phrase(MIXED_GROUPS, scope=QuantityScope.HELD).startswith("剩余：")
        assert cost_phrase(MIXED_GROUPS, scope=QuantityScope.UNKNOWN) == "消耗 4 件道具"

    def test_scope_is_keyword_only(self) -> None:
        """``scope`` 必须显式给：漏传是**调用点没表态口径**，应当直接报错。"""
        with pytest.raises(TypeError):
            reward_text(MIXED_GROUPS)  # type: ignore[call-arg]

    def test_empty_inputs_return_empty_string(self) -> None:
        for scope in QuantityScope:
            assert reward_phrase(None, scope=scope) == ""
            assert reward_phrase([], scope=scope) == ""
            assert reward_phrase([GetObjClass()], scope=scope) == ""


class TestCounters:
    """量词口径：件数 / 种数 / 组数。"""

    def test_items_vs_kinds(self) -> None:
        # 4 件（条目数），3 种（去重后：金币/钻石/活动币）
        assert count_items(MIXED_GROUPS) == 4
        assert count_item_kinds(MIXED_GROUPS) == 3
        # 组数是 1 —— 与"件数"完全不是一回事（top_pvp_box 曾把组数当件数）
        assert len(MIXED_GROUPS) == 1

    def test_accepts_flat_item_list(self) -> None:
        """已经摊平过的 ``list[ItemClass]`` 也要能数 —— 传错层级不能静默返回 0。"""
        flat = [ItemClass(itemID=200000050, itemAmount=1), ItemClass(itemID=200000050, itemAmount=2)]
        assert count_items(flat) == 2
        assert count_item_kinds(flat) == 1

    def test_unknown_objects_are_skipped(self) -> None:
        """服务端未建模的 ``bytes`` 占位不该让计数崩掉。"""
        assert count_items([b"raw-bytes"]) == 0  # type: ignore[list-item]


class TestItemNames:
    def test_known_and_unknown(self) -> None:
        assert item_name(200000050) == "金币"
        assert item_name(200300339) is None


class TestBareIdentifierGuard:
    """守卫本身要能抓到真实错误形态，且不误伤"数量恰好等于 ID"的巧合。"""

    def test_catches_id_used_as_name(self) -> None:
        assert bare_identifiers("道具 200000050×500") == [200000050]

    def test_catches_id_used_as_amount(self) -> None:
        assert bare_identifiers("获得 200000050 体力") == [200000050]

    def test_allows_id_shaped_quantity(self) -> None:
        """``×200000050`` 是"数量恰好等于某个 ID"，必须放行（见 reward_guard 文档）。"""
        assert bare_identifiers("当前持有：金币×200000050") == []

    def test_allows_large_coin_amounts(self) -> None:
        """★ 金币可能过亿 —— 8 位以上的数字本身**不是**违规。"""
        assert bare_identifiers("当前持有：金币×36610000") == []
        assert bare_identifiers("当前持有：金币×1234567890") == []

    def test_real_messages_are_clean(self) -> None:
        samples = [
            "一键领取 3 封邮件成功",
            "逐封领取：成功 3 封、失败 2 封（多为已读 / 已领过）",
            "日常任务：奖励已入账（当前持有：钻石×60）",
            "免费档吃宴席成功（未邀请好友、未花钻石）：获得 2 件道具",
            "第 1/4 件：血钻 ×5，单价 500 金币",
            "荣耀之巅宝箱领取成功（零钻石消耗）",
            "成功领取辉煌航迹搜集物资（获得 2 种物品，目录 3 项）",
        ]
        for text in samples:
            assert_no_bare_identifiers(text)
