"""米娅小助手 - 自动化版本管理与双端发布封包工具。

【核心定位】
针对项目长期维护设计：
1. 统一维护四处版本号（Android build.gradle, webapi/app.py, webapi/android_app.py, file_version_info.txt），
   消除手动修改漏改、不一致风险；
2. 自动化前置检查（JDK 21, py -3.11, PyInstaller, Android SDK, 核心单测）；
3. 一键出包：Android 端自动资产同步与 Gradle 构建，PC 端自动 PyInstaller 打包，统一归档至 dist/ 目录。

【常用命令】
    # 检查当前四处版本号是否一致
    python tools/release.py check

    # 检查双端打包环境依赖（JDK 21, py -3.11, PyInstaller, Android SDK）
    python tools/release.py check-env

    # 升级版本号（自动自增 versionCode，四处文件原子修改）
    python tools/release.py bump 0.9.4-beta

    # 运行发版前快速回归单测
    python tools/release.py test

    # 仅打包 Android 端 APK（自动同步 assets 并归档至 dist/android/）
    python tools/release.py android

    # 仅打包 PC 端 EXE（调用 PyInstaller 并产出至 dist/CherrytaleTool/）
    python tools/release.py pc

    # 一键完成检查、快速测试并打出双端发布包
    python tools/release.py all
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Sequence

# 强制终端输出 UTF-8，防止在 Windows 控制台打印中文或符号报错
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DIST_DIR: Final[Path] = PROJECT_ROOT / "dist"
DIST_ANDROID: Final[Path] = DIST_DIR / "android"
VENV_SITE_PACKAGES: Final[Path] = PROJECT_ROOT / ".venv" / "Lib" / "site-packages"

#: 双端统一的 Python 基准版本。
#: 2026-10-06 决策：PC 端由 3.14 降级对齐到 3.11 —— Chaquopy 只支持到 3.11，
#: 跨版本会导致"PC 单测全绿、安卓真机 SyntaxError/依赖缺失闪退"。详见 README「开发环境」。
UNIFIED_PYTHON_VERSION: Final[str] = "3.11"

# 4 处版本号文件路径
FILE_GRADLE: Final[Path] = PROJECT_ROOT / "android" / "app" / "build.gradle"
FILE_WEBAPI_APP: Final[Path] = PROJECT_ROOT / "webapi" / "app.py"
FILE_WEBAPI_ANDROID: Final[Path] = PROJECT_ROOT / "webapi" / "android_app.py"
FILE_VERSION_INFO: Final[Path] = PROJECT_ROOT / "file_version_info.txt"

# 正则表达式定义
RX_GRADLE_VCODE: Final[re.Pattern] = re.compile(r'versionCode\s+(\d+)')
RX_GRADLE_VNAME: Final[re.Pattern] = re.compile(r'versionName\s+["\']([^"\']+)["\']')
RX_WEBAPI_VER: Final[re.Pattern] = re.compile(r'APP_VERSION:\s*Final\[str\]\s*=\s*["\']([^"\']+)["\']')
RX_INFO_FILEVERS: Final[re.Pattern] = re.compile(r'filevers=\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)')
RX_INFO_PRODVERS: Final[re.Pattern] = re.compile(r'prodvers=\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)')
RX_INFO_FILE_STR: Final[re.Pattern] = re.compile(r"StringStruct\('FileVersion',\s*'([^']+)'\)")
RX_INFO_PROD_STR: Final[re.Pattern] = re.compile(r"StringStruct\('ProductVersion',\s*'([^']+)'\)")


@dataclass
class VersionSnapshot:
    gradle_code: int
    gradle_name: str
    webapi_desktop: str
    webapi_android: str
    filevers: tuple[int, int, int, int]
    prodvers: tuple[int, int, int, int]
    file_version_str: str
    prod_version_str: str

    @property
    def is_consistent(self) -> bool:
        """判断版本名称在各端是否对齐。"""
        return (
            self.gradle_name
            == self.webapi_desktop
            == self.webapi_android
            == self.prod_version_str
        )


def read_current_versions() -> VersionSnapshot:
    """从 4 个源文件中提取当前版本信息。"""
    gradle_text = FILE_GRADLE.read_text(encoding="utf-8")
    m_code = RX_GRADLE_VCODE.search(gradle_text)
    m_name = RX_GRADLE_VNAME.search(gradle_text)
    if not m_code or not m_name:
        raise ValueError(f"无法在 {FILE_GRADLE} 中解析 versionCode 或 versionName")
    gradle_code = int(m_code.group(1))
    gradle_name = m_name.group(1)

    web_text = FILE_WEBAPI_APP.read_text(encoding="utf-8")
    m_web = RX_WEBAPI_VER.search(web_text)
    if not m_web:
        raise ValueError(f"无法在 {FILE_WEBAPI_APP} 中解析 APP_VERSION")
    web_ver = m_web.group(1)

    android_text = FILE_WEBAPI_ANDROID.read_text(encoding="utf-8")
    m_and = RX_WEBAPI_VER.search(android_text)
    if not m_and:
        raise ValueError(f"无法在 {FILE_WEBAPI_ANDROID} 中解析 APP_VERSION")
    and_ver = m_and.group(1)

    info_text = FILE_VERSION_INFO.read_text(encoding="utf-8")
    m_fv = RX_INFO_FILEVERS.search(info_text)
    m_pv = RX_INFO_PRODVERS.search(info_text)
    m_fs = RX_INFO_FILE_STR.search(info_text)
    m_ps = RX_INFO_PROD_STR.search(info_text)
    if not (m_fv and m_pv and m_fs and m_ps):
        raise ValueError(f"无法在 {FILE_VERSION_INFO} 中解析完整 Windows 版本结构")

    filevers = (int(m_fv.group(1)), int(m_fv.group(2)), int(m_fv.group(3)), int(m_fv.group(4)))
    prodvers = (int(m_pv.group(1)), int(m_pv.group(2)), int(m_pv.group(3)), int(m_pv.group(4)))

    return VersionSnapshot(
        gradle_code=gradle_code,
        gradle_name=gradle_name,
        webapi_desktop=web_ver,
        webapi_android=and_ver,
        filevers=filevers,
        prodvers=prodvers,
        file_version_str=m_fs.group(1),
        prod_version_str=m_ps.group(1),
    )


def parse_semver_tuple(ver_str: str) -> tuple[int, int, int, int]:
    """将版本字符串（如 '0.9.3-beta' 或 '1.0.0'）转换为 4 元素数字元组供 Windows 属性使用。"""
    # 提取开头的数字部分，如 0.9.3
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", ver_str.strip())
    if not m:
        raise ValueError(f"版本号格式无效，无法解析为语义化数字版本：{ver_str}")
    major = int(m.group(1))
    minor = int(m.group(2))
    patch = int(m.group(3)) if m.group(3) is not None else 0
    return (major, minor, patch, 0)


def cmd_check_version() -> int:
    """检查 4 处版本号配置。"""
    print("\n🔍 正在检查各文件版本号...")
    snap = read_current_versions()

    print(f"  • android/app/build.gradle : versionCode={snap.gradle_code}, versionName={snap.gradle_name}")
    print(f"  • webapi/app.py            : APP_VERSION={snap.webapi_desktop}")
    print(f"  • webapi/android_app.py    : APP_VERSION={snap.webapi_android}")
    print(f"  • file_version_info.txt    : FileVersion={snap.file_version_str}, ProductVersion={snap.prod_version_str}")

    if snap.is_consistent:
        print(f"\n[OK] 版本号一致：{snap.gradle_name} (versionCode: {snap.gradle_code})")
        return 0
    else:
        print("\n[ERROR] 警告：四处版本号不一致！请执行 'python tools/release.py bump <版本号>' 进行统一对齐。")
        return 1


def cmd_bump_version(new_version: str, new_code: int | None = None, dry_run: bool = False) -> int:
    """原子化更新 4 处文件中的版本号。"""
    snap = read_current_versions()
    target_code = new_code if new_code is not None else (snap.gradle_code + 1)
    target_tuple = parse_semver_tuple(new_version)
    target_dotted = f"{target_tuple[0]}.{target_tuple[1]}.{target_tuple[2]}.{target_tuple[3]}"

    print(f"\n🚀 准备更新版本号：")
    print(f"  当前版本 : {snap.gradle_name} (code: {snap.gradle_code})")
    print(f"  目标版本 : {new_version} (code: {target_code})")
    print(f"  Windows属性元组 : {target_tuple} ({target_dotted})")

    # 1. build.gradle
    gradle_text = FILE_GRADLE.read_text(encoding="utf-8")
    new_gradle = RX_GRADLE_VCODE.sub(f"versionCode {target_code}", gradle_text, count=1)
    new_gradle = RX_GRADLE_VNAME.sub(f'versionName "{new_version}"', new_gradle, count=1)

    # 2. webapi/app.py
    web_text = FILE_WEBAPI_APP.read_text(encoding="utf-8")
    new_web = RX_WEBAPI_VER.sub(f'APP_VERSION: Final[str] = "{new_version}"', web_text, count=1)

    # 3. webapi/android_app.py
    and_text = FILE_WEBAPI_ANDROID.read_text(encoding="utf-8")
    new_and = RX_WEBAPI_VER.sub(f'APP_VERSION: Final[str] = "{new_version}"', and_text, count=1)

    # 4. file_version_info.txt
    info_text = FILE_VERSION_INFO.read_text(encoding="utf-8")
    tuple_str = f"({target_tuple[0]}, {target_tuple[1]}, {target_tuple[2]}, {target_tuple[3]})"
    new_info = RX_INFO_FILEVERS.sub(f"filevers={tuple_str}", info_text, count=1)
    new_info = RX_INFO_PRODVERS.sub(f"prodvers={tuple_str}", new_info, count=1)
    new_info = RX_INFO_FILE_STR.sub(f"StringStruct('FileVersion', '{target_dotted}')", new_info, count=1)
    new_info = RX_INFO_PROD_STR.sub(f"StringStruct('ProductVersion', '{new_version}')", new_info, count=1)

    if dry_run:
        print("\n[DRY RUN] 仅预览修改，未写入磁盘。")
        return 0

    # 统一写入
    FILE_GRADLE.write_text(new_gradle, encoding="utf-8")
    FILE_WEBAPI_APP.write_text(new_web, encoding="utf-8")
    FILE_WEBAPI_ANDROID.write_text(new_and, encoding="utf-8")
    FILE_VERSION_INFO.write_text(new_info, encoding="utf-8")

    print("\n[SUCCESS] 4 处版本号已成功更新完毕！")
    return cmd_check_version()


def compute_bump_version(current: str, part: str) -> str:
    """根据指定部位 (x/major, y/minor, z/patch) 计算自增后的版本号，保留预发布后缀。

    :param current: 当前版本字符串，例如 '0.9.4-beta' 或 '1.0.0'
    :param part: 升级部位，支持 '1'/'z'/'patch', '2'/'y'/'minor', '3'/'x'/'major'
    :return: 自增后的新版本字符串
    """
    part_clean = part.lower().strip()
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?(.*)$", current.strip())
    if not m:
        raise ValueError(f"无法解析当前版本号：{current}")
    major = int(m.group(1))
    minor = int(m.group(2))
    patch = int(m.group(3)) if m.group(3) is not None else 0
    suffix = m.group(4) or ""

    if part_clean in ("z", "patch", "1"):
        patch += 1
    elif part_clean in ("y", "minor", "2"):
        minor += 1
        patch = 0
    elif part_clean in ("x", "major", "3"):
        major += 1
        minor = 0
        patch = 0
    else:
        raise ValueError(f"未知版本升级部位 '{part}'，支持 1/z/patch, 2/y/minor, 3/x/major")

    return f"{major}.{minor}.{patch}{suffix}"


def cmd_bump_part(part: str, new_code: int | None = None, dry_run: bool = False) -> int:
    """根据 x/y/z 部位自增并更新 4 处文件中的版本号。"""
    snap = read_current_versions()
    target_ver = compute_bump_version(snap.gradle_name, part)
    return cmd_bump_version(target_ver, new_code=new_code, dry_run=dry_run)


def find_python_cmd(target_ver: str = UNIFIED_PYTHON_VERSION) -> list[str]:
    """智能定位 Python 解释器的可执行命令，防止 WindowsApps 假存根或缺少启动器导致异常。

    :param target_ver: **历史遗留形参**。双端已在 2026-10-06 统一到 3.11，
        因此无论传什么值（包括老代码/老单测里传的 ``"3.14"``）都返回同一个
        3.11 解释器。保留形参只是为了让旧调用点、旧单测不炸。
    :return: 可被 subprocess 执行的命令前缀，例如
        ``["C:\\...\\Cherrytale tool One\\.venv\\Scripts\\python.exe"]`` 或 ``["py", "-3.11"]``
    """
    # 1. 当前解释器已经是基准版本（在 .venv 里跑脚本时最常见）→ 直接复用
    if sys.version_info[:2] == (3, 11):
        return [sys.executable]
    # 2. 本工程 .venv
    venv_py = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.is_file():
        return [str(venv_py)]
    # 3. 系统用户目录 AppData\Local\Python\bin\python3.11.exe
    appdata_py = Path.home() / "AppData" / "Local" / "Python" / "bin" / "python3.11.exe"
    if appdata_py.is_file():
        return [str(appdata_py)]
    # 4. PATH 中的 python3.11
    w = shutil.which("python3.11")
    if w and "WindowsApps" not in w:
        return [w]
    # 5. 回退 py -3.11
    return ["py", "-3.11"]


def run_process_safely(
    cmd: Sequence[str],
    cwd: Path = PROJECT_ROOT,
    extra_env: dict[str, str] | None = None,
    timeout: float = 600.0,
    inject_pypath: bool = True,
) -> subprocess.CompletedProcess[str]:
    """安全执行外部子命令，继承环境变量并注入 UTF-8 设置。

    包含针对 Windows 平台的安全防护：
    1. 过滤 WindowsApps 0 字节假别名存根；
    2. 捕获 OSError（如 WinError 1920、FileNotFoundError），防止脚本崩溃。

    inject_pypath=False 时剥离 PYTHONPATH/PYTHONHOME：Gradle daemon 会把自身
    环境原样传给 Chaquopy 的构建解释器，任何 PYTHONPATH 都会让后者 import 到
    .venv 的新版 pip，遮蔽 Chaquopy 环境自带的 pip。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if inject_pypath:
        if VENV_SITE_PACKAGES.is_dir():
            curr_pypath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{VENV_SITE_PACKAGES};{curr_pypath}" if curr_pypath else str(VENV_SITE_PACKAGES)
            )
    else:
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
    if extra_env:
        env.update(extra_env)

    cmd_list = list(cmd)
    if not cmd_list:
        return subprocess.CompletedProcess(cmd_list, returncode=1, stdout="", stderr="命令为空")

    raw_exe = cmd_list[0]
    resolved = shutil.which(raw_exe) if not Path(raw_exe).is_file() else raw_exe
    # 如果命中了 WindowsApps 下的 0 字节无效执行存根，忽略该别名
    if resolved and "WindowsApps" in resolved:
        try:
            if Path(resolved).stat().st_size == 0:
                resolved = None
        except OSError:
            resolved = None
    if resolved:
        cmd_list[0] = resolved

    try:
        return subprocess.run(
            cmd_list,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except OSError as e:
        return subprocess.CompletedProcess(
            args=cmd_list,
            returncode=127,
            stdout="",
            stderr=f"外部进程启动失败: {e}",
        )


def cmd_check_env() -> int:
    """检查双端编译依赖环境。"""
    print("\n🛠️  检查双端构建依赖环境...")
    ok = True

    # 1. 检查统一的 Python 3.11 解释器（PC 打包与 Android Chaquopy 构建机共用同一份）
    py = find_python_cmd(UNIFIED_PYTHON_VERSION)
    res = run_process_safely([*py, "--version"])
    if res.returncode == 0:
        print(f"  [OK] 统一 Python 解释器: {res.stdout.strip()} ({' '.join(py)})")
    else:
        print(f"  [FAIL] 未检测到 Python {UNIFIED_PYTHON_VERSION}: {res.stderr.strip()}")
        ok = False

    # 2. 检查同一解释器下的 PyInstaller（PC 端打包用）
    res = run_process_safely([*py, "-m", "PyInstaller", "-v"])
    if res.returncode == 0:
        print(f"  [OK] PyInstaller {res.stdout.strip()} ({' '.join(py)})")
    else:
        print(f"  [FAIL] PyInstaller 无法调用（请先在 .venv 中安装）: {res.stderr.strip()}")
        ok = False

    # 3. 检查 Java JDK 21
    gradle_prop = PROJECT_ROOT / "android" / "gradle.properties"
    jdk_found = False
    if gradle_prop.is_file():
        text = gradle_prop.read_text(encoding="utf-8")
        m = re.search(r"org\.gradle\.java\.home\s*=\s*(.+)", text)
        if m:
            jdk_path = Path(m.group(1).strip().replace("\\\\", "\\"))
            java_exe = jdk_path / "bin" / "java.exe"
            if java_exe.is_file():
                j_ver = subprocess.run([str(java_exe), "-version"], capture_output=True, text=True)
                first_line = (j_ver.stderr or j_ver.stdout).splitlines()[0]
                print(f"  [OK] JDK 路径配置正确: {first_line.strip()} ({jdk_path})")
                jdk_found = True
    if not jdk_found:
        print("  [FAIL] 未在 android/gradle.properties 中正确配置 JDK 路径")
        ok = False

    # 4. 检查 Android SDK
    local_prop = PROJECT_ROOT / "android" / "local.properties"
    sdk_found = False
    if local_prop.is_file():
        text = local_prop.read_text(encoding="utf-8")
        m = re.search(r"sdk\.dir\s*=\s*(.+)", text)
        if m:
            sdk_path = Path(m.group(1).strip().replace("\\:", ":").replace("\\\\", "\\"))
            if sdk_path.is_dir():
                print(f"  [OK] Android SDK 路径正确: {sdk_path}")
                sdk_found = True
    if not sdk_found:
        print("  [FAIL] 未在 android/local.properties 中找到有效 Android SDK 路径")
        ok = False

    # 5. 检查前端语法
    res = run_process_safely(["node", "--check", "web/assets/app.js"])
    if res.returncode == 0:
        print("  [OK] 前端 Web 语法校验通过 (node --check web/assets/app.js)")
    else:
        print("  [WARN] 前端语法检测跳过或未安装 node")

    print(f"\n环境体检结果: {'全部就绪' if ok else '存在异常，请按上述提示修复'}")
    return 0 if ok else 1


def cmd_run_tests() -> int:
    """运行发版前快速回归核心单测。"""
    print("\n🧪 正在运行发版前快速回归单测...")
    test_files = [
        "tests/test_webapi_app.py",
        "tests/test_android_app.py",
        "tests/test_services_task_spec.py",
        "tests/test_release_tool.py",
    ]
    py314 = find_python_cmd("3.14")
    res = run_process_safely([*py314, "-m", "pytest", *test_files, "-q"])
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr)
        print("[FAIL] 冒烟测试未通过，构建终止！")
        return 1
    print("[OK] 冒烟测试全部通过！")
    return 0


def cmd_sync_android() -> int:
    """同步桌面代码与 TextAsset 配置表到 Android 工程。"""
    print("\n🔄 正在同步资产与代码至 Android 工程 (tools/sync_android_assets.py)...")
    py314 = find_python_cmd("3.14")
    res = run_process_safely([*py314, "tools/sync_android_assets.py"])
    print(res.stdout)
    if res.returncode != 0:
        print(res.stderr)
        print("[FAIL] Android 资产同步失败！")
        return 1
    print("[OK] Android 资产同步完成。")
    return 0


def get_file_info(path: Path) -> dict[str, str]:
    """计算文件大小（MB）与 SHA256。"""
    size_mb = f"{path.stat().st_size / (1024 * 1024):.2f} MB"
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return {"size": size_mb, "sha256": hasher.hexdigest()[:16]}


def _env_shell_looks_intact(env_python: Path) -> bool:
    """``env_python`` 所在的 venv 外壳是否完好（用于区分"沙箱干扰"与"env 真损坏"）。

    判据（2026-10-06 实测有效）：从**当前进程**能读到 ``<venv>/pyvenv.cfg``、
    里面的 ``home`` 指向存在的基础解释器、且该基础解释器能被拉起。
    这三条同时成立，却仍然启动不了 venv 解释器 —— 那就不是 venv 的问题。

    :param env_python: 形如 ``.../env/debug/Scripts/python.exe``。
    :return: 外壳完好返回 ``True``。
    """
    cfg = env_python.parent.parent / "pyvenv.cfg"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    home = ""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == "home":
            home = value.strip()
            break
    if not home:
        return False
    base_python = Path(home) / "python.exe"
    if not base_python.is_file():
        return False
    check = run_process_safely([str(base_python), "-c", "import sys"], inject_pypath=False)
    return check.returncode == 0


def cmd_build_android(skip_sync: bool = False) -> int:
    """构建 Android APK、剥离死空隙并归档。"""
    snap = read_current_versions()
    if not snap.is_consistent:
        print("[ERROR] 版本号不一致，请先统一版本号！")
        return 1

    if not skip_sync:
        if cmd_sync_android() != 0:
            return 1

    # 这里**故意不加** `gradlew clean`：clean 会连 Chaquopy 预建的 Python 环境
    # （android/app/build/python/env/）一起删掉，重建要重走整套 pip 流程且历史上
    # 出过重建故障。死空隙改由打包后的 apk_slim.slim_apk() 事后剥离解决（见下），
    # 不依赖构建环境、几秒钟完成、且保证死空隙归零。
    #
    # 【2026-10-06 排错定案】generateDebugPythonRequirements 报
    # "No module named 'pip._vendor.distlib.database'"，真凶不是构建环境损坏，
    # 而是 run_process_safely 注入给 gradlew 的 PYTHONPATH（.venv 的 pip 26.2.1
    # 已删除该模块）经 daemon 传进 Chaquopy 构建解释器，遮蔽了环境自带的
    # pip 20.1。因此 gradlew 一律走 inject_pypath=False 的隔离环境。

    # 构建环境预检：env 在但解释器导入不了 Chaquopy 依赖的 pip 内部模块时，
    # 提前给出可执行的诊断，而不是烧完一次 gradle 才看到糊成一团的报错。
    env_python = (
        PROJECT_ROOT
        / "android"
        / "app"
        / "build"
        / "python"
        / "env"
        / "debug"
        / "Scripts"
        / "python.exe"
    )
    if env_python.is_file():
        probe = run_process_safely(
            [str(env_python), "-c", "from pip._vendor.distlib.database import DistributionPath"],
            inject_pypath=False,
        )
        if probe.returncode != 0:
            # 【★ 2026-10-06 排错定案】**先分辨"沙箱/权限干扰"和"env 真损坏"再给建议。**
            #
            # 这个探针在**被沙箱隔离的 shell** 里必然失败，报
            # ``Cannot read '...\env\debug\pyvenv.cfg'``（rc=107）——
            # 而同一个环境、同一份 pyvenv.cfg，在沙箱外跑 ``python.exe -V`` 一切正常。
            # 对照实验（2026-10-06 实测）：在工程目录里**新建一个全新 venv**，
            # 沙箱内照样报同一个错，沙箱外立刻正常 ⇒ 与环境内容无关，是隔离层挡了
            # 3.11 解释器读工程路径下的文件。
            #
            # 现象极具误导性：看着就是"env 坏了"。若照着直觉删
            # ``android/app/build/python/env/``，Chaquopy 会重新下载全部依赖 ——
            # 而那正是历史上暴露 pypi.org 卡死的那一步（见 build.gradle 的 pip 镜像注释）。
            # 所以这里必须把话说准，别把人推去踩那个坑。
            if _env_shell_looks_intact(env_python):
                print("[FAIL] 探针失败，但**本进程能正常读取该环境的 pyvenv.cfg，且基础解释器可用** ——")
                print("       典型是**沙箱 / 权限干扰**，不是 env 损坏。")
                print("       处理：在沙箱外（用户自己的终端，或关闭隔离后）重跑本命令。")
                print("       ⚠️ 不要删 android/app/build/python/env/ —— 那会触发依赖全量重下。")
                print(probe.stderr.strip())
                return 1
            print("[FAIL] Chaquopy 构建环境自检失败：pip._vendor.distlib.database 导入不了。")
            print("       常见原因：env 被删后重建不完整，或 PYTHONPATH/PYTHONHOME 污染。")
            print("       处理：删除 android/app/build/python/env/ 后重跑，让 Chaquopy 重建；")
            print("       若反复失败，检查 buildPython（py -3.11）与外壳里的 PYTHON 变量。")
            print(probe.stderr.strip())
            return 1

    print("\n🤖 正在执行 Android 封包 (gradlew assembleDebug)...")
    gradlew = PROJECT_ROOT / "android" / "gradlew.bat"
    res = run_process_safely(
        [str(gradlew), "assembleDebug"],
        cwd=PROJECT_ROOT / "android",
        inject_pypath=False,
    )
    if res.returncode != 0:
        print(res.stderr)
        print(res.stdout[-2000:])
        print("[FAIL] Android 封包失败！")
        return 1

    built_apk = (
        PROJECT_ROOT
        / "android"
        / "app"
        / "build"
        / "outputs"
        / "apk"
        / "debug"
        / "app-debug.apk"
    )
    if not built_apk.is_file():
        print(f"[FAIL] 找不到生成的 APK 产物：{built_apk}")
        return 1

    DIST_ANDROID.mkdir(parents=True, exist_ok=True)
    target_apk = DIST_ANDROID / f"CherrytaleTool-{snap.gradle_name}.apk"
    shutil.copy2(built_apk, target_apk)

    # 死空隙剥离：增量打包可能留下 16~24 MB 纯零填充（实测 0.9.5 有 16.55 MB、
    # 0.9.8 有 23.99 MB，占整包 26%）。这种包能正常安装运行，只会安静地虚胖，
    # 所以必须在这里无条件兜底处理，而不是指望上游构建干净。
    print("\n🧹 正在检查并剥离 APK 死空隙...")
    try:
        from tools.apk_slim import apk_dead_space, slim_apk
    except ImportError as exc:  # pragma: no cover - 仅在工程结构被破坏时触发
        print(f"  [WARN] 无法导入 apk_slim，跳过剥离：{exc}")
    else:
        dead = apk_dead_space(target_apk)
        if dead >= 1024 * 1024:
            slim_apk(target_apk)
        else:
            print(f"  死空隙 {dead / 1024:.0f} KB，无需剥离。")

    info = get_file_info(target_apk)
    print(f"\n[SUCCESS] Android APK 打包成功并归档：")
    print(f"  产物路径 : {target_apk}")
    print(f"  文件大小 : {info['size']}")
    print(f"  SHA256   : {info['sha256']}")

    try:
        from tools.apk_slim import apk_dead_space as _dead
    except ImportError:  # pragma: no cover
        pass
    else:
        dead = _dead(target_apk)
        if dead >= 1024 * 1024:
            print(f"  [WARN] 空洞自检 : 仍有 {dead / 1048576:.2f} MB 死空隙未剥离！")
        else:
            print(f"  空洞自检 : 通过（{dead / 1024:.0f} KB，属正常对齐开销）")
    return 0


def cmd_build_pc() -> int:
    """构建 PC 端 Windows EXE、补齐运行时必需配置表并归档为 ZIP 发布包。"""
    snap = read_current_versions()
    if not snap.is_consistent:
        print("[ERROR] 版本号不一致，请先统一版本号！")
        return 1

    py314 = find_python_cmd("3.14")
    print(f"\n🖥️  正在执行 PC 端打包 (PyInstaller via {' '.join(py314)})...")
    res = run_process_safely(
        [*py314, "-m", "PyInstaller", "CherrytaleTool.spec", "--noconfirm"]
    )
    if res.returncode != 0:
        print(res.stderr)
        print(res.stdout[-2000:])
        print("[FAIL] PC 端 PyInstaller 打包失败！")
        return 1

    pc_dist_dir = DIST_DIR / "CherrytaleTool"
    exe_file = pc_dist_dir / "CherrytaleTool.exe"
    if not exe_file.is_file():
        print(f"[FAIL] 找不到生成的 EXE 产物：{exe_file}")
        return 1

    # ★ 构建自检（2026-10-05 头像排查教训）：PyInstaller 从工作区快照打包，
    #   并行改动可能造成"半新半旧"构建（前端新、python 侧旧）。
    #   这里对新代码的标志物做产物级校验，缺任何一项直接判失败。
    print("\n🔍 构建产物自检（新代码标志物）...")
    exe_blob = exe_file.read_bytes()
    internal_dir = pc_dist_dir / "_internal"
    checks = [
        (
            "EXE 内含 services.avatar_service 模块",
            b"services.avatar_service" in exe_blob,
        ),
        (
            "EXE 内含 tasks.login 模块",
            b"tasks.login" in exe_blob,
        ),
        (
            "打包内 login.js 含头像字段（avatar_data_url）",
            (internal_dir / "web" / "assets" / "login.js").is_file()
            and b"avatar_data_url" in (internal_dir / "web" / "assets" / "login.js").read_bytes(),
        ),
    ]
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}")
    if failed:
        print(f"[FAIL] 构建产物缺少新代码标志物：{failed}\n"
              "  处理：确认工作区源码完整后重新打包；不要用旧快照/旧提交打包。")
        return 1

    # 关键补丁：复制运行时必需的 33 张配置表至 dist/CherrytaleTool/Cherrytale Asset/TextAsset
    print("\n📦 正在将运行时必需配置表同步进 PC 发布包目录...")
    try:
        from tools.sync_android_assets import referenced_tables, TEXT_ASSET_SOURCE
        used_tables = referenced_tables()
        if not TEXT_ASSET_SOURCE.is_dir():
            print(f"  [WARN] 未找到配置表源目录：{TEXT_ASSET_SOURCE}")
        else:
            pc_text_asset_dir = pc_dist_dir / "Cherrytale Asset" / "TextAsset"
            pc_text_asset_dir.mkdir(parents=True, exist_ok=True)
            copied_count = 0
            copied_bytes = 0
            for tbl in sorted(used_tables):
                src_tbl = TEXT_ASSET_SOURCE / tbl
                if src_tbl.is_file():
                    shutil.copy2(src_tbl, pc_text_asset_dir / tbl)
                    copied_count += 1
                    copied_bytes += src_tbl.stat().st_size
            print(f"  [OK] 成功补齐 {copied_count} 张配置表 ({copied_bytes / (1024*1024):.1f} MB) 至 PC 发版目录")
    except Exception as e:
        print(f"  [WARN] 配置表同步异常: {e}")

    info_exe = get_file_info(exe_file)
    print(f"\n[SUCCESS] PC 端构建成功：")
    print(f"  运行目录 : {pc_dist_dir}")
    print(f"  可执行体 : {exe_file}")
    print(f"  EXE 大小 : {info_exe['size']}")
    print(f"  EXE SHA  : {info_exe['sha256']}")

    # 产物压缩：生成 CherrytaleTool-<version>-windows-x64.zip
    print("\n🗜️  正在打包 PC 端 ZIP 归档发布包...")
    zip_basename = DIST_DIR / f"CherrytaleTool-{snap.gradle_name}-windows-x64"
    target_zip = Path(f"{zip_basename}.zip")
    if target_zip.is_file():
        target_zip.unlink(missing_ok=True)

    try:
        created_zip = shutil.make_archive(
            base_name=str(zip_basename),
            format="zip",
            root_dir=str(DIST_DIR),
            base_dir="CherrytaleTool",
        )
        zip_path = Path(created_zip)
        info_zip = get_file_info(zip_path)
        print(f"\n[SUCCESS] PC 发布 ZIP 归档包已生成：")
        print(f"  归档路径 : {zip_path}")
        print(f"  文件大小 : {info_zip['size']}")
        print(f"  SHA256   : {info_zip['sha256']}")
    except Exception as e:
        print(f"  [FAIL] ZIP 归档打包失败: {e}")
        return 1

    return 0


def cmd_build_all(skip_tests: bool = False) -> int:
    """一键双端完整封包流水线。"""
    print("\n==========================================")
    print("      🚀 米娅小助手 双端发布流水线")
    print("==========================================")

    # 1. 检查版本
    if cmd_check_version() != 0:
        return 1

    # 2. 检查环境
    if cmd_check_env() != 0:
        return 1

    # 3. 运行测试门禁
    if not skip_tests:
        if cmd_run_tests() != 0:
            return 1

    # 4. 构建 Android
    if cmd_build_android() != 0:
        return 1

    # 5. 构建 PC
    if cmd_build_pc() != 0:
        return 1

    snap = read_current_versions()
    print("\n==========================================")
    print(f"🎉 双端封包圆满完成！发布版本：{snap.gradle_name}")
    print("==========================================")
    print(f"  Android APK : dist/android/CherrytaleTool-{snap.gradle_name}.apk")
    print(f"  PC ZIP 归档 : dist/CherrytaleTool-{snap.gradle_name}-windows-x64.zip")
    print(f"  PC 桌面目录 : dist/CherrytaleTool/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="米娅小助手双端自动化发布与版本管理工具")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # check
    subparsers.add_parser("check", help="检查各文件版本号一致性")

    # current-version
    subparsers.add_parser("current-version", help="仅打印当前版本号字符串（供脚本调用）")

    # check-env
    subparsers.add_parser("check-env", help="检查双端打包依赖环境（JDK, SDK, Python, PyInstaller）")

    # bump
    bump_parser = subparsers.add_parser("bump", help="升级版本号并同步修改 4 处文件")
    bump_parser.add_argument("version", help="新版本字符串，例如 0.9.4-beta 或 1.0.0")
    bump_parser.add_argument("--code", type=int, default=None, help="指定 Android versionCode（默认自增 1）")
    bump_parser.add_argument("--dry-run", action="store_true", help="仅预览修改，不实际改动文件")

    # bump-part (x.y.z: 1=patch/z, 2=minor/y, 3=major/x)
    bump_part_parser = subparsers.add_parser("bump-part", help="按 x/y/z 自增版本号并修改 4 处文件")
    bump_part_parser.add_argument("part", help="升级部位：1/z/patch, 2/y/minor, 3/x/major")
    bump_part_parser.add_argument("--code", type=int, default=None, help="指定 Android versionCode（默认自增 1）")
    bump_part_parser.add_argument("--dry-run", action="store_true", help="仅预览修改，不实际改动文件")

    # calc-bump
    calc_parser = subparsers.add_parser("calc-bump", help="计算并打印自增后的版本号预览（不写入文件）")
    calc_parser.add_argument("part", help="升级部位：1/z/patch, 2/y/minor, 3/x/major")

    # test
    subparsers.add_parser("test", help="运行快速回归核心单测")

    # sync
    subparsers.add_parser("sync", help="同步代码与配置表到 Android 目录")

    # android
    android_parser = subparsers.add_parser("android", help="构建 Android APK 并归档")
    android_parser.add_argument("--skip-sync", action="store_true", help="跳过资产同步步骤")

    # pc
    subparsers.add_parser("pc", help="构建 PC 端 Windows EXE、补齐配置表并生成 ZIP")

    # all
    all_parser = subparsers.add_parser("all", help="一键全流程：检查 -> 测试 -> Android 封包 -> PC 封包")
    all_parser.add_argument("--skip-tests", action="store_true", help="跳过快速单测门禁")

    args = parser.parse_args(argv)

    if args.command == "check":
        return cmd_check_version()
    elif args.command == "current-version":
        snap = read_current_versions()
        print(snap.gradle_name)
        return 0
    elif args.command == "check-env":
        return cmd_check_env()
    elif args.command == "bump":
        return cmd_bump_version(args.version, args.code, dry_run=args.dry_run)
    elif args.command == "bump-part":
        return cmd_bump_part(args.part, args.code, dry_run=args.dry_run)
    elif args.command == "calc-bump":
        snap = read_current_versions()
        print(compute_bump_version(snap.gradle_name, args.part))
        return 0
    elif args.command == "test":
        return cmd_run_tests()
    elif args.command == "sync":
        return cmd_sync_android()
    elif args.command == "android":
        return cmd_build_android(skip_sync=args.skip_sync)
    elif args.command == "pc":
        return cmd_build_pc()
    elif args.command == "all":
        return cmd_build_all(skip_tests=args.skip_tests)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
