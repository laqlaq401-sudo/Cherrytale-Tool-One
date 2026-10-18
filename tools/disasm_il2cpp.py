#!/usr/bin/env python3
"""反汇编 IL2CPP 里的指定方法，并把调用目标翻译成方法名。

【它解决什么问题】
``dump.cs`` 只给方法签名 —— IL2CPP 的**方法体在 GameAssembly.dll 的机器码里**。
本工具按 ``dump.cs`` 里的 **文件偏移**（``Offset: 0x…``）直接 seek 过去读那段字节，
用 capstone 反汇编，再把每条 ``call`` / ``[rip + …]`` 的目标地址交给
:mod:`tools.il2cpp_symtab` 反查名字 ——
于是"一堆看不懂的地址"就变成"它在调用 MD5 还是 DateTime"。

【用法】
    # 按方法名（内部会去 dump.cs 里查它的文件偏移）
    python tools/disasm_il2cpp.py --method GetRootPacketVCode
    python tools/disasm_il2cpp.py --method GetRootPacketVCode --length 4096

    # 同名重载有多个时，工具会拒绝猜，并列出全部候选 —— 用 --va 指定
    python tools/disasm_il2cpp.py --va 0x181019780

    # 或者直接给文件偏移（必须显式说明这是偏移）
    python tools/disasm_il2cpp.py --offset 0xD8DAF0 --length 4096

    # 只看调用目标（不看每条指令，输出短很多）
    python tools/disasm_il2cpp.py --method GetRootPacketVCode --calls-only

    # 看每个 call 之前 rcx/rdx/r8/r9 大概被塞了什么（参数绑定用，见下）
    python tools/disasm_il2cpp.py --va 0x181019780 --args

【为什么 --method 不再"取第一个命中"】
``dump.cs`` 里同名方法极多（实测 ``GetFightVerifyData`` 有 5 个实现：三个实例版
+ 一个静态版 + ``_YImo`` 版）。过去 ``--method`` 直接取搜索结果的第一条，
于是想反汇编 ``0x181019780`` 却会拿到 ``0x1805AAAA0``（另一个类的私有实例方法），
而且**不会有任何提示**。现在：精确同名有多个 → 列出全部并退出；
没有精确同名 → 用子串搜索，但命中多个同样拒绝猜。

【--args 的边界】
参数回溯是**线性扫描 + 寄存器最后一次赋值**的近似：只在同一段直线代码里可信，
遇到跳转/分支不跟随。它的用途是"读 call site 时少翻 30 行"，不是数据流分析。
调用（``call``）会破坏 caller-saved 寄存器，所以每次 call 后清空记录。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capstone import CS_ARCH_X86, CS_MODE_64, Cs  # noqa: E402

from tools.il2cpp_symtab import MethodSite, SymbolTable  # noqa: E402

DEFAULT_DLL = Path(__file__).resolve().parent.parent / "Cherrytale IL2CPP/GameAssembly.dll"

#: ``[rip + 0x1234]`` 形式的操作数
RIP_OPERAND = re.compile(r"\[rip \+ (0x[0-9a-fA-F]+)\]")

#: 属于"库函数"的调用目标（反查不到名字时提示可能是运行时内部函数）
IGNORED_PREFIXES = ("il2cpp_", "___")

#: 参与传参的寄存器（Win64 调用约定：rcx / rdx / r8 / r9）
ARG_REGS: tuple[str, ...] = ("rcx", "rdx", "r8", "r9")

#: 32/16/8 位寄存器名 → 64 位父寄存器
REG_ALIASES: dict[str, str] = {
    "rax": "rax", "eax": "rax", "ax": "rax", "al": "rax",
    "rcx": "rcx", "ecx": "rcx", "cx": "rcx", "cl": "rcx",
    "rdx": "rdx", "edx": "rdx", "dx": "rdx", "dl": "rdx",
    "r8": "r8", "r8d": "r8", "r8w": "r8", "r8b": "r8",
    "r9": "r9", "r9d": "r9", "r9w": "r9", "r9b": "r9",
}

#: 会写目标寄存器的指令（够用即可，不求完整）
WRITE_MNEMONICS = frozenset(
    {"mov", "movzx", "movsx", "movsxd", "lea", "xor", "add", "sub", "imul", "or", "and", "shl", "shr"}
)

#: 估算函数体长度时的上下限
MIN_AUTO_LENGTH = 64
MAX_AUTO_LENGTH = 64 * 1024


def describe_target(address: int, table: SymbolTable) -> str:
    """把地址翻译成"VA + 方法名"。"""
    site = table.resolve(address, kind="va")
    if site is None:
        return f"0x{address:X}  <未收录>"
    return f"0x{address:X}  {site.signature}"


def pick_site(table: SymbolTable, name: str) -> tuple[MethodSite | None, str | None]:
    """按方法名挑一个**唯一**的方法（兼容入口）。

    真正的选择逻辑在 :meth:`SymbolTable.pick` —— 放在符号表里是为了不依赖
    capstone 也能测（本模块 import 时就要求装好 capstone）。
    """
    return table.pick(name)


def auto_length(table: SymbolTable, site: MethodSite) -> int:
    """用"下一个方法的偏移"估算函数体长度上界。"""
    nxt = table.next_site(site)
    if nxt is None or nxt.offset <= site.offset:
        return MAX_AUTO_LENGTH
    return max(MIN_AUTO_LENGTH, min(nxt.offset - site.offset, MAX_AUTO_LENGTH))


def _arg_registers(instruction) -> tuple[str | None, str]:
    """解析一条指令是否写入了传参寄存器。

    :return: ``(寄存器, 原文)``；不相关时寄存器为 ``None``。
    """
    if instruction.mnemonic not in WRITE_MNEMONICS:
        return None, ""
    parts = instruction.op_str.split(",", 1)
    if not parts:
        return None, ""
    dst = parts[0].strip()
    reg = REG_ALIASES.get(dst)
    if reg not in ARG_REGS:
        return None, ""
    text = f"{instruction.mnemonic} {instruction.op_str}".strip()
    # xor ecx, ecx / sub rcx, rcx → 常量 0
    if len(parts) == 2 and instruction.mnemonic in ("xor", "sub"):
        if REG_ALIASES.get(parts[1].strip()) == reg:
            return reg, "0"
    return reg, text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="disasm_il2cpp.py", description="反汇编 IL2CPP 方法并翻译调用目标"
    )
    parser.add_argument("--method", help="方法名（在 dump.cs 里查它的偏移）")
    parser.add_argument("--va", help="直接给 VA（如 0x181019780），用于同名重载消歧")
    parser.add_argument("--offset", help="直接给文件偏移，如 0xD8DAF0")
    parser.add_argument("--length", type=int, default=3072, help="反汇编多少字节")
    parser.add_argument(
        "--auto-length",
        action="store_true",
        help="用「下一个方法的偏移」估算长度上界（避免固定 3KB 截断）",
    )
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    parser.add_argument("--calls-only", action="store_true", help="只打印调用/取址目标")
    parser.add_argument(
        "--args",
        action="store_true",
        help="在每个 call 处标注 rcx/rdx/r8/r9 的最后一次赋值（线性近似）",
    )
    args = parser.parse_args(argv)

    table = SymbolTable.load()

    site: MethodSite | None = None
    if args.va:
        va = int(args.va, 16)
        site = table.resolve(va, kind="va")
        if site is None:
            print(f"✘ VA 0x{va:X} 不在 dump.cs 的方法索引里")
            return 1
        print(f"方法：{site.signature}")
        print(f"VA 0x{site.va:X} | RVA 0x{site.rva:X} | 文件偏移 0x{site.offset:X}")
        offset = site.offset
        base_va = site.va
    elif args.method:
        site, error = pick_site(table, args.method)
        if error is not None:
            print(error)
            return 2
        assert site is not None
        print(f"方法：{site.signature}")
        print(f"VA 0x{site.va:X} | RVA 0x{site.rva:X} | 文件偏移 0x{site.offset:X}")
        offset = site.offset
        base_va = site.va
    elif args.offset:
        offset = int(args.offset, 16)
        found = table.resolve(offset, kind="offset")
        base_va = found.va if found else 0x180000000 + offset
        print(f"文件偏移 0x{offset:X}（反查：{found.signature if found else '未收录'}）")
    else:
        parser.error("需要 --method / --va / --offset 之一")
        return 2

    length = args.length
    if args.auto_length and site is not None:
        length = auto_length(table, site)
        print(f"长度自动估算：{length} 字节（下一个方法起于 offset 0x{site.offset + length:X}）")

    print(f"反汇编 {length} 字节，基址 VA 0x{base_va:X}")
    print("=" * 76)

    with args.dll.open("rb") as handle:
        handle.seek(offset)
        code = handle.read(length)

    disassembler = Cs(CS_ARCH_X86, CS_MODE_64)
    calls: list[tuple[int, int, str]] = []
    pending_args: dict[str, str] = {}

    for instruction in disassembler.disasm(code, base_va):
        text = f"{instruction.mnemonic} {instruction.op_str}".strip()

        reg, written = _arg_registers(instruction)
        if reg is not None:
            pending_args[reg] = written

        if instruction.mnemonic == "call" and args.args and pending_args:
            hint = "  ".join(f"{key}={pending_args[key]}" for key in ARG_REGS if key in pending_args)
            text = f"{text}    ; {hint}"

        if args.calls_only and instruction.mnemonic not in ("call", "lea", "jmp"):
            if reg is not None:
                continue
        else:
            print(f"  0x{instruction.address:X}  {text}")

        if instruction.mnemonic in ("call", "jmp"):
            match = re.fullmatch(r"0x([0-9a-fA-F]+)", instruction.op_str)
            if match:
                calls.append((instruction.address, int(match.group(1), 16), text))
        rip = RIP_OPERAND.search(instruction.op_str)
        if rip:
            target = instruction.address + instruction.size + int(rip.group(1), 16)
            calls.append((instruction.address, target, text))

        # call 会破坏 caller-saved 寄存器，记录到此为止
        if instruction.mnemonic == "call":
            pending_args.clear()

    print("=" * 76)
    print("调用 / 取址目标翻译：")
    for address, target, text in calls:
        print(f"  0x{address:X}  {text}")
        print(f"      → {describe_target(target, table)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
