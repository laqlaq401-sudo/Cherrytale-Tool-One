"""配置表解析器与日常任务业务表的单元测试。

运行方式：

    python -m tests.test_config_table
    python -m pytest tests/test_config_table.py -q

【为什么这些测试重要】
配置表是「脚本知道该做什么」的唯一依据：领哪个宝箱、任务要几次、行为编码是什么。
解析错一个字段**不会崩**，只会让脚本按错误的数据干活 ——
例如把 ``requiredNum``（活跃点阈值）当成宝箱 ID 发出去，
服务端只会回一个「参数错误」，而你会盯着报文怀疑人生。

因此这里分两层验证：

1. 解析器本身：终止符、CRLF、末行无换行、``NULL``、列数错位、重复列名；
2. 真实表的业务不变式：46 条日常任务、5 个宝箱、宝箱 ID 从 939000001 开始。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from models.config_table import (
    TableError,
    TableRow,
    column_names,
    iter_table,
    normalize_value,
    read_table,
    split_line,
    to_bool,
    to_int,
    to_int_list,
    to_text,
)
from models.game_config import (
    TABLE_DAILY_MISSION,
    TABLE_DAILY_MISSION_REWARD,
    boxes_for_point,
    load_daily_missions,
    load_daily_reward_boxes,
    mission_index,
    table_path,
)


def write_table(tmp_path: Path, text: str, name: str = "TinyTable") -> Path:
    """把测试用的配置表写到临时目录。

    刻意用 ``newline="\\n"``：Windows 上默认会把 ``\\n`` 写成 ``\\r\\n``，
    那样测的就不是真实文件了（真实素材是 LF）。
    """
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# ---------------------------------------------------------------------------
# 底层：单行切分
# ---------------------------------------------------------------------------
class TestSplitLine:
    def test_strips_record_terminator(self) -> None:
        """``┤`` 必须被剥掉，否则它会粘在最后一列上，导致按值比较全部失败。"""
        assert split_line("1|2|3┤\n") == ["1", "2", "3"]

    def test_handles_crlf(self) -> None:
        assert split_line("1|2├\r\n".replace("├", "┤")) == ["1", "2"]

    def test_keeps_empty_fields(self) -> None:
        """空的中间字段必须保留，否则后面所有列会整体错位。"""
        assert split_line("1||3┤") == ["1", "", "3"]

    def test_line_without_terminator(self) -> None:
        """末行没有结束符也要能解析（实测素材的最后一行就是这种）。"""
        assert split_line("1|2") == ["1", "2"]


class TestNormalizeValue:
    def test_null_becomes_none(self) -> None:
        assert normalize_value("NULL") is None

    def test_empty_becomes_none(self) -> None:
        assert normalize_value("") is None

    def test_trims_spaces(self) -> None:
        assert normalize_value("  249000006  ") == "249000006"

    def test_keeps_ordinary_value(self) -> None:
        assert normalize_value("冒險展開！") == "冒險展開！"


# ---------------------------------------------------------------------------
# 读表
# ---------------------------------------------------------------------------
class TestReadTable:
    def test_reads_rows_without_header(self, tmp_path: Path) -> None:
        path = write_table(tmp_path, "id|name┤\n1|a┤\n2|b┤\n")
        rows = read_table(path)
        assert len(rows) == 2
        assert rows[0]["id"] == "1"
        assert rows[1]["name"] == "b"

    def test_line_numbers_are_one_based_and_include_header(self, tmp_path: Path) -> None:
        """行号必须与你在编辑器里看到的行号一致（表头算第 1 行）。"""
        path = write_table(tmp_path, "id|name┤\n1|a┤\n2|b┤\n")
        rows = read_table(path)
        assert [row.line_no for row in rows] == [2, 3]
        assert rows[0].path == path

    def test_last_line_without_newline(self, tmp_path: Path) -> None:
        path = write_table(tmp_path, "id|name┤\n1|a┤\n2|b┤")  # 末尾没有换行
        assert [row["id"] for row in read_table(path)] == ["1", "2"]

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        path = write_table(tmp_path, "id|name┤\n\n1|a┤\n\n")
        assert len(read_table(path)) == 1

    def test_null_becomes_none(self, tmp_path: Path) -> None:
        path = write_table(tmp_path, "id|note┤\n1|NULL┤\n")
        assert read_table(path)[0]["note"] is None

    def test_column_count_mismatch_raises_with_line_number(self, tmp_path: Path) -> None:
        """列数错位必须立刻报错，并指出**哪一行**。

        错位后 ``row["misID"]`` 可能拿到隔壁列的值，而且看起来完全正常 ——
        这类 bug 比崩溃危险得多。
        """
        path = write_table(tmp_path, "id|name|extra┤\n1|a|b┤\n2|a┤\n")
        with pytest.raises(TableError) as excinfo:
            read_table(path)
        assert "第 3 行" in str(excinfo.value)

    def test_duplicate_column_raises(self, tmp_path: Path) -> None:
        path = write_table(tmp_path, "id|id┤\n1|2┤\n")
        with pytest.raises(TableError) as excinfo:
            read_table(path)
        assert "重复列名" in str(excinfo.value)

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        path = write_table(tmp_path, "   \n")
        with pytest.raises(TableError):
            read_table(path)

    def test_iter_table_matches_read_table(self, tmp_path: Path) -> None:
        path = write_table(tmp_path, "id|name┤\n1|a┤\n2|b┤\n")
        assert [dict(row) for row in iter_table(path)] == [dict(row) for row in read_table(path)]


# ---------------------------------------------------------------------------
# 字段类型转换
# ---------------------------------------------------------------------------
class TestTypeConversion:
    """``to_int`` / ``to_bool`` / ``to_int_list`` 的行为。"""

    @staticmethod
    def make_row(**values: str) -> TableRow:
        # 行号固定为 2：测试报错信息里是否带行号
        return TableRow(values, line_no=2, path=Path("TinyTable"))

    def test_to_int(self) -> None:
        assert to_int(self.make_row(count="3"), "count") == 3

    def test_to_int_null_uses_default(self) -> None:
        row = self.make_row(count="NULL")
        assert to_int(row, "count") is None
        assert to_int(row, "count", default=1) == 1

    def test_to_int_rejects_garbage_and_reports_column(self) -> None:
        row = self.make_row(count="三次")
        with pytest.raises(TableError) as excinfo:
            to_int(row, "count")
        assert "count" in str(excinfo.value)

    def test_to_bool(self) -> None:
        assert to_bool(self.make_row(flag="1"), "flag") is True
        assert to_bool(self.make_row(flag="false"), "flag") is False
        assert to_bool(self.make_row(flag="NULL"), "flag") is None

    def test_to_text(self) -> None:
        assert to_text(self.make_row(name="a"), "name") == "a"
        assert to_text(self.make_row(name="NULL"), "name") is None

    def test_to_int_list_single_multiple_and_empty(self) -> None:
        assert to_int_list(self.make_row(p="-1"), "p") == [-1]
        assert to_int_list(self.make_row(p="1,2 3"), "p") == [1, 2, 3]
        assert to_int_list(self.make_row(p="NULL"), "p") == []

    def test_to_int_list_rejects_garbage(self) -> None:
        with pytest.raises(TableError):
            to_int_list(self.make_row(p="1,x"), "p")


# ---------------------------------------------------------------------------
# 真实配置表（依赖素材目录，缺失时自动跳过）
# ---------------------------------------------------------------------------
MATERIAL_READY = config.TEXT_ASSET_DIR.is_dir()
requires_material = pytest.mark.skipif(
    not MATERIAL_READY,
    reason="配置表目录不存在（可用环境变量 CHERRYTALE_MATERIAL_ROOT 指定素材位置）",
)


@requires_material
class TestRealDailyMissionTable:
    """真实日常任务表的业务不变式。

    【为什么把这些数字写死】
    47 / 5 / 18 / 4 都是实测值。它们一旦变化，说明游戏更新了配置表，
    而我们的实现假设（例如「活跃宝箱只有 5 档」）需要重新确认。
    让测试在这里失败，远好过让脚本拿着一份过时的理解去发包。

    （关于 47：文件共 48 行 = 1 行表头 + 47 行数据；由于最后一行没有换行符，
     ``wc -l`` 只会报 47 —— 这个细节正好说明为什么要用解析器而不是数行数。）
    """

    EXPECTED_MISSION_COUNT = 47
    EXPECTED_BOX_COUNT = 5

    def test_column_names(self) -> None:
        mission_columns = column_names(table_path(TABLE_DAILY_MISSION))
        box_columns = column_names(table_path(TABLE_DAILY_MISSION_REWARD))
        assert len(mission_columns) == 18
        assert len(box_columns) == 4
        # 抽查几个关键列名：列名一旦被改，下面所有 to_int(row, "dmID") 都会拿到 None
        assert "dmID" in mission_columns
        assert "actionID" in mission_columns
        assert "dmrID" in box_columns

    def test_missions_loaded_and_ids_unique(self) -> None:
        missions = load_daily_missions()
        assert len(missions) == self.EXPECTED_MISSION_COUNT
        assert len(mission_index(missions)) == len(missions)

    def test_first_mission_fields(self) -> None:
        """逐字段核对第一条任务，确保「列 → 字段」的映射没有整体错位。"""
        mission = load_daily_missions()[0]
        assert mission.mission_id == 249000006
        assert mission.title == "冒險展開！"
        assert mission.action_id == "030101"
        assert mission.required_count == 1
        assert mission.add_point == 1
        assert mission.line_no == 2

    def test_action_id_is_digit_string(self) -> None:
        """``actionID`` 在配置表与代码里都是**字符串**，不要当数字用。"""
        for mission in load_daily_missions():
            if mission.action_id is not None:
                assert mission.action_id.isdigit()

    def test_reward_boxes(self) -> None:
        boxes = load_daily_reward_boxes()
        assert len(boxes) == self.EXPECTED_BOX_COUNT
        assert [box.box_id for box in boxes] == [
            939000001,
            939000002,
            939000003,
            939000004,
            939000005,
        ]
        assert [box.required_point for box in boxes] == [1, 3, 5, 7, 9]

    def test_box_id_differs_from_required_point(self) -> None:
        """防止「把 requiredNum 当成宝箱 ID」这个已经被实测纠正过的错误再犯。

        实测两者完全不同：``dmrID`` 是 939000001 起的大 ID，
        ``requiredNum`` 才是 1/3/5/7/9 的活跃点阈值。
        若哪天上游把两者改成相同，这个断言会提醒我们重新确认语义。
        """
        box = load_daily_reward_boxes()[0]
        assert box.box_id != box.required_point

    def test_boxes_for_point(self) -> None:
        boxes = load_daily_reward_boxes()
        assert boxes_for_point(boxes, 0) == []
        assert [box.required_point for box in boxes_for_point(boxes, 3)] == [1, 3]
        assert [box.required_point for box in boxes_for_point(boxes, 9)] == [1, 3, 5, 7, 9]


if __name__ == "__main__":  # 支持 `python -m tests.test_config_table`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))

