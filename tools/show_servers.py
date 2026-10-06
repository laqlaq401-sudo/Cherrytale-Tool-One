#!/usr/bin/env python3
"""把真实抓包里的**区服列表**解出来，并演示「选服」会选中哪一个。

【这个脚本回答什么问题】
「登录之后到底有哪些区服、编号是多少、我们会替玩家选哪个？」
这三个问题在写任何自动化之前都必须先有确定答案 —— 否则你会拿着一个
猜出来的 ``serverID`` 去登录，然后对着服务端一句含糊的错误码发呆。

数据来源有两处（脚本自动挑）：

1. ``captures/*.har`` 里那条 **8872 字节的 1002 响应** —— 完整数据，最准；
2. ``protocol_samples/gateway_samples.py`` 里同一条响应的**前 256 字节** ——
   离线可用（抓包文件不在时），但只能看到列表开头的几个区服。

【怎么用】
    python tools/show_servers.py                    # 解出列表 + 打印自动挑中的区服
    python tools/show_servers.py --server-id 3      # 模拟"我要进 3 区"
    python tools/show_servers.py --fixture          # 强制用离线夹具（不看抓包）
    python tools/show_servers.py --write notes/server_list.md
    python tools/show_servers.py --verbose          # 连字段级解码一起打印

【环境变量】
    CHERRYTALE_CAPTURE_DIR   抓包目录（默认 <项目根>/captures）
    CHERRYTALE_SERVER_ID     目标区服编号（等价于 --server-id）

.. note::
   本脚本只做「解码 + 选择」，**不会发任何网络请求**。
   这也是为什么它可以在协议尚未完全确认时放心运行。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Final, Sequence

# tools/ 不是包，无法直接 ``import config``，只能手动把项目根加进 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (必须在 sys.path 调整之后导入)
from models.envelope import EnvelopeError, parse_envelope  # noqa: E402
from models.packet_ids import packet_id  # noqa: E402
from models.server import (  # noqa: E402
    AccountClientInfoLoginRes,
    LoginPacket,
    ServerSelectError,
    group_servers,
)
from tools.extract_har import (  # noqa: E402  (复用抓包读取逻辑，避免两份实现漂移)
    HAR_FILES,
    iter_gateway_posts,
    response_bytes,
    sniff_packet_id,
)

#: 登录响应（区服列表就在这一包里）的消息号，取 1002
LOGIN_RES_PACKET_ID: Final[int] = packet_id("AccountClientInfoLoginRes")


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


def find_in_captures(capture_dir: Path) -> tuple[str, bytes] | None:
    """在抓包目录里找第一条**登录响应**（消息号 1002）。

    :return: ``(来源说明, 响应字节)``；找不到返回 ``None``。

    为什么要遍历所有 HAR 而不是硬编码下标？抓包文件会换、条目顺序会变，
    下标写死的结果是"某天突然解出别的包"，而且报错信息完全看不出问题在哪。
    """
    for har_name in HAR_FILES:
        har_path = capture_dir / har_name
        if not har_path.is_file():
            continue
        for item in iter_gateway_posts(har_path):
            body = response_bytes(item["entry"])
            if body is None:
                continue
            if sniff_packet_id(body) == LOGIN_RES_PACKET_ID:
                return f"captures/{har_name} 第 {item['index']} 条响应", body
    return None


def find_in_fixture() -> tuple[str, bytes]:
    """从离线夹具里取登录响应（抓包文件不在时的退路）。

    :raises LookupError: 夹具里没有 1002 样本时。

    .. note::
       夹具为了控制体积，只内联了大响应的前 256 字节，
       因此**解出的区服列表是不完整的** —— 调用方必须把这个局限告诉使用者。
    """
    from protocol_samples.gateway_samples import SAMPLES

    for sample in SAMPLES:
        if sample.get("response_packet_id") == LOGIN_RES_PACKET_ID:
            return (
                f"夹具 {sample['name']}（仅前 {len(sample['response_hex']) // 2} 字节）",
                bytes.fromhex(sample["response_hex"]),
            )
    raise LookupError(
        "夹具里没有登录响应（1002）样本；请先运行 python tools/extract_har.py 重新导出"
    )


def build_server_table(response: AccountClientInfoLoginRes) -> list[str]:
    """把区服列表渲染成 Markdown 表格行。"""
    lines = [
        "| 编号 | 名称 | 分组 | 状态 | 人数 | 角色 | 备注 |",
        "|---:|---|---:|---:|---:|---|---|",
    ]
    for server in response.allServerList:
        character = "有" if server.has_character() else "无"
        lines.append(
            f"| {server.serverID} | {server.serverName} | {server.serverGroupID} "
            f"| {server.serverState} | {server.people_count} | {character} | {server.serverNote} |"
        )
    return lines


def render_report(
    *,
    source: str,
    response: AccountClientInfoLoginRes,
    chosen_text: str,
    packet: LoginPacket | None,
    verbose: bool,
) -> str:
    """渲染完整报告（既用于 ``--write``，也用于 ``--verbose`` 时的终端输出）。"""
    lines: list[str] = [
        "# 区服列表（由 tools/show_servers.py 自动生成，请勿手工编辑）",
        "",
        "> 重新生成：`python tools/show_servers.py --write notes/server_list.md`",
        "",
        "## 数据来源",
        "",
        f"- 抓包 / 夹具：{source}",
        f"- 消息号：{LOGIN_RES_PACKET_ID}（AccountClientInfoLoginRes）",
        f"- 账号 ID：{response.accountID}，errorCode：{response.errorCode}",
        f"- 区服数量：{len(response.allServerList)}",
        "",
        "## 区服列表",
        "",
    ]
    lines.extend(build_server_table(response))
    lines.extend(["", "## 分组", ""])
    for group in group_servers(response.allServerList):
        lines.append(f"- 组 {group.group_id}：{len(group.servers)} 个区服，编号 {list(group.ids())}")
    lines.extend(["", "## 本次选择", "", chosen_text, ""])
    if packet is not None:
        lines.extend(
            [
                "## 生成的登录包（1003 LoginPacket）",
                "",
                f"- serverID = **{packet.serverID}**（区服编号就在这里生效）",
                f"- accountID = {packet.accountID}",
                f"- 编码后的字节（hex）：`{packet.to_hex()}`",
                "",
            ]
        )
    if verbose:
        lines.extend(["## 字段级解码（1002 响应子包）", "", "```", response.describe(), "```", ""])
    return "\n".join(lines)


def select_and_describe(
    response: AccountClientInfoLoginRes,
    *,
    server_id: int | None,
    group_id: int | None,
    prefer_with_character: bool = False,
) -> tuple[LoginPacket | None, str, str | None]:
    """按配置挑一个区服，并生成对应的登录包。

    :param prefer_with_character: 只在「本账号已有角色」的区服里挑
        （实测抓包里 167 个区服中有 24 个满足）。
    :return: ``(登录包或 None, 可读说明, 错误信息或 None)``。

    出错时**不抛异常**：本工具的目的是"把状况说清楚"，所以把失败原因
    当返回值交给 ``main`` 打印，避免调用方还要包一层 try/except。
    """
    try:
        chosen = response.choose_server(
            server_id=server_id,
            group_id=group_id,
            prefer_with_character=prefer_with_character,
        )
        packet = LoginPacket.for_server(
            chosen,
            account_id=response.accountID,
            channel_platform_type=config.CHANNEL_PLATFORM_TYPE_EL,
            client_version=config.GAME_CLIENT_VERSION,
            language_code=config.GAME_LANGUAGE,
        )
    except ServerSelectError as exc:
        # 选服失败与"选出来的区服编号为 0"都归到这里，统一给出可操作的提示
        return None, "", str(exc)

    if server_id is not None:
        reason = "你显式指定的区服（--server-id / CHERRYTALE_SERVER_ID）"
    elif response.suggestedServer is not None and chosen.serverID == response.suggestedServer.serverID:
        reason = "服务端建议的区服（suggestedServer）"
    elif any(server.serverID == chosen.serverID for server in response.lastLoginedServerList):
        reason = "最近登录过的区服（lastLoginedServerList）"
    else:
        reason = "列表里编号最小的区服（兜底策略）"
    if prefer_with_character and chosen.has_character():
        reason += "；且已限定在「本账号有角色」的区服里"

    text = (
        f"- 选中：{chosen.label()}\n"
        f"- 选择依据：{reason}\n"
        f"- 将填入 1003 LoginPacket 的 serverID：**{chosen.serverID}**"
    )
    return packet, text, None


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="show_servers.py",
        description="解出抓包里的区服列表并演示选服（只读，不联网）",
    )
    parser.add_argument("--captures", metavar="目录", help="抓包目录（默认 <项目根>/captures）")
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="强制使用离线夹具（夹具只内联前 256 字节，列表可能不完整）",
    )
    parser.add_argument(
        "--server-id",
        type=int,
        metavar="编号",
        help="指定区服编号；默认取环境变量 CHERRYTALE_SERVER_ID（未设置则自动挑）",
    )
    parser.add_argument(
        "--group-id", type=int, metavar="组号", help="只在指定分组里挑（默认不限制分组）"
    )
    parser.add_argument(
        "--with-character",
        action="store_true",
        help="只在「本账号已有角色」的区服里挑（避免自动化时在新区开出空号）",
    )
    parser.add_argument(
        "--write", metavar="路径", help="把完整报告写入文件，例如 notes/server_list.md"
    )
    parser.add_argument("--verbose", action="store_true", help="额外打印字段级解码")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """程序入口。

    :return: 成功返回 ``0``；解析失败或选服失败返回 ``1``。
    """
    force_utf8_output()
    args = build_parser().parse_args(argv)

    # ---- 取数据：优先真实抓包（完整），退化到离线夹具（截断）----
    # ``truncated`` 会一直传到解析层：夹具只内联大响应的前 256 字节，
    # 而区服列表包声明了 8781 字节 —— 必须开宽容模式，否则会白丢能用的信息。
    note = ""
    truncated = False
    if args.fixture:
        source, body = find_in_fixture()
        truncated = True
        note = "（夹具只内联了前 256 字节，列表可能不完整）"
    else:
        found = find_in_captures(resolve_capture_dir(args.captures))
        if found is None:
            source, body = find_in_fixture()
            truncated = True
            note = "（抓包里没找到 1002 响应，已退回离线夹具；列表可能不完整）"
        else:
            source, body = found

    try:
        view = parse_envelope(body, tolerate_truncation=truncated)
        response = view.decode_sub_packet(AccountClientInfoLoginRes)
    except EnvelopeError as exc:
        print(f"✘ 解析失败：{exc}")
        print("  这条响应不是合法的 RootPacket，或字段对齐与预期不同；")
        print("  先用 --verbose 看一遍原始字段，再回来核对 models/server.py 的字段顺序。")
        return 1

    server_id = args.server_id if args.server_id is not None else config.SERVER_ID
    group_id = args.group_id if args.group_id is not None else config.SERVER_GROUP_ID

    packet, chosen_text, error = select_and_describe(
        response,
        server_id=server_id,
        group_id=group_id,
        prefer_with_character=args.with_character,
    )

    # ---- 终端摘要 ----
    print("=" * 72)
    print(f"数据来源 : {source}{note}")
    print(f"账号 ID  : {response.accountID}    errorCode: {response.errorCode}")
    print(f"区服数量 : {len(response.allServerList)}")
    print(f"选服配置 : {config.server_selection_summary()}")
    print("=" * 72)
    print(response.describe_servers())
    print("-" * 72)
    if error:
        print(f"✘ {error}")
    else:
        print(chosen_text)
        assert packet is not None  # error 为空时 packet 必然存在
        print(f"登录包（1003）编码字节：{packet.to_hex()}")
    print("-" * 72)
    print("提示：加 --verbose 可打印字段级解码；--write notes/server_list.md 可落成报告。")
    if args.verbose:
        print(response.describe())

    if args.write:
        report = render_report(
            source=f"{source}{note}",
            response=response,
            chosen_text=chosen_text,
            packet=packet,
            verbose=args.verbose,
        )
        output_path = Path(args.write).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(report + "\n")
        print(f"✔ 报告已写入：{output_path}")

    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())


