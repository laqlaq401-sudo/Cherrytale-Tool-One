#!/usr/bin/env python3
"""诊断 pythonnet / clr_loader 的「运行时解析」问题（Windows 桌面窗口专用）。

用法::

    .venv/Scripts/python.exe tools/probe_clr.py            # 人类可读摘要
    .venv/Scripts/python.exe tools/probe_clr.py --json     # 机器可读（便于贴 issue）

【为什么要写这个脚本（而不是手敲环境变量）】
    main_gui.py 启动 pywebview 原生窗口时，Windows 后端要先 ``import clr``，
    由 clr_loader 去加载 .NET 运行时和 ``pythonnet/runtime/Python.Runtime.dll``。
    失败时会报::

        Failed to resolve Python.Runtime.Loader.Initialize
        from ...\\pythonnet\\runtime\\Python.Runtime.dll

    这句话的含义是：**CLR 起来了、DLL 文件也找到了，但加载进去的程序集里
    没有 clr_loader 要找的入口点**。最典型的两种原因：

    1. 「张冠李戴」—— 拿 .NET Framework 的加载方式（netfx）去开 .NET
       Core 的程序集，或反过来；两边对入口点的存法不一样。
    2. 「拿错了版本」—— pythonnet 与 clr_loader 版本不配套，
       pythonnet 期望的入口点名在 clr_loader 里已经改了（或反过来）。

    这两种原因靠"看报错"分不出来，但**逐一切换 ``PYTHONNET_RUNTIME``
    试一次就一目了然**：哪种模式 OK、哪种模式报什么错，一次拿全。

    实测（本机 2026-10-03）这条链其实有**三层**，每层报错都不一样，
    所以本工具的探测矩阵刻意覆盖到最外层：
      ① ``import clr`` 起不起得来            → 需要 ``PYTHONNET_RUNTIME=coreclr``；
      ② ``System.Windows.Forms`` 能否加载     → 需要带 WindowsDesktop 的 runtimeconfig；
      ③ ``import webview.platforms.winforms`` 能否成功 → 需要补 SystemEvents 程序集。
    判定标准取**最外层**（pywebview 能导入）—— 那才是"窗口能不能起来"的充要条件。

【为什么每个模式都跑子进程】
    ``import clr`` 一旦失败，CLR 会处于"半初始化"状态，同进程里再试第二次
    结果不可信（也常常直接崩）。所以这里每换一个环境变量就新起一个解释器，
    本脚本只负责收集与打印摘要。

【为什么不直接读 .venv 里的源码】
    工程约定「禁止把 .venv 内容读进上下文」，而且真正决定成败的是
    **二进制程序集的目标框架与版本**，不是 Python 源码。本脚本改用
    「扫 DLL 字节里的 ``.NETCoreApp,Version=vX.Y`` 特征串」来判断它到底是
    哪个 build —— 这就是程序集自述的 ``TargetFrameworkAttribute``，
    结论比翻文档更硬。

退出码：``0`` = 至少有一种组合能让 **WinForms**（pywebview 真正需要的东西）可用；
``1`` = 全失败。
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

#: 只验证「pythonnet 能不能加载 CLR」的最小代码。
_CODE_IMPORT_CLR: Final[str] = (
    "import clr\n"
    "from System import Environment\n"
    "print('CLR_OK', Environment.Version)\n"
)

#: 完整验证「pywebview 的 WinForms 后端能不能用」—— 这才是窗口真正的需求方：
#: 光能 ``import clr`` 不够，pywebview 的 winforms.py 第一句就是
#: ``clr.AddReference('System.Windows.Forms')``。
_CODE_WINFORMS: Final[str] = (
    "import clr\n"
    "clr.AddReference('System.Windows.Forms')\n"
    "from System.Windows.Forms import Form\n"
    "print('WINFORMS_OK', Form)\n"
)

#: 带自定义 runtimeconfig 的变体（``{}`` 处填配置文件的绝对路径）。
_CODE_WINFORMS_CONFIG: Final[str] = (
    "import pythonnet\n"
    "pythonnet.load('coreclr', runtime_config=r'{}')\n"
    "import clr\n"
    "clr.AddReference('System.Windows.Forms')\n"
    "from System.Windows.Forms import Form\n"
    "print('WINFORMS_OK', Form)\n"
)

#: 目标本身：能不能把 pywebview 的 winforms 后端导进来（**裸**跑，不补程序集）。
#:
#: pywebview 6.2.1 的 ``winforms.py`` 只 ``clr.AddReference('System.Windows.Forms')``，
#: 紧接着就 ``from Microsoft.Win32 import SystemEvents`` —— 而该类型在
#: ``Microsoft.Win32.SystemEvents.dll`` 里。.NET Framework 有 GAC 兜着，.NET Core
#: 下 pythonnet 不会自动把它找出来，于是报::
#:
#:     ImportError: cannot import name 'SystemEvents' from 'Microsoft.Win32'
_CODE_WEBVIEW_PLAIN: Final[str] = (
    "import pythonnet\n"
    "pythonnet.load('coreclr', runtime_config=r'{}')\n"
    "import webview.platforms.winforms\n"
    "print('WEBVIEW_OK')\n"
)

#: 最后一问：wheel 里到底有没有 netfx 专用程序集？有的话能不能用它绕开 .NET Core 的坑。
#:
#: netfx 路线是整个问题的"源头答案"：pywebview 的 WinForms 后端就是按 .NET Framework
#: 写的（``FileDialogNative+IFileDialog`` 在 .NET Core 的 WinForms 里不存在）。
_CODE_NETFX: Final[str] = (
    "import os, glob, traceback\n"
    "import pythonnet\n"
    "pkg = os.path.dirname(pythonnet.__file__)\n"
    "dlls = sorted(glob.glob(os.path.join(pkg, '**', 'Python.Runtime.dll'), recursive=True))\n"
    "top = [d for d in dlls if os.path.basename(os.path.dirname(d)) == 'runtime']\n"
    "others = [d for d in dlls if d not in top]\n"
    "print('DLLS total=' + str(len(dlls)) + ' runtime=' + str(len(top))"
    " + ' nested=' + str(len(others)))\n"
    "for d in others:\n"
    "    print('  nested:', d)\n"
    "info = []\n"
    "try:\n"
    "    if others:\n"
    "        pythonnet.load('netfx', entry_dll=others[0])\n"
    "    else:\n"
    "        pythonnet.load('netfx')\n"
    "    info.append('load=ok')\n"
    "except Exception as exc:\n"
    "    info.append('load=' + type(exc).__name__ + ':' + str(exc)[:70])\n"
    "try:\n"
    "    import webview.platforms.winforms\n"
    "    info.append('winforms=ok')\n"
    "except Exception as exc:\n"
    "    info.append('winforms=' + type(exc).__name__ + ':' + str(exc)[:60])\n"
    "print('RESULT', ' '.join(info))\n"
    "print('WEBVIEW_OK' if 'winforms=ok' in info else 'WEBVIEW_FAIL')\n"
)
#:
#: ``TRUSTED_PLATFORM_ASSEMBLIES``（TPA）是 .NET 宿主为本进程准备的"可解析程序集清单"，
#: 由 runtimeconfig 里声明的那些共享框架决定。它既是最硬的证据（在不在里面一目了然），
#: 也给出了**按绝对路径** AddReference 的落点 —— 绕开"按名字解析失败"。
_CODE_WEBVIEW_FIX: Final[str] = (
    "import os, traceback\n"
    "import pythonnet\n"
    "pythonnet.load('coreclr', runtime_config=r'{}')\n"
    "import clr\n"
    "from System import AppContext, Guid, Type\n"
    "tpa = (AppContext.GetData('TRUSTED_PLATFORM_ASSEMBLIES') or '').split(os.pathsep)\n"
    "se = [p for p in tpa if 'SystemEvents' in p]\n"
    "prop = 'System.Runtime.InteropServices.BuiltInComInterop.IsSupported'\n"
    "info = ['comFlag=' + str(AppContext.GetData(prop))]\n"
    "info.append('systemevents=' + ('hit' if se else 'miss'))\n"
    "try:\n"
    "    t = Type.GetTypeFromCLSID(Guid('DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7'))\n"
    "    info.append('clsid=' + ('ok' if t is not None else 'none'))\n"
    "except Exception as exc:\n"
    "    info.append('clsid=' + type(exc).__name__ + ':' + str(exc)[:30])\n"
    "if se:\n"
    "    try:\n"
    "        clr.AddReference(se[0])\n"
    "        info.append('add=ok')\n"
    "    except Exception as exc:\n"
    "        info.append('add=' + type(exc).__name__)\n"
    "try:\n"
    "    import webview.platforms.winforms\n"
    "    info.append('winforms=ok')\n"
    "except Exception as exc:\n"
    "    import linecache\n"
    "    tb = traceback.extract_tb(exc.__traceback__)[-1]\n"
    "    info.append('fail@' + tb.filename.split(os.sep)[-1] + ':' + str(tb.lineno))\n"
    "    info.append('ctx=' + '|'.join((linecache.getline(tb.filename, n) or '').strip()"
    " for n in range(max(1, tb.lineno - 11), tb.lineno + 1)))\n"
    "print('RESULT', ' '.join(info))\n"
    "print('WEBVIEW_OK' if 'winforms=ok' in info else 'WEBVIEW_FAIL')\n"
)

#: .NET 共享框架名（生成 runtimeconfig 时要用）。
_CORECLR_FRAMEWORK: Final[str] = "Microsoft.NETCore.App"
_DESKTOP_FRAMEWORK: Final[str] = "Microsoft.WindowsDesktop.App"

#: 从 DLL 字节里抽取 ``TargetFrameworkAttribute`` 里的目标框架串。
_TFM_RE: Final[re.Pattern[bytes]] = re.compile(rb"\.NET[A-Za-z]*,Version=v[\d.]+")

#: 需要我们报告版本的相关分发（缺了就显示「未安装」，不是错误）。
_WATCHED_DISTS: Final[tuple[str, ...]] = ("pythonnet", "clr_loader", "pywebview")


def force_utf8_output() -> None:
    """把标准输出/错误流强制改为 UTF-8（原因见 ``main.py`` 里的同名函数）。

    【为什么探针也要这一句】Windows 上 stdout 被重定向到文件/管道时按 **GBK** 编码，
    而本脚本的输出里有 ``⇒`` 这类 GBK 打不出的字符 —— 结果就是"探测全成功，
    最后打印结论时崩掉"（本工具第一次跑就踩到了）。``errors="replace"``
    保证再有意外字符也只是显示成 ``?``，不会中断整个诊断。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _version(dist: str) -> str | None:
    """返回分发的版本号；未安装时返回 ``None``（调用方负责显示成「未安装」）。"""
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def _pythonnet_dir() -> Path | None:
    """定位 ``pythonnet`` 包目录（**不 import 它**，避免触发 CLR 初始化）。"""
    try:
        dist = metadata.distribution("pythonnet")
    except metadata.PackageNotFoundError:
        return None
    candidate = Path(dist.locate_file("pythonnet"))
    return candidate if candidate.is_dir() else None


def _scan_assemblies(root: Path) -> list[dict[str, Any]]:
    """扫描 pythonnet 包内的程序集与配置，报告目标框架与体积。

    :param root: ``pythonnet`` 包目录。
    :return: 每项形如 ``{"path": 相对路径, "size": 字节, "tfm": [...]}``。

    .. note::
       用 ``rglob`` **递归**扫（2026-10-03 修正）：pythonnet 的 wheel 可能把
       netfx 专用程序集放在 ``runtime/netfx/`` 这类子目录里，早先只扫两层会漏掉它
       —— 而"到底有没有 netfx 程序集"正是能不能绕开 .NET Core 那个坑的关键。
    """
    found: list[dict[str, Any]] = []
    for pattern in ("*.dll", "*.json"):
        for path in sorted(root.rglob(pattern)):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            item: dict[str, Any] = {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "tfm": [],
            }
            if path.suffix.lower() == ".dll":
                # 程序集里会有多份 TargetFrameworkAttribute（依赖项各带一份），
                # 去重并排序，固定输出顺序便于人工比对。
                item["tfm"] = sorted(
                    {m.decode("ascii") for m in _TFM_RE.findall(path.read_bytes())}
                )
            found.append(item)
    return found


def _dotnet_roots() -> list[Path]:
    """列出本机可能的 dotnet 安装根目录（与 ``main_gui._dotnet_roots`` 同一套判断）。

    :return: 真实存在的目录列表。

    .. note::
       探针刻意**不 import main_gui**：那会把 webapi/fastapi 一整串依赖拉进来，
       而"窗口起不来"的时候，探针恰恰应该是最后还能跑的东西。
    """
    candidates: list[str | None] = [
        os.environ.get("DOTNET_ROOT"),
        r"C:\Program Files\dotnet",
        r"C:\Program Files (x86)\dotnet",
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(str(Path(local_appdata) / "Microsoft" / "dotnet"))
    roots: list[Path] = []
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _framework_versions(root: Path, framework: str) -> list[str]:
    """列出 ``<root>/shared/<framework>/`` 下的版本号（按数值升序，不是字典序）。

    :param root: dotnet 安装根目录。
    :param framework: 共享框架名（如 ``Microsoft.NETCore.App``）。
    :return: 版本字符串列表；目录不存在时为空列表。

    .. note::
       必须按**数值**排序：字典序下 ``"10.0.11" < "8.0.30"``，
       会把 .NET 8 当成"最高版本"。
    """
    folder = root / "shared" / framework
    if not folder.is_dir():
        return []

    def numeric(name: str) -> tuple[int, ...]:
        return tuple(int(part) for part in name.split(".") if part.isdigit())

    return sorted((child.name for child in folder.glob("*") if child.is_dir()), key=numeric)


def _write_runtime_config(target: Path) -> Path | None:
    """写一份「把 Windows 桌面框架也装进进程」的 runtimeconfig.json。

    :param target: 写到哪里（建议 ``tmp/clr-runtimeconfig.json``，已 gitignore）。
    :return: 写好的路径；本机没有任何合格 .NET 运行时时返回 ``None``。

    【为什么非写不可（2026-10-03 真机实测）】
    只设 ``PYTHONNET_RUNTIME=coreclr`` 时 pythonnet 能加载 CLR，但 .NET 宿主只装了
    ``Microsoft.NETCore.App``（控制台运行时），于是 pywebview 里那句
    ``clr.AddReference('System.Windows.Forms')`` 抛::

        System.IO.FileNotFoundException: Could not load file or assembly
        'System.Windows.Forms, Culture=neutral, PublicKeyToken=null'

    runtimeconfig 就是「告诉 .NET 宿主该把哪些共享框架装进这个进程」的清单；
    而 ``System.Windows.Forms`` 在 **Microsoft.WindowsDesktop.App** 里。
    """
    for root in _dotnet_roots():
        desktop = _framework_versions(root, _DESKTOP_FRAMEWORK)
        core = _framework_versions(root, _CORECLR_FRAMEWORK)
        if not desktop or not core:
            continue
        major = desktop[-1].split(".", 1)[0]
        config = {
            "runtimeOptions": {
                "tfm": f"net{major}.0",
                # 同大版本内允许向上滚动到本机实际装着的补丁版本
                "rollForward": "latestMinor",
                "frameworks": [
                    {"name": _DESKTOP_FRAMEWORK, "version": desktop[-1]},
                    {"name": _CORECLR_FRAMEWORK, "version": core[-1]},
                ],
                "configProperties": {
                    # 【必开】.NET Core 默认**关闭内置 COM 互操作**：不开这一项，
                    # Type.GetTypeFromCLSID(...) 返回 None，pywebview 的
                    # OpenFolderDialog（winforms.py:680）在**导入期**就报
                    # `AttributeError: 'NoneType' object has no attribute 'GetMethod'`。
                    "System.Runtime.InteropServices.BuiltInComInterop.IsSupported": True,
                },
            }
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return target
    return None


def _signatures() -> dict[str, str]:
    """取 ``pythonnet.load`` / ``clr_loader.get_coreclr`` 的真实签名。

    :return: ``{"pythonnet.load": "(runtime=None, **params)", ...}``。

    【为什么要打印签名】``runtime_config`` 是"让宿主加载 Windows 桌面框架"的
    唯一开关，但它的名字/位置随 clr_loader 版本变。打印真实签名，就不必去翻
    .venv 里的源码（工程约定禁止读 .venv 内容）。
    """
    import inspect

    result: dict[str, str] = {}
    for module_name, attr in (
        ("pythonnet", "load"),
        ("clr_loader", "get_coreclr"),
        ("clr_loader", "get_netfx"),
    ):
        try:
            module = __import__(module_name)
            result[f"{module_name}.{attr}"] = str(inspect.signature(getattr(module, attr)))
        except Exception as exc:  # pragma: no cover - 缺包/改签名时才走到
            result[f"{module_name}.{attr}"] = f"<取不到签名：{exc!r}>"
    return result


def _probe(label: str, mode: str | None, code: str) -> dict[str, Any]:
    """在**子进程**里按指定 ``PYTHONNET_RUNTIME`` 跑一段探针代码。

    :param label: 结果里显示的名字（如 ``"coreclr+WinForms"``）。
    :param mode: ``None`` = 不设该变量（= pythonnet 的默认选择）；否则设为
        ``"coreclr"`` / ``"netfx"`` / ``"mono"``。
    :param code: 子进程要执行的源码（见本模块的 ``_CODE_*`` 常量）。
    :return: ``{"mode", "ok", "exit", "stdout", "stderr"}``。

    .. note::
       ``stderr`` 只保留**最后一行**。失败时堆栈很长，而摘要只需要
       「哪一句结论」；完整堆栈自行复现（把 ``-c`` 后面那段代码抄出来跑即可）。
    """
    env = dict(os.environ)
    if mode is None:
        env.pop("PYTHONNET_RUNTIME", None)
    else:
        env["PYTHONNET_RUNTIME"] = mode

    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",  # Windows 默认按 GBK 解码，会抛 UnicodeDecodeError（见 .clinerules）
        errors="replace",
        env=env,
        timeout=180,
    )
    out_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    err_lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
    return {
        "mode": label,
        "ok": proc.returncode == 0
        and any(ln.startswith(("CLR_OK", "WINFORMS_OK", "WEBVIEW_OK")) for ln in out_lines),
        "exit": proc.returncode,
        # 两个流都留**尾部**：失败时结论在 stderr，而"走到哪一步"在 stdout，
        # 只看其中一个就会把关键信息吞掉（本工具第一版就踩了这个坑）。
        "stdout_tail": out_lines[-2:],
        "stderr_tail": err_lines[-2:],
    }


def main(argv: list[str] | None = None) -> int:
    """打印环境清单 + 各运行时模式的探测结果。

    :param argv: 命令行参数（含 ``--json`` 时输出 JSON）。
    :return: ``0`` = 至少一种模式成功；``1`` = 全部失败（原生窗口必然起不来）。
    """
    force_utf8_output()
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args

    root = _pythonnet_dir()
    config_path = _write_runtime_config(
        Path(__file__).resolve().parent.parent / "tmp" / "clr-runtimeconfig.json"
    )
    cases: list[tuple[str, str | None, str]] = [
        ("默认(不设环境变量)", None, _CODE_IMPORT_CLR),
        ("coreclr", "coreclr", _CODE_IMPORT_CLR),
        ("coreclr+WinForms", "coreclr", _CODE_WINFORMS),
    ]
    if config_path is not None:
        cases += [
            (
                "coreclr+配置+WinForms",
                "coreclr",
                _CODE_WINFORMS_CONFIG.format(config_path),
            ),
            (
                "coreclr+配置+pywebview(裸)",
                "coreclr",
                _CODE_WEBVIEW_PLAIN.format(config_path),
            ),
            (
                "coreclr+配置+pywebview(补程序集)",
                "coreclr",
                _CODE_WEBVIEW_FIX.format(config_path),
            ),
        ]
    cases += [
        ("netfx", "netfx", _CODE_IMPORT_CLR),
        ("netfx+子目录程序集+pywebview", None, _CODE_NETFX),
        ("mono", "mono", _CODE_IMPORT_CLR),
    ]
    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "machine": platform.machine(),
        "dists": {name: _version(name) for name in _WATCHED_DISTS},
        "pythonnet_dir": str(root) if root else None,
        "assemblies": _scan_assemblies(root) if root else [],
        "runtime_config": str(config_path) if config_path else None,
        "signatures": _signatures(),
        "probes": [_probe(label, mode, code) for label, mode, code in cases],
    }
    # 判定标准是 **pywebview 的 winforms 后端能不能被导入**（= 窗口能不能起来），
    # 而不是"能不能 import clr" —— 后者成功、前者失败的组合本机已实测到两种。
    window_ready = [p for p in report["probes"] if p["ok"] and "pywebview" in p["mode"]]
    report["ok"] = bool(window_ready)

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    print("=== 环境 ===")
    print(f"Python   : {report['python']} ({report['machine']})")
    print(f"解释器    : {report['executable']}")
    for name, version in report["dists"].items():
        print(f"{name:<9}: {version or '未安装'}")

    print("\n=== pythonnet 包内文件（tfm = 程序集自述的目标框架） ===")
    if not report["assemblies"]:
        print("(没找到 pythonnet 包 —— pywebview 原生窗口必然起不来)")
    for item in report["assemblies"]:
        tfm = ",".join(item["tfm"]) or "-"
        print(f"{item['path']:<28} {item['size']:>8} B  {tfm}")

    print("\n=== 探测矩阵（每一行都是一个独立子进程） ===")
    for probe in report["probes"]:
        mark = "OK  " if probe["ok"] else "FAIL"
        lines = [ln.strip() for ln in probe["stdout_tail"] + probe["stderr_tail"]]
        detail = " ⟵ ".join(lines[-2:]) or f"exit={probe['exit']}"
        print(f"[{mark}] {probe['mode']:<20} {detail}")

    print("\n=== 相关 API 签名 ===")
    for name, sig in report["signatures"].items():
        print(f"{name}{sig}")
    print(f"\n生成的 runtimeconfig：{report['runtime_config'] or '（没有合格的 .NET 运行时）'}")

    if window_ready:
        print(f"\n结论：pywebview 可导入（{window_ready[0]['mode']}）⇒ 原生窗口能起来")
        return 0
    print(
        "\n结论：没有一种组合能让 WinForms 可用 ⇒ 原生窗口起不来。\n"
        "      请把这整份输出贴给维护者（上面的 tfm 与 API 签名是定位关键）"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
