#!/usr/bin/env python3
"""Fiddler SAZ 筛查工具：把"某份抓包里有哪些网关报文"一次列清楚。

【为什么需要它】
逆向期间反复要做同一件事：抓了一份 ``captures/*.saz``，想知道"里面有没有我要的包"。
过去靠临时敲 ``python -c``（既长又容易敲错，还要每次重新想 ``unzip -p`` 的用法）。
更麻烦的是**大包**：本次实际遇到的 SAZ 有 95MB（含 CDN 资源下载），
整体读进上下文会瞬间吃掉大量 Token。

本工具只做两件事，且都只读**必要的那几条**：

1. ``--list``：列出全部 ``captures/*.saz``（文件、大小）；
2. 默认：对每个 SAZ 打印**网关 POST** 一览（会话名 / 请求消息号 / 请求体积 /
   响应消息号 / 响应体积），可用 ``--decode raw/02`` 再解某一条的字段图。

【用法】
    python tools/extract_saz_posts.py --list
    python tools/extract_saz_posts.py                        # 全部 saz 的网关 POST 一览
    python tools/extract_saz_posts.py --match 荣耀            # 只处理文件名含"荣耀"的
    python tools/extract_saz_posts.py --match 荣耀 --decode raw/02   # 解某一条的字段图

【为什么筛选条件是"请求里含网关域名"而不是"解析所有会话"】
SAZ 里 90% 以上是 CDN/浏览器流量（实测 271 个会话里只有 5 个是业务 POST）。
先按域名筛掉，再解析，才能保证"只把要用的字节读进来"。

【容错说明】
某些会话的请求/响应体在 SAZ 里是残缺的（TLS 隧道、被中断的下载……），
解析会失败。本工具对这类会话只**跳过并计数**，绝不因此中断整次筛查 ——
否则你会在"99 条都好看，第 100 条崩了"这种地方浪费一次次重跑。
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# tools/ 不是包，无法直接 import config，只能手动把项目根加进 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (必须在 sys.path 调整之后导入)
from models.envelope import EnvelopeError, parse_envelope  # noqa: E402
from models.packet_ids import packet_name  # noqa: E402
from models.protobuf_wire import ProtobufWireError, fields_by_number  # noqa: E402

#: 网关域名关键字（只筛它 —— 见模块文档"为什么筛选条件是请求里含网关域名"）
GATEWAY_HOST: Final[str] = "game-ct-labs"


@dataclass(frozen=True)
class Session:
    """一个 Fiddler 会话（请求 + 响应）。"""

    name: str  # 例如 raw/02_c.txt
    request: bytes
    response: bytes

    @property
    def index(self) -> str:
        """会话编号（``raw/02_c.txt`` → ``raw/02``），用于 ``--decode``。"""
        return self.name.removesuffix("_c.txt")

    def request_line(self) -> str:
        """请求行之外的第一行文本（POST/DELETE 目标，便于确认是不是网关）。"""
        return self.request.split(b"\r\n", 1)[0].decode("latin-1", "replace")

    def is_gateway_post(self) -> bool:
        """是不是发往游戏网关的 POST（SAZ 里其余流量与我们无关）。"""
        return self.request.startswith(b"POST") and GATEWAY_HOST.encode() in self.request


def body_of(raw: bytes) -> bytes:
    """取 HTTP 报文的**正文**（跳过头部）。

    :return: 分隔符 ``\\r\\n\\r\\n`` 之后的部分；没有分隔符时返回空字节。
    """
    _head, separator, body = raw.partition(b"\r\n\r\n")
    return body if separator else b""


def iter_sessions(archive: Path) -> Iterator[Session]:
    """按会话编号顺序产出 SAZ 里的会话。

    【为什么把"缺响应"也产出来】
    抓包时常常只有请求（响应还没到就被保存了）。静默丢掉会让"这次为什么没看到结果"
    变成一个查不出来的问题，所以这里原样产出，由调用方决定怎么显示。
    """
    with zipfile.ZipFile(archive) as handle:
        names = sorted(n for n in handle.namelist() if n.endswith("_c.txt"))
        for name in names:
            request = handle.read(name)
            response_name = name.removesuffix("_c.txt") + "_s.txt"
            try:
                response = handle.read(response_name)
            except KeyError:
                response = b""
            yield Session(name=name, request=request, response=response)


def describe_capture(archive: Path) -> None:
    """打印一份 SAZ 的网关 POST 一览（消息号 + 体积）。"""
    print(f"== {archive.name}（{archive.stat().st_size:,} B）==")
    found = 0
    skipped = 0
    for session in iter_sessions(archive):
        if not session.is_gateway_post():
            continue
        found += 1
        request_body = body_of(session.request)
        response_body = body_of(session.response)

        try:
            request_id = parse_envelope(request_body).packet_id
            request_label = f"{packet_name(request_id)}({request_id})"
        except (EnvelopeError, ProtobufWireError):
            skipped += 1
            request_label = "<请求体不完整/非 protobuf>"

        try:
            response_id = parse_envelope(response_body).packet_id
            response_label = f"{packet_name(response_id)}({response_id})"
        except (EnvelopeError, ProtobufWireError):
            response_label = "<响应体不完整/为空>"

        print(
            f"  · {session.index:<9} 请求 {request_label:<32}"
            f" {len(request_body):>6} B  →  响应 {response_label:<32}"
            f" {len(response_body):>6} B"
        )

    if found == 0:
        print("  （没有网关 POST —— 这份包可能只抓到了 CDN/浏览器流量）")
    elif skipped:
        print(f"  ⚠ 有 {skipped} 条请求体不完整（SAZ 导出残缺），已跳过其消息号解析")


def decode_session(archive: Path, index: str) -> None:
    """打印某个会话的请求/响应**顶层字段图**（不解大字段内容）。"""
    target = index if index.endswith("_c.txt") else f"{index}_c.txt"
    session = next((s for s in iter_sessions(archive) if s.name == target), None)
    if session is None:
        print(f"✘ 没找到会话 {index}（编号形如 raw/02）")
        return

    print(f"== {archive.name} · {session.index} ==")
    print(f"请求行：{session.request_line()}")
    for label, body in (("请求", body_of(session.request)), ("响应", body_of(session.response))):
        if not body:
            print(f"{label}：<空>")
            continue
        try:
            view = parse_envelope(body)
        except (EnvelopeError, ProtobufWireError) as exc:
            print(f"{label}：解析失败（{exc}）")
            continue
        print(
            f"{label}：消息号 {view.packet_id}"
            f"（{view.class_name or '未收录'}），子包 {len(view.sub_packet_bytes)} B"
        )
        try:
            grouped = fields_by_number(view.sub_packet_bytes)
        except ProtobufWireError as exc:
            print(f"  子包字段图读取失败：{exc}")
            continue
        for number in sorted(grouped):
            entries = grouped[number]
            detail = ", ".join(
                f"varint={field.value}"
                if field.wire_type == 0
                else f"len={len(field.as_bytes())}"
                for field in entries[:4]
            )
            print(f"  #{number:<3} ×{len(entries):<2} {detail}")


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="extract_saz_posts.py",
        description="Fiddler SAZ 筛查：列出网关 POST、按需解码某条报文的字段图",
    )
    parser.add_argument("--list", action="store_true", help="只列出 captures/*.saz")
    parser.add_argument(
        "--match",
        default="",
        help="只处理文件名包含该关键字的 SAZ（例如 荣耀 / 竞技 / 天命 / 失控）",
    )
    parser.add_argument("--decode", default="", help="解码指定会话编号（例如 raw/02）")
    args = parser.parse_args(argv)

    archives = sorted(config.PROJECT_ROOT.glob("captures/*.saz"))
    if args.match:
        archives = [path for path in archives if args.match in path.name]

    if args.list:
        print(f"SAZ 归档（{config.PROJECT_ROOT / 'captures'}）：")
        for path in archives:
            print(f"  · {path.name:<28} {path.stat().st_size:>12,} B")
        return 0

    if not archives:
        print("✘ 没有匹配的 .saz（用 --list 看看有哪些）")
        return 1

    for path in archives:
        if args.decode:
            decode_session(path, args.decode)
        else:
            describe_capture(path)
            print()
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())