"""配置表（TextAsset）解析器：``|`` 分隔字段、``┤`` 结尾的文本表。

【为什么需要一个专门的解析器，而不是随手 split】
``Cherrytale Asset/TextAsset/`` 下有 405 个配置表，全部是无扩展名的纯文本。
实测格式（以 ``DailyMissionData`` 为例）::

    dmID|misTitle|...|goToParameter|status┤
    249000006|冒險展開！|...|-2|0┤

这种格式有三个必须处理的坑，每个坑都会导致「按值匹配全部失败」这类极难排查的问题：

1. **记录结束符 ``┤`` 必须剥掉**，否则它会粘在最后一个字段上，
   于是 ``status`` 的值变成 ``"0┤"``，任何 ``== "0"`` 的比较都会失败；
2. **文件最后一行没有换行符**（实测），处理时要保证末行同样被解析出来；
3. **空值统一写成字符串 ``NULL``**，必须转成 ``None``；否则 ``"NULL"`` 会被当成
   一个有效字符串，例如把 ``goTo`` 的 ``"NULL"`` 当成系统 ID 拿去跳转。

行尾的 ``┤`` 是 U+2524，UTF-8 编码下占三字节（``E2 94 A4``）。用错编码读取时它会变成
乱码，``endswith("┤")`` 就永远为假 —— 所以本模块**强制 UTF-8**。

【为什么不用 csv 模块】
csv 默认处理引号转义，而本游戏的配置表不做引号转义。用它反而会引入
「带引号的值被悄悄去掉引号」这类意外。直接 split 更可控；而且字段数是固定的
（``DailyMissionData`` 18 列、``DailyMissionRewardData`` 4 列、``ActiveMissionData`` 20 列），
可以顺手校验列数，把「少一个字段导致整行错位」这种灾难挡在门外。

【怎么用】
    from models.config_table import iter_table, read_table, to_int

    for row in iter_table(config.TEXT_ASSET_DIR / "DailyMissionData"):
        print(row.line_no, row["dmID"], row["misTitle"])

    rows = read_table(config.TEXT_ASSET_DIR / "DailyMissionRewardData")
    box_ids = [to_int(row["dmrID"]) for row in rows]
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final, Iterator, Mapping

_LOGGER: Final[logging.Logger] = logging.getLogger("models.config_table")

#: 记录结束符。实测本游戏所有文本配置表都用它结束一条记录。
RECORD_TERMINATOR: Final[str] = "┤"

#: 字段分隔符
FIELD_SEPARATOR: Final[str] = "|"

#: 视为「空值」的写法。空字符串也算，因为文本表里的空字段通常就是「没有值」。
NULL_TOKENS: Final[frozenset[str]] = frozenset({"NULL", "null", "Null", "NONE", "none", ""})

#: 解析用的编码。**必须是 UTF-8**：``┤`` 用 GBK 读取会变成两个乱码字符，
#: ``endswith("┤")`` 就永远不成立，末列的值会莫名多出两个字符。
TABLE_ENCODING: Final[str] = "utf-8"


class TableError(ValueError):
    """配置表格式异常。

    出现的典型原因：表被手工改坏了、游戏更新后换了格式、或读错了文件。
    异常信息里一定带上**文件名 + 行号 + 实际内容片段**——
    因为「哪张表出错」和「哪一行出错」是两件完全不同的事，缺一个都很难定位。
    """

    def __init__(self, path: Path, line_no: int, message: str, preview: str = "") -> None:
        self.path = path
        self.line_no = line_no
        self.preview = preview
        detail = f"\n  出错内容开头：{preview}" if preview else ""
        super().__init__(f"{path.name} 第 {line_no} 行：{message}{detail}")


# ---------------------------------------------------------------------------
# 底层：单行 → 字段列表
# ---------------------------------------------------------------------------
def split_line(line: str) -> list[str]:
    """把一行文本切成字段列表（已剥掉行尾的记录结束符）。

    :param line: 一行原始文本（可带 ``\\n`` 或 ``\\r\\n``，本函数会自行去掉）。
    """
    text = line.rstrip("\r\n")
    if text.endswith(RECORD_TERMINATOR):
        text = text[: -len(RECORD_TERMINATOR)]
    return text.split(FIELD_SEPARATOR)


def normalize_value(value: str) -> str | None:
    """把 ``NULL`` 这类占位符统一转成 ``None``（见模块文档第 3 个坑）。"""
    stripped = value.strip()
    return None if stripped in NULL_TOKENS else stripped


class TableRow(dict[str, str | None]):
    """一行配置。

    它既是「字段名 → 值（``None`` 表示空）」的普通字典，
    又额外携带两个定位信息：

    - :attr:`line_no`：这一行在文件里的行号（1 开始）；
    - :attr:`path`：它来自哪个配置表文件。

    **本类保证一个不变量：所有 ``NULL`` 占位符都已被转成 ``None``。**
    归一化刻意放在这里而不是读取函数里，是为了让「任何构造路径」都自动满足它 ——
    否则手工构造（例如测试、或将来从 JSON 造行）很容易漏掉归一化，
    于是 ``goTo`` 变成字符串 ``"NULL"``，被当成系统 ID 拿去跳转。

    配置表动辄几千行，报错时只说「dmID 不是整数」等于没说，
    必须能直接指出「哪个文件、哪一行、哪一列」才能立刻去改。
    """

    __slots__ = ("line_no", "path")

    def __init__(
        self,
        values: Mapping[str, str | None],
        line_no: int,
        path: Path | None = None,
    ) -> None:
        super().__init__(
            {
                name: (None if value is None else normalize_value(value))
                for name, value in values.items()
            }
        )
        self.line_no = line_no
        self.path = path if path is not None else Path("<未知来源>")

    def __repr__(self) -> str:
        return f"TableRow(line_no={self.line_no}, {dict.__repr__(self)})"


# ---------------------------------------------------------------------------
# 读表
# ---------------------------------------------------------------------------
def iter_raw_rows(
    path: Path, *, encoding: str = TABLE_ENCODING
) -> Iterator[tuple[int, list[str]]]:
    """流式产出 ``(行号, 字段列表)``，**行号从 1 开始**（第 1 行是表头）。

    为什么流式而不是一次性读进内存？
        配置表大小差别很大（``DailyMissionData`` 6 KB，``ActiveMissionData`` 1.27 MB），
        流式实现让「读大表」和「读小表」共用一套代码，不必为内存做特判。
    """
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        line_no = 0
        for line in handle:
            line_no += 1
            if not line.strip():
                # 跳过空行：部分表在末尾会多一个换行，空行不是数据。
                continue
            yield line_no, split_line(line)


def _require_consistent_width(
    path: Path, header: list[str], fields: list[str], line_no: int
) -> None:
    """校验本行字段数与表头一致。

    为什么值得为此抛异常？
        少一个字段会让**后面所有列整体左移一位**，于是 ``row["misID"]`` 可能拿到
        隔壁列的值。这种错误不会报错、不会崩溃，只会让程序「按错误的数据干活」，
        是配置表解析里最危险的一类 bug。
    """
    if len(fields) != len(header):
        raise TableError(
            path,
            line_no,
            f"字段数 {len(fields)} 与表头 {len(header)} 不一致",
            FIELD_SEPARATOR.join(fields[:8]),
        )


def read_table(path: Path, *, encoding: str = TABLE_ENCODING) -> list[TableRow]:
    """读取整张表，返回 :class:`TableRow` 列表（**不含表头行**）。

    :raises TableError: 文件为空、表头有重复列名、或某行字段数与表头不一致时。

    小表（几十到几千行）请用本函数；只有超大表才需要 :func:`iter_table`。
    """
    raw_rows = list(iter_raw_rows(path, encoding=encoding))
    if not raw_rows:
        raise TableError(path, 1, "文件为空，连表头都没有")

    header_line_no, header = raw_rows[0]
    if not header:
        raise TableError(path, header_line_no, "表头为空")
    if len(header) != len(set(header)):
        duplicates = sorted({name for name in header if header.count(name) > 1})
        raise TableError(
            path, header_line_no, f"表头存在重复列名：{duplicates}", FIELD_SEPARATOR.join(header[:8])
        )

    rows: list[TableRow] = []
    for line_no, fields in raw_rows[1:]:
        _require_consistent_width(path, header, fields, line_no)
        # 归一化（NULL → None）由 TableRow 自己保证，这里只管「列名 → 原始文本」
        rows.append(TableRow(dict(zip(header, fields)), line_no, path))
    return rows


def iter_table(path: Path, *, encoding: str = TABLE_ENCODING) -> Iterator[TableRow]:
    """逐行产出 :class:`TableRow`（**不含表头行**）。

    与 :func:`read_table` 的差别：本函数不把整张表放进内存，
    但**无法**在开始前就发现「中间某行列数不对」，只能遇到时才抛错。
    读小表请直接用 :func:`read_table`。
    """
    header: list[str] | None = None
    for line_no, fields in iter_raw_rows(path, encoding=encoding):
        if header is None:
            header = fields
            if not header:
                raise TableError(path, line_no, "表头为空")
            continue
        _require_consistent_width(path, header, fields, line_no)
        yield TableRow(dict(zip(header, fields)), line_no, path)


def column_names(path: Path, *, encoding: str = TABLE_ENCODING) -> list[str]:
    """只读表头（用于快速确认某张表的列名与顺序）。"""
    for _line_no, fields in iter_raw_rows(path, encoding=encoding):
        return fields
    raise TableError(path, 1, "文件为空，连表头都没有")


# ---------------------------------------------------------------------------
# 字段类型转换
# ---------------------------------------------------------------------------
def to_int(row: TableRow, key: str, *, default: int | None = None) -> int | None:
    """取整数字段。

    :param default: 字段为空（``NULL``）时返回的值。默认 ``None``。
    :raises TableError: 字段值不是合法整数时（带行号与列名，便于直接去改表）。

    注意它接受的是**整行**而不是裸字符串：这样出错信息里能带上行号与列名，
    否则你只会看到一句「invalid literal for int()」，完全不知道是哪张表的哪一行。
    """
    value = row.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise TableError(
            row.path,
            row.line_no,
            f"列 {key!r} 的值 {value!r} 不是整数",
        ) from exc


def to_bool(row: TableRow, key: str, *, default: bool | None = None) -> bool | None:
    """取布尔字段（配置表里用 ``1``/``0`` 或 ``true``/``false`` 表示）。"""
    value = row.get(key)
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "y"):
        return True
    if lowered in ("0", "false", "no", "n"):
        return False
    raise TableError(row.path, row.line_no, f"列 {key!r} 的值 {value!r} 不是布尔值")


def to_text(row: TableRow, key: str, *, default: str | None = None) -> str | None:
    """取文本字段（``NULL`` 已由解析层转成 ``None``）。"""
    value = row.get(key)
    return default if value is None else value


def to_int_list(row: TableRow, key: str, *, separators: str = ",; ") -> list[int]:
    """取整数数组字段。

    实测本游戏的数组列（如 ``goToParameter``）通常只写一个值或 ``NULL``，
    但仍然按「可能有多个」实现：一旦游戏改成 ``1,2,3``，
    这里能正确解析，而不是把它当成一个天文数字的 ID 发出去。
    """
    value = row.get(key)
    if value is None:
        return []
    text = value.replace(",", " ").replace(";", " ").strip()
    if not text:
        return []
    result: list[int] = []
    for token in text.split():
        try:
            result.append(int(token))
        except ValueError as exc:
            raise TableError(
                row.path, row.line_no, f"列 {key!r} 的元素 {token!r} 不是整数"
            ) from exc
    return result

