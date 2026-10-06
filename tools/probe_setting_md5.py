#!/usr/bin/env python3
"""尝试复现客户端算出来的 ``settingMd5``（``RootPacket`` 字段 3）。

【这个脚本要解决什么】
实测发现：``RootPacket`` 字段 3 的值形如::

    2.2.0-el-h-win|77bf18767983db6fb2b17e5e3a891d45
    └─ 客户端版本 ─┘ └────── 32 位 hex（像 MD5）──────┘

它**不在任何响应里**（响应的元字段只有 ``packetID`` / ``PlayerDataBasicClass`` /
``SpecialActivityRefreshClass``），也**不是** ``LoginRes.loginSetting``（那只是活动信息）。
但它会**随时间变化**（14:2x 抓到 ``af484fe5…``，19:5x 抓到 ``77bf1876…``），
所以最大可能是**客户端按本地配置表算出来的指纹**（配置一更新，指纹就变）。

本脚本把"主流可能的算法"都试一遍：只要能对上某个实测值，我们就能自己算，
从而把 `-1` 这个占位值换成真值（实测：战斗类请求缺它会被前端拦成
`{"response_code":"OK"}`）。

【用法】

    python tools/probe_setting_md5.py
    python tools/probe_setting_md5.py --target 77bf18767983db6fb2b17e5e3a891d45

【为什么写成脚本而不是命令行临时拼】
要遍历 405 张表、算十几种配方，多行逻辑写进 ``python -c`` 既难读也容易敲错；
按项目约定：多行逻辑一律落成 ``tools/`` 下的脚本，再用单行命令执行。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Final

# tools/ 不是包，无法直接 import config，只能手动把项目根加进 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (必须在 sys.path 调整之后导入)

#: 实测抓到的两个目标值（都来自同一客户端、不同时间）—— 只要对上任意一个，
#: 就说明配方对了（另一个对不上是因为期间配置更新过）。
TARGETS: Final[tuple[str, ...]] = (
    "77bf18767983db6fb2b17e5e3a891d45",  # captures/荣耀之巅_抓包2.saz raw/16_c
    "af484fe5aea6a7957cd7b33171099722",  # captures/荣耀之巅.saz raw/02_c
)


def manifest_order(asset_dir: Path) -> list[str]:
    """按 ``ExcelList``（配置清单）给出的顺序列出表名。"""
    manifest = (asset_dir / "ExcelList").read_text(encoding="utf-8-sig").strip()
    return [name.strip() for name in manifest.split("|") if name.strip()]


def iter_files(asset_dir: Path, names: Sequence[str]) -> Iterator[tuple[str, bytes]]:
    """按给定顺序产出 ``(表名, 原始字节)``（跳过不存在的）。"""
    for name in names:
        path = asset_dir / name
        if path.is_file():
            yield name, path.read_bytes()


def recipe_join_contents(files: Sequence[tuple[str, bytes]]) -> str:
    """配方 1：把所有内容直接拼接后取 MD5。"""
    hasher = hashlib.md5()
    for _name, data in files:
        hasher.update(data)
    return hasher.hexdigest()


def recipe_name_then_content(files: Sequence[tuple[str, bytes]]) -> str:
    """配方 2：每个文件前先喂入表名（``name+content`` 顺序拼接）。"""
    hasher = hashlib.md5()
    for name, data in files:
        hasher.update(name.encode("utf-8"))
        hasher.update(data)
    return hasher.hexdigest()


def recipe_content_then_name(files: Sequence[tuple[str, bytes]]) -> str:
    """配方 3：``content`` 在前、表名在后。"""
    hasher = hashlib.md5()
    for name, data in files:
        hasher.update(data)
        hasher.update(name.encode("utf-8"))
    return hasher.hexdigest()


def recipe_md5_of_md5s(files: Sequence[tuple[str, bytes]]) -> str:
    """配方 4：先算每张表的 MD5，再把它们的 hex 拼接后取 MD5。"""
    hasher = hashlib.md5()
    for _name, data in files:
        hasher.update(hashlib.md5(data).hexdigest().encode("ascii"))
    return hasher.hexdigest()


def recipe_name_colon_md5(files: Sequence[tuple[str, bytes]]) -> str:
    """配方 5：``name:md5hex\\n`` 逐行拼接后取 MD5（像清单文件）。"""
    hasher = hashlib.md5()
    for name, data in files:
        digest = hashlib.md5(data).hexdigest()
        hasher.update(f"{name}:{digest}\n".encode("utf-8"))
    return hasher.hexdigest()


def recipe_name_pipe_md5(files: Sequence[tuple[str, bytes]]) -> str:
    """配方 6：``name|md5hex\\n``（另一种常见的清单格式）。"""
    hasher = hashlib.md5()
    for name, data in files:
        digest = hashlib.md5(data).hexdigest()
        hasher.update(f"{name}|{digest}\n".encode("utf-8"))
    return hasher.hexdigest()


def recipe_raw_md5_digests(files: Sequence[tuple[str, bytes]]) -> str:
    """配方 7：把每张表的 MD5 **原始字节**（16 字节）拼接后取 MD5。"""
    hasher = hashlib.md5()
    for _name, data in files:
        hasher.update(hashlib.md5(data).digest())
    return hasher.hexdigest()


#: 配方名 → 函数（顺序即输出顺序）
RECIPES: Final[dict[str, Callable[[Sequence[tuple[str, bytes]]], str]]] = {
    "内容直接拼接": recipe_join_contents,
    "表名+内容": recipe_name_then_content,
    "内容+表名": recipe_content_then_name,
    "md5(各表 md5 hex)": recipe_md5_of_md5s,
    "name:md5\\n": recipe_name_colon_md5,
    "name|md5\\n": recipe_name_pipe_md5,
    "md5(各表 md5 raw)": recipe_raw_md5_digests,
}


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="probe_setting_md5.py",
        description="尝试复现客户端 settingMd5（RootPacket 字段 3）的算法",
    )
    parser.add_argument("--target", default="", help="只比对指定目标值（默认比对两个实测值）")
    parser.add_argument("--limit", type=int, default=8, help="最多显示几种配方结果")
    args = parser.parse_args(argv)

    asset_dir = config.TEXT_ASSET_DIR
    names = manifest_order(asset_dir)
    files = list(iter_files(asset_dir, names))
    total = sum(len(data) for _n, data in files)
    print(f"配置表目录：{asset_dir}")
    print(f"清单表数：{len(names)}（实际读到 {len(files)} 张，共 {total / 1024 / 1024:.1f} MB）")

    targets = (args.target,) if args.target else TARGETS
    print(f"目标值：{', '.join(targets)}")
    print()

    hits = 0
    for index, (label, func) in enumerate(RECIPES.items()):
        if index >= args.limit:
            break
        digest = func(files)
        mark = "  ✅ 命中！" if digest in targets else ""
        if mark:
            hits += 1
        print(f"  {label:<18} {digest}{mark}")

    print()
    if hits:
        print("✔ 有配方命中 —— 可以照它在 Python 侧算出 settingMd5（需确认顺序与是否含 ExcelList）")
    else:
        print(
            "✘ 这些配方都没命中。下一步可试：\n"
            "   · 只对特定子集（如启动时加载的表）取哈希；\n"
            "   · 先对每张表做 Base64/MD5 的另一种组合；\n"
            "   · 或反汇编客户端的 settingMd5 计算函数（dump.cs 无方法体，需要 disasm）。"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
