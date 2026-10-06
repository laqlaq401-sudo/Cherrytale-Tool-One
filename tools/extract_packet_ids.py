#!/usr/bin/env python3
"""从 dump.cs 提取「消息号表」与「常量表」，自动生成 Python 模块。

【为什么必须自动生成，而不是手抄】
``dump.cs`` 里的 ``ePacketFormat_RPS`` 枚举有 **868 个成员**（每个协议类对应一个
消息号），``RPS_Const`` 有 **672 个常量**。手抄有两个致命问题：

1. 抄错一个数字，症状是「服务端收到一个不认识的包，静默丢弃」——
   服务端不会告诉你哪个字段错了，排查起来毫无线索；
2. dump.cs 会随游戏版本更新而重新导出，手抄的表无法跟着更新。

所以这里做成「一条命令重新生成」，并在生成文件头部写明「请勿手工编辑」。

【关键事实（已在 dump.cs 中核实）】
- ``ePacketFormat_RPS`` 位于行 384447~385319，成员格式固定为::

      \tpublic const ePacketFormat_RPS LoginPacket = 1003;

- **类名 == 枚举成员名**：客户端用 ``NetWorkModule.GetSendPacketType()`` 拿到
  ``out string className``，再按名字查这张表得到消息号。因此「类名 → 消息号」
  这张映射表就是发包的全部依据（不需要抓包也能确定要填哪个号）。
- 枚举内数值**无重复**（已用 ``grep -oE '= [0-9]+;' | sort | uniq -d`` 验证为空），
  所以「消息号 → 类名」的反查表也是安全的。

【用法】
    python tools/extract_packet_ids.py                  # 生成 models/packet_ids.py
    python tools/extract_packet_ids.py --target consts  # 生成 models/const_ids.py
    python tools/extract_packet_ids.py --target all     # 两个都生成
    python tools/extract_packet_ids.py --check          # 只校验现有文件是否过期（不写盘）
    python tools/extract_packet_ids.py --find DMReward  # 按名字查消息号（含模糊匹配）

【环境变量】
    CHERRYTALE_DUMP_CS   指定 dump.cs 路径（默认取 config.DUMP_CS_PATH）
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path
from typing import Final, Iterator, Sequence

# tools/ 不是包，无法直接 ``import config``，只能在 sys.path 里手动加项目根目录。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (必须在 sys.path 调整之后导入)

#: 枚举名。它同时是「类名 → 消息号」查询表的键空间名称。
PACKET_ENUM_NAME: Final[str] = "ePacketFormat_RPS"

#: 常量类名（资源 ID、系统 ID、UI 键名等）。
CONST_CLASS_NAME: Final[str] = "RPS_Const"

#: 枚举成员行，例如 ``\tpublic const ePacketFormat_RPS DMReward = 6005;``
_ENUM_MEMBER_RE: Final[re.Pattern[str]] = re.compile(
    rf"^\s*public const {PACKET_ENUM_NAME} (\w+) = (-?\d+);"
)

#: 常量成员行，例如 ``\tpublic const int COINS_ID = 200000050;``
_CONST_MEMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*public const (int|long|float|double|bool|string) (\w+) = (.+);"
)


# ---------------------------------------------------------------------------
# dump.cs 解析
# ---------------------------------------------------------------------------
def iter_type_block(path: Path, declaration: str) -> Iterator[str]:
    """逐行产出某个类型（class / enum）声明体**内部**的行。

    :param path: dump.cs 路径。
    :param declaration: 类型声明的**行首前缀**，例如 ``"public enum ePacketFormat_RPS"``。

    为什么必须用「行首前缀」而不是「包含子串」？
        dump.cs 前半部分有一大段泛型实例清单，长这样::

            |-Func<DailyMission_UIView.Mission, int>..ctor
            |-Dictionary<int, List<DailyMis_AchieveNetWorkModule.GuideMission>>..ctor

        这些行同样包含类型名，用「包含」匹配会把它们一起抓进来，
        解析出几百条根本不属于该类型的「垃圾成员」。用行首前缀即可排除。

    为什么只扫一遍、不先定位行号？
        本文件 33.2 MB，Python 逐行迭代约 1~2 秒。先 find 再 read 要遍历两次；
        而本函数要在两个目标（枚举 + 常量类）上各调用一次，简单实现更不易出错。

    类型的结束标记是**顶格的** ``}``：嵌套类型结束时会带缩进（``\t}``），
    因此 ``line.startswith("}")`` 能精确定位到外层类型的结尾。
    """
    inside = False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not inside:
                if line.startswith(declaration):
                    inside = True
                continue
            if line.startswith("}"):
                return
            yield line


def extract_packet_ids(path: Path) -> list[tuple[str, int]]:
    """提取「协议类名 → 消息号」列表（保持 dump.cs 中的原始顺序）。

    保持原始顺序而不是排序，是因为 dump.cs 里的顺序本身就是**按业务分组**的：
    1001 段是登录、6001 段是日常任务、13001 段是邮件……排查时更容易对照。
    """
    entries: list[tuple[str, int]] = []
    for line in iter_type_block(path, f"public enum {PACKET_ENUM_NAME}"):
        match = _ENUM_MEMBER_RE.match(line)
        if match:
            entries.append((match.group(1), int(match.group(2))))
    return entries


def _parse_literal(kind: str, raw: str) -> int | float | bool | str | None:
    """把 C# 常量字面量转成 Python 值；无法安全解析时返回 ``None``。

    为什么解析不了要返回 ``None`` 而不是抛异常？
        常量表里混着少量非常规写法（例如字符串拼接 ``"a" + "b"``）。
        我们宁可**跳过并统计**，也不要因为一个无关常量让整个生成流程失败——
        但统计数字会打印出来，避免出现「悄悄漏掉一批」这种情况。

    :param kind: C# 类型关键字（``int`` / ``long`` / ``float`` / ``double`` / ``bool`` / ``string``）。
    :param raw: 等号右边的原始文本。
    """
    text = raw.strip()

    if kind in ("int", "long"):
        try:
            return int(text)
        except ValueError:
            return None

    if kind in ("float", "double"):
        # C# 的浮点字面量常带后缀，例如 ``1.0f``、``2d``，直接 float() 会失败。
        try:
            return float(text.rstrip("fFdD"))
        except ValueError:
            return None

    if kind == "bool":
        if text == "true":
            return True
        if text == "false":
            return False
        return None

    if kind == "string":
        # 只接受最简单的 "xxx" 形态。转义序列（\" \\）与拼接表达式一律跳过：
        # 猜错一个转义会让整个字符串变成错值，而它可能正好是签名用的盐。
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            inner = text[1:-1]
            if "\\" in inner or '"' in inner:
                return None
            return inner
        return None

    return None


def extract_consts(path: Path) -> tuple[list[tuple[str, int | float | bool | str]], int]:
    """提取 ``RPS_Const`` 里的常量。

    :return: ``(可用常量列表, 被跳过的成员数)``。

    ``RPS_Const`` 里同时存在 ``public const`` 与 ``public static readonly``：
    后者（如 ``defaulResolution``）的值是运行时算出来的，dump.cs 里没有初值，
    因此本正则只匹配 ``public const``，天然把 readonly 排除在外。
    """
    entries: list[tuple[str, int | float | bool | str]] = []
    skipped = 0
    for line in iter_type_block(path, f"public class {CONST_CLASS_NAME}"):
        match = _CONST_MEMBER_RE.match(line)
        if not match:
            continue
        value = _parse_literal(match.group(1), match.group(3))
        if value is None:
            skipped += 1
            continue
        entries.append((match.group(2), value))
    return entries, skipped



# ---------------------------------------------------------------------------
# 生成 Python 模块：消息号表
# ---------------------------------------------------------------------------
#: 生成文件的来源标注（相对项目根目录）
_SOURCE_LABEL: Final[str] = "Cherrytale IL2CPP/dump.cs"

_PACKET_MODULE_DOC = '''"""Cherrytale 协议消息号表（**由 tools/extract_packet_ids.py 自动生成，请勿手工编辑**）。

【这张表解决什么问题】
客户端发包时，外层封包 ``RootPacket`` 里有一个 ``packetID`` 字段，
服务端靠它决定「这一包数据该按哪个 protobuf 类解析」。
它的取值来自 dump.cs 里的 ``ePacketFormat_RPS`` 枚举，而**枚举成员名与协议类名完全一致**：

    协议类 LoginPacket   → 枚举成员 LoginPacket   → 消息号 1003
    协议类 DMReward      → 枚举成员 DMReward      → 消息号 6005

所以在 Python 侧只要写出类名就能查到该填的消息号，既不用猜，也不用抓包核对。

【怎么用】
    from models.packet_ids import packet_id, packet_name, search

    packet_id("DMReward")        # 6005
    packet_name(6005)            # 'DMReward'
    packet_id("DMRewad")         # 抛 KeyError，并提示相近的名字 'DMReward'
    search("daily")              # 列出名字里含 daily 的全部协议

【维护方式】
dump.cs 更新后重新生成即可（**不要手改本文件**，手改会在下次生成时被覆盖）::

    python tools/extract_packet_ids.py

【生成信息】
    · 来源文件：Cherrytale IL2CPP/dump.cs
    · 来源枚举：ePacketFormat_RPS
    · 成员数量：{count}

生成文件里刻意**不写生成时间**：否则每次重新生成都会产生无意义的 diff，
``--check`` 也就无法用来判断「表是否真的过期」。
"""'''


def _render_dict(entries: Sequence[tuple[str, object]], indent: str = "    ") -> list[str]:
    """把 ``(名字, 值)`` 列表渲染成多行 dict 字面量（每行一条，便于 diff 阅读）。

    用 ``repr()`` 而不是自己拼引号：字符串里的特殊字符、数字的符号都由 Python 保证正确，
    这正是「生成」比「手抄」安全的地方。
    """
    return [f"{indent}{name!r}: {value!r}," for name, value in entries]



#: 生成文件里附带的查询函数源码（作为字符串嵌入，因为它要写进**生成的文件**）
_PACKET_HELPERS = '''

# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------
def packet_id(name: str) -> int:
    """按协议类名取消息号。

    :raises KeyError: 名字不存在时。错误信息里会附上**最相近的候选名字**——
        逆向时最常见的手误是少打/多打一个字母，直接给候选能省下翻 dump.cs 的时间。
    """
    try:
        return PACKET_IDS[name]
    except KeyError as exc:
        candidates = difflib.get_close_matches(name, PACKET_IDS.keys(), n=5, cutoff=0.5)
        hint = f"；最相近的名字：{'、'.join(candidates)}" if candidates else ""
        raise KeyError(f"未知的协议类名 {name!r}{hint}") from exc


def packet_name(value: int) -> str | None:
    """按消息号取协议类名。

    :return: 类名；消息号不在表中时返回 ``None``。

    返回 ``None`` 而不是抛异常，是为了让日志代码可以写成
    ``packet_name(pid) or f"<未知消息号 {pid}>"`` 这种一行表达式。
    """
    return PACKET_NAMES.get(value)


def search(keyword: str, *, limit: int = 50) -> list[tuple[str, int]]:
    """按关键词模糊查找协议（大小写不敏感，同时匹配类名与消息号数字）。

    典型用途：想找「日常任务」相关协议时执行 ``search("Daily")``，
    不必事先知道确切的类名。
    """
    lowered = keyword.lower()
    hits: list[tuple[str, int]] = []
    for name, value in PACKET_IDS.items():
        if lowered in name.lower() or keyword in str(value):
            hits.append((name, value))
            if len(hits) >= limit:
                break
    return hits
'''


def render_packet_ids_module(entries: Sequence[tuple[str, int]]) -> str:
    """渲染 ``models/packet_ids.py`` 的完整内容。"""
    lines: list[str] = []
    lines.append(_PACKET_MODULE_DOC.format(count=len(entries)))
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("import difflib")
    lines.append("from typing import Final")
    lines.append("")
    lines.append("#: 生成来源（相对项目根目录）")
    lines.append(f'SOURCE_DUMP: Final[str] = "{_SOURCE_LABEL}"')
    lines.append("")
    lines.append("#: 消息号所在枚举名（协议类名 == 枚举成员名，这是查表能成立的原因）")
    lines.append(f'SOURCE_ENUM: Final[str] = "{PACKET_ENUM_NAME}"')
    lines.append("")
    lines.append("#: 本表收录的消息号数量")
    lines.append(f"PACKET_COUNT: Final[int] = {len(entries)}")
    lines.append("")
    lines.append("#: 协议类名 → 消息号。**发包时唯一需要查的表。**")
    lines.append("PACKET_IDS: Final[dict[str, int]] = {")
    for name, value in entries:
        lines.append(f"    {name!r}: {value!r},")
    lines.append("}")
    lines.append("")
    lines.append("#: 消息号 → 协议类名（反查，用于把收到的包翻译成可读名字）")
    lines.append("PACKET_NAMES: Final[dict[int, str]] = {")
    for name, value in entries:
        lines.append(f"    {value!r}: {name!r},")
    lines.append("}")
    lines.append(_PACKET_HELPERS.strip("\n"))
    return "\n".join(lines) + "\n"



# ---------------------------------------------------------------------------
# 生成 Python 模块：常量表
# ---------------------------------------------------------------------------
_CONST_MODULE_DOC = '''"""RPS_Const 常量表（**由 tools/extract_packet_ids.py 自动生成，请勿手工编辑**）。

【这张表解决什么问题】
游戏里所有「资源 ID」都是裸数字：金币 200000050、钻石 200000001、体力 200000002……
直接写数字会让代码充满看不懂的魔法值，而且最容易出的错就是**记错位数**
（``200000005`` 与 ``200000050`` 只差一位），后果可能是买错东西或领错奖励。

``RPS_Const`` 是客户端自己定义的常量类，本表把它原样导出，
让 Python 侧可以写成 ``INT_CONSTS["COINS_ID"]`` 这种自解释的形式。

【怎么用】
    from models.const_ids import INT_CONSTS, const_value, search

    INT_CONSTS["COINS_ID"]          # 200000050 金币
    INT_CONSTS["DIAMOND_ID"]        # 200000001 钻石
    const_value("Language_Prefab_FOLDER")
    search("DRAGON")                # 模糊查找相关常量

【一个容易踩的坑：bool 是 int 的子类】
Python 里 ``isinstance(True, int)`` 的结果是 ``True``，所以 ``INT_CONSTS`` 必须
显式排除 ``bool``，否则 ``IsOfficelState = true`` 这类布尔常量会混进「资源 ID」里，
之后按 ID 取值时就会拿到 True。

【维护方式】
    python tools/extract_packet_ids.py --target consts

【生成信息】
    · 来源文件：Cherrytale IL2CPP/dump.cs
    · 来源类型：RPS_Const
    · 可用常量数量：{count}（另有 {skipped} 个非常规写法被跳过，例如字符串拼接）
"""'''

_CONST_HELPERS = '''

# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------
def const_value(name: str) -> int | float | bool | str:
    """按名字取常量值。

    :raises KeyError: 名字不存在时，错误信息里附上最相近的候选名字。
    """
    try:
        return CONSTS[name]
    except KeyError as exc:
        candidates = difflib.get_close_matches(name, CONSTS.keys(), n=5, cutoff=0.5)
        hint = f"；最相近的名字：{'、'.join(candidates)}" if candidates else ""
        raise KeyError(f"未知的常量名 {name!r}{hint}") from exc


def int_const(name: str) -> int:
    """按名字取**整型**常量（资源 ID 请用本函数，返回类型明确）。

    :raises KeyError: 名字不存在，或该常量不是整数（例如它其实是字符串）。
        两种情况刻意都抛 KeyError：调用方想要的都是「一个可用的整数 ID」，
        区分这两种失败对排查没有帮助，反而要求上层多写一段异常处理。
    """
    value = const_value(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise KeyError(f"常量 {name!r} 不是整数，而是 {type(value).__name__}：{value!r}")
    return value


def search(keyword: str, *, limit: int = 50) -> list[tuple[str, int | float | bool | str]]:
    """按关键词模糊查找常量（大小写不敏感）。"""
    lowered = keyword.lower()
    hits: list[tuple[str, int | float | bool | str]] = []
    for name, value in CONSTS.items():
        if lowered in name.lower() or keyword in str(value):
            hits.append((name, value))
            if len(hits) >= limit:
                break
    return hits
'''



def render_const_ids_module(
    entries: Sequence[tuple[str, int | float | bool | str]], skipped: int
) -> str:
    """渲染 ``models/const_ids.py`` 的完整内容。"""
    lines: list[str] = []
    lines.append(_CONST_MODULE_DOC.format(count=len(entries), skipped=skipped))
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("import difflib")
    lines.append("from typing import Final")
    lines.append("")
    lines.append(f'SOURCE_DUMP: Final[str] = "{_SOURCE_LABEL}"')
    lines.append(f'SOURCE_TYPE: Final[str] = "{CONST_CLASS_NAME}"')
    lines.append("")
    lines.append("#: 可用常量数量")
    lines.append(f"CONST_COUNT: Final[int] = {len(entries)}")
    lines.append("")
    lines.append('#: 因写法特殊被跳过的成员数量（例如 "a" + "b" 这类拼接表达式）')
    lines.append(f"SKIPPED_CONST_COUNT: Final[int] = {skipped}")
    lines.append("")
    lines.append("#: 常量名 → 值（原样导出 RPS_Const 里的 public const 成员）")
    lines.append("CONSTS: Final[dict[str, int | float | bool | str]] = {")
    for name, value in entries:
        lines.append(f"    {name!r}: {value!r},")
    lines.append("}")
    lines.append("")
    lines.append("#: 从 CONSTS 派生的「纯整数」视图：资源 ID / 系统 ID 都在这里。")
    lines.append("#: ⚠️ 必须排除 bool —— Python 里 isinstance(True, int) 的结果是 True。")
    lines.append("INT_CONSTS: Final[dict[str, int]] = {")
    lines.append("    name: value")
    lines.append("    for name, value in CONSTS.items()")
    lines.append("    if isinstance(value, int) and not isinstance(value, bool)")
    lines.append("}")
    lines.append(_CONST_HELPERS.strip("\n"))
    return "\n".join(lines) + "\n"



# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def force_utf8_output() -> None:
    """把标准输出/错误流强制改为 UTF-8。

    【为什么独立脚本要自带这几行，而不是从 main.py 导入】
    Windows 默认编码是 cp936(GBK)，输出被重定向到管道时中文会变成乱码。
    本文件可能被单独拷出去用（甚至用在别的项目里），若反过来 import main.py，
    就会平白依赖上整条命令行入口链（argparse、任务注册表……），反而更脆弱。
    这几行代码的重复是刻意换取的「零耦合」，与 tools/inspect_textasset.py 保持一致。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def resolve_dump_path(override: str | None) -> Path:
    """确定 dump.cs 路径，优先级：命令行参数 > config（含环境变量）> 默认相对路径。"""
    if override:
        return Path(override).expanduser()
    return config.DUMP_CS_PATH


def write_module(path: Path, content: str) -> None:
    """写出生成文件。

    ``newline="\\n"`` 是刻意指定的：Windows 上默认会把 ``\\n`` 写成 ``\\r\\n``，
    那样生成结果就会随平台变化，``--check`` 在别的机器上会误报「不一致」。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def check_module(path: Path, expected: str) -> bool:
    """比较磁盘上的文件与「现在重新生成会得到的内容」。

    :return: 一致返回 ``True``；文件缺失或有差异返回 ``False``，
        并打印差异片段（``-`` 是磁盘现状，``+`` 是重新生成的结果）。
    """
    if not path.is_file():
        print(f"✘ 文件不存在：{path}")
        print("  修复：运行 python tools/extract_packet_ids.py 生成它")
        return False

    actual = path.read_text(encoding="utf-8")
    if actual == expected:
        print(f"✔ 一致     : {path.name}（{len(expected.splitlines())} 行）")
        return True

    diff = list(
        difflib.unified_diff(
            actual.splitlines(),
            expected.splitlines(),
            fromfile=path.name,
            tofile="<重新生成>",
            lineterm="",
            n=1,
        )
    )
    print(f"✘ 已过期   : {path.name} 与 dump.cs 有 {len(diff)} 行差异")
    print("  差异片段（- 磁盘现状 / + 重新生成的结果）：")
    for line in diff[:20]:
        print(f"    {line}")
    print("  修复：运行 python tools/extract_packet_ids.py 覆盖它")
    return False


def _search_entries(
    entries: Sequence[tuple[str, object]], keyword: str, limit: int
) -> list[tuple[str, object]]:
    """在已解析出的 ``(名字, 值)`` 列表里做关键词过滤（供 ``--find`` 使用）。

    生成出来的模块里也有一份同名函数，但「表还没落盘时也要能查」，
    所以脚本必须自带最小实现。重复的只是一个几行循环，不值得为此抽公共模块。
    """
    lowered = keyword.lower()
    hits = [
        (name, value)
        for name, value in entries
        if lowered in name.lower() or keyword in str(value)
    ]
    return hits[:limit]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_packet_ids.py",
        description="从 dump.cs 生成协议消息号表与常量表（自动生成，避免手抄出错）",
    )
    parser.add_argument("--dump", metavar="路径", help="dump.cs 路径（默认取 config.DUMP_CS_PATH）")
    parser.add_argument(
        "--target",
        choices=("packets", "consts", "all"),
        default="packets",
        help="生成哪张表：packets=消息号（默认）、consts=常量、all=两张都生成",
    )
    parser.add_argument(
        "--check", action="store_true", help="只校验现有文件是否与 dump.cs 一致（不写盘）"
    )
    parser.add_argument("--find", metavar="关键词", help="查询消息号（模糊匹配，不写盘）")
    parser.add_argument("--limit", type=int, default=30, help="--find 最多显示多少条（默认 30）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    force_utf8_output()
    args = build_parser().parse_args(argv)

    dump_path = resolve_dump_path(args.dump)
    if not dump_path.is_file():
        print(f"✘ 找不到 dump.cs：{dump_path}")
        print("  可用 --dump 指定路径，或设置环境变量 CHERRYTALE_MATERIAL_ROOT")
        return 1

    print(f"dump.cs    : {dump_path}（{dump_path.stat().st_size / 1024 / 1024:.1f} MB）")

    # ---- 纯查询模式：不写盘、不解析常量表，尽量快 ----
    if args.find:
        packet_entries = extract_packet_ids(dump_path)
        hits = _search_entries(packet_entries, args.find, args.limit)
        print(f"查询       : {args.find!r}（命中 {len(hits)} 条，最多显示 {args.limit} 条）")
        print("-" * 72)
        for name, value in hits:
            print(f"  {value:>6}  {name}")
        if not hits:
            print("  （没有命中，换个关键词试试，例如 Daily / Mail / Login / Draw）")
        return 0

    targets = ("packets", "consts") if args.target == "all" else (args.target,)
    everything_ok = True

    for target in targets:
        if target == "packets":
            packet_entries = extract_packet_ids(dump_path)
            if not packet_entries:
                print("✘ 没解析到任何消息号：dump.cs 的格式可能与预期不同")
                return 1
            content = render_packet_ids_module(packet_entries)
            output_path = config.PROJECT_ROOT / "models" / "packet_ids.py"
            print(f"消息号表   : 解析到 {len(packet_entries)} 条")
        else:
            const_entries, skipped = extract_consts(dump_path)
            if not const_entries:
                print("✘ 没解析到任何常量：dump.cs 的格式可能与预期不同")
                return 1
            content = render_const_ids_module(const_entries, skipped)
            output_path = config.PROJECT_ROOT / "models" / "const_ids.py"
            print(f"常量表     : 解析到 {len(const_entries)} 条（跳过 {skipped} 条非常规写法）")

        if args.check:
            okay = check_module(output_path, content)
        else:
            write_module(output_path, content)
            print(f"✔ 已生成   : {output_path}（{len(content.splitlines())} 行）")
            okay = True
        everything_ok = everything_ok and okay

    return 0 if everything_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

