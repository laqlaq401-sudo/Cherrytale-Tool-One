#!/usr/bin/env python3
"""从抓包里**反解**战斗结算 token 的签名绑定（哪个字段进了 MD5 的第几位）。

【它解决什么问题】
``crypto/battle_signer.py`` 里的公式是"读反汇编 + 人工确认 call site 参数"得到的。
公式本身只有一行，真正容易错的是**绑定**：``iRandomData2`` 在竞技场是对手
``playerID``、在主线是 ``timeCost``、在炼成阵是 ``score``。过去确认一条绑定要
人眼翻几十行汇编，而且每个新玩法都要重来一遍。

本工具换一条路：**token 是已知的、salt 是已知的，那就穷举候选数字把它解出来**。
候选来自同一份抓包里解码出的全部整数（protobuf varint + 数字字符串），
解出来的结果还会告诉你**每个数字第一次出现在哪条报文** —— 这就是绑定的证据。

【用法】
    # 解一份抓包里所有结算 token 的绑定
    python tools/solve_signer_binding.py --saz "captures/old/1.0.0NEED/失控炼成阵.saz"

    # 只解某一条报文
    python tools/solve_signer_binding.py --saz ... --entry raw/26

    # 已知 token，只想知道它由什么拼出来
    python tools/solve_signer_binding.py --saz ... --token 0a3396051faf186762d81815977630bf

【输出怎么读】
    ✔ 0a3396…bf  =  MD5("29744" + "53247" + "" + "03120908")
        29744 ← raw/01_c.txt   （进战响应下发的 fightToken）
        53247 ← raw/26_c.txt   （结算包里的 Score）
``←`` 后面是"这个数字第一次出现的报文"，据此判断它是 fightToken 还是业务字段。

【边界】
- 只穷举**两个**数字位 + 一个胜负位，对应现有的两种公式族；三段以上需要扩族。
- 候选池取"目标报文之前的所有报文"（fightToken 通常在进战时下发，先于结算包）。
- 解不出来不等于公式错：可能是三段拼接、字段被 varint 编码成负数、
  或参与签名的值根本没在抓包里出现过（例如服务端下发的随机数）。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterator, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crypto.battle_signer import BATTLE_VERIFY_SALT  # noqa: E402
from models.protobuf_wire import iter_fields  # noqa: E402
from tools.extract_saz_posts import body_of, iter_sessions  # noqa: E402

#: 32 位十六进制 token（前后不能紧邻十六进制字符，避免截断更长的串）
TOKEN_PATTERN: Final[re.Pattern[bytes]] = re.compile(
    rb"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"
)

#: 报文里裸的数字字符串（日志、拼接串里常见）
NUMBER_PATTERN: Final[re.Pattern[bytes]] = re.compile(rb"(?<![0-9A-Za-z])[0-9]{1,10}(?![0-9A-Za-z])")

#: 胜负位候选（空串 = 该玩法不参与胜负）
WIN_LOSE_CHOICES: Final[tuple[str, ...]] = ("", "0", "1")

#: 候选数字的长度上限（再长基本是时间戳/哈希片段）
MAX_DIGITS: Final[int] = 10

#: 默认排除的位数 —— 10 位整数几乎全是时间戳，留在候选池里只会拖慢穷举
DEFAULT_EXCLUDED_DIGITS: Final[frozenset[int]] = frozenset({10})


@dataclass(frozen=True)
class Candidate:
    """一个可能参与签名的数字。"""

    value: str
    origin: str  # 第一次出现的报文编号


@dataclass(frozen=True)
class Binding:
    """解出的一组绑定。"""

    token: str
    first: Candidate
    second: Candidate
    win_lose: str
    raw: str

    def describe(self) -> str:
        """人类可读的一行结论。"""
        return (
            f'MD5("{self.first.value}" + "{self.second.value}" + "{self.win_lose}"'
            f' + "{BATTLE_VERIFY_SALT}") = {self.token}'
        )


def _walk_numbers(fields: Iterator, depth: int = 0) -> Iterator[str]:
    """递归收集 protobuf 里的整数与纯数字字符串。"""
    for field in fields:
        if field.wire_type == 0:
            yield str(field.as_int64())
        elif field.wire_type == 2:
            try:
                text = field.as_string()
            except Exception:  # noqa: BLE001 —— 非 UTF-8 字节串，忽略
                text = None
            if text and text.isdigit():
                yield text
            if depth >= 4:
                continue
            try:
                nested = field.as_fields()
            except Exception:  # noqa: BLE001 —— 不是嵌套消息，忽略
                nested = []
            if nested:
                yield from _walk_numbers(iter(nested), depth + 1)


def numbers_in(body: bytes, excluded_digits: frozenset[int] = DEFAULT_EXCLUDED_DIGITS) -> set[str]:
    """从一段报文正文里抽出所有可能的整数候选。"""
    found: set[str] = {match.decode() for match in NUMBER_PATTERN.findall(body)}
    try:
        found.update(_walk_numbers(iter(iter_fields(body, stop_at_truncation=True))))
    except Exception:  # noqa: BLE001 —— 非 protobuf 正文（图片/HTML），只用正则结果
        pass
    return {
        item
        for item in found
        if 0 < len(item) <= MAX_DIGITS and len(item) not in excluded_digits
    }


def build_pool(
    archive: Path,
    stop_entry: str | None,
    excluded_digits: frozenset[int] = DEFAULT_EXCLUDED_DIGITS,
) -> tuple[list[Candidate], dict[str, list[str]]]:
    """按报文顺序建候选池，并顺手记下每条报文里的 token。

    :return: ``(候选列表, {报文编号: [token, ...]})``
    """
    pool: list[Candidate] = []
    seen: set[str] = set()
    tokens_by_entry: dict[str, list[str]] = {}

    for session in iter_sessions(archive):
        for body in (body_of(session.request), body_of(session.response)):
            if not body:
                continue
            for value in numbers_in(body, excluded_digits):
                if value in seen:
                    continue
                seen.add(value)
                pool.append(Candidate(value=value, origin=session.name))
        found = {match.decode().lower() for match in TOKEN_PATTERN.findall(session.request + session.response)}
        if found:
            tokens_by_entry[session.name] = sorted(found)
        if stop_entry is not None and session.name.startswith(stop_entry):
            break
    return pool, tokens_by_entry


def solve(token: str, pool: Sequence[Candidate], salt: str = BATTLE_VERIFY_SALT) -> list[Binding]:
    """穷举 ``a + b + winLose + salt`` 找到能算出该 token 的组合。

    【为什么用 hashlib 的 copy 而不是每次拼字符串】
    候选池动辄上千个，两两组合是百万级。``md5(a)`` 只算一次，
    之后 ``copy()`` 再 update(b) —— 比 "拼接字符串 + 重新 md5" 快一个数量级。
    """
    target = token.lower()
    tail = [f"{win}{salt}".encode() for win in WIN_LOSE_CHOICES]
    bindings: list[Binding] = []

    for first in pool:
        base = hashlib.md5(first.value.encode("ascii"))
        for second in pool:
            with_second = base.copy()
            with_second.update(second.value.encode("ascii"))
            for win, suffix in zip(WIN_LOSE_CHOICES, tail):
                candidate = with_second.copy()
                candidate.update(suffix)
                if candidate.hexdigest() == target:
                    bindings.append(
                        Binding(
                            token=target,
                            first=first,
                            second=second,
                            win_lose=win,
                            raw=f"{first.value}{second.value}{win}{salt}",
                        )
                    )
    return bindings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="solve_signer_binding.py",
        description="从抓包反解战斗结算 token 的签名绑定",
    )
    parser.add_argument("--saz", type=Path, required=True, help="抓包文件（.saz）")
    parser.add_argument("--entry", help="只解这条报文（如 raw/26）")
    parser.add_argument("--token", help="已知 token（32 位 hex），只解它")
    parser.add_argument("--salt", default=BATTLE_VERIFY_SALT, help="签名盐（默认取 battle_signer）")
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=1500,
        help="候选池上限（0 = 不限；默认 1500）",
    )
    parser.add_argument(
        "--include-10digit",
        action="store_true",
        help="把 10 位整数（几乎都是时间戳）也放进候选池",
    )
    parser.add_argument("--show-unexplained", action="store_true", help="列出没解出来的 token")
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="不指定 --entry/--token 时最多解多少个 token（默认 20，防止长时间穷举）",
    )
    args = parser.parse_args(argv)

    # 逐行刷新：穷举可能跑几十秒，被中断时也要能看到已经跑到哪一条
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001 —— 老版本/被重定向时忽略
        pass

    if not args.saz.exists():
        print(f"✘ 找不到抓包：{args.saz}")
        return 1

    excluded = frozenset() if args.include_10digit else DEFAULT_EXCLUDED_DIGITS
    pool, tokens_by_entry = build_pool(args.saz, args.entry, excluded)
    if not pool:
        print("✘ 没从这份抓包里解出任何数字候选")
        return 1

    if args.max_candidates and len(pool) > args.max_candidates:
        print(
            f"⚠ 候选 {len(pool)} 个，超过上限 {args.max_candidates}，只取最早出现的部分"
            "（可用 --max-candidates 0 取消限制）"
        )
        pool = pool[: args.max_candidates]

    targets: list[tuple[str, str]] = []
    if args.token:
        targets = [(args.token.lower(), "命令行指定")]
    else:
        for entry, tokens in tokens_by_entry.items():
            # 指定 --entry 时只看那一条报文；否则按顺序扫，用 --limit 兜住耗时
            if args.entry and not entry.startswith(args.entry):
                continue
            for token in tokens:
                targets.append((token, entry))

    if not args.entry and not args.token:
        if len(targets) > args.limit:
            print(f"⚠ 全量扫描命中 {len(targets)} 个 token，只解前 {args.limit} 个（用 --limit 调整）")
            targets = targets[: args.limit]

    print(f"抓包：{args.saz.name}  候选数字：{len(pool)}  待解 token：{len(targets)}", flush=True)
    print("=" * 78, flush=True)

    solved = 0
    unexplained: list[tuple[str, str]] = []
    for index, (token, entry) in enumerate(targets, start=1):
        print(f"[{index}/{len(targets)}] 解 {token}（{entry}）……", end="", flush=True)
        bindings = solve(token, pool, salt=args.salt)
        if not bindings:
            print(" 未解出", flush=True)
            unexplained.append((token, entry))
            continue
        solved += 1
        print(" 命中", flush=True)
        # 同一个拼接串可能有多种切分（"17"+"1" 与 "171"+""），token 本身无法区分 —— 必须并排列出
        by_raw: dict[str, list[Binding]] = {}
        for binding in bindings:
            by_raw.setdefault(binding.raw, []).append(binding)
        for group in list(by_raw.values())[:3]:
            head = group[0]
            print(f"    = {head.describe()}")
            print(f"      {head.first.value:>10}  ← {head.first.origin}")
            print(f"      {head.second.value:>10}  ← {head.second.origin}")
            if len(group) > 1:
                alternatives = "、".join(
                    f'"{item.first.value}"+"{item.second.value}"+"{item.win_lose}"' for item in group[1:]
                )
                print(f"      ⚠ 拼接后字符串完全相同，token 无法区分切分方式：{alternatives}")
                print("         → 必须看报文里哪个值真实存在/字段类型是 string 才能定案")
        if len(by_raw) > 3:
            print(f"    …… 另有 {len(by_raw) - 3} 种不同的拼接方式同样成立")

    print("=" * 78)
    print(f"解出 {solved} / {len(targets)}")
    if unexplained and args.show_unexplained:
        print("未解出（可能是别的签名族、或参与值没出现在抓包里）：")
        for token, entry in unexplained:
            print(f"  - {token}  （报文 {entry}）")

    return 0 if solved == len(targets) else 1


if __name__ == "__main__":
    raise SystemExit(main())
