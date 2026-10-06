"""剥离 APK 里的零填充空洞，并用 debug 证书重新签名。

【为什么需要这个工具】
AGP 的增量打包会在 APK 中间留下大块**纯零填充空洞**：它不被任何 zip 条目引用，
不承载数据，安装后也不占设备空间，但实实在在占着分发体积。

2026-10-05 实测（CherrytaleTool 0.9.8-beta）：
    磁盘 90.69 MB = 有效条目 66.23 MB + 死空隙 23.99 MB + 目录/头 0.47 MB
    死空隙占了整包的 26%，位置在 assets/chaquopy/build.json 之后。
历史版本对照（同一套工具链）：
    0.9.4  60.14 MB / 死空隙  0.00 MB
    0.9.5  77.25 MB / 死空隙 16.55 MB
    0.9.6  60.92 MB / 死空隙  0.00 MB
    0.9.8  90.69 MB / 死空隙 23.99 MB
即：**时有时无**，属于构建状态相关，不是设计如此。

【为什么不用 gradlew clean 解决】
clean 会连 Chaquopy 预建的 Python 构建环境（android/app/build/python/env/）一起删掉，
而该环境在本机**无法重建**：venv 的 Scripts/python.exe 读不到自己旁边的 pyvenv.cfg，
于是退化成 base 解释器（pip 24.0 没有 _vendor.distlib）→
Chaquopy 的 pip_install.py 直接 ModuleNotFoundError。
详见 notes/android_apk_slim_plan.md。所以这里改用「事后剥离」：
不依赖构建环境、几秒钟完成、且**保证**死空隙为 0。

【安全性】
- 只重写 zip 容器，条目的名字、顺序、压缩方式（STORED/DEFLATE）全部原样保留
  —— resources.arsc 与 assets/chaquopy/app.imy 必须保持 STORED，这点已断言校验；
- 只剔除 v1(JAR) 签名文件，随后 zipalign 对齐、apksigner 用 debug 证书重签
  （与原包同一把 key，设备上可原地覆盖升级）；
- 结束时跑 apksigner verify 自证。

【用法】
    py -3.11 tools/apk_slim.py <apk>                 # 就地剥离（先备份为 .orig）
    py -3.11 tools/apk_slim.py <apk> -o out.apk      # 另存
    py -3.11 tools/apk_slim.py <apk> --check         # 只体检，不改动
"""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: 超过这个量级才认为是「构建残留空洞」而非正常对齐 padding。
#: 干净构建实测约 0.14~0.37 MB（980 个条目做 4 字节/页对齐的累计开销），
#: 带残留的包会多出整整一个 app.imy 的量级（16~24 MB）。
DEAD_SPACE_THRESHOLD: Final[int] = 1024 * 1024

#: v1(JAR) 签名文件后缀，重签前必须剔除
_V1_SIG_SUFFIXES: Final[tuple[str, ...]] = (".SF", ".RSA", ".DSA", ".MF", ".EC")

#: debug 证书的固定约定（AGP 生成 debug keystore 时写死的）
_DEBUG_KEYSTORE: Final[Path] = Path.home() / ".android" / "debug.keystore"
_DEBUG_ALIAS: Final[str] = "androiddebugkey"
_DEBUG_STOREPASS: Final[str] = "android"


def apk_dead_space(path: Path) -> int:
    """返回 APK 内不被任何 zip 条目引用的字节数（零填充空洞 + 对齐 padding）。

    算法：按中央目录里每个条目的 header_offset 读出**本地头**，算出数据区间
    ``[header_offset + 30 + name_len + extra_len, + compress_size)``，排序后扫空隙。

    注意必须用本地头里的 name_len/extra_len —— 中央目录里的 extra 字段长度
    与实际写入的本地头可以不同，直接用中央目录的值会算错偏移。
    """
    spans: list[tuple[int, int]] = []
    with zipfile.ZipFile(path) as zf, path.open("rb") as fp:
        for info in zf.infolist():
            fp.seek(info.header_offset)
            header = fp.read(30)
            if len(header) < 30:
                continue
            name_len, extra_len = struct.unpack("<HH", header[26:30])
            start = info.header_offset + 30 + name_len + extra_len
            spans.append((start, start + info.compress_size))

    spans.sort()
    dead = 0
    cursor = 0
    for start, end in spans:
        if start > cursor:
            dead += start - cursor
        cursor = max(cursor, end)
    return dead


def find_android_tools() -> tuple[Path, Path]:
    """定位 Android SDK 里的 zipalign 与 apksigner。

    SDK 路径从 android/local.properties 的 ``sdk.dir`` 读（AGP 写进去的那份），
    build-tools 取版本号最大的一个。
    """
    local_props = PROJECT_ROOT / "android" / "local.properties"
    sdk_dir: Path | None = None
    if local_props.is_file():
        for line in local_props.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("sdk.dir"):
                raw = line.split("=", 1)[1].strip()
                # local.properties 里的路径是 Java properties 转义形式：
                #   C\:\\Androidtools\\Sdk  ->  C:\Androidtools\Sdk
                sdk_dir = Path(raw.replace("\\:", ":").replace("\\\\", "\\"))
                break
    if sdk_dir is None or not sdk_dir.is_dir():
        raise FileNotFoundError("找不到 Android SDK：android/local.properties 里没有有效的 sdk.dir")

    bt_root = sdk_dir / "build-tools"
    if not bt_root.is_dir():
        raise FileNotFoundError(f"找不到 build-tools：{bt_root}")

    def ver_key(p: Path) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in p.name.split("."))
        except ValueError:
            return (0,)

    for bt in sorted(bt_root.iterdir(), key=ver_key, reverse=True):
        zipalign = bt / "zipalign.exe"
        apksigner = bt / "apksigner.bat"
        if zipalign.is_file() and apksigner.is_file():
            return zipalign, apksigner
    raise FileNotFoundError(f"build-tools 下找不到 zipalign/apksigner：{bt_root}")


def _rewrite_zip(src: Path, dst: Path) -> None:
    """把 src 的条目原样重写到 dst（跳过 v1 签名文件），死空隙自然消失。"""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", allowZip64=True) as zout:
        for info in zin.infolist():
            name = info.filename
            if name.startswith("META-INF/") and name.upper().endswith(_V1_SIG_SUFFIXES):
                continue
            zout.writestr(info, zin.read(name))


def slim_apk(apk: Path, out: Path | None = None, keep_original: bool = True) -> int:
    """剥离 apk 的死空隙并重签，返回剥离掉的字节数。"""
    apk = apk.resolve()
    out = (out or apk).resolve()
    before = apk.stat().st_size
    dead_before = apk_dead_space(apk)
    if dead_before < DEAD_SPACE_THRESHOLD:
        print(f"✔ 死空隙仅 {dead_before / 1048576:.2f} MB，无需剥离：{apk.name}")
        return 0

    zipalign, apksigner = find_android_tools()
    if not _DEBUG_KEYSTORE.is_file():
        print(f"✘ 找不到 debug 证书，无法重签：{_DEBUG_KEYSTORE}")
        return 0

    work = apk.with_suffix(".slim.tmp")
    aligned = apk.with_suffix(".slim.aligned")
    for tmp in (work, aligned):
        tmp.unlink(missing_ok=True)

    print(f"  剥离中：{apk.name}  死空隙 {dead_before / 1048576:.2f} MB")
    _rewrite_zip(apk, work)

    res = subprocess.run(
        [str(zipalign), "-p", "-f", "4", str(work), str(aligned)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"✘ zipalign 失败：{res.stdout}{res.stderr}")
        return 0
    work.unlink(missing_ok=True)

    if out == apk and keep_original:
        backup = apk.with_suffix(apk.suffix + ".bloated")
        shutil.copy2(apk, backup)
        print(f"  原包已备份：{backup.name}")

    shutil.copy2(aligned, out)
    aligned.unlink(missing_ok=True)

    # v1/v4 关掉，与原包保持一致（原包就是 v2+v3，没有 META-INF 签名文件、
    # 也没有 .idsig 伴随文件）。minSdk 26 允许只签 v2+v3。
    res = subprocess.run(
        [
            str(apksigner), "sign",
            "--ks", str(_DEBUG_KEYSTORE),
            "--ks-pass", f"pass:{_DEBUG_STOREPASS}",
            "--key-pass", f"pass:{_DEBUG_STOREPASS}",
            "--ks-key-alias", _DEBUG_ALIAS,
            "--v1-signing-enabled", "false",
            "--v2-signing-enabled", "true",
            "--v3-signing-enabled", "true",
            "--v4-signing-enabled", "false",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        print(f"✘ apksigner 签名失败：{res.stdout}{res.stderr}")
        return 0

    res = subprocess.run(
        [str(apksigner), "verify", str(out)], capture_output=True, text=True
    )
    if res.returncode != 0:
        print(f"✘ 签名校验失败：{res.stdout}{res.stderr}")
        return 0

    after = out.stat().st_size
    print(
        f"✔ 已剥离 {dead_before / 1048576:.2f} MB 死空隙 | "
        f"{before / 1048576:.2f} MB -> {after / 1048576:.2f} MB | 签名校验通过"
    )
    return before - after


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("apk", type=Path, help="要处理的 APK")
    parser.add_argument("-o", "--output", type=Path, help="输出路径（默认就地覆盖）")
    parser.add_argument("--check", action="store_true", help="只体检死空隙，不改动文件")
    args = parser.parse_args(argv)

    if not args.apk.is_file():
        print(f"✘ 文件不存在：{args.apk}")
        return 1

    dead = apk_dead_space(args.apk)
    if args.check:
        total = args.apk.stat().st_size
        print(f"APK      : {args.apk}")
        print(f"磁盘大小 : {total / 1048576:.2f} MB")
        print(f"死空隙   : {dead / 1048576:.2f} MB  ({dead * 100.0 / total:.1f}%)")
        print(f"判定     : {'需要剥离' if dead >= DEAD_SPACE_THRESHOLD else '正常'}")
        return 0

    slim_apk(args.apk, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
