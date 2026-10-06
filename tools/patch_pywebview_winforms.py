#!/usr/bin/env python3
"""给 pywebview 的 Windows 后端打本地补丁：让它在 .NET Core / .NET 10 下也能导入。

用法::

    .venv/Scripts/python.exe tools/patch_pywebview_winforms.py            # 打补丁（幂等）
    .venv/Scripts/python.exe tools/patch_pywebview_winforms.py --check    # 只看状态
    .venv/Scripts/python.exe tools/patch_pywebview_winforms.py --restore  # 还原成 .orig

【为什么需要这个补丁（2026-10-03 真机故障）】
pywebview 6.2.1 的 ``webview/platforms/winforms.py`` 在**模块级类体**里做了一串
.NET 反射::

    class OpenFolderDialog:
        windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
        iFileDialogType = windowsFormsAssembly.GetType(
            'System.Windows.Forms.FileDialogNative+IFileDialog')
        ...
        setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)

``FileDialogNative`` / ``FileDialog+VistaDialogEvents`` 是 **.NET Framework 专有**的
WinForms 内部类型，.NET Core / .NET 5+ 里根本不存在 ⇒ ``GetType`` 返回 ``None`` ⇒
``None.GetMethod(...)`` 抛 ``AttributeError: 'NoneType' object has no attribute
'GetMethod'``。

要命之处在于它发生在 **import 期（类体）** ⇒ ``import webview.platforms.winforms``
整体失败 ⇒ pywebview 的 Windows 后端（``winforms`` 与 ``edgechromium`` 都走它）
**一律起不来** ⇒ 现象就是 ``main_gui.py`` 弹不出原生窗口（或只剩 --app 回退）。

本项目**从不使用"选择文件夹"对话框**（只有文件/文件夹对话框里的 folder 分支用得到），
所以把这段反射整体包进 ``try``、取不到就把相关成员置 ``None`` 即可：import 继续成功，
真实窗口（WebView2）照常工作；万一哪天真调用 ``show()`` 会拿到明确报错。

【为什么落成工具，而不是手改一次】
site-packages 里的改动会在 ``pip install -r requirements.txt``、重建 venv 或
重新打包（PyInstaller 会照抄 venv 里的这份文件）之后消失 ⇒ 必须有一个
**幂等、可检查、可还原**的脚本随时补回去。发布流程里也应先跑它。
详见 ``notes/web_layer.md``「§十二补 5」。

退出码：``0`` = 已打好补丁（或本来就已打好）；``1`` = 打不上（上游改了文件，需人工核对）。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

#: 补丁标记：用它判断"是否已打过"，不依赖行号（上游小改也不会误判）。
MARKER = "# 【2026-10-03 本地补丁 · Cherrytale tool One】"

#: 原始片段（pywebview 6.2.1）。必须**原样**出现才能替换，否则说明上游改过了。
OLD_BLOCK = """    windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
    iFileDialogType = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialogNative+IFileDialog'
    )
    OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
    FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
    createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags)
    onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags)
    getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags)
    setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)
    fosPickFoldersBitFlag = (
        windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+FOS')
        .GetField('FOS_PICKFOLDERS')
        .GetValue(None)
    )

    vistaDialogEventsConstructorInfo = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialog+VistaDialogEvents'
    ).GetConstructor(flags, None, [FileDialogType], [])
    adviseMethodInfo = iFileDialogType.GetMethod('Advise')
    unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise')
    showMethodInfo = iFileDialogType.GetMethod('Show')
"""

#: 打完补丁后的片段（逻辑完全等价，只是"取不到就退化"）。
NEW_BLOCK = """    windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')

    # ------------------------------------------------------------------
    # 【2026-10-03 本地补丁 · Cherrytale tool One】.NET Core / .NET 5+ 的
    # System.Windows.Forms **没有** FileDialogNative+IFileDialog、+FOS 以及
    # FileDialog+VistaDialogEvents 这些 .NET Framework 专有内部类型：
    # 下面的反射查找会拿到 None，紧接着的 `.GetMethod(...)` 抛
    # `AttributeError: 'NoneType' object has no attribute 'GetMethod'`。
    # 要命的是它发生在**类体里（import 期）** ⇒ 整个 winforms 后端 import 失败
    # ⇒ pywebview 在 coreclr 下一律起不来（原生窗口全废）。
    # 本工具从不使用"选择文件夹"对话框，所以把整段反射包进 try：取不到就把相关
    # 成员置 None，让 import 继续。
    # 补回方式：.venv/Scripts/python.exe tools/patch_pywebview_winforms.py
    # （原始文件备份在同目录 winforms.py.orig，见 notes/web_layer.md §十二补 5）
    # ------------------------------------------------------------------
    try:
        iFileDialogType = windowsFormsAssembly.GetType(
            'System.Windows.Forms.FileDialogNative+IFileDialog'
        )
        OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
        FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
        createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags)
        onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags)
        getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags)
        setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)
        fosPickFoldersBitFlag = (
            windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+FOS')
            .GetField('FOS_PICKFOLDERS')
            .GetValue(None)
        )
        vistaDialogEventsConstructorInfo = windowsFormsAssembly.GetType(
            'System.Windows.Forms.FileDialog+VistaDialogEvents'
        ).GetConstructor(flags, None, [FileDialogType], [])
        adviseMethodInfo = iFileDialogType.GetMethod('Advise')
        unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise')
        showMethodInfo = iFileDialogType.GetMethod('Show')
    except Exception:  # .NET Core：上面这些类型不存在，退化成"文件夹对话框不可用"
        iFileDialogType = None
        OpenFileDialogType = None
        FileDialogType = None
        createVistaDialogMethodInfo = None
        onBeforeVistaDialogMethodInfo = None
        getOptionsMethodInfo = None
        setOptionsMethodInfo = None
        fosPickFoldersBitFlag = None
        vistaDialogEventsConstructorInfo = None
        adviseMethodInfo = None
        unadviseMethodInfo = None
        showMethodInfo = None
"""


def force_utf8_output() -> None:
    """把标准输出/错误流强制改为 UTF-8。

    【为什么】本脚本会打印 ``✔`` / ``✘`` 这类 GBK 编不出来的字符；Windows 上
    stdout 被重定向到文件/管道时默认按 GBK 编码，会直接抛 ``UnicodeEncodeError``
    （工程里多个工具都踩过，见 .clinerules）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _target_file() -> Path:
    """定位**当前解释器**里的 ``webview/platforms/winforms.py``。

    :return: 目标文件路径。
    :raises SystemExit: 该解释器里没装 pywebview（提示里直接给出正确命令）。
    """
    spec = importlib.util.find_spec("webview")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit(
            "✘ 找不到 webview 包：请用装了 pywebview 的解释器运行，例如\n"
            "     .venv/Scripts/python.exe tools/patch_pywebview_winforms.py"
        )
    return Path(next(iter(spec.submodule_search_locations))) / "platforms" / "winforms.py"


def _as_newline_style(block: str, sample: str) -> str:
    """把片段里的 ``\\n`` 调成目标文件的行尾风格（Windows 上可能是 CRLF）。"""
    return block.replace("\n", "\r\n") if "\r\n" in sample else block


def _state(text: str) -> str:
    """判断文件处于哪种状态：``patched`` / ``original`` / ``unknown``。"""
    if MARKER in text:
        return "patched"
    return "original" if OLD_BLOCK in text else "unknown"


def main(argv: list[str] | None = None) -> int:
    """按命令行参数执行：打补丁 / 只看状态 / 还原。

    :param argv: 命令行参数（``None`` 表示取 :data:`sys.argv[1:]`）。
    :return: 进程退出码（``0`` 正常，``1`` 打不上或找不到目标）。
    """
    force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="patch_pywebview_winforms.py",
        description="给 pywebview 的 WinForms 后端打 .NET Core 兼容补丁（幂等）",
    )
    parser.add_argument("--check", action="store_true", help="只报告状态，不改文件")
    parser.add_argument("--restore", action="store_true", help="用同名 .orig 备份还原（撤销补丁）")
    args = parser.parse_args(argv)

    target = _target_file()
    backup = target.with_suffix(target.suffix + ".orig")  # → winforms.py.orig
    if not target.is_file():
        print(f"✘ 文件不存在：{target}")
        return 1

    text = target.read_text(encoding="utf-8")
    state = _state(text)
    label = {"patched": "已打补丁", "original": "未打补丁", "unknown": "无法识别（上游可能改过）"}
    if args.check:
        print(f"{label[state]}：{target}")
        return 0 if state != "unknown" else 1

    if args.restore:
        if not backup.is_file():
            print(f"✘ 找不到备份 {backup} —— 原始文件已丢失，无法还原", file=sys.stderr)
            return 1
        target.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"✔ 已还原为原始文件：{target}（源：{backup.name}）")
        return 0

    if state == "patched":
        print(f"✔ 本来就是打过补丁的状态，无需改动：{target}")
        return 0
    if state == "unknown":
        print(
            "✘ 既没有补丁标记、也匹配不到原始片段 —— 上游很可能改过这段代码。\n"
            f"  请人工核对：{target}\n"
            "  判据：`class OpenFolderDialog` 的**类体**里只要还在用\n"
            "  `GetType('System.Windows.Forms.FileDialogNative+IFileDialog')` 的返回值\n"
            "  直接 `.GetMethod(...)`，在 .NET Core 下就会抛 AttributeError 并让整个\n"
            "  winforms 后端 import 失败（详见本文件顶部说明）。",
            file=sys.stderr,
        )
        return 1

    old = _as_newline_style(OLD_BLOCK, text)
    new = _as_newline_style(NEW_BLOCK, text)
    if not backup.is_file():
        backup.write_text(text, encoding="utf-8")
        print(f"· 已备份原始文件：{backup}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"✔ 已打补丁：{target}")
    print("  验证：.venv/Scripts/python.exe main_gui.py  →  应直接弹出原生窗口（不再白屏）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

