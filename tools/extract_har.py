#!/usr/bin/env python3
"""从 Fiddler HAR 抓包里提取游戏网关的真实字节，生成测试夹具与流量清单。

【为什么要有这个脚本】
我们的对手是「服务端只回一个含糊错误」的协议。任何凭猜测写出来的报文模型，
都必须拿**真实字节**验证过才算数。人工从 HAR 里一条条复制 base64、
再手工核对字段编号，既慢又容易抄错，所以固化成脚本：
一次运行就把「真实请求/响应字节 + 它们分别是什么包」落成可复用的测试夹具。

【HAR 里的两个坑（本脚本已分别处理）】
1. **响应体是 base64，无损** → 可以直接用，这是我们的主要素材；
2. **请求体是 text，有损** → Fiddler 把二进制当文本导出，非 ASCII 字节全被替换成了
   ``\\ufffd``。因此请求体**无法**从 HAR 还原，只能由 HexView 提供。
   本脚本对缺失请求体的样本会**显式标注**，而不是假装它有。

【样本取舍（避免夹具文件动辄几百 KB）】
- 响应 ≤ ``INLINE_LIMIT``：完整 hex 内联，可直接离线测试；
- 响应 > ``INLINE_LIMIT``：只内联前 ``PREVIEW_BYTES`` 字节（足够识别 packetID 与包类型），
  并在报告里注明完整数据仍在 HAR 中。

【用法】
    python tools/extract_har.py            # 生成夹具与流量清单
    python tools/extract_har.py --list     # 只打印清单，不写盘
    python tools/extract_har.py --check    # 校验夹具是否与 HAR 一致（不写盘）

【环境变量】
    CHERRYTALE_CAPTURE_DIR   抓包目录（默认 <项目根>/captures）
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any, Final, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (必须在 sys.path 调整之后导入)
from models.packet_ids import packet_name  # noqa: E402
from models.protobuf_wire import ProtobufWireError, decode_varint  # noqa: E402

#: 抓包目录里的 HAR 文件（按此顺序处理，报告与夹具顺序一致）
HAR_FILES: Final[tuple[str, ...]] = (
    "login.har",
    "cherrytale-fiddler-1.har",
)

#: 游戏网关特征（端口 1893 是实测确认的）
GATEWAY_PORT_MARKER: Final[str] = "1893"

#: 响应内联阈值：超过它只保留前 PREVIEW_BYTES 字节
INLINE_LIMIT: Final[int] = 4096
PREVIEW_BYTES: Final[int] = 256

#: 首次握手请求（88 字节）—— 由用户从 Fiddler 的 HexView 复制。
#:
#: 【为什么硬编码在这里】
#: HAR 导出的请求体是有损文本，唯独这一条用户手工提供了完整 hex，
#: 而它恰好是**最重要的一条**：整条链路的第一个包（版本控制握手）。
#: 有了它，信封的「编码侧」也能拿真实字节验证，而不是只能测解码侧。
VERSION_CONTROL_REQUEST_HEX: Final[str] = (
    "08c785061a022d3150ffffffffffffffffff01"
    "6089e7ddee8b34722042413332453738413343353236343333383130344244"
    "43413637444136393933baac30180802120e322e322e302d656c2d682d7769"
    "6e1a047a68636e"
)

#: 夹具文件与流量清单的路径（相对项目根）
FIXTURE_PATH: Final[str] = "protocol_samples/gateway_samples.py"
REPORT_PATH: Final[str] = "notes/gateway_traffic.md"

_FIXTURE_DOC = '''"""游戏网关的真实抓包样本（**由 tools/extract_har.py 自动生成，请勿手工编辑**）。

【这批样本解决什么问题】
协议逆向上最容易犯的错，是「按 dump.cs 的字段清单推断出编号，然后直接拿它组包」。
编号一旦差一位，服务端会安静地把值读到别的字段上 —— 现象是「参数看着没错但行为怪」，
而错误信息帮不上任何忙。

所以本项目要求：**任何报文模型都要在真实字节上验证过**。
本文件就是那批真实字节：请求侧一条（首次握手），响应侧多条（覆盖多种业务包）。

【怎么用】
    from protocol_samples.gateway_samples import SAMPLES, VERSION_CONTROL_REQUEST_HEX

    sample = SAMPLES[0]
    bytes.fromhex(sample["response_hex"])      # 真实响应字节
    sample["response_packet_id"]               # 已经预先解出的消息号（省一步排查）

【数据来源与已知局限】
- 响应：HAR 里的 ``content.text``（base64，**无损**）；
- 请求：只有首次握手那条 —— 由用户从 Fiddler 的 HexView 复制。
  HAR 里的请求体是 text，二进制字节被替换成了 ``\\ufffd``，**无法还原**，
  因此其余样本的 ``request_hex`` 是空串（脚本刻意保留字段以标注这种缺失）。
- 大响应（超过 {inline_limit} 字节）只内联前 {preview} 字节，
  ``response_truncated`` 为 True；完整数据仍在 captures/ 的 HAR 里。
"""'''

#: 大响应预览不足的部分用这个标注
_TRUNCATED_NOTE: Final[str] = "（仅前若干字节，完整数据见 HAR）"


def iter_gateway_posts(har_path: Path) -> Iterator[dict[str, Any]]:
    """产出 HAR 里所有发往游戏网关的 POST 条目（附上序号，便于与报告对照）。"""
    try:
        har = json.loads(har_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 HAR：{har_path}（{exc}）") from exc

    for index, entry in enumerate(har.get("log", {}).get("entries", [])):
        request = entry.get("request", {})
        if request.get("method") != "POST":
            continue
        if GATEWAY_PORT_MARKER not in str(request.get("url", "")):
            continue
        yield {"index": index, "entry": entry}


def response_bytes(entry: dict[str, Any]) -> bytes | None:
    """取响应体原始字节。

    :return: 字节；HAR 未提供内容（例如 ``body was omitted``）时返回 ``None``。

    为什么必须检查 ``encoding``？
        HAR 用 ``content.encoding == "base64"`` 表示「这里是 base64 编码的二进制」。
        若缺失该字段，``text`` 就是**有损文本**（Fiddler 对超大二进制体会省略内容），
        直接 base64 解码只会得到垃圾 —— 宁可返回 None 并标注，也不要伪造数据。
    """
    content = entry.get("response", {}).get("content", {})
    text = content.get("text")
    if not text or content.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(text)
    except (ValueError, TypeError):
        return None


def sniff_packet_id(data: bytes) -> int | None:
    """从响应字节里猜出消息号。

    ``RootPacket`` 的第一个字段就是 ``packetID``（编号 1、varint），
    所以正常情况下字节开头是 ``08 <varint>``。
    返回 ``None`` 表示「开头不像 RootPacket」——这本身也是有价值的信息
    （说明该响应的外层结构与我们预期不同，值得单独看一眼）。
    """
    try:
        value, position = decode_varint(data)
    except ProtobufWireError:
        return None
    if value != (1 << 3) | 0:  # 字段 1、线类型 varint → 0x08
        return None
    try:
        return decode_varint(data, position)[0]
    except ProtobufWireError:
        return None


# ---------------------------------------------------------------------------
# 样本收集
# ---------------------------------------------------------------------------
def collect_samples(capture_dir: Path) -> list[dict[str, Any]]:
    """扫描抓包目录，把网关流量整理成结构化样本。

    :return: 每个元素是一条 POST 的完整信息（请求/响应字节 + 已解出的包类型）。

    ``request_hex`` 的处理原则（**宁可空缺，也不伪造**）：
        HAR 里的请求体是有损文本，唯一可靠的请求字节来自用户用 HexView 复制的
        首次握手包，所以只有「请求体大小为 88 字节」的样本会带上它，
        并在 ``request_hex_source`` 里注明来源。
    """
    samples: list[dict[str, Any]] = []

    for har_name in HAR_FILES:
        har_path = capture_dir / har_name
        if not har_path.is_file():
            continue

        for item in iter_gateway_posts(har_path):
            entry = item["entry"]
            request = entry.get("request", {})
            request_size = int(request.get("bodySize") or 0)

            body = response_bytes(entry)
            truncated = False
            if body is not None and len(body) > INLINE_LIMIT:
                body_hex = body[:PREVIEW_BYTES].hex()
                truncated = True
            else:
                body_hex = body.hex() if body else ""

            packet_id = sniff_packet_id(body) if body else None

            # 88 字节的首包：请求体已知（HexView 提供）
            if request_size == 88:
                request_hex = VERSION_CONTROL_REQUEST_HEX
                request_hex_source = "用户从 Fiddler HexView 复制（时间戳/vCode 随抓包不同）"
            else:
                request_hex = ""
                request_hex_source = "缺失：HAR 导出的请求体是有损文本，需 HexView 提供"

            samples.append(
                {
                    "name": f"{har_path.stem}#{item['index']}",
                    "source": f"captures/{har_name}",
                    "entry_index": item["index"],
                    "request_size": request_size,
                    "request_hex": request_hex,
                    "request_hex_source": request_hex_source,
                    "request_headers": {
                        str(h.get("name", "")): str(h.get("value", ""))
                        for h in request.get("headers", [])
                    },
                    "response_size": len(body) if body else 0,
                    "response_hex": body_hex,
                    "response_truncated": truncated,
                    "response_packet_id": packet_id,
                    "response_class": packet_name(packet_id) if packet_id else None,
                }
            )

    return samples


# ---------------------------------------------------------------------------
# 渲染夹具文件
# ---------------------------------------------------------------------------
def _wrap_hex(value: str, indent: str = "    ", width: int = 64) -> list[str]:
    """把长 hex 串切成多行字符串字面量（便于阅读与 diff）。"""
    if not value:
        return [f'{indent}""']
    return [f'{indent}"{value[i : i + width]}"' for i in range(0, len(value), width)]


def render_fixture(samples: list[dict[str, Any]]) -> str:
    """渲染 ``protocol_samples/gateway_samples.py`` 的完整内容。"""
    lines: list[str] = [
        _FIXTURE_DOC.format(inline_limit=INLINE_LIMIT, preview=PREVIEW_BYTES),
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any, Final",
        "",
        "#: 首次握手请求（88 字节）—— 整条链路的第一个包，也是唯一有完整请求体的样本",
        "VERSION_CONTROL_REQUEST_HEX: Final[str] = (",
        *_wrap_hex(VERSION_CONTROL_REQUEST_HEX, indent="    "),
        ")",
        "",
        "#: 抓包样本（每条 = 一次 POST 的请求/响应，以及预先解出的消息号与包名）",
        "SAMPLES: Final[tuple[dict[str, Any], ...]] = (",
    ]

    for sample in samples:
        lines.append("    {")
        for key, value in sample.items():
            if key in ("request_hex", "response_hex") and value:
                lines.append(f"        {key!r}: (")
                lines.extend(_wrap_hex(value, indent="            "))
                lines.append("        ),")
            else:
                lines.append(f"        {key!r}: {value!r},")
        lines.append("    },")

    lines.append(")")
    return "\n".join(lines) + "\n"




# ---------------------------------------------------------------------------
# 渲染流量清单
# ---------------------------------------------------------------------------
_REPORT_DOC = """# 网关流量清单

> 本文件由 `tools/extract_har.py` 自动生成，请勿手工编辑。
> 重新生成：`python tools/extract_har.py`

## 这批数据回答了什么

| 问题 | 结论 |
|---|---|
| 网关地址与端口 | `game-ct-labs.ecchi.xxx:1893`（服务端在载荷中也确认了它） |
| 通信方式 | 标准 HTTP POST 一问一答，`Content-Type: application/octet-stream` |
| 报文格式 | 裸 protobuf（`RootPacket`：字段 1 = `packetID`） |
| 签名 / 加密 / 分帧 | **都没有** —— 无需反汇编去挖 vCode 算法 |
| 包裹层 | 无私有长度前缀；HTTP 自带 `Content-Length` |

## 请求头（抓包实测，逐项照抄）

```
{headers}
```

## 流量明细

| # | 来源 | 请求字节 | 响应字节 | 响应消息号 | 响应包名 | 请求体 hex |
|---:|---|---:|---:|---:|---|---|
{table}
"""


def render_report(samples: list[dict[str, Any]]) -> str:
    """渲染 ``notes/gateway_traffic.md``。"""
    known_headers: dict[str, str] = {}
    for sample in samples:
        if sample["request_headers"]:
            known_headers = sample["request_headers"]
            break
    headers_block = "\n".join(f"{name}: {value}" for name, value in known_headers.items())

    rows: list[str] = []
    for position, sample in enumerate(samples, start=1):
        has_request = "有" if sample["request_hex"] else "缺失"
        packet_id = sample["response_packet_id"]
        class_name = sample["response_class"] or "（外层结构待确认）"
        rows.append(
            f"| {position} | {sample['name']} | {sample['request_size']} | "
            f"{sample['response_size']} | {packet_id if packet_id else '?'} | "
            f"{class_name} | {has_request} |"
        )

    recognized = sum(1 for s in samples if s["response_packet_id"])
    missing_request = sum(1 for s in samples if not s["request_hex"])

    body = _REPORT_DOC.format(headers=headers_block, table="\n".join(rows))
    return (
        body
        + "\n## 统计\n\n"
        + f"- 抓到的网关 POST 总数：**{len(samples)}**\n"
        + f"- 能解出响应消息号的：**{recognized}**\n"
        + f"- 请求体缺失的：**{missing_request}**"
        + "（HAR 导出的请求体是有损文本；如需完整字节，请在 Fiddler 里用 HexView 复制）\n"
    )


# ---------------------------------------------------------------------------
# 写盘
# ---------------------------------------------------------------------------
def _validate_python(content: str, path: Path) -> None:
    """写盘前做语法校验（生成器最容易犯的错就是拼错引号，留下一个坏文件）。"""
    import ast

    try:
        ast.parse(content, filename=str(path))
    except SyntaxError as exc:
        raise RuntimeError(
            f"生成的内容有语法错误（第 {exc.lineno} 行：{exc.msg}）—— 本次未写盘"
        ) from exc


def write_text(path: Path, content: str, *, validate: bool = False) -> None:
    """写出文件（``newline="\\n"`` 保证跨平台一致）。"""
    if validate:
        _validate_python(content, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def check_text(path: Path, expected: str) -> bool:
    """比较磁盘上的文件与「现在重新生成会得到的内容」。"""
    if not path.is_file():
        print(f"✘ 文件不存在：{path}")
        print("  修复：运行 python tools/extract_har.py")
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


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def force_utf8_output() -> None:
    """把标准输出/错误流强制改为 UTF-8（原因见 main.py 里的同名函数）。"""
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_har.py",
        description="从 Fiddler HAR 抓包提取网关真实字节，生成测试夹具与流量清单",
    )
    parser.add_argument(
        "--captures", metavar="目录", help="抓包目录（默认 <项目根>/captures）"
    )
    parser.add_argument("--list", action="store_true", help="只打印流量清单，不写盘")
    parser.add_argument(
        "--check", action="store_true", help="只校验夹具/清单是否与抓包一致（不写盘）"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    force_utf8_output()
    args = build_parser().parse_args(argv)

    capture_dir = resolve_capture_dir(args.captures)
    if not capture_dir.is_dir():
        print(f"✘ 找不到抓包目录：{capture_dir}")
        print("  把 .har 文件放进该目录，或用 --captures 指定位置")
        return 1

    samples = collect_samples(capture_dir)
    if not samples:
        print(f"✘ 在 {capture_dir} 里没找到发往网关（端口 {GATEWAY_PORT_MARKER}）的 POST")
        return 1

    recognized = sum(1 for s in samples if s["response_packet_id"])
    missing = sum(1 for s in samples if not s["request_hex"])

    print(f"抓包目录   : {capture_dir}")
    print(f"网关 POST  : {len(samples)} 条")
    print(f"可识别包   : {recognized} 条（响应首字段能解出消息号与包名）")
    if missing:
        print(f"· 请求体缺失: {missing} 条（HAR 有损导出；需要时请在 Fiddler 用 HexView 复制）")

    fixture = render_fixture(samples)
    report = render_report(samples)
    fixture_path = config.PROJECT_ROOT / FIXTURE_PATH
    report_path = config.PROJECT_ROOT / REPORT_PATH

    if args.list:
        print()
        print(report)
        return 0

    if args.check:
        fixture_ok = check_text(fixture_path, fixture)
        report_ok = check_text(report_path, report)
        return 0 if (fixture_ok and report_ok) else 1

    write_text(fixture_path, fixture, validate=True)
    write_text(report_path, report)
    print(
        f"✔ 夹具     : {fixture_path.relative_to(config.PROJECT_ROOT)}"
        f"（{len(fixture.splitlines())} 行）"
    )
    print(
        f"✔ 流量清单 : {report_path.relative_to(config.PROJECT_ROOT)}"
        f"（{len(report.splitlines())} 行）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

