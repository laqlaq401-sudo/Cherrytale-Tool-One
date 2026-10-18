#!/usr/bin/env python3
"""从 ``dump.cs`` 提取「RVA → 方法名」索引 —— 自制精简版 ``script.json``。

【为什么需要它】
反汇编 ``GameAssembly.dll`` 时看到的是 ``call 0x1800D8C3E0`` 这样的地址，
光看地址并不知道调用了什么。而 ``dump.cs`` 里每个方法上方都写着::

    // RVA: 0xD8F0F0 Offset: 0xD8DAF0 VA: 0x180D8F0F0
    public void GetRootPacketVCode(RootPacket iRootPacket) { }

把这类注释与方法签名配起来，就有了「地址 → 方法名」的字典 ——
反汇编遇到 ``call`` / ``lea`` 时就能立刻反查出名字，
从而判断某段代码是在算哈希、拼字符串，还是在读时间。

【用法】
    python tools/il2cpp_symtab.py                            # 统计信息
    python tools/il2cpp_symtab.py --grep GetRootPacketVCode  # 按名字找地址
    python tools/il2cpp_symtab.py --lookup 0x1800D8F0F0     # 按地址找名字

作为模块使用::

    from tools.il2cpp_symtab import SymbolTable
    table = SymbolTable.load()
    print(table.resolve(0x1800D8F0F0))
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: ``dump.cs`` 里方法上方的地址注释
ADDRESS_COMMENT = re.compile(
    r"//\s*RVA:\s*0x(?P<rva>[0-9A-Fa-f]+)"
    r"\s*Offset:\s*0x(?P<offset>[0-9A-Fa-f]+)"
    r"\s*VA:\s*0x(?P<va>[0-9A-Fa-f]+)"
)

#: 默认的 dump.cs 位置
DEFAULT_DUMP = Path(__file__).resolve().parent.parent / "Cherrytale IL2CPP/dump.cs"

#: ``IL2CPP`` 生成的模块基址（VA = RVA + 0x180000000）
MODULE_BASE = 0x180000000


@dataclass(frozen=True)
class MethodSite:
    """一个方法的位置信息。"""

    #: 映像内地址（相对基址）
    rva: int
    #: **文件偏移** —— 用它直接 seek 到 DLL 里读机器码，不必解析 PE
    offset: int
    #: 虚拟地址（RVA + 基址），反汇编里出现的地址就是它
    va: int
    #: 方法签名
    signature: str

    @property
    def name(self) -> str:
        """从签名里抽出方法名（够用即可，不追求完整还原）。"""
        head = self.signature.split("(")[0].strip()
        head = head.rstrip("{").strip()
        return head.split()[-1] if head.split() else head


class SymbolTable:
    """``地址 → 方法`` 的索引。"""

    def __init__(self, sites: list[MethodSite]) -> None:
        self.sites = sites
        self.by_va = {site.va: site for site in sites}
        self.by_rva = {site.rva: site for site in sites}
        self.by_offset = {site.offset: site for site in sites}

    @classmethod
    def load(cls, dump_path: Path | None = None) -> SymbolTable:
        """解析 ``dump.cs``，建立索引（34MB 文件约几秒）。"""
        path = dump_path or DEFAULT_DUMP
        sites: list[MethodSite] = []
        pending: dict[str, int] | None = None

        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = ADDRESS_COMMENT.search(line)
                if match:
                    pending = {
                        "rva": int(match.group("rva"), 16),
                        "offset": int(match.group("offset"), 16),
                        "va": int(match.group("va"), 16),
                    }
                    continue
                if pending is not None:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("//"):
                        sites.append(
                            MethodSite(
                                rva=pending["rva"],
                                offset=pending["offset"],
                                va=pending["va"],
                                signature=stripped.rstrip("{").strip(),
                            )
                        )
                    pending = None
        return cls(sites)

    def resolve(self, address: int, kind: str | None = None) -> MethodSite | None:
        """按地址查方法。

        :param kind: 明确指定地址口径 —— ``"va"`` / ``"rva"`` / ``"offset"``。
            **调用方应当尽量指定**：``offset`` 与 ``rva`` 在本表里会撞号
            （实测 7611 / 159098 ≈ 4.8%），不指定时只能靠猜测。
        :param address: 地址值。

        【为什么要 kind】
        过去按 ``va → rva → offset`` 顺序试探，一旦某个**文件偏移**恰好等于
        另一个方法的 **RVA**，就会静默返回**错误的方法名**（例如传 ``0x408000``
        会得到 ``set_BuyVipGiftPacket``，而它其实是 ``set_HamiRewardPacketRes``
        的文件偏移）。这类错误不会报错，只会让后续判断建立在错的方法上。
        """
        if kind == "va":
            return self.by_va.get(address) or self.by_va.get(address + MODULE_BASE)
        if kind == "rva":
            return self.by_rva.get(address)
        if kind == "offset":
            return self.by_offset.get(address)

        if address in self.by_va:
            return self.by_va[address]
        if address in self.by_rva:
            return self.by_rva[address]
        if address in self.by_offset:
            return self.by_offset[address]
        # 允许传 RVA 而索引里按 VA 存（反之亦然）
        if address < MODULE_BASE and (address + MODULE_BASE) in self.by_va:
            return self.by_va[address + MODULE_BASE]
        if address >= MODULE_BASE and (address - MODULE_BASE) in self.by_rva:
            return self.by_rva[address - MODULE_BASE]
        return None

    def ambiguities(self, address: int) -> list[MethodSite]:
        """列出这个数值在不同口径下命中的**不同**方法（撞号诊断用）。"""
        found: list[MethodSite] = []
        for site in (
            self.by_va.get(address),
            self.by_rva.get(address),
            self.by_offset.get(address),
        ):
            if site is not None and site not in found:
                found.append(site)
        return found

    def find_by_name(self, name: str) -> list[MethodSite]:
        """按**方法名精确匹配**（区分重载：同名多个会全部返回）。"""
        lowered = name.lower()
        return [site for site in self.sites if site.name.lower() == lowered]

    def search(self, keyword: str) -> list[MethodSite]:
        """按名字（不区分大小写）子串搜索。"""
        lowered = keyword.lower()
        return [site for site in self.sites if lowered in site.signature.lower()]

    def next_site(self, site: MethodSite) -> MethodSite | None:
        """按**文件偏移**找紧邻的下一个方法（用于估算函数体长度上界）。"""
        later = [item for item in self.sites if item.offset > site.offset]
        return min(later, key=lambda item: item.offset) if later else None

    def pick(self, name: str) -> tuple[MethodSite | None, str | None]:
        """按方法名挑一个**唯一**的方法。

        :return: ``(site, None)`` 成功；``(None, 说明)`` 失败（重载歧义或找不到）。

        【为什么不"取第一个"】
        同名重载在 ``dump.cs`` 里遍地都是（``GetFightVerifyData`` 就有 4 个实现）。
        静默取第一个会让整次分析建立在错误的方法上，而输出看起来完全正常 ——
        这类错误最难发现，所以这里宁可报错也不猜。
        """
        exact = self.find_by_name(name)
        if len(exact) == 1:
            return exact[0], None
        if len(exact) > 1:
            lines = [f"✘ 方法名 {name!r} 有 {len(exact)} 个同名实现，拒绝猜："]
            for site in exact:
                lines.append(f"    VA 0x{site.va:X} (offset 0x{site.offset:X})  {site.signature}")
            lines.append("  请用 --va 0x<上面的 VA> 指定")
            return None, "\n".join(lines)

        fuzzy = self.search(name)
        if not fuzzy:
            return None, f"✘ dump.cs 里找不到方法 {name!r}"
        if len(fuzzy) > 1:
            lines = [f"✘ {name!r} 没有精确同名，但有 {len(fuzzy)} 个子串命中，拒绝猜："]
            for site in fuzzy[:20]:
                lines.append(f"    VA 0x{site.va:X} (offset 0x{site.offset:X})  {site.signature}")
            if len(fuzzy) > 20:
                lines.append(f"    …… 另有 {len(fuzzy) - 20} 个")
            lines.append("  请用更精确的名字，或用 --va 指定")
            return None, "\n".join(lines)
        return fuzzy[0], None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="il2cpp_symtab.py", description="从 dump.cs 提取地址→方法名索引"
    )
    parser.add_argument("--dump", type=Path, help="dump.cs 路径")
    parser.add_argument("--grep", metavar="关键词", help="按名字搜索（子串）")
    parser.add_argument("--exact", metavar="方法名", help="按方法名精确搜索（含重载）")
    parser.add_argument("--lookup", nargs="+", metavar="地址", help="按地址查询")
    parser.add_argument(
        "--kind",
        choices=("va", "rva", "offset"),
        help="配合 --lookup 指定地址口径（强烈建议指定，避免撞号误判）",
    )
    args = parser.parse_args(argv)

    table = SymbolTable.load(args.dump)
    print(f"索引完成：{len(table.sites)} 个方法")

    if args.grep:
        for site in table.search(args.grep):
            print(f"  VA 0x{site.va:X} (offset 0x{site.offset:X})  {site.signature}")

    if args.exact:
        hits = table.find_by_name(args.exact)
        print(f"精确匹配 {args.exact!r}：{len(hits)} 个")
        for site in hits:
            print(f"  VA 0x{site.va:X} (offset 0x{site.offset:X})  {site.signature}")

    if args.lookup:
        for raw in args.lookup:
            address = int(raw, 16)
            site = table.resolve(address, kind=args.kind)
            if site is None:
                print(f"  0x{address:X} → 未收录")
                continue
            print(f"  0x{address:X} → VA 0x{site.va:X} offset 0x{site.offset:X}")
            print(f"      {site.signature}")
            clashes = table.ambiguities(address)
            if len(clashes) > 1:
                print(f"      ⚠ 该数值在多个口径下命中 {len(clashes)} 个方法（请用 --kind 消歧）：")
                for item in clashes:
                    print(f"         VA 0x{item.va:X}  {item.signature[:70]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
