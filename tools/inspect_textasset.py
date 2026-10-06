"""配置表预览工具：探测 TextAsset 目录下文件的编码、分隔符与结构。

【为什么需要它】
``Cherrytale Asset/TextAsset/`` 下有 405 个文件，其中 **397 个没有扩展名**
（例如 ``AchievementData`` 31 KB、``ActiveMissionData`` 1.27 MB）。
面对这种「没有后缀的文件」，你无法靠文件名判断该怎么解析。
本工具只读文件开头的一小段，就能告诉你：

- 是不是带 BOM、用什么编码（UTF-8 / UTF-16 / GBK）；
- 用什么分隔符（逗号 / 制表符 / 竖线 …）；
- 首行是什么、大概有多少列。

【用法】
    python tools/inspect_textasset.py --list                # 列出全部配置表
    python tools/inspect_textasset.py --list "Activity*"    # 按通配符过滤
    python tools/inspect_textasset.py --name AchievementData
    python tools/inspect_textasset.py --scan --limit 20     # 批量探测前 20 个

本工具**只读取文件开头**（默认 64 KB），不会把 1 MB+ 的配置表整个读进内存。
"""

from __future__ import annotations

import argparse
import codecs
import sys
from pathlib import Path
from typing import Final

# 让本脚本能 import 到项目根目录下的 config（tools/ 不是包，只能手动加路径）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (必须在 sys.path 调整之后导入)

#: 读取的最大字节数：足以判断编码与结构，又不会造成内存负担
DEFAULT_PREVIEW_BYTES: Final[int] = 64 * 1024

#: 候选分隔符（``\x01`` 是部分导出工具用的罕见分隔符）
DELIMITER_CANDIDATES: Final[tuple[str, ...]] = (",", "\t", "|", ";", "\x01")

#: 部分导出工具会在每行末尾追加一个「记录结束符」。
#: 实测**本游戏**的配置表每条记录以 ``┤`` 结尾（例如 AchievementData 的
#: ``subID|groupID|...|status┤``），因此解析时必须先剥掉它，
#: 否则它会被当成最后一列内容的一部分，导致「按值匹配」全部失败。
RECORD_TERMINATOR_CANDIDATES: Final[tuple[str, ...]] = ("┤", "\x1e", "\x03")


def read_preview(path: Path, max_bytes: int = DEFAULT_PREVIEW_BYTES) -> tuple[str, str]:
    """读取文件开头并判断编码。

    :return: ``(文本内容, 编码名称)``。
    """
    with path.open("rb") as handle:
        raw = handle.read(max_bytes)

    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace"), "utf-8 (带 BOM)"
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16", errors="replace"), "utf-16 (带 BOM)"

    # 没有 BOM 时：优先按 UTF-8 严格解码，失败再退到 GBK。
    # 顺序很重要 —— 若先用 GBK，合法的 UTF-8 中文会被解成乱码却「不报错」。
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue

    return raw.decode("latin-1", errors="replace"), "latin-1（回退方案）"


def detect_delimiter(first_line: str) -> tuple[str | None, int]:
    """从首行推断分隔符。

    :return: ``(分隔符, 列数)``；没有找到分隔符时返回 ``(None, 1)``。
    """
    counts = {candidate: first_line.count(candidate) for candidate in DELIMITER_CANDIDATES}
    best = max(counts, key=lambda candidate: counts[candidate])
    if counts[best] == 0:
        return None, 1
    return best, counts[best] + 1


def render_delimiter(delimiter: str | None) -> str:
    """把分隔符渲染成可读文本（制表符要显式写出来，否则看不出来）。"""
    if delimiter is None:
        return "<未发现分隔符>"
    return {"\t": "\\t (制表符)", "\x01": "\\x01 (控制符)"}.get(delimiter, delimiter)


def detect_record_terminator(first_line: str) -> str | None:
    """检测行尾的「记录结束符」。

    :return: 结束符字符；没有则返回 ``None``。

    为什么要单独检测它？因为它既不是分隔符也不是换行符，
    而是导出工具额外插入的符号。漏剥它会让最后一个字段多出一个怪字符，
    进而导致所有「按值匹配」静默失败——这类 bug 极难排查。
    """
    if first_line and first_line[-1] in RECORD_TERMINATOR_CANDIDATES:
        return first_line[-1]
    return None


def render_record_terminator(terminator: str | None) -> str:
    """把记录结束符渲染成可读文本（附上码位，避免你分不清相似符号）。"""
    if terminator is None:
        return "<无>"
    return f"{terminator} (U+{ord(terminator):04X})"


def describe_file(path: Path, *, lines: int = 3, max_bytes: int = DEFAULT_PREVIEW_BYTES) -> str:
    """生成单个文件的探测报告。"""
    text, encoding = read_preview(path, max_bytes)
    raw_lines = text.splitlines()
    first_line = raw_lines[0] if raw_lines else ""

    terminator = detect_record_terminator(first_line)
    # 剥掉结束符再数分隔符，否则列数判断可能被它干扰
    header = first_line[:-1] if terminator else first_line
    delimiter, columns = detect_delimiter(header)

    report = [
        f"文件      : {path.name}",
        f"大小      : {path.stat().st_size / 1024:.1f} KB",
        f"编码      : {encoding}",
        f"分隔符    : {render_delimiter(delimiter)}",
        f"行尾标记  : {render_record_terminator(terminator)}",
        f"列数(估计): {columns}",
    ]
    for index, line in enumerate(raw_lines[:lines], start=1):
        preview = line[:200]
        suffix = " …" if len(line) > 200 else ""
        report.append(f"  第{index}行: {preview}{suffix}")
    return "\n".join(report)


def list_tables(pattern: str = "*") -> list[Path]:
    """列出配置表文件（按名称排序）。"""
    if not config.TEXT_ASSET_DIR.is_dir():
        return []
    return sorted(
        (path for path in config.TEXT_ASSET_DIR.iterdir() if path.is_file() and path.match(pattern)),
        key=lambda path: path.name,
    )


def force_utf8_output() -> None:
    """把标准输出/错误流强制改为 UTF-8 编码。

    【为什么这里故意「重复」了 main.py 里的同名函数】
    本文件是**独立脚本**：你可能只把它单独拷出来用，甚至在不同项目里复用。
    如果它反过来 import main.py，就会平白依赖上一整条命令行入口链
    （argparse、config、任务注册表……），反而更脆弱。
    这 4 行代码的重复是刻意换取的「零耦合」，比强行抽公共模块更划算。

    Windows 默认 cp936(GBK)，输出重定向到管道时中文会变成乱码
    （例如「配置表目录」显示成 ``□□□ñ□Ŀ¼``），因此必须在入口处改掉。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inspect_textasset.py",
        description="探测 TextAsset 配置表的编码与分隔符（只读文件开头，不吃内存）",
    )
    parser.add_argument("--list", nargs="?", const="*", metavar="通配符",
                        help="列出配置表，可选用通配符过滤，例如 \"Activity*\"")
    parser.add_argument("--name", metavar="文件名", help="探测指定文件的详细结构")
    parser.add_argument("--scan", action="store_true", help="批量探测所有文件的分隔符分布")
    parser.add_argument("--limit", type=int, default=10, help="--scan 时最多处理多少个文件")
    parser.add_argument("--lines", type=int, default=3, help="预览行数（默认 3）")
    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()
    args = build_parser().parse_args(argv)

    if not config.TEXT_ASSET_DIR.is_dir():
        print(f"✘ 找不到配置表目录：{config.TEXT_ASSET_DIR}")
        print("  可用环境变量指定素材位置：export CHERRYTALE_MATERIAL_ROOT=/path/to/materials")
        return 1

    if args.list is not None:
        tables = list_tables(args.list)
        print(f"配置表目录：{config.TEXT_ASSET_DIR}")
        print(f"共 {len(tables)} 个文件（过滤条件：{args.list}）")
        for path in tables:
            print(f"  {path.stat().st_size:>10,} B  {path.name}")
        return 0

    if args.name:
        target = config.TEXT_ASSET_DIR / args.name
        if not target.is_file():
            print(f"✘ 文件不存在：{target}")
            return 1
        print(describe_file(target, lines=args.lines))
        return 0

    if args.scan:
        tables = list_tables()[: args.limit]
        print(f"批量探测前 {len(tables)} 个文件（按名称排序）：")
        print("-" * 72)
        for path in tables:
            text, encoding = read_preview(path)
            lines = text.splitlines()
            first_line = lines[0] if lines else ""
            delimiter, columns = detect_delimiter(first_line)
            print(
                f"{path.name:<32} {encoding:<14} "
                f"{render_delimiter(delimiter):<16} 列数≈{columns}"
            )
        print("-" * 72)
        print("下一步：确认格式后，可在 models/ 中为对应配置表生成数据结构。")
        return 0

    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
