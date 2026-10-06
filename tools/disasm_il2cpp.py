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

    # 或者直接给偏移
    python tools/disasm_il2cpp.py --offset 0xD8DAF0 --length 4096

    # 只看调用目标（不看每条指令，输出短很多）
    python tools/disasm_il2cpp.py --method GetRootPacketVCode --calls-only
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


def describe_target(address: int, table: SymbolTable) -> str:
    """把地址翻译成"VA + 方法名"。"""
    site = table.resolve(address)
    if site is None:
        return f"0x{address:X}  <未收录>"
    return f"0x{address:X}  {site.signature}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="disasm_il2cpp.py", description="反汇编 IL2CPP 方法并翻译调用目标"
    )
    parser.add_argument("--method", help="方法名（在 dump.cs 里查它的偏移）")
    parser.add_argument("--offset", help="直接给文件偏移，如 0xD8DAF0")
    parser.add_argument("--length", type=int, default=3072, help="反汇编多少字节")
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    parser.add_argument("--calls-only", action="store_true", help="只打印调用/取址目标")
    args = parser.parse_args(argv)

    table = SymbolTable.load()

    site: MethodSite | None = None
    if args.method:
        found = [item for item in table.search(args.method) if args.method in item.name]
        if not found:
            print(f"✘ dump.cs 里找不到方法 {args.method!r}")
            return 1
        site = found[0]
        print(f"方法：{site.signature}")
        print(f"VA 0x{site.va:X} | RVA 0x{site.rva:X} | 文件偏移 0x{site.offset:X}")
        offset = site.offset
        base_va = site.va
    else:
        offset = int(args.offset, 16)
        found = table.resolve(offset)
        base_va = found.va if found else 0x180000000 + offset
        print(f"文件偏移 0x{offset:X}（反查：{found.signature if found else '未收录'}）")

    print(f"反汇编 {args.length} 字节，基址 VA 0x{base_va:X}")
    print("=" * 76)

    with args.dll.open("rb") as handle:
        handle.seek(offset)
        code = handle.read(args.length)

    disassembler = Cs(CS_ARCH_X86, CS_MODE_64)
    calls: list[tuple[int, int, str]] = []
    for instruction in disassembler.disasm(code, base_va):
        text = f"{instruction.mnemonic} {instruction.op_str}".strip()
        if args.calls_only and instruction.mnemonic not in ("call", "lea", "jmp"):
            continue
        print(f"  0x{instruction.address:X}  {text}")
        if instruction.mnemonic in ("call", "jmp"):
            match = re.fullmatch(r"0x([0-9a-fA-F]+)", instruction.op_str)
            if match:
                calls.append((instruction.address, int(match.group(1), 16), text))
        rip = RIP_OPERAND.search(instruction.op_str)
        if rip:
            target = instruction.address + instruction.size + int(rip.group(1), 16)
            calls.append((instruction.address, target, text))

    print("=" * 76)
    print("调用 / 取址目标翻译：")
    for address, target, text in calls:
        print(f"  0x{address:X}  {text}")
        print(f"      → {describe_target(target, table)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
