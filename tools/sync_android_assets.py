"""把桌面工程的最新代码增量同步进安卓工程（android/app/src/main/python/）。

【为什么要有这个脚本】
安卓工程里的 Python 代码是桌面工程的**副本**（Chaquopy 要求源码放在
``app/src/main/python/`` 下）。手工复制迟早漏文件，所以固化成一条命令：

    .\\.venv\\Scripts\\python.exe tools/sync_android_assets.py

【同步规则】
- 整目录替换：``client / crypto / models / services / tasks / webapi / web``，
  即桌面删除的文件在安卓侧也会删除（rmtree 后 copytree）；
- 单文件：``config.py``、``web_server.py``、``main.py``；
- **安装 pydantic 垫片**：``tools/android_shims/pydantic/`` →
  ``android/app/src/main/python/pydantic/``（垫片的源码保存在受版本控制的
  tools/ 下；android python 目录整体被 .gitignore 视为生成物）；
- **打包运行时配置表**：扫描代码引用的表名，把 ``Cherrytale Asset/TextAsset``
  里被引用的表复制进 APK assets（``app/src/main/assets/material/``），
  由 MainActivity 在首次启动时解压到应用目录并设置
  ``CHERRYTALE_MATERIAL_ROOT``。2026-10-02 真机踩坑：配置表不进包，
  日常宝箱 / 活动预览 / 主线状态全部 FileNotFoundError；
- 排除垃圾：``__pycache__``、``*.pyc``、备份文件（如 ``styles.backup.css``）。

【为什么不同步更多东西】
``tools/``（逆向脚本）、``notes/``、``captures/``、素材目录与运行无关，
同步只会白白增大 APK；``main_gui.py``（PC 独立窗口）依赖 pywebview，
安卓上没有意义；``main.py`` 的 ``selftest`` 依赖 dump.cs 素材，但任务执行
需要的 ``services/runner.py`` 会经由 ``webapi/android_app.py`` 间接导入
``main`` 的两个工具函数 —— 所以 ``main.py`` 也一并同步。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

# 强制终端输出 UTF-8，防止在 Windows 控制台打印中文或符号报错
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
ANDROID_PYTHON: Path = PROJECT_ROOT / "android" / "app" / "src" / "main" / "python"

#: 整目录替换的清单（目录名在桌面与安卓两侧同名）
SYNC_DIRS: tuple[str, ...] = (
    "client",
    "crypto",
    "models",
    "services",
    "tasks",
    "webapi",
    "web",
)

#: 单文件同步清单
SYNC_FILES: tuple[str, ...] = (
    "config.py",
    "web_server.py",
    "main.py",
    # 【★ 2026-10-05】vCode 密钥的本机存放点必须随 APK 走：config.GAME_V_CODE_KEY
    # 不再有内置默认值，Android 端（非 frozen）的探测目录是 config.py 同级
    # （RESOURCE_ROOT = Path(__file__).parent），Chaquopy 把 src/main/python/ 下的
    # 所有 .py 展开到运行时同一目录树 —— 放这里就能命中。
    # 漏了它，所有要算 vCode 的任务（wallet / arena / wudou / yimo / top_pvp…）
    # 在 APK 里全部 RuntimeError。已被 .gitignore 覆盖（匹配任意层级），不进 git。
    "config_local.py",
)

#: 同步时跳过的文件/目录名
IGNORE_NAMES: frozenset[str] = frozenset({
    "__pycache__",
    "styles.backup.css",
})

#: 游戏配置表源目录（149.7MB 全量太大，只打包代码真正引用的子集）
TEXT_ASSET_SOURCE: Path = PROJECT_ROOT / "Cherrytale Asset" / "TextAsset"
TEXT_ASSET_TARGET: Path = (
    PROJECT_ROOT / "android" / "app" / "src" / "main"
    / "assets" / "material" / "Cherrytale Asset" / "TextAsset"
)

#: 扫描字符串字面量用的正则（表名都是 Identifier 风格）
_TABLE_NAME_RX = re.compile(r'["\']([A-Za-z][A-Za-z0-9_]{2,})["\']')

#: 这些目录里出现表名字符串才算"运行时引用"
_CODE_DIRS: tuple[str, ...] = ("models", "tasks", "services", "webapi", "client")


def referenced_tables() -> set[str]:
    """扫描运行时代码，返回被引用的配置表文件名集合。

    【为什么按引用扫描而不是全量打包】TextAsset 全量 149.7MB（405 项），
    而代码实际读的只有 29 张 / 4.2MB。全量打包会让 APK 膨胀 3 倍以上。
    这里用"字符串字面量 ∩ 表文件名"的朴素交集——项目里所有表名都是
    直接写死的字面量（无动态拼接，2026-10-02 已 grep 确认），朴素办法
    恰好精确。新增表引用后重跑本脚本即可自动带上。
    """
    tables = {p.name for p in TEXT_ASSET_SOURCE.iterdir() if p.is_file()}
    refs: set[str] = set()
    for d in _CODE_DIRS:
        for py in (PROJECT_ROOT / d).rglob("*.py"):
            refs.update(_TABLE_NAME_RX.findall(py.read_text(encoding="utf-8", errors="replace")))
    return tables & refs


def _ignore(dir_path: str, names: list[str]) -> list[str]:
    """shutil.copytree 的 ignore 回调。"""
    return [name for name in names if name in IGNORE_NAMES or name.endswith(".pyc")]


#: 角色头像占位图：源（入 git）→ 目标（随包分发）
#:
#: 【为什么必须有这一步】``web/assets/avatars/`` 在 .gitignore 里，196 张真头像
#: 全部是"本机预提取的生成物"，不入库。占位图如果只丢进去，换机器 / 重新克隆就没了，
#: 缺图角色会变成破图。所以把源文件放在受版本控制的 ``assets/`` 下，由本脚本
#: （发布链的必经步骤、无重依赖）保证它在同步时一定就位。
#:
#: 【为什么叫 000h 而不是 placeholder.png】换算只产出 ``[a-z]+\d+h`` 形态的 code，
#: 用同构名可以让"每个 icon_code 要么为空、要么有同名文件"这条测试守卫无脑成立；
#: 且 ``000h`` 在热更清单里没有对应 bundle，**不可能与任何真头像冲突** ——
#: 将来从 CDN 补齐真图后，占位图自动被取代（换算命中真 code 就返回真 code）。
PLACEHOLDER_SRC: Path = PROJECT_ROOT / "assets" / "placeholder_avatar.png"
PLACEHOLDER_DST: Path = PROJECT_ROOT / "web" / "assets" / "avatars" / "000h.png"


def ensure_placeholder_avatar(*, dry_run: bool) -> None:
    """幂等保证 ``web/assets/avatars/000h.png`` 存在。

    源缺失只告警不中断：占位图是"化妆"用品，不该因为它挡住整个打包流程；
    但必须**明确说出来**，否则又是一个静默失效。
    """
    if not PLACEHOLDER_SRC.is_file():
        print(
            f"⚠ 角色头像占位图源缺失：{PLACEHOLDER_SRC}\n"
            f"   —— web/assets/avatars/000h.png 无法保证就位，缺图的角色会显示破图"
        )
        return
    if PLACEHOLDER_DST.is_file() and PLACEHOLDER_DST.read_bytes() == PLACEHOLDER_SRC.read_bytes():
        return  # 已就位且内容一致，不动（幂等）
    if dry_run:
        print(f"[dry] 占位图 → {PLACEHOLDER_DST.relative_to(PROJECT_ROOT)}")
        return
    PLACEHOLDER_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PLACEHOLDER_SRC, PLACEHOLDER_DST)
    print(f"✔ 角色头像占位图已就位：{PLACEHOLDER_DST.relative_to(PROJECT_ROOT)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要做的操作，不实际复制",
    )
    args = parser.parse_args(argv)

    if not ANDROID_PYTHON.is_dir():
        print(f"✘ 安卓 Python 目录不存在：{ANDROID_PYTHON}")
        return 1

    changed = 0

    # ★ 必须在同步 web/ 之前：这样 copytree 才能把占位图一起带进安卓侧
    ensure_placeholder_avatar(dry_run=args.dry_run)

    for name in SYNC_DIRS:
        src = PROJECT_ROOT / name
        dst = ANDROID_PYTHON / name
        if not src.is_dir():
            print(f"⚠ 桌面目录缺失，跳过：{name}")
            continue
        if args.dry_run:
            print(f"[dry] 目录替换：{name}/ -> android")
            continue
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=_ignore)
        changed += 1
        print(f"✔ 目录已同步：{name}/")

    for name in SYNC_FILES:
        src = PROJECT_ROOT / name
        dst = ANDROID_PYTHON / name
        if not src.is_file():
            print(f"⚠ 桌面文件缺失，跳过：{name}")
            continue
        if args.dry_run:
            print(f"[dry] 文件复制：{name} -> android")
            continue
        shutil.copy2(src, dst)
        changed += 1
        print(f"✔ 文件已同步：{name}")

    # 打包自检（2026-10-05 踩坑后补）：config_local.py 缺失时上面的循环只会
    # 静默跳过，打出来的 APK 一切照常、唯独所有要算 vCode 的任务在运行期
    # 全部 RuntimeError（wallet / arena / wudou / yimo / top_pvp…），极难排查。
    # 这里在打包机上就把话说死。
    if not (ANDROID_PYTHON / "config_local.py").is_file():
        print(
            "⚠⚠⚠ APK 将不含 config_local.py —— 里面所有需要 vCode 的任务都会报"
            "「vCode 密钥未配置」。若这是本机没配密钥所致，请先在工程根目录创建"
            " config_local.py（GAME_V_CODE_KEY=...）再重新打包。"
        )

    # pydantic 垫片：从受版本控制的 tools/android_shims/ 安装
    # （android python 目录被 .gitignore 视为生成物，垫片源码必须放在 git 里）
    shim_src = PROJECT_ROOT / "tools" / "android_shims" / "pydantic"
    shim_dst = ANDROID_PYTHON / "pydantic"
    if not shim_src.is_dir():
        print(f"✘ 垫片源目录不存在：{shim_src}")
        return 1
    if args.dry_run:
        print("[dry] 安装 pydantic 垫片 -> android")
    else:
        if shim_dst.exists():
            shutil.rmtree(shim_dst)
        shutil.copytree(shim_src, shim_dst, ignore=_ignore)
        print(f"✔ pydantic 垫片已安装（{len(list(shim_dst.glob('*.py')))} 个文件）")

    # 运行时配置表 → APK assets（MainActivity 启动时解压到应用目录）
    if not TEXT_ASSET_SOURCE.is_dir():
        print(f"⚠ 配置表源目录缺失，跳过：{TEXT_ASSET_SOURCE}")
    else:
        used = referenced_tables()
        if args.dry_run:
            print(f"[dry] 配置表 {len(used)} 张 -> APK assets")
        else:
            if TEXT_ASSET_TARGET.exists():
                shutil.rmtree(TEXT_ASSET_TARGET)
            TEXT_ASSET_TARGET.mkdir(parents=True)
            total = 0
            for name in sorted(used):
                shutil.copy2(TEXT_ASSET_SOURCE / name, TEXT_ASSET_TARGET / name)
                total += (TEXT_ASSET_TARGET / name).stat().st_size
            changed += 1
            print(f"✔ 配置表已打包：{len(used)} 张（{total / 1048576:.1f} MB）-> app/src/main/assets/material/")

    print(f"\n完成：{changed} 项同步。下一步：在 android/ 目录执行 gradlew assembleDebug 重新封包。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
