"""个人矿「角色条件」数据层测试（``DataRelative`` + 属性表 + 条件判定）。

【这个文件钉死什么】
个人矿的 7 类条件（元素/资质/职业/星级/等级/品质/种族）**数据全在**
``DataRelative`` 里 —— 那是一张"表间引用索引"，逐行对齐源表，
把被文本导出抹空的引用列原样给出。

之前整张表被跳过了（按普通表解析必然失败），于是得出了
「角色的元素/资质拿不到、只能按等级兜底」的**错误结论**，
真机上 21047 一直回 ``-2``。这个文件就是那条结论的**撤回钉子**。

运行方式：

    python -m pytest tests/test_game_config_pit_conditions.py -q

【为什么用真实配置表（不 mock）】
本文件测的是"素材里到底有没有这些数据"。mock 掉就等于只测了我们自己编的假数据。
素材不在时 Skip，而不是假装通过。
"""

from __future__ import annotations

import pytest

from models.config_table import read_table, to_int
from models.game_config import (
    NO_REFERENCE,
    PIT_CONDITION_KINDS,
    REL_ROLE_MAIN_ELEMENT,
    REL_ROLE_MAIN_INFO,
    REL_ROLE_MAIN_QUALIFICATION,
    REL_ROLE_INFO_JOB,
    REL_ROLE_INFO_RACE,
    PitConditionTerm,
    RoleAttributes,
    load_data_relative,
    load_personal_pit_conditions,
    load_role_attributes,
    table_path,
)

#: 庫爾蒂絲：火（138000004）、资质 23、职业 900000005、种族 900001001。
KURTIS_MAIN_ID = 130003000


def _material_available() -> bool:
    try:
        return all(
            table_path(name).is_file()
            for name in (
                "DataRelative",
                "RoleMainData",
                "RoleInfoData",
                "RoleElementData",
                "RoleQualificationData",
                "AlliancePersonalPitData",
                "DrawSingleData",
            )
        )
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(
    not _material_available(),
    reason="缺少 Cherrytale Asset/TextAsset 素材（DataRelative / Role* / AlliancePersonalPitData）",
)


@pytest.fixture(scope="module")
def attributes() -> dict[int, RoleAttributes]:
    """整份文件共用一个属性表（读 4 张表，别每个用例读一遍）。"""
    return load_role_attributes()


# ---------------------------------------------------------------------------
# ① DataRelative：被抹空的引用列都在这里
# ---------------------------------------------------------------------------
class TestDataRelative:
    """★ 核心：`DataRelative` 是"表间引用索引"，不是普通配置表。"""

    def test_parses_as_relation_index(self) -> None:
        """每两条一组：关系声明 + 逐行对齐的值列表。"""
        relations = load_data_relative()
        assert len(relations) > 100, "应该解析出上百条关系"
        for key in (
            REL_ROLE_MAIN_ELEMENT,
            REL_ROLE_MAIN_QUALIFICATION,
            REL_ROLE_MAIN_INFO,
            REL_ROLE_INFO_JOB,
            REL_ROLE_INFO_RACE,
        ):
            assert key in relations, f"缺少关系：{key}"

    def test_role_element_values_align_with_role_main_rows(self) -> None:
        """值数必须与 ``RoleMainData`` 行数一致 —— 对齐是这张表的全部价值。"""
        relations = load_data_relative()
        rows = read_table(table_path("RoleMainData"))
        assert len(relations[REL_ROLE_MAIN_ELEMENT]) == len(rows)

    def test_element_mapping_matches_an_independent_truth_source(self) -> None:
        """★★ 全案地基：用**抽卡表**里独立给出的元素做 160/160 交叉验证。

        ``DrawSingleData`` 有 160 行同时给出 ``objectID``（RoleMainID）与
        ``element``。它与 ``DataRelative`` 是**两份互不相干**的数据 ——
        两边全对得上，才说明我们读对了列、也没错位。
        """
        relations = load_data_relative()
        main_rows = read_table(table_path("RoleMainData"))
        element_of = dict(
            zip(
                (to_int(row, "roleMainID") for row in main_rows),
                relations[REL_ROLE_MAIN_ELEMENT],
            )
        )

        truth = {
            to_int(row, "objectID"): to_int(row, "element")
            for row in read_table(table_path("DrawSingleData"))
            if to_int(row, "objectID")
            and to_int(row, "element")
            and to_int(row, "element")
            > 0
            and 130_000_000 <= (to_int(row, "objectID") or 0) < 131_000_000
        }
        assert len(truth) >= 100, "抽卡真值样本太少，这条验证就没有意义了"

        mismatched = {
            main_id: (element_of.get(main_id), expected)
            for main_id, expected in truth.items()
            if element_of.get(main_id) != expected
        }
        assert not mismatched, f"元素映射与抽卡真值不符：{list(mismatched.items())[:5]}"


# ---------------------------------------------------------------------------
# ② 角色属性表
# ---------------------------------------------------------------------------
class TestRoleAttributes:
    def test_covers_every_role_form(self, attributes: dict[int, RoleAttributes]) -> None:
        rows = read_table(table_path("RoleMainData"))
        assert len(attributes) == len(rows)

    def test_known_role_has_expected_attributes(
        self, attributes: dict[int, RoleAttributes]
    ) -> None:
        """庫爾蒂絲：火 / 资质 23 / 职业 900000005 / 种族 900001001。"""
        attrs = attributes[KURTIS_MAIN_ID]
        assert attrs.element_id == 138000004, "庫爾蒂絲 是火属性"
        assert attrs.qualification_print == 23
        assert attrs.job_id == 900000005
        assert attrs.race_id == 900001001

    def test_element_ids_are_base_ids_not_types(
        self, attributes: dict[int, RoleAttributes]
    ) -> None:
        """★ 反例钉子：元素存的是 **elementID**（13800000x），不是 elementType（0~6）。

        ``IsAchieveLimit`` 读的是 ``RoleElementData+0x10``（elementID）。
        如果有人"顺手"改成 elementType，矿点的 ``elementCondition``
        （表里给的就是 13800000x）会**永远匹配不上**，而且不会报错。
        """
        element_ids = {attrs.element_id for attrs in attributes.values()}
        assert all(value >= 138_000_000 for value in element_ids if value != NO_REFERENCE), (
            "元素应是 elementID（138xxxxxx 段）"
        )
        assert not {value for value in element_ids if 0 <= value <= 6}, (
            "出现了 elementType（0~6）—— 说明取值口径被改错了"
        )

    def test_every_base_element_is_represented(
        self, attributes: dict[int, RoleAttributes]
    ) -> None:
        """7 个基础元素都要有角色 —— 否则某些矿点永远开不了。"""
        counts: dict[int, int] = {}
        for attrs in attributes.values():
            counts[attrs.element_id] = counts.get(attrs.element_id, 0) + 1
        for element_id in range(138_000_001, 138_000_008):
            assert counts.get(element_id, 0) > 0, f"元素 {element_id} 没有任何角色"


# ---------------------------------------------------------------------------
# ③ 条件语义（依据 NewPitPersonalLimit.IsAchieveLimit 的反汇编）
# ---------------------------------------------------------------------------
class TestConditionSemantics:
    """元素/资质/职业/种族是 **==**；星级/等级/品质是 **>=**。"""

    ATTRIBUTES = RoleAttributes(
        role_main_id=1,
        element_id=138000004,
        qualification_print=23,
        job_id=900000005,
        race_id=900001001,
        quality=2,
    )

    def _matches(self, term: PitConditionTerm, *, level: int = 50, star: int = 5) -> bool:
        return term.matches(attributes=self.ATTRIBUTES, level=level, star=star)

    def test_categorical_conditions_require_equality(self) -> None:
        assert self._matches(PitConditionTerm("element", 138000004)) is True
        assert self._matches(PitConditionTerm("element", 138000003)) is False, (
            "元素是 ==，不是 >="
        )
        assert self._matches(PitConditionTerm("qualification", 23)) is True
        assert self._matches(PitConditionTerm("qualification", 21)) is False, (
            "资质是 ==，不是 >=（23 不该满足 21 以外的档）"
        )
        assert self._matches(PitConditionTerm("job", 900000005)) is True
        assert self._matches(PitConditionTerm("job", 900000001)) is False
        assert self._matches(PitConditionTerm("race", 900001001)) is True
        assert self._matches(PitConditionTerm("race", 900001002)) is False

    def test_ordinal_conditions_require_at_least(self) -> None:
        assert self._matches(PitConditionTerm("star", 4)) is True
        assert self._matches(PitConditionTerm("star", 6)) is False, "星级 5 < 6"
        assert self._matches(PitConditionTerm("lv", 50)) is True
        assert self._matches(PitConditionTerm("lv", 51)) is False
        assert self._matches(PitConditionTerm("quality", 2)) is True
        assert self._matches(PitConditionTerm("quality", 3)) is False

    def test_star_and_lv_come_from_the_roster_not_the_table(self) -> None:
        """星级/等级是**实例**属性（``RoleClass``），与形态属性表无关。

        所以即使 ``attributes=None``，这两类条件也必须能判。
        """
        assert PitConditionTerm("star", 4).matches(attributes=None, level=0, star=4) is True
        assert PitConditionTerm("lv", 10).matches(attributes=None, level=10, star=0) is True

    def test_missing_attributes_never_pass_a_categorical_condition(self) -> None:
        """拿不到属性时**判不满足** —— 宁可判缺，也不要放行一个可能不合格的角色。"""
        for kind, value in (
            ("element", 138000004),
            ("qualification", 23),
            ("job", 900000005),
            ("race", 900001001),
            ("quality", 1),
        ):
            assert (
                PitConditionTerm(kind, value).matches(attributes=None, level=99, star=9)
                is False
            ), f"{kind} 在没有属性时不该通过"

    def test_label_is_human_readable(self) -> None:
        """元素要带中文名 —— 用户靠"缺哪个元素"决定去练谁，裸 ID 读不懂。"""
        assert PitConditionTerm("element", 138000004).label == "元素=火（138000004）"
        assert PitConditionTerm("star", 4).label == "星级≥4"
        assert PitConditionTerm("qualification", 23).label == "资质=23"

    def test_base_element_names_match_the_real_table(self) -> None:
        """``BASE_ELEMENT_NAMES`` 是手抄的常量 —— 拿真实 ``RoleElementData`` 核一遍。"""
        from models.game_config import BASE_ELEMENT_NAMES

        rows = {
            to_int(row, "elementID"): row.get("elementName")
            for row in read_table(table_path("RoleElementData"))
        }
        for element_id, name in BASE_ELEMENT_NAMES.items():
            assert rows.get(element_id) == name, (
                f"元素 {element_id} 的名字对不上：常量写 {name}，表里是 {rows.get(element_id)}"
            )

    def test_all_kinds_are_covered(self) -> None:
        """7 类条件都要有判定规则（漏一类会静默放行）。"""
        for kind in PIT_CONDITION_KINDS:
            term = PitConditionTerm(kind, 1)
            assert term.label  # 不抛异常即可


# ---------------------------------------------------------------------------
# ④ 矿点模板条件
# ---------------------------------------------------------------------------
class TestPitConditions:
    def test_all_templates_are_loaded(self) -> None:
        conditions = load_personal_pit_conditions()
        assert len(conditions) == 140

    def test_every_template_requires_element_and_qualification(self) -> None:
        """★ 140/140 都要求元素 + 资质 —— 这正是"只按等级挑人必然被拒"的证据。"""
        conditions = load_personal_pit_conditions()
        missing = [
            pit_id
            for pit_id, condition in conditions.items()
            if not {"element", "qualification"}
            <= {term.kind for term in condition.terms}
        ]
        assert not missing, f"这些模板没要求元素/资质（与实测统计不符）：{missing[:5]}"

    def test_multi_value_conditions_are_split_per_value(self) -> None:
        """``elementCondition=138000001$138000002`` 配 ``1$1`` → 拆成两条。"""
        condition = load_personal_pit_conditions()[935000131]
        elements = sorted(
            term.value for term in condition.terms if term.kind == "element"
        )
        assert elements == [138000001, 138000002], "两个元素要各成一条条件"
        assert all(term.need == 1 for term in condition.terms if term.kind == "element")

    def test_known_template_matches_its_table_row(self) -> None:
        """935000114「牛刀小試」：人数 2、资质 23、水元素。"""
        condition = load_personal_pit_conditions()[935000114]
        assert condition.role_max == 2
        assert condition.title == "牛刀小試"
        assert {(term.kind, term.value) for term in condition.terms} == {
            ("qualification", 23),
            ("element", 138000003),
        }

    def test_describe_terms_is_readable(self) -> None:
        condition = load_personal_pit_conditions()[935000136]
        text = condition.describe_terms()
        assert "元素=" in text and "星级≥4" in text and "种族=" in text

    def test_template_without_terms_describes_as_unconditional(self) -> None:
        from models.game_config import PersonalPitCondition

        assert PersonalPitCondition(1, role_max=1).describe_terms() == "无条件"
