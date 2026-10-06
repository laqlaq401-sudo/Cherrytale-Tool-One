#!/usr/bin/env python3
"""角色头像全量提取：热更 CDN → 解伪装 → 导出 PNG（一次性工具，2026-10-05）。

【它补齐的是映射链的最后一环】
登录 1004 抓到的头像编号（``PlayerClass.icon``）是**头像道具的 itemID**：
    ItemData[itemID].iconAssetID（如 "a001_03h_Icon"）
      → 去掉 ``_Icon`` 后缀 = 文件名（如 "a001_03h"）
      → ``web/assets/avatars/{文件名}.png``（运行时纯文件名匹配，见
        ``services/avatar_service.py``）
图片本体从热更 CDN 下载（``https://pfdd.88kongque.com/patch/files/{name}.{md5}``），
解伪装（``tmp/download.py`` 逆向：假版本号 2018.3.5f1 → 2020.3.41f1 等长覆盖 +
0x3FB/0xD99/0x197C 尾部镜像换位）后用 UnityPy 导出 Texture2D。

【目录分流（用户拍板 2026-10-05）】
    * runtime 集：``img_hero_{code}h_icon`` 主头像（ItemData 头像道具引用的）
      → ``web/assets/avatars/{code}.png``，随包分发、运行时直接命中；
    * 冷存集：``h_s_icon`` 小图 + 旧版非 h 图标 → ``captures/avatar_cold_storage/``
      （captures/ 本就不入库；万一哪天要小图/旧图，挪过来即可）。
两处都不进 git（``web/assets/avatars/`` 已 gitignore）。

【增量】runtime 集由 ``web/assets/avatars/manifest.json`` 记 code→md5，
md5 未变且 PNG 在就直接跳过；冷存集按"文件已存在"跳过。游戏更新后换新 index
重跑本脚本，只补变化的部分。

【依赖与运行环境】UnityPy 全家 —— 仅本脚本（离线）使用，不进运行时/APK。
提取环境 ``tmp/venv_extract``（Python 3.11 + UnityPy 1.25.4 + pyfmodex +
archspec + 手工拷入的 tpk_ar 源码；轮子缓存 tmp/wheels）。运行方式::

    tmp/venv_extract/Scripts/python.exe tools/extract_avatar_icons.py --all
    tmp/venv_extract/Scripts/python.exe tools/extract_avatar_icons.py --probe
    tmp/venv_extract/Scripts/python.exe tools/extract_avatar_icons.py --codes a001_03h
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from models.config_table import read_table  # noqa: E402

INDEX_PATH = ROOT_DIR / "tmp" / "index_save.txt"
WORK_DIR = ROOT_DIR / "tmp" / "avatar_bundles"  # 解密后的 bundle 缓存
OUT_DIR = ROOT_DIR / "web" / "assets" / "avatars"  # runtime 静态目录（gitignore）
COLD_DIR = ROOT_DIR / "captures" / "avatar_cold_storage"  # 冷存集（gitignore）
MANIFEST_PATH = OUT_DIR / "manifest.json"

# 与 tmp/download.py（逆向自 DecryptUnityAsset.cs）一致
SERVER_URL = "https://pfdd.88kongque.com/patch/files/"
VERSION_FAKE = bytes([0x32, 0x30, 0x31, 0x38, 0x2E, 0x33, 0x2E, 0x35, 0x66, 0x31, 0x00])
VERSION_REAL = bytes([0x32, 0x30, 0x32, 0x30, 0x2E, 0x33, 0x2E, 0x34, 0x31, 0x66, 0x31])
V7_INDICES = [0x3FB, 0xD99, 0x197C]
UA = {"User-Agent": "UnityPlayer/2020.3.41f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)"}
DOWNLOAD_WORKERS = 8

# index 里的 bundle 名形如
#   assets_resources_{honly|versionshare}_…_img_hero_{code}_icon
# code 是 img_hero_ 与 _icon 之间的一整段（现役带 h 尾：a001_03h；小图再带 _s：
# a001_03h_s；旧版/默认无 h：a000_01、001）—— 它同时是表里 iconAssetID 去掉
# ``_Icon`` 后的名字，所以**直接拿它当导出文件名**，运行时纯文件名命中。
_BUNDLE_RX = re.compile(r"img_hero_(?P<code>[a-z0-9_]+?)_icon$")


# ---------------------------------------------------------------------------
# 下载与解伪装
# ---------------------------------------------------------------------------
def decrypt(data: bytes) -> bytes:
    """脱壳：UnityFS 头的假版本号替换 + 固定偏移换位（照抄 tmp/download.py）。"""
    if not data.startswith(b"UnityFS"):
        return data
    buf = bytearray(data)
    size = len(buf)
    for idx in V7_INDICES:
        if size > idx:
            buf[idx], buf[size - idx] = buf[size - idx], buf[idx]
    head_end = min(0x1000, size)
    head = bytearray(buf[:head_end])
    old_len, new_len = len(VERSION_FAKE), len(VERSION_REAL)
    for i in range(len(head) - old_len + 1):
        if head[i : i + old_len] == VERSION_FAKE:
            head[i : i + new_len] = VERSION_REAL
    buf[:head_end] = head
    return bytes(buf)


def load_index() -> dict[str, tuple[int, str]]:
    data = json.loads(INDEX_PATH.read_text(encoding="utf-8-sig"))
    return {
        name: (val[0], val[1])
        for name, val in data.items()
        if isinstance(val, list) and len(val) >= 2
    }


def download_bundle(name: str, md5: str) -> bytes | None:
    """按 {name}.{md5} 直取并解伪装；结果缓存到 WORK_DIR。失败返回 None。"""
    target = WORK_DIR / f"{name}.{md5}"
    if target.is_file() and target.stat().st_size > 0:
        return target.read_bytes()
    url = f"{SERVER_URL}{name}.{md5}"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
        if not raw:
            print(f"  ✗ 空响应 …{name[-45:]}")
            return None
        data = decrypt(raw)
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return data
    except OSError as exc:
        print(f"  ✗ {type(exc).__name__}: {str(exc)[:120]} …{name[-45:]}")
        return None


# ---------------------------------------------------------------------------
# index 分组：runtime（主头像）/ 冷存（小图 + 旧版）
# ---------------------------------------------------------------------------
def classify_index(index: dict[str, tuple[int, str]]) -> tuple[dict[str, str], dict[str, str]]:
    """把 index 里的 img_hero_*_icon 三套 bundle 分成 runtime / 冷存两组。

    :return: ``(runtime: code→bundle_name, cold: code→bundle_name)``。
        code = bundle 名里 img_hero_ 与 _icon 之间的一整段（如 a001_03h /
        a001_03h_s / a000_01），即表里 iconAssetID 去掉 ``_Icon`` 后的文件名。
    """
    runtime: dict[str, str] = {}
    cold: dict[str, str] = {}
    for name in index:
        m = _BUNDLE_RX.search(name)
        if not m:
            continue
        code = m.group("code")
        # 现役主头像 = code 以 h 结尾（且不是 _s 小图）；h_s 小图与旧版进冷存
        if code.endswith("h") and not code.endswith("_s"):
            runtime[code] = name
        else:
            cold[code] = name
    return runtime, cold


def itemdata_avatar_codes() -> set[str]:
    """ItemData 里头像道具引用的主头像代码（iconAssetID 去 ``_Icon``）。

    用途：index 快照可能落后于游戏版本——ItemData 引用了但 index 里
    没有的代码会作为缺口汇报出来。
    """
    from models.game_config import table_path

    try:
        item_path = table_path("ItemData")
    except FileNotFoundError:
        return set()
    codes: set[str] = set()
    for row in read_table(item_path):
        asset = str(row.get("iconAssetID") or "").strip()
        if asset.endswith("_Icon"):
            codes.add(asset.removesuffix("_Icon"))
    return codes


def load_manifest() -> dict[str, str]:
    if MANIFEST_PATH.is_file():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_manifest(manifest: dict[str, str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# UnityPy 提取
# ---------------------------------------------------------------------------
def export_bundle(bundle_name: str, md5: str, code: str, out_dir: Path) -> Path | None:
    """下载 → 解伪装 → 导出第一张 Texture2D 为 ``{code}.png``。"""
    import UnityPy  # 延迟导入：只有真正要解包时才需要

    data = download_bundle(bundle_name, md5)
    if not data:
        return None
    env = UnityPy.load(data)
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            img = obj.read().image
        except Exception as exc:  # 个别贴图缺数据是常态，跳过即可
            print(f"  ⚠ {code}: Texture2D 读取失败 {str(exc)[:90]}")
            return None
        if img is None:
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{code}.png"
        img.save(target, optimize=True)
        return target
    print(f"  ⚠ {code}: bundle 里没有 Texture2D")
    return None


# ---------------------------------------------------------------------------
# 各模式
# ---------------------------------------------------------------------------
def run_set(
    group: dict[str, str], out_dir: Path, index: dict[str, tuple[int, str]], *, threaded: bool
) -> tuple[int, int]:
    """提取一组 bundle；返回 (成功含已存在, 失败)。threaded 时先并行预热下载。"""
    pre_existing = [code for code in group if (out_dir / f"{code}.png").is_file()]
    todo = [(code, name) for code, name in sorted(group.items()) if code not in set(pre_existing)]
    if threaded and todo:
        # 并行预热下载（解伪装在下载线程里做），主线程再串行解包
        def fetch(item: tuple[str, str]) -> None:
            download_bundle(item[1], index[item[1]][1])

        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            futures = [pool.submit(fetch, item) for item in todo]
            for f in as_completed(futures):
                f.result()
    ok = fail = 0
    for i, (code, name) in enumerate(todo, 1):
        path = export_bundle(name, index[name][1], code, out_dir)
        if path:
            ok += 1
            if i % 25 == 0 or i == len(todo):
                print(f"  … {i}/{len(todo)}")
        else:
            fail += 1
    return ok + len(pre_existing), fail


def cmd_all(args: argparse.Namespace) -> int:
    index = load_index()
    runtime, cold = classify_index(index)
    print(f"index 清单 {len(index)} 条：runtime 主头像 {len(runtime)} 个，冷存 {len(cold)} 个")

    # index 快照可能落后于游戏：ItemData 引用了但 index 没有的代码，报缺口
    missing = itemdata_avatar_codes() - set(runtime)
    if missing:
        print(f"⚠ ItemData 引用但 index 里没有的主头像代码 {len(missing)} 个"
              f"（换最新 index 重跑可补）：{sorted(missing)[:12]}…")

    manifest = load_manifest()
    # runtime 集：md5 未变且 PNG 在 → 跳过（增量）
    skipped = 0
    for code, name in list(runtime.items()):
        md5 = index[name][1]
        if manifest.get(code) == md5 and (OUT_DIR / f"{code}.png").is_file():
            runtime.pop(code)
            skipped += 1
    print(f"runtime 集增量跳过 {skipped} 个，本次处理 {len(runtime)} 个；"
          f"冷存集按文件存在跳过")

    ok_r, fail_r = run_set(runtime, OUT_DIR, index, threaded=True)
    ok_c, fail_c = run_set(cold, COLD_DIR, index, threaded=False)

    # 更新 manifest（只记 runtime 集当前成功的 code→md5）
    for code, name in runtime.items():
        if (OUT_DIR / f"{code}.png").is_file():
            manifest[code] = index[name][1]
    save_manifest(manifest)
    print(
        f"\n完成：runtime 成功 {ok_r} 失败 {fail_r} → {OUT_DIR}\n"
        f"      冷存   成功 {ok_c} 失败 {fail_c} → {COLD_DIR}\n"
        f"      manifest {len(manifest)} 条"
    )
    return 0 if fail_r == 0 else 1


def cmd_probe(args: argparse.Namespace) -> int:
    index = load_index()
    runtime, cold = classify_index(index)
    sample = dict(list(runtime.items())[:3])
    print(f"probe 样本：{list(sample)}")
    for code, name in sample.items():
        path = export_bundle(name, index[name][1], code, WORK_DIR)
        print(f"  {code} → {path}")
    return 0


def cmd_codes(args: argparse.Namespace) -> int:
    index = load_index()
    for code in args.codes:
        name = None
        for candidate in (f"img_hero_{code}_icon",):
            for n in index:
                if n.endswith(candidate):
                    name = n
                    break
        if not name:
            print(f"· {code}: index 里没有对应 bundle")
            continue
        path = export_bundle(name, index[name][1], code, WORK_DIR)
        print(f"  {code} → {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--all", action="store_true", help="全量提取（runtime→静态目录，其余→冷存）")
    modes.add_argument("--probe", action="store_true", help="只提取 3 个样本验证环境")
    modes.add_argument("--codes", nargs="*", default=None, help="手动指定代码（如 a001_03h）")
    args = parser.parse_args()

    if args.probe:
        return cmd_probe(args)
    if args.codes:
        return cmd_codes(args)
    return cmd_all(args)


if __name__ == "__main__":
    raise SystemExit(main())
