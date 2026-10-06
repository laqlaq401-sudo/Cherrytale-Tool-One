#!/usr/bin/env python3
"""从 global-metadata.dat 提取**字符串字面量**（编译器眼中的字符串常量池）。

【为什么要自己解析，而不是用 grep -a 搜二进制】
用 ``grep -a -o`` 在二进制里搜文本，得到的是「连续的可打印字节」——
它无法区分「一个字符串在哪里结束、下一个从哪里开始」。于是你会看到这种怪东西::

    auer1342e595709065940eb77dc1fea3c87b43432d18c9-9348-4b90-bfbf-9f2a10e1f15b43743_

这其实是 2~3 个字符串**粘在一起**的结果。既然分不清边界，就无法判断
「到底哪一段才是签名用的盐」，据此实现的算法必然是错的。

而 ``global-metadata.dat`` 里本来就有**精确的字符串字面量索引表**：
每条记录 8 字节，给出「长度」与「在数据区中的偏移」。
按它读取，得到的就是编译器眼里一个不多、一个不少的字符串。

【文件结构（已用本文件实测验证，本机版本 29）】
====  ================================================================
偏移  含义
====  ================================================================
0x00  sanity = 0xFAB11BAF（魔数，用来确认文件没搞错）
0x04  version = 元数据版本
0x08  stringLiteralOffset      索引表起始偏移
0x0C  stringLiteralCount       索引表字节数（每条 8 字节）
0x10  stringLiteralDataOffset  字符串数据区起始偏移
0x14  stringLiteralDataCount   数据区字节数
====  ================================================================

索引表每条 8 字节：``uint32 length; int32 dataIndex;``
字符串本身是 **UTF-8 且不带结尾的 \\0**，必须按 ``length`` 精确切片。

【用法】
    python tools/extract_metadata_strings.py                  # 生成 notes/metadata_strings.txt
    python tools/extract_metadata_strings.py --grep login     # 搜索命中（含大小写不敏感）
    python tools/extract_metadata_strings.py --grep 030101    # 验证配置表里的 actionID 是否作为字面量存在
    python tools/extract_metadata_strings.py --check          # 校验生成文件是否过期
    python tools/extract_metadata_strings.py --limit 5        # 每类最多展示多少条（默认 40）

【环境变量】
    CHERRYTALE_METADATA_DAT   指定 global-metadata.dat 路径（默认同 dump.cs 目录）
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterator, Sequence

# tools/ 不是包，无法直接 ``import config``，只能手动把项目根加进 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (必须在 sys.path 调整之后导入)

#: 元数据文件魔数。读出来不是它，说明文件选错了（例如误传了 GameAssembly.dll）。
METADATA_MAGIC: Final[int] = 0xFAB11BAF

#: 头部前 24 字节就够取出我们需要的一切（见模块文档的结构表）
_HEADER_STRUCT: Final[struct.Struct] = struct.Struct("<Ii")
_HEADER_OFFSETS_STRUCT: Final[struct.Struct] = struct.Struct("<IIII")

#: 索引表条目：uint32 length; int32 dataIndex
_LITERAL_ENTRY_STRUCT: Final[struct.Struct] = struct.Struct("<Ii")

#: 生成的说明文件（相对项目根目录）
OUTPUT_RELATIVE_PATH: Final[str] = "notes/metadata_strings.txt"


class MetadataError(RuntimeError):
    """global-metadata.dat 结构不符合预期。

    最典型的原因：文件被截断、选错了文件、或游戏大版本更新后格式变了。
    这类错误必须**明确报出来**——如果悄悄返回一个空列表，
    「没找到 salt」会被误读成「游戏里本来就没有 salt」，排查方向直接跑偏。
    """


@dataclass(frozen=True)
class MetadataHeader:
    """我们关心的那部分头部信息。"""

    version: int
    literal_table_offset: int
    literal_table_size: int
    literal_data_offset: int
    literal_data_size: int

    @property
    def literal_count(self) -> int:
        """字符串字面量的**条数**（= 索引表字节数 / 8）。"""
        return self.literal_table_size // _LITERAL_ENTRY_STRUCT.size


def read_header(path: Path) -> MetadataHeader:
    """只读文件开头 24 字节，解析出字符串字面量表的两个区域的偏移与大小。

    为什么要用 ``read(24)`` 而不是整份读入？
        文件有 17.7 MB，而头部信息只占前 24 字节。后续数据区按需读取，
        既省内存，也让本函数可以被反复调用而不心疼。
    """
    with path.open("rb") as handle:
        raw = handle.read(24)

    if len(raw) < 24:
        raise MetadataError(f"{path} 太小（{len(raw)} 字节），不像有效的元数据文件")

    sanity, version = _HEADER_STRUCT.unpack_from(raw, 0)
    if sanity != METADATA_MAGIC:
        raise MetadataError(
            f"{path} 的魔数不对：期望 0x{METADATA_MAGIC:08X}，实际 0x{sanity:08X}"
            "（很可能传错了文件，例如误传 GameAssembly.dll）"
        )

    table_offset, table_size, data_offset, data_size = _HEADER_OFFSETS_STRUCT.unpack_from(raw, 8)
    if table_size % _LITERAL_ENTRY_STRUCT.size != 0:
        raise MetadataError(
            f"字符串字面量索引表大小 {table_size} 不是 8 的整数倍，格式可能已变化"
        )

    return MetadataHeader(
        version=version,
        literal_table_offset=table_offset,
        literal_table_size=table_size,
        literal_data_offset=data_offset,
        literal_data_size=data_size,
    )


def iter_string_literals(path: Path) -> Iterator[str]:
    """按索引表顺序产出全部字符串字面量（**可能含重复**）。

    【为什么保留重复】
    同一个字符串在代码里出现多次时，IL2CPP 的索引表里就有多条记录。
    去重会丢掉「出现次数」这个信息，而出现次数恰恰能提示重要程度
    （例如某个 salt 被 5 处引用，就比只出现 1 次的字符串更值得关注）。
    需要的调用方自己 ``set()`` 去重即可。
    """
    header = read_header(path)

    with path.open("rb") as handle:
        handle.seek(header.literal_table_offset)
        table = handle.read(header.literal_table_size)

        handle.seek(header.literal_data_offset)
        data = handle.read(header.literal_data_size)

    if len(table) < header.literal_table_size:
        raise MetadataError("索引表被截断：文件可能不完整")
    if len(data) < header.literal_data_size:
        raise MetadataError("字符串数据区被截断：文件可能不完整")

    for index in range(header.literal_count):
        length, data_index = _LITERAL_ENTRY_STRUCT.unpack_from(
            table, index * _LITERAL_ENTRY_STRUCT.size
        )
        if length <= 0:
            yield ""
            continue
        # 字符串不带结尾 \0（长度已在索引表里给出），因此必须精确切片；
        # 用 errors="replace" 而不是抛异常：个别字符串可能含非法 UTF-8 字节，
        # 不该因为一条坏数据就让整份分析失败。
        yield data[data_index : data_index + length].decode("utf-8", errors="replace")


def load_literals(path: Path) -> list[str]:
    """读全部字面量（列表，含重复）。"""
    return list(iter_string_literals(path))


# ---------------------------------------------------------------------------
# 分类：把几万条字面量收敛成「人能看」的几组
# ---------------------------------------------------------------------------
#: URL（带 scheme，如 https://...）
_URL_RE: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://\S+$")

#: 已知顶级域名白名单。
#: **必须用白名单**：若只写「点号分隔的标识符」这种模式，
#: ``Arial.ttf``、``DataSet.Namespace``、``Alliance.eSortType`` 全都会被判成域名，
#: 实测能刷出 294 条噪声，把真正的几个业务域名彻底淹没。
KNOWN_TLDS: Final[frozenset[str]] = frozenset(
    {
        # 通用
        "com", "net", "org", "edu", "gov", "info", "biz", "io", "co", "me", "cc",
        "tv", "app", "dev", "xyz", "top", "site", "online", "shop", "link", "cloud",
        # 地区
        "tw", "cn", "jp", "kr", "hk", "sg", "th", "vn", "ph", "my", "id", "in",
        "us", "uk", "de", "fr", "es", "it", "ru", "br", "mx", "au", "ca",
        # 本作实际使用的专有后缀
        "xxx",
    }
)

#: 主机名分段允许的字符
_HOST_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_\-]+$")

#: 纯十六进制摘要：md5(32) / sha1(40) / sha256(64) / sha512(128)
_HASH_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|[0-9a-fA-F]{128})$"
)

#: UUID 形式（常用于设备号、订单号，以及拼接式的盐）
_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: IPv4（可带端口）
_IP_RE: Final[re.Pattern[str]] = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?$")

#: X.509 OID 的常见根前缀。
#: 它们长得和 IPv4 **一模一样**（如 ``2.5.29.15``、``1.2.840``），但其实是证书里的
#: 对象标识符。不排除的话，「IP 地址」那一节会被 60 多条 OID 淹没，
#: 真正可能有用的那一条（实测 ``119.28.41.111``）反而看不见。
_OID_PREFIXES: Final[tuple[str, ...]] = ("0.9.", "1.0.", "1.2.", "1.3.", "2.5.")

#: 版本号形态（如 ``2020.3.49f1``、``1.0.0``）
_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:[a-zA-Z]\d*)?$")

#: **具体**的密钥语义关键词。
#: 刻意不用 ``key`` / ``sign`` / ``token`` 这种泛词：实测它们会命中 1500+ 条
#: 普通文案（例如 "Public Key:"、"Signature Algorithm:"），多到无法人工浏览。
SECRET_KEYWORDS: Final[tuple[str, ...]] = (
    "appsecret", "app_secret", "secretkey", "secret_key",
    "signkey", "sign_key", "signaturekey", "signature_key",
    "apikey", "api_key", "aeskey", "aes_key", "hmackey", "hmac_key",
    "masterkey", "master_key", "privatekey", "private_key",
    "salt", "saltvalue", "sign_salt", "signsalt",
)


def looks_like_domain(text: str) -> bool:
    """判断是否像一个**真实域名**（或域名+路径）。

    步骤：去掉路径与端口 → 分段 → 末段必须在 :data:`KNOWN_TLDS` 白名单里 →
    每段字符合法。这样 ``game-ct-labs.ecchi.xxx`` 命中，而 ``Arial.ttf`` 不会。
    """
    if not text or any(ch.isspace() for ch in text):
        return False
    host = text.split("/", 1)[0].split(":", 1)[0]
    labels = host.split(".")
    if len(labels) < 2:
        return False
    if labels[-1].lower() not in KNOWN_TLDS:
        return False
    return all(bool(_HOST_LABEL_RE.match(label)) for label in labels)


def looks_like_ip(text: str) -> bool:
    """判断是否是 IPv4（可带端口）。

    两道过滤：

    1. 各段必须在 0~255 之间（排除 ``999.1.1.1`` 这类越界写法）；
    2. 排除 X.509 OID 的常见根前缀（见 :data:`_OID_PREFIXES`）——
       否则证书里的 ``2.5.29.15`` 会被当成 IP 列出来。
    """
    if not _IP_RE.match(text):
        return False
    if text.startswith(_OID_PREFIXES):
        return False
    host = text.split(":", 1)[0]
    return all(0 <= int(part) <= 255 for part in host.split("."))


def looks_like_secret(text: str) -> bool:
    """判断是否像「密钥 / 盐」候选。

    只接受两类：

    1. 摘要或 UUID 形态（结构本身就说明它是密钥材料）；
    2. **不含空白**的短标识符，且带具体密钥关键词（如 ``xxx_salt``、``appSecret``）。

    为什么要限制「不含空白」？带空格的几乎都是给人看的日志文案，
    而不是拿去参与哈希计算的盐 —— 混在一起会让真正有价值的候选被淹掉。
    """
    if _HASH_RE.match(text) or _UUID_RE.match(text):
        return True
    if not text or any(ch.isspace() for ch in text):
        return False
    if not 8 <= len(text) <= 80:
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in SECRET_KEYWORDS)


def classify(literals: Sequence[str]) -> dict[str, list[tuple[str, int]]]:
    """把字面量归类，并统计每条的**出现次数**。

    :return: ``{分类名: [(字符串, 出现次数), ...]}``，每组按次数降序。

    关于「出现次数」：IL2CPP 的字符串字面量是**唯一化**存储的 ——
    实测 27884 条去重后仍是 27884 条，所以当前每条的计数都是 1。
    保留计数逻辑有两个原因：一是换工具/换版本后它可能重新变得有意义，
    二是让输出格式稳定，便于 ``--check`` 逐行比对。
    """
    from collections import Counter

    counter: Counter[str] = Counter(literals)

    groups: dict[str, list[tuple[str, int]]] = {
        "URL（完整地址，可直接使用）": [],
        "域名 / 主机名": [],
        "IP 地址（可能是旧版硬编码服务器）": [],
        "疑似密钥：摘要 / UUID": [],
        "疑似密钥：含具体关键词的标识符": [],
        "版本号": [],
    }

    for text, count in counter.most_common():
        if not text:
            continue

        if _URL_RE.match(text):
            groups["URL（完整地址，可直接使用）"].append((text, count))
            # 顺便把主机名单独收一份：有时域名清单比完整 URL 更好用
            host = text.split("://", 1)[1].split("/", 1)[0]
            if looks_like_domain(host):
                groups["域名 / 主机名"].append((host, count))
            continue

        if looks_like_domain(text):
            groups["域名 / 主机名"].append((text, count))
            continue

        if looks_like_ip(text):
            groups["IP 地址（可能是旧版硬编码服务器）"].append((text, count))
            continue

        if _HASH_RE.match(text) or _UUID_RE.match(text):
            groups["疑似密钥：摘要 / UUID"].append((text, count))
            continue

        if looks_like_secret(text):
            groups["疑似密钥：含具体关键词的标识符"].append((text, count))
            continue

        if _VERSION_RE.match(text):
            groups["版本号"].append((text, count))

    return groups


def _display(text: str, *, limit: int = 200) -> str:
    """把一条字面量渲染成**严格单行**的可读文本。

    【为什么必须转义控制字符】
    有些字面量本身含换行、制表符甚至 NUL（例如多行日志模板、SQL 片段）。
    原样写进报告会让「一条一行」的格式崩掉 —— 实测就因此让 ``grep`` 把整份报告
    当成了二进制文件而拒绝匹配。转义后每条严格占一行，内容仍然看得清。
    """
    escaped = (
        text.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    escaped = "".join(ch if ch >= " " else f"\\x{ord(ch):02x}" for ch in escaped)
    return escaped if len(escaped) <= limit else escaped[:limit] + "…"


def _render_section(title: str, items: Sequence[tuple[str, int]], limit: int) -> list[str]:
    """渲染一节内容（每行形如 ``[出现 3 次] 字符串``）。"""
    lines = [f"## {title}（共 {len(items)} 条，按出现次数降序，最多显示 {limit} 条）", ""]
    if not items:
        lines.extend(["  （无）", ""])
        return lines
    for text, count in items[:limit]:
        lines.append(f"  [{count:>3} 次] {_display(text)}")
    if len(items) > limit:
        lines.append(f"  …… 其余 {len(items) - limit} 条已省略（用 --limit 调整）")
    lines.append("")
    return lines


def render_report(
    header: MetadataHeader, literals: Sequence[str], *, limit: int
) -> str:
    """渲染 ``notes/metadata_strings.txt`` 的完整内容。"""
    from collections import Counter

    counter: Counter[str] = Counter(literals)
    groups = classify(literals)

    lines: list[str] = [
        "# Cherrytale 元数据字符串字面量",
        "#",
        "# 本文件由 tools/extract_metadata_strings.py 自动生成，请勿手工编辑。",
        "# 重新生成：python tools/extract_metadata_strings.py",
        "#",
        f"# 来源文件     : {config.DUMP_CS_PATH.parent.name}/global-metadata.dat",
        f"# 元数据版本   : {header.version}",
        f"# 字面量总数   : {len(literals)} 条（去重后 {len(counter)} 条）",
        "#",
        "# 【怎么用这份文件】",
        "#   1. 找接口地址    → 看「URL（完整地址）」与「域名 / 主机名」两节；",
        "#   2. 找签名盐      → 看「疑似密钥」两节（摘要/UUID、含具体关键词的标识符）；",
        "#   3. 找服务器地址  → 看「IP 地址」与「域名 / 主机名」两节；",
        "#   4. 查某个具体字符串 → python tools/extract_metadata_strings.py --grep 关键词",
        "#",
        "# 说明：IL2CPP 的字符串字面量是**唯一化**存储的（27884 条去重后仍是 27884 条），",
        "#       所以「[1 次]」这个计数当前恒为 1；保留它只是为了让格式稳定、将来可比。",
        "",
        "=" * 78,
        "",
    ]

    for title, items in groups.items():
        lines.extend(_render_section(title, items, limit))

    lines.extend(["=" * 78, ""])
    lines.extend(
        _render_section(
            "出现次数最多的字面量（全量，前 N 条）",
            counter.most_common(limit),
            limit,
        )
    )
    return "\n".join(lines)


def search_literals(
    literals: Sequence[str], keyword: str, *, limit: int
) -> list[tuple[str, int]]:
    """按关键词搜索字面量（大小写不敏感），返回 ``[(字符串, 出现次数)]``。"""
    from collections import Counter

    lowered = keyword.lower()
    counter: Counter[str] = Counter(text for text in literals if lowered in text.lower())
    return list(counter.most_common(limit))



# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def force_utf8_output() -> None:
    """把标准输出/错误流强制改为 UTF-8（原因见 main.py 里的同名函数）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def default_metadata_path() -> Path:
    """定位 ``global-metadata.dat``：环境变量优先，否则与 dump.cs 同目录。"""
    override = os.getenv("CHERRYTALE_METADATA_DAT", "")
    if override:
        return Path(override).expanduser()
    return config.DUMP_CS_PATH.parent / "global-metadata.dat"


def write_text(path: Path, content: str) -> None:
    """写出报告文件（``newline="\\n"`` 保证跨平台一致，``--check`` 才不会误报）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def check_text(path: Path, expected: str) -> bool:
    """比较磁盘上的报告与「现在重新生成会得到的内容」。"""
    if not path.is_file():
        print(f"✘ 文件不存在：{path}")
        print("  修复：运行 python tools/extract_metadata_strings.py")
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
    print(f"✘ 已过期   : {path.name} 有 {len(diff)} 行差异")
    for line in diff[:20]:
        print(f"    {line}")
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_metadata_strings.py",
        description="从 global-metadata.dat 精确提取字符串字面量（用来找接口地址与签名盐）",
    )
    parser.add_argument("--metadata", metavar="路径", help="global-metadata.dat 路径")
    parser.add_argument("--grep", metavar="关键词", help="搜索字面量（大小写不敏感），不写盘")
    parser.add_argument("--limit", type=int, default=40, help="每类最多显示/写入多少条（默认 40）")
    parser.add_argument("--output", metavar="路径", help=f"输出文件（默认 {OUTPUT_RELATIVE_PATH}）")
    parser.add_argument("--check", action="store_true", help="只校验输出文件是否过期（不写盘）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    force_utf8_output()
    args = build_parser().parse_args(argv)

    path = Path(args.metadata).expanduser() if args.metadata else default_metadata_path()
    if not path.is_file():
        print(f"✘ 找不到元数据文件：{path}")
        print("  可用 --metadata 指定，或设置环境变量 CHERRYTALE_METADATA_DAT")
        return 1

    try:
        header = read_header(path)
        literals = load_literals(path)
    except MetadataError as exc:
        print(f"✘ 解析失败：{exc}")
        return 1

    size_mb = path.stat().st_size / 1024 / 1024
    print(f"元数据     : {path}（{size_mb:.1f} MB，版本 {header.version}）")
    print(f"字面量     : {len(literals)} 条（去重后 {len(set(literals))} 条）")

    if args.grep:
        hits = search_literals(literals, args.grep, limit=args.limit)
        print(f"搜索       : {args.grep!r}（命中 {len(hits)} 条，最多显示 {args.limit} 条）")
        print("-" * 72)
        for text, count in hits:
            print(f"  [{count:>3} 次] {_display(text)}")
        if not hits:
            print("  （没有命中，换个关键词试试）")
        return 0

    content = render_report(header, literals, limit=args.limit)
    output_path = (
        Path(args.output).expanduser()
        if args.output
        else config.PROJECT_ROOT / OUTPUT_RELATIVE_PATH
    )

    if args.check:
        return 0 if check_text(output_path, content) else 1

    write_text(output_path, content)
    print(f"✔ 已生成   : {output_path}（{len(content.splitlines())} 行）")
    print("  继续深挖：--grep getServerState / --grep salt / --grep 030101")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

