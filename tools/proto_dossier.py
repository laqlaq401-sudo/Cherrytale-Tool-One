#!/usr/bin/env python3
"""proto_dossier.py —— 一条命令输出某协议的「档案卡」。

【它解决什么问题】
设计一个新任务板块时，要凑齐五类证据，而它们的来源完全不同：

==============================  ==========================================
证据                            唯一来源
==============================  ==========================================
1. 消息号（请求 / 响应）         models/packet_ids.py（本地生成表，0 成本）
2. 字段顺序（= protobuf 编号）   models/proto_fields.py（本地生成表，0 成本）
3. 调用链（谁发的、谁收的）      **只有 dump.cs 有** —— 必须检索
4. 相关配置表（TextAsset 名）    Cherrytale Asset/TextAsset/ 的文件名
5. 还没弄清的疑点                notes/open_questions.md
==============================  ==========================================

过去第 3、4、5 项是「现场拼 grep 命令、来回 6~10 次」完成的，而每次检索
都要把 dump.cs 的上下文灌进上下文（``grep -C 25`` 每处就是 50 行），
非常费 Token，还容易看漏。

本工具把它们压成**一次调用**，并施加三条硬约束：

- 输出默认 ≤ 80 行，超长自动截断 —— 原始 dump.cs 内容永不进入上下文；
- 引用只输出「行号 + 裁剪到 110 字符的单行签名」，不输出上下文块；
- 引用按「有用程度」排序（NetModule 请求入口 / UIView 界面动作 → 类定义 →
  信封槽位 → 常量映射），并丢弃编译器生成物 ——
  默认显示的那几行必须是能用的证据，而不是一堆属性访问器；
- dump.cs 与配置表目录各只扫一遍，一次调用可服务多个协议类。

【用法】
    python tools/proto_dossier.py 37019                      # 按消息号
    python tools/proto_dossier.py GetFreeGiftPacket           # 按类名
    python tools/proto_dossier.py 37019 34021 LoginPacket     # 批量（只扫一遍）
    python tools/proto_dossier.py 37019 --json                # 另存 notes/dossier/
    python tools/proto_dossier.py 37019 --full                # 不截断（慎用）

【环境变量】
    CHERRYTALE_MATERIAL_ROOT / CHERRYTALE_DUMP_CS —— 与其它 tools 脚本一致。

【性能参考（本机实测）】
    dump.cs 约 34 MB / 40 万行，单遍扫描（含逐类引用判定）约 3~8 秒。
    所以**批量查询请一次给多个类**，不要把同一个类分几次跑。

【⚠️ 诚实声明】
    第 3 项（调用链）是**静态可见引用**的启发式结果：dump.cs 只有签名没有方法体，
    因此「谁调用了谁」只能靠 ``SendPacket<Xxx>`` 这类泛型实参、回调命名
    （``OnXxx…``）、以及 NetModule / UIView 的类名来推断。查不到≠不存在，
    真查不到时才升级到反汇编（``tools/disasm_il2cpp.py``）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Sequence

# tools/ 不是包，无法直接 ``import config``，只能手动把项目根加进 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (必须在 sys.path 调整之后导入)
from models import packet_ids, proto_fields  # noqa: E402
from tools.extract_proto_fields import (  # noqa: E402
    extract_fields_for_classes,
    force_utf8_output,
    resolve_dump_path,
)

# ---------------------------------------------------------------------------
# 可调参数：全部集中在这里，方便一眼看到「输出到底有多大」
# ---------------------------------------------------------------------------
#: 一次调用默认最多打印多少行（这是本工具省 Token 的核心约束）
DEFAULT_MAX_LINES: Final[int] = 80

#: 每个类默认展示多少条引用（其余只报总数）
DEFAULT_REF_LIMIT: Final[int] = 6

#: 单行引用最多保留多少字符（泛型签名很长，不裁会撑爆整屏）
REF_TEXT_WIDTH: Final[int] = 110

#: 每个类在扫描阶段最多先收集多少条引用（防内存膨胀；渲染时再截到 REF_LIMIT）
DEFAULT_REF_CAP: Final[int] = 400

#: 单次扫描时，一个类名/词干至少要有多长才允许参与匹配（太短会命中大量噪声）
MIN_STEM_LEN: Final[int] = 5

#: 待确认清单（本工具会从中筛出与目标类相关的条目）
QUESTIONS_PATH: Final[Path] = config.PROJECT_ROOT / "notes" / "open_questions.md"

#: ``--json`` 的默认落盘目录
DOSSIER_DIR: Final[Path] = config.PROJECT_ROOT / "notes" / "dossier"

_SEP: Final[str] = "=" * 78
_SUB: Final[str] = "-" * 78


# ---------------------------------------------------------------------------
# 数据结构：全部是纯数据（不含任何打印逻辑），这样既能渲染文本，也能导 JSON
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Reference:
    """dump.cs 里的一处「静态可见引用」。

    :param line_no: 行号（1 起）——有了它才能一步跳到 ``sed -n 'X,Yp'``，
        而不必再搜一遍。
    :param kind: 分类标签（``定义`` / ``发送`` / ``回调`` / ``网络模块`` / ``UI`` / ``方法``）。
    :param text: 已裁剪成单行的签名。
    :param owner: 这行所在的类名（``None`` = 不在任何类里）。有了它才知道
        「**是谁**在发这个包」，而不只是「有这么一行代码」。
    """

    line_no: int
    kind: str
    text: str
    owner: str | None = None


@dataclass(frozen=True)
class ConfigCandidate:
    """配置表候选（按**文件名**启发式匹配到的 TextAsset）。

    :param name: 文件名（``Cherrytale Asset/TextAsset/`` 下多数没有扩展名）。
    :param size: 字节数 —— 用来快速判断「大表（几百 KB，多半是主数据）」
        还是「小表（几 KB，多半是子表）」。
    """

    name: str
    size: int


@dataclass(frozen=True)
class Question:
    """``notes/open_questions.md`` 里的一行待确认项。"""

    qid: str
    text: str
    source: str
    blocks: str
    command: str


@dataclass
class DossierEntry:
    """一个协议的档案卡（渲染文本与导出 JSON 共用同一份数据）。"""

    class_name: str
    packet_id: int | None = None
    pair_id: int | None = None
    pair_name: str | None = None
    in_table: bool = False
    fields: tuple[tuple[str, int], ...] = ()
    field_status: str = ""
    member_count: int = 0
    references: list[Reference] = field(default_factory=list)
    ref_total: int = 0
    configs: list[ConfigCandidate] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 小工具：裁剪、词干提取、引用分类
# ---------------------------------------------------------------------------
def trim_line(line: str) -> str:
    """把 dump.cs 的一行压成「单行短签名」。

    两个动作：把连续空白压成一个空格（dump.cs 用制表符缩进，不压会显得很乱）、
    超长则截断加省略号。**截断是为了不让一行泛型签名占掉大半屏 Token。**
    """
    text = " ".join(line.split())
    if len(text) <= REF_TEXT_WIDTH:
        return text
    return text[: REF_TEXT_WIDTH - 1] + "…"


#: 命名里「不含业务含义」的前缀（用于 :func:`stem_of`）
_LEAD_NOISE: Final[tuple[str, ...]] = (
    "Get",
    "Send",
    "Req",
    "Request",
    "Receive",
    "Query",
    "Add",
    "Update",
)

#: 命名里「不含业务含义」的后缀（注意 ``PacketRes`` 必须排在 ``Packet`` 前面）
_TAIL_NOISE: Final[tuple[str, ...]] = ("PacketRes", "Packet", "Res", "Class")


def stem_of(class_name: str) -> str:
    """把类名压成「业务词干」，用来抓回调名。

    例：``GetFreeGiftPacket`` → ``FreeGift``（于是能匹配到 ``OnGetFreeGiftRes``）；
    ``BBQFriendsPacket`` → ``BBQFriends``；``GiftInfoClass`` → ``GiftInfo``。

    词干短于 :data:`MIN_STEM_LEN` 时**原样返回类名**——
    过短的词干（``RootPacket`` → ``Root``）会命中大量无关代码，反而更费 Token。
    """
    stem = class_name
    for suffix in _TAIL_NOISE:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            stem = stem[: -len(suffix)]
            break
    for prefix in _LEAD_NOISE:
        if stem.startswith(prefix) and len(stem) - len(prefix) >= MIN_STEM_LEN:
            stem = stem[len(prefix) :]
            break
    return class_name if len(stem) < MIN_STEM_LEN else stem


#: 引用分类标记：(标签, 正则, 这行为什么有价值)
_REF_MARKERS: Final[tuple[tuple[str, re.Pattern[str], str], ...]] = (
    ("发送", re.compile(r"SendPacket\s*<"), "客户端组包并发出请求的地方"),
    ("回调", re.compile(r"\bOn[A-Z]\w*"), "收到响应后的处理函数（命名惯例 OnXxx…）"),
    ("网络模块", re.compile(r"Net\w*Module"), "负责该业务的 NetModule 家族类"),
    ("UI", re.compile(r"UIView|UI_View"), "触发请求 / 展示结果的界面类"),
)

#: 类/结构体/接口/枚举的定义行（用来识别「这就是那个类的定义」）
_DEF_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:public|internal)\s+(?:sealed\s+|abstract\s+|static\s+|partial\s+)*"
    r"(?:class|struct|interface|enum)\s+(\w+)"
)

#: 方法声明行。dump.cs 里方法体一律写成 ``{ }``（只有签名、没有实现），
#: 所以「以 ``) { }`` 结尾」就是方法声明的可靠特征。
_METHOD_RE: Final[re.Pattern[str]] = re.compile(r"\([^()]*\)\s*\{\s*\}\s*$")

#: 编译器生成物的特征（dump.cs 用 ``|-`` 前缀标出自动生成的类/方法名）。
#: 这些行只说明「附近有一段匿名代码」，没有任何可操作信息，直接丢掉。
_GENERATED_RE: Final[re.Pattern[str]] = re.compile(r"\|\-|<>c__|PrivateImplementationDetails")

#: 引用的「有用程度」排序（数字越小越靠前、越先展示）。
#:
#: 【为什么必须排序】
#:     实测：单个类往往命中十几~几十处，而默认只展示 6 处。
#:     未排序时排在最前面的全是 ``RootPacket`` 的 get_/set_ 属性访问器
#:     （每个包都有，几乎不含信息量），真正有用的
#:     「NetModule 的请求入口 / UIView 的界面动作 / VirtualPacket 的分发器」
#:     反而被挤到看不见 —— 那就完全违背了这个工具的初衷。
_KIND_RANK: Final[dict[str, int]] = {
    "发送": 0,  # 组包并发出请求（dump.cs 没有方法体，命中很少）
    "网络模块": 1,  # NetModule 里的方法 = 请求入口
    "UI": 2,  # UIView 里的方法 = 触发点 / 结果展示
    "定义": 3,  # 类定义本身（能看到基类与 TypeDefIndex）
    "分发器": 4,  # VirtualPacket.Get_Xxx：请求在这里被分发
    "回调": 5,
    "信封槽位": 6,  # RootPacket 的 get_/set_ —— 有用，但谁都知道它存在
    "ID映射": 7,  # IDMAP 里的字符串 ID 常量
    "方法": 8,
}


def _kind_rank(kind: str) -> int:
    """取分类的排序权重（未知分类排最后）。"""
    return _KIND_RANK.get(kind, len(_KIND_RANK))


def refine_kind(kind: str, owner: str | None, text: str) -> str:
    """用「所属类」对分类做二次修正，并识别编译器生成物。

    【为什么不能只靠单行文本】
        逐行分类会受「参数里恰好出现了某个类名」影响，很不稳定。例如
        ``GiftPackageNetModule.RequesGetFreeGiftPacket(...)`` 之所以被认出来，
        只是因为参数类型写着 ``GiftPackageNetModule.GiftPackage``（运气好）。

        真正稳定的依据是**它属于哪个类**：NetModule 里的方法就是请求入口，
        UIView 里的方法就是界面动作，RootPacket 里的 get_/set_ 就是信封槽位。
        所以这里按 ``owner``（扫描时记录的当前类）重新贴标签。

    :param kind: :func:`classify_reference` 给出的初判。
    :param owner: 该行所属的类名（``None`` = 不在任何类里）。
    :param text: 该行内容（用来识别生成物与 RootPacket 访问器）。
    :return: 修正后的分类；返回 ``"生成代码"`` 表示应当丢弃。
    """
    if _GENERATED_RE.search(text):
        return "生成代码"
    if owner is None:
        return kind
    if re.search(r"Net\w*Module", owner):
        return "网络模块"
    if re.search(r"UIView|UI_View", owner):
        return "UI"
    if owner == "RootPacket" and re.search(r"\b[gs]et_\w+", text):
        return "信封槽位"
    if owner.startswith("IDMAP"):
        return "ID映射"
    if owner == "VirtualPacket":
        return "分发器"
    return kind


def reference_sort_key(ref: Reference) -> tuple[int, int]:
    """引用排序键：先按有用程度，再按行号（保持稳定、可复现）。"""
    return (_kind_rank(ref.kind), ref.line_no)


def classify_reference(line: str, class_name: str) -> str | None:
    """给一行 dump.cs 分类；返回 ``None`` 表示「这行没有信息量，丢掉」。

    【为什么必须过滤】
        dump.cs 里出现类名的地方绝大多数是无用的（字段声明、方法参数、泛型实参…）。
        真正有用的是：类定义本身，以及带上述四种标记的调用点。
        过滤掉其余行，是「一次扫描只吐几十行、而不是几千行」的关键 ——
        这一步直接决定了工具省不省 Token。
    """
    declaration = _DEF_RE.match(line)
    if declaration and declaration.group(1) == class_name:
        return "定义"
    for kind, pattern, _why in _REF_MARKERS:
        if pattern.search(line):
            return kind
    # 兜底：方法声明 / 调用点里出现了本类型，例如 NetModule 的
    #     public void RequesGetFreeGiftPacket(GiftPackage p, Action cb) { }
    # 这行虽然没有上面的标记，却正是「谁在发这个包」的证据，不能丢。
    if _METHOD_RE.search(line):
        return "方法"
    return None


# ---------------------------------------------------------------------------
# 目标解析：把命令行参数变成「协议类名」
# ---------------------------------------------------------------------------
def normalize_targets(queries: Sequence[str]) -> tuple[list[str], list[str]]:
    """把命令行参数统一成协议类名，并收集告警。

    两种写法都接受：消息号（``37019``）或类名（``GetFreeGiftPacket``，大小写不敏感）。
    不是报文类的辅助类（例如 ``FriendClass``）也允许 —— 它们同样有字段和引用要看，
    只是没有消息号而已（不报错，只给一句提示）。

    :return: ``(类名列表（去重、保持输入顺序）, 告警列表)``。
    """
    targets: list[str] = []
    warnings: list[str] = []
    for raw in queries:
        query = raw.strip()
        if not query:
            continue
        if query.isdigit():
            pid = int(query)
            name = packet_ids.packet_name(pid)
            if name is None:
                warnings.append(
                    f"消息号 {pid} 不在 ePacketFormat_RPS 里"
                    f"（该表共 {packet_ids.PACKET_COUNT} 条）"
                )
                continue
            query = name
        elif query not in packet_ids.PACKET_IDS:
            matched = next(
                (n for n in packet_ids.PACKET_IDS if n.lower() == query.lower()), None
            )
            if matched is not None:
                query = matched
            else:
                warnings.append(
                    f"{query} 没有消息号（可能不是报文类，例如 FriendClass）"
                    " —— 仍会继续查它的字段与引用"
                )
        if query not in targets:
            targets.append(query)
    return targets, warnings


# ---------------------------------------------------------------------------
# 单遍扫描 dump.cs，收引用
# ---------------------------------------------------------------------------
def scan_references(
    dump_path: Path,
    class_names: Sequence[str],
    *,
    per_class_cap: int = DEFAULT_REF_CAP,
) -> dict[str, tuple[list[Reference], int]]:
    """**单遍**扫描 dump.cs，收集每个目标类的静态可见引用。

    :param dump_path: dump.cs 路径（测试里会传一个自造的小文件）。
    :param class_names: 目标类名。
    :param per_class_cap: 每个类最多保留多少条（超出只计数，不占内存）。
    :return: ``{类名: (保留的引用列表, 命中总数)}``。

    【为什么所有目标共用一次遍历】
        dump.cs 34 MB，重复打开就是重复 IO。批量查询比逐个查询快得多
        （见模块文档的性能参考），所以命令行支持一次给多个类。

    【为什么同时匹配「词干」】
        回调函数叫 ``OnGetFreeGiftRes``，**并不包含完整的** ``GetFreeGiftPacketRes``。
        只匹配完整类名的话，最关键的回调行一条都抓不到。
        所以再用 :func:`stem_of` 得到的词干（``FreeGift``）兜一层。
    """
    result: dict[str, tuple[list[Reference], int]] = {
        name: ([], 0) for name in class_names
    }
    needles: list[tuple[str, str]] = [(name, stem_of(name)) for name in class_names]

    current_class: str | None = None
    with dump_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            # 顶格的 `}` 结束当前类，`public class X` 开启新类。
            # 记住「当前在哪个类里」，渲染时才能写出
            # ``GiftPackageNetModule :: RequesGetFreeGiftPacket(...)`` 这种带归属的证据。
            if line.startswith("}"):
                current_class = None
            declaration = _DEF_RE.match(line)
            if declaration:
                current_class = declaration.group(1)

            for name, stem in needles:
                hit = name in line or (stem != name and stem in line)
                if not hit:
                    continue
                kind = classify_reference(line, name)
                if kind is None:
                    continue
                kind = refine_kind(kind, current_class, line)
                if kind == "生成代码":
                    # 编译器生成物（``|-…`` / ``<>c__…``）没有任何可操作信息
                    continue
                kept, total = result[name]
                total += 1
                if len(kept) < per_class_cap:
                    kept.append(
                        Reference(
                            line_no=line_no,
                            kind=kind,
                            text=trim_line(line),
                            owner=current_class,
                        )
                    )
                result[name] = (kept, total)
    return result


# ---------------------------------------------------------------------------
# 配置表候选：只看文件名与体积，绝不读内容
# ---------------------------------------------------------------------------
#: 切词后要丢掉的通用词（它们几乎出现在每张表名里，用来匹配毫无区分度）
_CONFIG_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "Packet",
        "PacketRes",
        "Res",
        "Class",
        "Data",
        "List",
        "Info",
        "New",
        "All",
        "Type",
        "Time",
        "Task",
        "Item",
    }
)

#: 驼峰切词（``StoreTypeList`` → ``Store`` / ``Type`` / ``List``）
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z][a-z0-9]+|[A-Z]{2,}")


def config_keywords(class_name: str) -> tuple[str, ...]:
    """从类名切出「用来匹配配置表文件名」的关键词（最多 3 个，按重要性排序）。

    例：``GetFreeGiftPacket`` → ``('FreeGift', 'Gift', 'Free')``。
    第一个是主关键词，排序时优先。
    """
    stem = stem_of(class_name)
    words = [
        word
        for word in _WORD_RE.findall(stem)
        if word not in _CONFIG_STOPWORDS and len(word) >= 4
    ]
    keywords: list[str] = []
    if len(stem) >= MIN_STEM_LEN and stem not in _CONFIG_STOPWORDS:
        keywords.append(stem)
    for word in words:
        if word != stem and word not in keywords:
            keywords.append(word)
    return tuple(keywords[:3])


def find_config_candidates(class_name: str, *, limit: int = 6) -> list[ConfigCandidate]:
    """在 ``Cherrytale Asset/TextAsset/`` 里按**文件名**找候选配置表。

    ⚠️ 这是启发式匹配：命中只说明「名字像」，**不代表字段一定对应**。
    真正的结构要用 ``tools/inspect_textasset.py`` 看首行才能确认。

    【为什么不读文件内容】
        TextAsset 里单个文件可达 14~25 MB。文件名 + 体积已经能给出足够线索，
        读内容会把时间与 Token 一起烧光。
    """
    keywords = config_keywords(class_name)
    directory = config.TEXT_ASSET_DIR
    if not keywords or not directory.is_dir():
        return []

    found: list[ConfigCandidate] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        name_lower = path.name.lower()
        if not any(key.lower() in name_lower for key in keywords):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        found.append(ConfigCandidate(name=path.name, size=size))

    # 排序：主关键词命中的排前面；同组内体积大的排前面（大表多半是主数据表）
    main = keywords[0].lower()
    found.sort(key=lambda cand: (main not in cand.name.lower(), -cand.size))
    return found[:limit]


# ---------------------------------------------------------------------------
# 待确认清单
# ---------------------------------------------------------------------------
def parse_open_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    """解析 ``notes/open_questions.md`` 的表格行。

    只认「首列形如 ``Q-001``」的行，因此标题、说明文字、分隔线都会被自动忽略 ——
    于是那份文档可以随便加说明，不会破坏本工具。
    """
    if not path.is_file():
        return []

    questions: list[Question] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        if len(cells) < 6 or not cells[0].startswith("Q-"):
            continue
        # 容错：单元格里万一手写了 `|`，多出来的碎片统一并回「复现命令」列
        command = " ".join(cells[4:-1]) if len(cells) > 6 else cells[4]
        questions.append(
            Question(
                qid=cells[0],
                text=cells[1],
                source=cells[2],
                blocks=cells[3],
                command=command,
            )
        )
    return questions


def match_questions(
    questions: Sequence[Question], class_name: str, packet_id: int | None
) -> list[Question]:
    """筛出与目标类有关的待确认项（命中类名，或命中消息号）。"""
    hits: list[Question] = []
    for question in questions:
        blob = " ".join(
            (question.text, question.source, question.blocks, question.command)
        )
        if class_name in blob:
            hits.append(question)
        elif packet_id is not None and re.search(rf"\b{packet_id}\b", blob):
            hits.append(question)
    return hits


# ---------------------------------------------------------------------------
# 组装档案卡（纯数据，不打印）
# ---------------------------------------------------------------------------
def build_entries(
    queries: Sequence[str],
    *,
    dump_path: Path,
    use_config: bool = True,
    questions_path: Path = QUESTIONS_PATH,
    ref_cap: int = DEFAULT_REF_CAP,
) -> tuple[list[DossierEntry], list[str]]:
    """组装档案卡数据。

    :param queries: 命令行给的目标（消息号或类名，可多个）。
    :param dump_path: dump.cs 路径。
    :param use_config: 是否扫描 TextAsset 目录找配置表候选。
    :param questions_path: 待确认清单路径。
    :param ref_cap: 每个类最多保留多少条引用。
    :return: ``(档案卡列表, 告警列表)``。

    【为什么把「取数」与「渲染」分成两步】
        取数只做一次，之后既可以渲染成给人看的文本，也可以导出 JSON，
        还能被单元测试直接断言 —— 不必去解析自己打印的字符串。
    """
    targets, warnings = normalize_targets(queries)
    if not targets:
        return [], warnings

    # ---- 第 1 遍扫描：引用（所有目标共用同一遍，见 scan_references 的说明）----
    if dump_path.is_file():
        refs = scan_references(dump_path, targets, per_class_cap=ref_cap)
    else:
        refs = {}
        warnings.append(f"找不到 dump.cs：{dump_path} —— 字段与引用信息会缺失")

    # ---- 第 2 遍扫描：只服务「生成表里没有收录」的类（通常是 0~2 个）----
    known = set(proto_fields.known_classes())
    unlisted = [name for name in targets if name not in known]
    extra_fields: dict[str, tuple[str, ...]] = {}
    member_counts: dict[str, int] = {}
    if unlisted and dump_path.is_file():
        extra_fields, member_counts = extract_fields_for_classes(dump_path, unlisted)

    questions_all = parse_open_questions(questions_path)

    entries: list[DossierEntry] = []
    for name in targets:
        entry = DossierEntry(class_name=name, packet_id=packet_ids.PACKET_IDS.get(name))

        if entry.packet_id is not None:
            # 请求 / 响应成对且相邻（1001→1002、33057→33058、37019→37020…）。
            # 这是**启发式**，渲染时会明确标注，绝不冒充官方声明。
            neighbour = packet_ids.packet_name(entry.packet_id + 1)
            if neighbour is not None:
                entry.pair_id = entry.packet_id + 1
                entry.pair_name = neighbour

        if name in known:
            entry.in_table = True
            field_map = proto_fields.field_tags(name)
            entry.fields = tuple(sorted(field_map.items(), key=lambda pair: pair[1]))
            entry.member_count = len(entry.fields)
            entry.field_status = f"proto_fields 已收录（{len(entry.fields)} 个字段）"
        elif name in extra_fields:
            names = extra_fields[name]
            entry.member_count = member_counts.get(name, 0)
            entry.fields = tuple(
                (field_name, tag) for tag, field_name in enumerate(names, start=1)
            )
            if names:
                entry.field_status = "未收录 → 已现场从 dump.cs 抽出（临时结果，建议补进生成表）"
            elif entry.member_count == 0:
                entry.field_status = "未收录 → 确认是空报文（类里没有 ProtoMember，正常）"
            else:
                entry.field_status = (
                    "未收录 → ⚠️ 有 ProtoMember 却解析不出字段，解析规则需要更新"
                )
        else:
            entry.field_status = "未收录，且 dump.cs 里找不到这个类（请检查拼写）"

        kept, total = refs.get(name, ([], 0))
        # 按「有用程度」排序：先看谁在发 / 收这个包，再看它在信封里的槽位。
        # 这一步决定了默认那 6 行里到底是有用信息，还是一堆属性访问器。
        entry.references = sorted(kept, key=reference_sort_key)
        entry.ref_total = total
        if use_config:
            entry.configs = find_config_candidates(name)
        entry.questions = match_questions(questions_all, name, entry.packet_id)
        entries.append(entry)

    return entries, warnings


# ---------------------------------------------------------------------------
# 渲染：把档案卡变成「人能一眼看懂、且行数受控」的文本
# ---------------------------------------------------------------------------
#: 单个类最多打印多少行字段（字段特别多的类只列前面这些）
FIELD_DISPLAY_LIMIT: Final[int] = 24


def render_entries(
    entries: Sequence[DossierEntry],
    *,
    warnings: Sequence[str] = (),
    max_lines: int = DEFAULT_MAX_LINES,
    ref_limit: int = DEFAULT_REF_LIMIT,
    full: bool = False,
) -> str:
    """渲染档案卡文本。

    【行数硬约束】
        末尾会检查总行数；超过 ``max_lines`` 就截断并给出提示。
        告警（warnings）放在最前面，因此永远不会被截掉 ——
        最需要在第一时间看到的信息，不能因为省 Token 被丢掉。
    """
    lines: list[str] = [
        _SEP,
        f"协议档案卡 · {len(entries)} 个目标 · 来源：packet_ids / proto_fields / dump.cs",
        _SEP,
    ]

    if warnings:
        for warning in warnings:
            lines.append(f"⚠️  {warning}")
        lines.append(_SUB)

    for entry in entries:
        lines.append("")
        lines.append(f"■ {entry.class_name}")

        if entry.packet_id is None:
            lines.append("  消息号 : （无：不是报文类）")
        else:
            lines.append(f"  消息号 : {entry.packet_id}")
        if entry.pair_name is not None:
            lines.append(
                f"  配对   : {entry.pair_id} {entry.pair_name}（+1 推断，非官方声明）"
            )

        lines.append(f"  字段   : {entry.field_status}")
        shown_fields = entry.fields if full else entry.fields[:FIELD_DISPLAY_LIMIT]
        for field_name, tag in shown_fields:
            lines.append(f"      #{tag:<3} {field_name}")
        if len(entry.fields) > len(shown_fields):
            lines.append(
                f"      …（共 {len(entry.fields)} 个字段，还有 "
                f"{len(entry.fields) - len(shown_fields)} 个未显示；--full 看全部）"
            )

        if entry.references:
            shown_refs = entry.references if full else entry.references[:ref_limit]
            hidden = entry.ref_total - len(shown_refs)
            tail = (
                "已全部列出"
                if hidden <= 0
                else f"仅列前 {len(shown_refs)} 处（--full 看全部）"
            )
            lines.append(
                f"  引用   : 命中 {entry.ref_total} 处 · {tail} · 静态可见，可能不全"
            )
            for ref in shown_refs:
                owner = f"{ref.owner} :: " if ref.owner and ref.kind != "定义" else ""
                lines.append(f"      L{ref.line_no} [{ref.kind}] {owner}{ref.text}")
        else:
            lines.append(
                "  引用   : 没有找到带标记的引用（可能是纯数据类型，或名字没有业务词干）"
            )

        if entry.configs:
            lines.append("  配置表候选（按文件名启发式匹配，不等于字段对应）:")
            for cand in entry.configs:
                lines.append(f"      {cand.name:<34} {cand.size / 1024:>9.1f} KB")

        if entry.questions:
            lines.append("  待确认项（notes/open_questions.md）:")
            for question in entry.questions:
                lines.append(f"      {question.qid} {question.text}")
                lines.append(f"           命令: {question.command}")

    if not full and len(lines) > max_lines:
        hidden_lines = len(lines) - max_lines
        keep = max(max_lines - 2, 1)
        lines = lines[:keep]
        lines.append(
            f"  …（为省 Token 截断了 {hidden_lines} 行：加 --full 看全部，"
            "或 --max-lines 调大）"
        )
        lines.append(_SEP)

    return "\n".join(lines)


def entry_to_dict(entry: DossierEntry) -> dict[str, object]:
    """把档案卡转成可 JSON 序列化的字典（键名与输出文本一一对应）。"""
    return {
        "class_name": entry.class_name,
        "packet_id": entry.packet_id,
        "pair_id": entry.pair_id,
        "pair_name": entry.pair_name,
        "field_status": entry.field_status,
        "member_count": entry.member_count,
        "fields": [{"tag": tag, "name": name} for name, tag in entry.fields],
        "reference_total": entry.ref_total,
        "references": [
            {
                "line": ref.line_no,
                "kind": ref.kind,
                "owner": ref.owner,
                "text": ref.text,
            }
            for ref in entry.references
        ],
        "config_candidates": [
            {"name": cand.name, "size": cand.size} for cand in entry.configs
        ],
        "open_questions": [
            {
                "id": question.qid,
                "text": question.text,
                "source": question.source,
                "blocks": question.blocks,
                "command": question.command,
            }
            for question in entry.questions
        ],
    }


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proto_dossier.py",
        description="一条命令输出协议档案卡：消息号 + 字段 + 调用链 + 配置表 + 待确认项",
    )
    parser.add_argument(
        "queries",
        nargs="+",
        metavar="消息号或类名",
        help="例如 37019 或 GetFreeGiftPacket（可一次给多个，只会扫一遍 dump.cs）",
    )
    parser.add_argument(
        "--dump", metavar="路径", help="dump.cs 路径（默认取 config.DUMP_CS_PATH）"
    )
    parser.add_argument(
        "--json",
        nargs="?",
        const="",
        metavar="路径",
        help="同时导出 JSON；不给路径则写到 notes/dossier/<类名>.json",
    )
    parser.add_argument("--full", action="store_true", help="不截断输出（慎用：可能几百行）")
    parser.add_argument(
        "--max-lines",
        type=int,
        default=DEFAULT_MAX_LINES,
        metavar="N",
        help=f"输出行数上限（默认 {DEFAULT_MAX_LINES}）",
    )
    parser.add_argument(
        "--ref-limit",
        type=int,
        default=DEFAULT_REF_LIMIT,
        metavar="N",
        help=f"每个类展示多少条引用（默认 {DEFAULT_REF_LIMIT}）",
    )
    parser.add_argument(
        "--no-config",
        action="store_true",
        help="跳过 TextAsset 目录扫描（只要字段与引用时更快）",
    )
    parser.add_argument(
        "--questions", metavar="路径", help=f"待确认清单路径（默认 {QUESTIONS_PATH.name}）"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    force_utf8_output()
    args = build_parser().parse_args(argv)

    dump_path = resolve_dump_path(args.dump)
    questions_path = (
        Path(args.questions).expanduser() if args.questions else QUESTIONS_PATH
    )

    entries, warnings = build_entries(
        args.queries,
        dump_path=dump_path,
        use_config=not args.no_config,
        questions_path=questions_path,
        ref_cap=10_000 if args.full else DEFAULT_REF_CAP,
    )

    if not entries:
        print("✘ 没有任何可查询的目标。")
        for warning in warnings:
            print(f"  ⚠️  {warning}")
        print("  用法示例：python tools/proto_dossier.py 37019")
        return 2

    print(
        render_entries(
            entries,
            warnings=warnings,
            max_lines=args.max_lines,
            ref_limit=args.ref_limit,
            full=args.full,
        )
    )

    if args.json is not None:
        target = (
            Path(args.json).expanduser()
            if args.json
            else DOSSIER_DIR / f"{entries[0].class_name}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {entry.class_name: entry_to_dict(entry) for entry in entries}
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n✔ JSON 已写出：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
