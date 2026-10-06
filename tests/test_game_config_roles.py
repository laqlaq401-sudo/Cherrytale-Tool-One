"""角色 ID 换算表（``models/game_config.RoleIdMap``）的测试。

【这个文件钉死什么】
**``RoleClass.roleID`` 与矿点的 ``needRoleInfoID`` 不是同一个 ID 空间。**
这是 2026-10-05 修掉的那个真 bug 的根因，也是最容易再犯的一处 ——
两个字段看起来都是"角色的 ID"，但一个在 130xxxxxx 段、一个在 133xxxxxx 段，
直接比 ``==`` 永远为假。所以这里**既测换算逻辑，也测"不许直接相等"**。

运行方式：

    python -m pytest tests/test_game_config_roles.py -q

【为什么直接用真实配置表（不 mock）】
本文件测的是"配置表的真实形状" —— ID 段区间、品质升级链、名称列。
把它们 mock 掉，就等于只测了我们自己写的那份假数据。素材不在时会 Skip，
而不是假装通过（参照 ``tests/test_capture_1003_regression.py`` 的做法）。
"""

from __future__ import annotations

import pytest

from models.config_table import read_table, to_int
from models.game_config import (
    TABLE_ROLE_INFO,
    TABLE_ROLE_MAIN,
    RoleIdMap,
    load_role_id_map,
    table_path,
)

#: 2026-10-05 抓包日志里 7 个空槽的 ``needRoleInfoID``（见 ``notes/hero_roster_plan.md``）。
#: 这份清单是"修复真的能解决问题"的判据 —— 它们必须全部可被某个 roleMainID 命中。
CAPTURED_NEED_ROLE_INFO_IDS: tuple[int, ...] = (
    133003000,
    133007400,
    133008400,
    133011300,
    133013400,
    133015900,
    133016200,
)

#: 庫爾蒂絲：RoleMainID 130003000 ↔ RoleInfoID 133003000（品质升级链 0..6）。
KURTIS_MAIN_ID = 130003000
KURTIS_INFO_ID = 133003000


def _material_available() -> bool:
    """素材目录里是否有这两张表（没有时整个文件 Skip）。"""
    try:
        return table_path(TABLE_ROLE_MAIN).is_file() and table_path(
            TABLE_ROLE_INFO
        ).is_file()
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(
    not _material_available(),
    reason="缺少 Cherrytale Asset/TextAsset 素材（RoleMainData / RoleInfoData）",
)


@pytest.fixture(scope="module")
def role_ids() -> RoleIdMap:
    """整份文件共用一个换算表（读两张表约 7000 行，别每个用例读一遍）。"""
    return load_role_id_map()


# ---------------------------------------------------------------------------
# ① 两个 ID 空间确实不同（本文件存在的理由）
# ---------------------------------------------------------------------------
class TestIdSpacesAreDistinct:
    """★ 核心：RoleMainID(130xxxxxx) 与 RoleInfoID(133xxxxxx) 是两个空间。"""

    def test_table_key_ranges_do_not_overlap(self) -> None:
        """两张表的主键区间**不相交** —— 这就是"直接比 == 永远为假"的证据。"""
        main_ids = [to_int(row, "roleMainID") for row in read_table(table_path(TABLE_ROLE_MAIN))]
        info_ids = [to_int(row, "roleID") for row in read_table(table_path(TABLE_ROLE_INFO))]
        main_ids = [value for value in main_ids if value]
        info_ids = [value for value in info_ids if value]

        assert min(main_ids) >= 130_000_000 and max(main_ids) < 131_000_000, (
            f"RoleMainData.roleMainID 应在 130xxxxxx 段，实测 {min(main_ids)}~{max(main_ids)}"
        )
        assert min(info_ids) >= 133_000_000 and max(info_ids) < 134_000_000, (
            f"RoleInfoData.roleID 应在 133xxxxxx 段，实测 {min(info_ids)}~{max(info_ids)}"
        )
        assert set(main_ids) & set(info_ids) == set(), (
            "两个 ID 空间出现交集 —— 说明对字段语义的理解需要重新核实"
        )

    def test_role_info_id_passed_as_role_main_id_never_matches(
        self, role_ids: RoleIdMap
    ) -> None:
        """把 **RoleInfoID** 误当成名册的 ``roleID`` 传进来时，**必须匹配不上**。

        这正是修复前线上代码干的事（``role.roleID == slot.needRoleInfoID``，
        而 ``role.roleID`` 来自名册 —— 是 RoleMainID）。如果有人把 ``matches()``
        退回成朴素相等，这条会立刻红。
        """
        assert role_ids.matches(KURTIS_MAIN_ID, KURTIS_INFO_ID) is True, "正确换算后应命中"
        assert role_ids.matches(KURTIS_INFO_ID, KURTIS_INFO_ID) is False, (
            "把 RoleInfoID 当 RoleMainID 传进来不该命中 —— 否则等于没做换算"
        )

    def test_captured_requirements_are_all_reachable(self, role_ids: RoleIdMap) -> None:
        """★ 修复判据：抓包日志里 7 个空槽的需求，**全部**能被某个 roleMainID 命中。

        它们全不可达 ⇒ 团体矿永远报"名册中没有可用的匹配"（修复前的症状）。
        """
        unreachable = [
            info_id
            for info_id in CAPTURED_NEED_ROLE_INFO_IDS
            if not role_ids.main_ids_for_info(info_id)
        ]
        assert not unreachable, f"这些 needRoleInfoID 换算不出任何 roleMainID：{unreachable}"
        assert len(role_ids.main_ids_for_info(KURTIS_INFO_ID)) >= 1


# ---------------------------------------------------------------------------
# ② 换算逻辑
# ---------------------------------------------------------------------------
class TestConversion:
    def test_base_form_maps_to_its_own_info(self, role_ids: RoleIdMap) -> None:
        assert role_ids.base_main_id(KURTIS_MAIN_ID) == KURTIS_MAIN_ID
        assert KURTIS_INFO_ID in role_ids.role_info_ids(KURTIS_MAIN_ID)

    def test_quality_up_forms_map_to_the_same_info(self, role_ids: RoleIdMap) -> None:
        """品质升级形态（130003001 / 130003002 / …）都换算到同一个 RoleInfoID。

        【为什么这条重要】名册里同一个角色的不同品质形态是不同的 roleMainID；
        矿点要求的是"这个角色"，所以任一形态都该算命中 —— 否则"我明明有这张卡"
        却占不了坑。
        """
        forms = role_ids.main_ids_for_info(KURTIS_INFO_ID)
        assert len(forms) > 1, "庫爾蒂絲应有多个品质升级形态，否则这条断言没有意义"
        for form in forms:
            assert role_ids.base_main_id(form) == KURTIS_MAIN_ID, f"{form} 的链首应是 {KURTIS_MAIN_ID}"
            assert role_ids.matches(form, KURTIS_INFO_ID) is True

    def test_reverse_lookup_is_the_inverse_of_matches(self, role_ids: RoleIdMap) -> None:
        """``main_ids_for_info`` 与 ``matches`` 必须自洽（互为逆运算）。"""
        for main_id in role_ids.main_ids_for_info(KURTIS_INFO_ID):
            assert role_ids.matches(main_id, KURTIS_INFO_ID) is True
        # 反过来：不属于该角色的 roleMainID 一律不命中
        assert role_ids.matches(130007400, KURTIS_INFO_ID) is False

    def test_unknown_and_invalid_inputs_are_safe(self, role_ids: RoleIdMap) -> None:
        """查不到的 ID **不抛异常**：换算不出来就返回空/False，绝不猜一个角色。"""
        assert role_ids.role_info_ids(999_999_999) == ()
        assert role_ids.base_main_id(999_999_999) == 999_999_999, "不在表里就原样返回"
        assert role_ids.matches(999_999_999, KURTIS_INFO_ID) is False
        assert role_ids.matches(KURTIS_MAIN_ID, 0) is False, "需求为 0 表示'无要求'，不该算命中"
        assert role_ids.matches(KURTIS_MAIN_ID, -1) is False
        assert role_ids.name_of(999_999_999) == ""


# ---------------------------------------------------------------------------
# ③ 名称
# ---------------------------------------------------------------------------
class TestNames:
    def test_name_by_role_main_id(self, role_ids: RoleIdMap) -> None:
        assert role_ids.name_of(KURTIS_MAIN_ID) == "庫爾蒂絲"

    def test_name_by_role_info_id(self, role_ids: RoleIdMap) -> None:
        """矿点给的是 RoleInfoID，报错文案要能直接翻成中文名。"""
        assert role_ids.name_of_info(KURTIS_INFO_ID) == "庫爾蒂絲"

    def test_unknown_name_is_empty_not_invented(self, role_ids: RoleIdMap) -> None:
        """查不到名字返回**空串** —— 调用方渲染成"未知角色"，不许编一个名字。"""
        assert role_ids.name_of_info(999_999_999) == ""


# ---------------------------------------------------------------------------
# ④ 内存构造（生产代码之外的第二个入口，测试夹具在用）
# ---------------------------------------------------------------------------
class TestInMemoryConstruction:
    """``RoleIdMap`` 可以完全用内存数据构造 —— 夹具与离线重放都靠它。"""

    def test_hand_built_map_behaves_like_the_real_one(self) -> None:
        role_map = RoleIdMap(
            parent={130003001: 130003000, 130003002: 130003001},
            info_of_base={130003000: (133003000,)},
            name_of_info={133003000: "庫爾蒂絲"},
        )
        assert role_map.base_main_id(130003002) == 130003000, "两级链也要能回溯到链首"
        assert role_map.matches(130003002, 133003000) is True
        assert role_map.name_of(130003001) == "庫爾蒂絲"
        assert role_map.main_ids_for_info(133003000) == (130003000, 130003001, 130003002)

    def test_cycle_in_parent_chain_does_not_hang(self) -> None:
        """配置表万一出现环，回溯必须**停下来**（否则整个任务卡死）。"""
        role_map = RoleIdMap(
            parent={130000002: 130000001, 130000001: 130000002},
            info_of_base={130000001: (133000001,)},
            name_of_info={},
        )
        assert role_map.base_main_id(130000002) in (130000001, 130000002)
