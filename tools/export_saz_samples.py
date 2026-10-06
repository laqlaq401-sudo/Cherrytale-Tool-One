#!/usr/bin/env python3
"""把 Fiddler SAZ 里的指定会话导出成 ``protocol_samples`` 常量（**子包字节**）。

【为什么要有这个工具】
``tools/extract_har.py`` 能把 **HAR** 抓包生成夹具，但本项目的抓包大多存成
**SAZ**（Fiddler 原生格式，zip 封装）。过去要把某一条会话做成测试夹具，
只能现场敲 ``python -c`` 手工复制 hex —— 又长又容易抄错，而且本项目已明令
禁止 ``python -c`` 直连（见 ``.clinerules`` 终端卫生第 9 条）。

本工具把这件事固化：**一条命令**把会话的请求/响应子包字节写成 ``Final[str]``
常量，并自动带上"数据来源 + 再生命令"的文件头 —— 下次抓包重复同样的操作即可。

【为什么导出的是"子包字节"而不是整段 HTTP body】
``ProtoMessage.decode()`` 吃的是剥掉 ``RootPacket`` 信封之后的那一段；
夹具只存这一段，测试就不必依赖信封解析，也不会被 HTTP 头干扰。
（与 ``protocol_samples/sweep_samples.py`` 的约定一致。）

【导出规则】
- 常量名 = ``{前缀}{会话编号}_{REQUEST|RESPONSE}_HEX``（前缀默认取会话目录名大写）；
- **空子包也导出**（如 14011 ``GetStoreTypeListPacket`` → ``""``）：
  "这条请求确实是空包"本身就是要断言的事实，省略常量反而让它无法被验证。

【用法】
    PYTHONIOENCODING=utf-8 python tools/export_saz_samples.py \\
        --match 市集 --sessions raw/04,raw/06,raw/09 --prefix MARKET
    # 加 --out protocol_samples/market_samples.py 才写盘，否则只打印到 stdout

【环境变量】
    CHERRYTALE_CAPTURE_DIR   抓包目录（默认 <项目根>/captures）
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

# tools/ 与项目根都不在包路径里：前者供本文件 import 同目录的 extract_saz_posts
# （pytest 从项目根收集测试时才会走到这一行），后者供 import config / models。
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (必须在 sys.path 调整之后导入)
from extract_saz_posts import Session, body_of, iter_sessions  # noqa: E402
from models.envelope import EnvelopeError, parse_envelope  # noqa: E402
from models.protobuf_wire import ProtobufWireError  # noqa: E402

#: 每行十六进制字符数（64 字符 = 32 字节，便于人工与 HexView 对照）
HEX_LINE_WIDTH: Final[int] = 64

#: 常量单行上限：超过就折成括号内多行拼接（生成的代码才不会被 linter 抱怨行长）
SINGLE_LINE_LIMIT: Final[int] = 60


def force_utf8_output() -> None:
    """把标准输出/错误流强制改为 UTF-8（原因见 ``tools/extract_saz_posts.py``）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def resolve_capture_dir(override: str | None) -> Path:
    """定位抓包目录：命令行参数 > 环境变量 > ``<项目根>/captures``。"""
    if override:
        return Path(override).expanduser()
    from_env = os.getenv("CHERRYTALE_CAPTURE_DIR", "")
    if from_env:
        return Path(from_env).expanduser()
    return config.PROJECT_ROOT / "captures"


def find_archive(capture_dir: Path, match: str) -> Path:
    """挑出唯一一份 SAZ（文件名含 ``match``）；0 份或多份都直接报错。

    【为什么"多份也报错"而不是偷偷取第一份】
    取错抓包会让夹具里的字节与文件头声明的来源不一致 —— 这种"看起来对"的
    夹具比没有夹具更危险，所以宁可停下来让人把 ``--match`` 写精确。
    """
    archives = sorted(capture_dir.glob("*.saz"))
    if match:
        archives = [path for path in archives if match in path.name]
    if not archives:
        raise SystemExit(f"✘ 在 {capture_dir} 里没找到匹配 '{match}' 的 .saz")
    if len(archives) > 1:
        names = "、".join(path.name for path in archives)
        raise SystemExit(f"✘ 匹配到多份 SAZ（请把 --match 写精确）：{names}")
    return archives[0]


def load_sessions(archive: Path, wanted: Sequence[str]) -> list[Session]:
    """按 ``--sessions`` 给的顺序取出会话；缺哪条就报哪条（不静默跳过）。"""
    by_index = {session.index: session for session in iter_sessions(archive)}
    picked: list[Session] = []
    missing: list[str] = []
    for index in wanted:
        session = by_index.get(index)
        if session is None:
            missing.append(index)
        else:
            picked.append(session)
    if missing:
        raise SystemExit(f"✘ {archive.name} 里没有这些会话：{'、'.join(missing)}")
    return picked


def constant_name(session: Session, kind: str, prefix: str) -> str:
    """常量名：``{前缀}{编号}_{REQUEST|RESPONSE}_HEX``（前缀默认取目录名大写）。"""
    folder, _, index = session.index.partition("/")
    head = prefix or folder.upper()
    return f"{head}{index}_{kind}_HEX"


def describe_body(body: bytes) -> str:
    """一句话说明这段字节是什么包（写给夹具注释与终端摘要看）。"""
    if not body:
        return "空子包（0 B）"
    try:
        view = parse_envelope(body)
    except (EnvelopeError, ProtobufWireError) as exc:
        return f"<无法解析：{exc}>"
    name = view.class_name or "未收录"
    return f"{name}({view.packet_id})，子包 {len(view.sub_packet_bytes)} B"


def sub_packet_of(body: bytes) -> bytes:
    """取出报文里的**业务子包字节**（``ProtoMessage.decode()`` 直接吃的那段）。

    【为什么必须剥掉信封】
    夹具是给模型解码用的：``ProtoMessage.decode()`` 期望的正是子包字节。
    把整个 ``RootPacket``（含 ``packetID`` / ``vCode`` / ``settingMd5``）写进
    夹具，测试就变成"解码信封"而不是"解码业务字段"，还会与
    ``protocol_samples/sweep_samples.py`` 的约定分叉 —— 那种"看起来对"的夹具
    比没有夹具更容易误导人（本次首版导出就踩了这个坑：14011 明明是空子包，
    却导出了一整段含 vCode 的信封字节）。

    取不到子包（空 body / 解析失败）时返回 ``b""``：调用方据此导出空字符串常量，
    而"这条为什么是空的"由 :func:`describe_body` 的注释说明。
    """
    if not body:
        return b""
    try:
        return parse_envelope(body).sub_packet_bytes
    except (EnvelopeError, ProtobufWireError):
        return b""


def render_hex_literal(hex_text: str) -> str:
    """把 hex 渲染成单行或括号内多行拼接的 Python 字面量。"""
    if not hex_text:
        return '""'
    if len(hex_text) <= SINGLE_LINE_LIMIT:
        return f'"{hex_text}"'
    chunks = [
        hex_text[start : start + HEX_LINE_WIDTH]
        for start in range(0, len(hex_text), HEX_LINE_WIDTH)
    ]
    body = "".join(f'    "{chunk}"\n' for chunk in chunks)
    return f"(\n{body})"


def render_fixture(
    archive: Path, sessions: Sequence[Session], prefix: str, command: str
) -> str:
    """生成夹具文件的完整文本（含"数据来源 + 再生命令"文件头）。"""
    lines = [
        '"""'
        + f"{archive.name} 的真实抓包样本"
        + "（**由 tools/export_saz_samples.py 自动生成，请勿手工编辑**）。",
        "",
        "【数据来源】",
        f"``captures/{archive.name}``（用户实测导出；原始 SAZ 仍在 captures/ 里）。",
        "",
        "【怎么用】",
        "在你自己的测试里 import 下面这些常量（全部是**子包字节**）。",
        "",
        "【导出命令（如需更新，原样重跑即可）】",
        f"    {command}",
        "",
        "【为什么存子包字节】",
        "``ProtoMessage.decode()`` 直接吃这段（已剥掉 ``RootPacket`` 信封），",
        "这样测试不依赖信封解析，也不会被 HTTP 头干扰。",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Final",
        "",
    ]
    for session in sessions:
        for kind, raw in (
            ("REQUEST", session.request),
            ("RESPONSE", session.response),
        ):
            body = body_of(raw)
            sub = sub_packet_of(body)
            name = constant_name(session, kind, prefix)
            lines.append(f"#: {session.index} {kind.lower()}：{describe_body(body)}")
            lines.append(f"{name}: Final[str] = {render_hex_literal(sub.hex())}")
            lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """命令行参数（三个必知的旋钮 + 一个可选输出路径）。"""
    parser = argparse.ArgumentParser(
        prog="export_saz_samples.py",
        description="把 SAZ 里指定会话的请求/响应子包字节导出成 protocol_samples 常量",
    )
    parser.add_argument(
        "--captures", metavar="目录", help="抓包目录（默认 <项目根>/captures）"
    )
    parser.add_argument(
        "--match", default="", help="只找文件名含该关键字的 SAZ（例如 市集）"
    )
    parser.add_argument(
        "--sessions",
        required=True,
        help="要导出的会话编号，逗号分隔（例如 raw/04,raw/06）",
    )
    parser.add_argument(
        "--prefix", default="", help="常量前缀（默认取会话目录名大写，例如 RAW）"
    )
    parser.add_argument("--out", metavar="文件", help="写盘路径；缺省只打印到 stdout")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：读 SAZ → 生成夹具文本 → 打印或写盘。"""
    force_utf8_output()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)

    capture_dir = resolve_capture_dir(args.captures)
    if not capture_dir.is_dir():
        print(f"✘ 找不到抓包目录：{capture_dir}")
        return 1

    archive = find_archive(capture_dir, args.match)
    wanted = [item.strip() for item in args.sessions.split(",") if item.strip()]
    sessions = load_sessions(archive, wanted)

    command = "PYTHONIOENCODING=utf-8 python tools/export_saz_samples.py " + " ".join(
        shlex.quote(arg) for arg in raw_argv
    )
    text = render_fixture(archive, sessions, args.prefix, command)

    print(f"抓包文件 : {archive.name}")
    for session in sessions:
        print(f"  · {session.index:<9} 请求 {describe_body(body_of(session.request))}")
        print(f"    {' ' * 9} 响应 {describe_body(body_of(session.response))}")

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = config.PROJECT_ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8", newline="\n")
        # 仅用于打印：优先显示相对项目根的短路径；写到项目外（如 tmp_path、
        # 其他盘符）时 relative_to 会抛 ValueError，此时退回显示绝对路径。
        try:
            shown = str(out_path.relative_to(config.PROJECT_ROOT))
        except ValueError:
            shown = str(out_path)
        print(f"✔ 已写出 : {shown}（{len(text.splitlines())} 行）")
    else:
        print("（未指定 --out，以下为将要写出的内容）")
        print()
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
