"""签名工具链的回归测试：符号表消歧 + 抓包反解绑定。

这里守的是两类**静默错误**：

1. ``disasm_il2cpp.py --method`` 过去"取第一个同名命中"。``dump.cs`` 里
   ``GetFightVerifyData`` 有 4 个同名实现，取第一个会拿到别的类的私有实例方法，
   而输出看起来完全正常 —— 这种错误只能靠测试钉住。
2. ``SymbolTable.resolve()`` 过去按 ``va → rva → offset`` 试探，文件偏移与
   另一个方法的 RVA 撞号时会返回**错误的方法名**（实测 7611 / 159098 ≈ 4.8%）。
3. ``solve_signer_binding.py`` 能不能从抓包里**独立地**解出已知的绑定 ——
   这是"公式正确"之外的第二重证据：证明绑定值确实来自报文，而不是人脑补的。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from tools.il2cpp_symtab import SymbolTable
from tools.solve_signer_binding import build_pool, solve

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
CAPTURE_DIR: Final[Path] = PROJECT_ROOT / "captures/old/1.0.0NEED"


@pytest.fixture(scope="session")
def table() -> SymbolTable:
    """整份 dump.cs 只解析一次（34MB，几秒）。"""
    return SymbolTable.load()


class TestSymbolDisambiguation:
    """方法名解析必须"要么唯一，要么拒绝猜"。"""

    def test_refuses_ambiguous_overloads(self, table: SymbolTable) -> None:
        """同名重载有 4 个 → 必须报错并列出全部候选，不能取第一个。"""
        site, error = table.pick( "GetFightVerifyData")
        assert site is None
        assert error is not None
        assert "拒绝猜" in error
        # 真正要用的静态版必须出现在候选里，否则用户无从选择
        assert "0x181019780" in error

    def test_resolves_unique_name(self, table: SymbolTable) -> None:
        """唯一同名 → 正常返回，且 VA 与 dump.cs 注释一致。"""
        site, error = table.pick( "GetRootPacketVCode")
        assert error is None
        assert site is not None
        assert site.va == 0x180D8F0F0
        assert site.name == "GetRootPacketVCode"

    def test_missing_name_reports(self, table: SymbolTable) -> None:
        """找不到时必须给明确报错，而不是静默返回 None 继续跑。"""
        site, error = table.pick( "ThisMethodDoesNotExistAnywhere")
        assert site is None
        assert error is not None
        assert "找不到" in error


class TestAddressKinds:
    """地址口径必须能显式指定（offset 与 rva 会撞号）。"""

    #: 实测撞号地址：作为文件偏移与作为 RVA 命中两个不同方法
    COLLIDING: Final[int] = 0x408000

    def test_kind_disambiguates(self, table: SymbolTable) -> None:
        as_offset = table.resolve(self.COLLIDING, kind="offset")
        as_rva = table.resolve(self.COLLIDING, kind="rva")
        assert as_offset is not None and as_rva is not None
        assert as_offset is not as_rva
        assert "set_HamiRewardPacketRes" in as_offset.signature
        assert "set_BuyVipGiftPacket" in as_rva.signature

    def test_ambiguities_lists_both(self, table: SymbolTable) -> None:
        """撞号诊断要能把两个候选都列出来，供人判断。"""
        clashes = table.ambiguities(self.COLLIDING)
        assert len(clashes) == 2

    def test_next_site_is_forward(self, table: SymbolTable) -> None:
        """估算函数长度的依据：下一个方法的偏移必须更大。"""
        site = table.find_by_name("GetRootPacketVCode")[0]
        nxt = table.next_site(site)
        assert nxt is not None
        assert nxt.offset > site.offset


@pytest.mark.parametrize(
    ("capture", "token", "first", "second", "win_lose"),
    [
        ("失控炼成阵.saz", "0a3396051faf186762d81815977630bf", "29744", "53247", ""),
        ("竞技场抓包.saz", "1b829bfbec08fe639d11c01644a2604d", "29324", "6175214", "0"),
        ("主线通关抓包.saz", "0ef8ecfcd972a606bcf04e91efe86bad", "120483", "17", "1"),
    ],
)
def test_solver_recovers_known_binding(
    capture: str,
    token: str,
    first: str,
    second: str,
    win_lose: str,
) -> None:
    """从抓包独立解出三条已知绑定（含 battle_signer 里的 golden vector）。"""
    archive = CAPTURE_DIR / capture
    if not archive.exists():
        pytest.skip(f"抓包不存在：{archive}")

    pool, _ = build_pool(archive, stop_entry=None)
    assert pool, "候选池为空，说明报文解析退化"

    bindings = solve(token, pool)
    assert bindings, f"{capture} 里解不出 {token}，公式或候选池可能已退化"
    assert any(
        item.first.value == first and item.second.value == second and item.win_lose == win_lose
        for item in bindings
    ), f"期望的绑定 ({first}, {second}, {win_lose!r}) 不在解集里：{bindings[:3]}"


def test_solver_flags_concatenation_ambiguity() -> None:
    """主线向量存在 "17"+"1" 与 "171"+"" 两种切分 —— 工具必须把两种都摆出来。

    这条测试记录一个**固有局限**：token 只对拼接后的字符串做 MD5，
    所以纯靠 token 无法区分切分方式，必须回到报文里看字段类型。
    """
    archive = CAPTURE_DIR / "主线通关抓包.saz"
    if not archive.exists():
        pytest.skip(f"抓包不存在：{archive}")

    pool, _ = build_pool(archive, stop_entry=None)
    bindings = solve("0ef8ecfcd972a606bcf04e91efe86bad", pool)
    raws = {item.raw for item in bindings}
    # 同一个拼接串、不同切分 → 只应产生一个 raw，但多个 binding
    assert len(raws) == 1
    assert len(bindings) > 1
