"""``main_gui.py`` 与 ``web_server.py`` 的守护测试（离线；**不开窗口**）。

【能测什么、不能测什么】
能测：命令行参数的默认值、"暴露到局域网但没给令牌"必须拒绝、服务线程的
启动/停止生命周期（关窗口要能停服务、端口要能释放）、注入脚本引用了正确的桥方法。
不能测：真正弹出 pywebview 窗口 —— 那需要交互式桌面会话，在自动化里
要么挂住、要么弹窗骚扰用户。窗口本身由使用者手工确认（见 notes/web_layer.md）。

运行方式：

    python -m tests.test_main_gui
    python -m pytest tests/test_main_gui.py -q
"""

from __future__ import annotations

import json
import os
import socket
import sys
from types import SimpleNamespace

import pytest

import main_gui
import web_server
from webapi import app as webapp


def _free_port() -> int:
    """取一个空闲端口（与 test_webapi_app.py 同一手法）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _port_is_free(port: int) -> bool:
    """端口当前是否空闲（用来验证"服务停掉后端口确实释放了"）。"""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


# ---------------------------------------------------------------------------
# ① 命令行参数
# ---------------------------------------------------------------------------
class TestParsers:
    def test_window_defaults(self) -> None:
        args = main_gui.build_parser().parse_args([])
        assert args.host == "127.0.0.1"  # 默认只服务本机
        assert args.port == 8765
        assert args.token is None
        assert args.frameless is True  # 需求：默认无边框
        assert args.no_window is False

    def test_frameless_can_be_turned_off(self) -> None:
        args = main_gui.build_parser().parse_args(["--no-frameless"])
        assert args.frameless is False

    def test_web_server_defaults(self) -> None:
        args = web_server.build_parser().parse_args([])
        assert args.host == "127.0.0.1"
        assert args.port == web_server.DEFAULT_PORT
        assert args.token is None
        assert args.verbose is False


# ---------------------------------------------------------------------------
# ② "暴露到局域网必须给令牌"（两个入口的安全底线）
# ---------------------------------------------------------------------------
class TestLanGuard:
    def test_window_refuses_lan_without_token(self) -> None:
        """参数有问题时返回 2，且**不会真的起服务**。"""
        assert main_gui.main(["--host", "0.0.0.0", "--no-window"]) == 2

    def test_web_server_refuses_lan_without_token(self) -> None:
        assert web_server.main(["--host", "0.0.0.0"]) == 2

    def test_loopback_hosts_cover_local_names(self) -> None:
        assert "127.0.0.1" in web_server.LOOPBACK_HOSTS
        assert "localhost" in web_server.LOOPBACK_HOSTS
        assert "::1" in web_server.LOOPBACK_HOSTS


# ---------------------------------------------------------------------------
# ③ 服务线程生命周期（"关窗口 = 停服务"的机器化验证）
# ---------------------------------------------------------------------------
class TestServerThread:
    def test_start_and_stop_releases_port(self) -> None:
        """启动后端口可用；``stop()`` 之后端口必须释放。

        【为什么这条重要】
        端口不释放 = 用户"关了窗口，下次启动却说端口被占用"。
        这是桌面壳最容易留下的一类残留。
        """
        port = _free_port()
        application = webapp.create_app()
        server = main_gui.ServerThread(application, "127.0.0.1", port)
        server.start()
        try:
            assert server.is_alive is True
            assert _port_is_free(port) is False  # 正在监听
        finally:
            server.stop()

        assert server.is_alive is False
        assert _port_is_free(port) is True


# ---------------------------------------------------------------------------
# ④ 无边框窗口的注入脚本
# ---------------------------------------------------------------------------
class TestWindowControls:
    def test_injected_script_calls_the_bridge_methods(self) -> None:
        """注入的按钮必须调用 :class:`main_gui.DesktopChrome` 上真实存在的方法。

        （写错方法名的表现是"按钮点了没反应"，而那在手工点之前很难发现。）
        """
        script = main_gui._WINDOW_CONTROLS_JS
        assert "pywebview.api" in script
        assert "minimize" in script
        assert "close" in script
        # 桥对象上确实有这两个方法
        chrome = main_gui.DesktopChrome()
        assert callable(chrome.minimize)
        assert callable(chrome.close)

    def test_injected_script_has_maximize_and_edge_resize(self) -> None:
        """2026-09-30 起注入脚本还要有：最大化按钮、边缘缩放把手、侧边栏停靠。

        - ``toggle_maximize``：最大化按钮走桥上的同名方法；
        - ``begin_edge_resize``：8 个缩放把手 mousedown 时的入口；
        - ``stopPropagation``：把手必须拦在 target 阶段 —— pywebview 的
          easy_drag 监听挂在 window 冒泡上，不拦就会"边缩放边拖动窗口"；
        - ``win-dock``：按钮停靠进顶栏右上角的预留位（Windows 惯例位置；
          2026-10-01 从侧边栏移回顶栏 —— 侧边栏里放窗口控制不合操作直觉）。
        """
        script = main_gui._WINDOW_CONTROLS_JS
        assert "toggle_maximize" in script
        assert "begin_edge_resize" in script
        assert "stopPropagation" in script
        assert "win-dock" in script
        # 桥未就绪时的重试（2026-10-01：两页跳转后 pywebview 桥是异步注入的，
        # 立刻点击不能静默失效）
        assert "setInterval" in script
        chrome = main_gui.DesktopChrome()
        assert callable(chrome.toggle_maximize)
        assert callable(chrome.begin_edge_resize)

    def test_wire_window_events_subscribes_loaded(self) -> None:
        """窗口控制必须挂在 ``events.loaded`` 上、每次页面加载后重新注入。

        【为什么这条是回归线（2026-10-01）】前端拆成两页后登录/退出都会真实
        跳转，一次性注入的按钮在重进后消失（用户实测：重新登录后右上角
        空了）。订阅发生在 ``webview.start()`` 之前才不会漏掉首次加载。
        """
        subscribed: list = []

        class FakeEvents:
            def __iadd__(self, handler):
                subscribed.append(handler)
                return self

        class FakeWindow:
            events = SimpleNamespace(loaded=FakeEvents())

        main_gui._wire_window_events(FakeWindow())

        assert len(subscribed) == 1, "loaded 事件必须恰好订阅一次"
        # 处理器被事件触发时不能崩：FakeWindow 没有 evaluate_js，
        # _inject_window_controls 里会吞掉异常只记 warning
        subscribed[0]()

    def test_chrome_without_window_is_safe(self) -> None:
        """桥还没绑定窗口时被调用（页面加载比窗口对象赋值更快）不能崩。"""
        chrome = main_gui.DesktopChrome()
        chrome.minimize()
        chrome.close()
        chrome.toggle_maximize()
        chrome.begin_edge_resize("se")  # 未知/未绑定都不该抛错或起线程

    def test_chrome_keeps_window_reference_private(self) -> None:
        """桥对象上**不能**出现公开的 ``window`` 属性（2026-09-22 真机踩坑的回归）。

        【根因回顾】pywebview 在注入 ``window.pywebview`` 之前会**递归遍历桥对象的
        公开属性**（``webview/util.py::inject_pywebview()``）：它会跳过 ``_`` 开头的
        名字，但对"非可调用、且像对象"的属性继续往里爬。过去这里叫 ``self.window``，
        于是扫描链变成 ``Window`` → ``native``（WinForms 的 .NET 窗体）
        → ``AccessibilityObject`` → ``Owner`` → ``AccessibleRole``
        → 枚举成员 ``Alert`` → ``Alert`` → …… 永不穷尽，直到
        ``RecursionError: maximum recursion depth exceeded``：终端刷出几百行
        ``Error while processing window.native.AccessibilityObject…``，
        且 GUI 线程被占住数秒（窗口刚出现时白屏/无响应，随后自己恢复）。

        实测 pywebview 6.2.1（当前最新）仍然如此 ⇒ 只能靠"名字以 ``_`` 开头"规避。
        这两条断言钉住的正是那个规避：持有窗口等状态一律用私有字段、
        公开成员只许是桥方法（方法不参与递归扫描）。
        """
        chrome = main_gui.DesktopChrome()

        assert set(vars(chrome)) == {"_window", "_maximized", "_saved_bounds"}, (
            "持有窗口/最大化状态的字段必须都是私有名"
        )
        assert [name for name in dir(chrome) if not name.startswith("_")] == [
            "begin_edge_resize",
            "close",
            "minimize",
            "toggle_maximize",
        ], "桥对象上不允许出现其它公开属性（pywebview 会递归扫描它们）"

    def test_attach_window_drives_minimize_and_close(self) -> None:
        """绑定窗口后，两个按钮方法必须真的转发到 pywebview 的窗口对象上。"""
        calls: list[str] = []

        class FakeWindow:
            def minimize(self) -> None:
                calls.append("minimize")

            def destroy(self) -> None:
                calls.append("destroy")

        chrome = main_gui.DesktopChrome()
        chrome._attach_window(FakeWindow())
        chrome.minimize()
        chrome.close()

        assert calls == ["minimize", "destroy"]


# ---------------------------------------------------------------------------
# ④′ 窗口几何纯函数（工作区收紧 / 边缘缩放）
# ---------------------------------------------------------------------------
class TestWindowGeometry:
    def test_fit_default_screen_keeps_default_size(self) -> None:
        """1080p 100% 缩放：默认 1180×860 本来就不压任务栏，不该被改小。"""
        screen, work = (1920, 1080), (0, 0, 1920, 1032)
        assert main_gui._fit_window_size(screen, work) == (1180, 860)

    def test_fit_scaled_screen_clamps_height(self) -> None:
        """125% 缩放（有效高 864、工作区底 816）：860 会压任务栏，须收到 768。

        居中于屏幕要不盖任务栏 ⇒ 窗高 ≤ 2×工作区底 − 屏高 = 2×816 − 864 = 768。
        """
        screen, work = (1536, 864), (0, 0, 1536, 816)
        assert main_gui._fit_window_size(screen, work) == (1180, 768)

    def test_fit_side_taskbar_clamps_width(self) -> None:
        """任务栏在左侧：只约束宽度，高度不动。"""
        screen, work = (1920, 1080), (48, 0, 1920, 1080)
        width, height = main_gui._fit_window_size(screen, work)
        assert width == 1180  # 2×1920−1920 远大于默认值
        assert height == 860

    def test_fit_query_failure_falls_back_to_defaults(self) -> None:
        assert main_gui._fit_window_size(None, None) == (
            main_gui.WINDOW_WIDTH,
            main_gui.WINDOW_HEIGHT,
        )

    def test_fit_tiny_screen_floors_at_min_size(self) -> None:
        """极小屏：宁可轻微压线也不能开不出可用的窗口（保底 min_size）。"""
        screen, work = (800, 600), (0, 0, 800, 552)
        width, height = main_gui._fit_window_size(screen, work)
        assert width >= main_gui.WINDOW_MIN_SIZE[0]
        assert height >= main_gui.WINDOW_MIN_SIZE[1]

    def test_edge_geometry_east_and_south(self) -> None:
        # 东缘右拉 80：右边界跟着走，左上不动
        assert main_gui._edge_geometry("e", 10, 20, 400, 300, 80, 0, 420, 640) == (
            10,
            20,
            480,
            300,
        )
        # 南缘下拉 -100（往上推）：高度钳到最小值
        assert main_gui._edge_geometry("s", 10, 20, 400, 300, 0, -500, 420, 640) == (
            10,
            20,
            400,
            640,
        )

    def test_edge_geometry_west_moves_left_edge(self) -> None:
        """西缘右拉 60：宽度变小、左边界右移（右边界钉住）。"""
        assert main_gui._edge_geometry("w", 10, 20, 400, 300, 60, 0, 100, 100) == (
            70,
            20,
            340,
            300,
        )

    def test_edge_geometry_corner_and_clamp(self) -> None:
        # 东北角：右拉 50、上推 30 ⇒ 宽 +50、高 +30、top 上移
        assert main_gui._edge_geometry("ne", 10, 20, 400, 300, 50, -30, 100, 100) == (
            10,
            -10,
            450,
            330,
        )
        # 西缘往回拽过头：宽度钳到最小，位置联动回弹
        assert main_gui._edge_geometry("w", 10, 20, 400, 300, 500, 0, 100, 100) == (
            310,
            20,
            100,
            300,
        )


# ---------------------------------------------------------------------------
# ⑤ urls 提示
# ---------------------------------------------------------------------------
class TestUrlHints:
    def test_loopback_hint_mentions_local_only(self) -> None:
        lines = web_server.describe_urls("127.0.0.1", 8765, None)
        assert any("127.0.0.1:8765" in line for line in lines)
        assert any("只允许本机访问" in line for line in lines)

    def test_lan_hint_includes_token_when_set(self) -> None:
        lines = web_server.describe_urls("0.0.0.0", 8765, "abc123")
        joined = "\n".join(lines)
        assert "?token=abc123" in joined

    def test_lan_hint_warns_without_token(self) -> None:
        lines = web_server.describe_urls("0.0.0.0", 8765, None)
        assert any("任何人" in line for line in lines)


# ---------------------------------------------------------------------------
# ⑥ CLR 运行时选择（2026-10-03 真机踩坑的回归）
# ---------------------------------------------------------------------------
class TestClrRuntime:
    """钉住「Windows 上必须让 pythonnet 走 .NET Core」这条结论。

    【为什么值得单测】这条判断错了的表现是**窗口直接起不来**：控制台抛出
    ``RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize``，而它只在
    「装了 pywebview」的机器上才暴露 —— 纯逻辑测试是这个"缺环境就整块不可用"的
    开关唯一能在自动化里守住的地方（真机现象见 notes/web_layer.md §十二补 4）。
    """

    def test_windows_with_coreclr_selects_coreclr(self) -> None:
        """本机就是这种组合：Windows + 装了 .NET 8/9/10 桌面运行时。"""
        assert (
            main_gui.choose_pythonnet_runtime(
                platform="win32", already_set=False, has_coreclr=True
            )
            == "coreclr"
        )

    def test_user_override_is_respected(self) -> None:
        """用户显式设了 PYTHONNET_RUNTIME（比如就是要 netfx）→ 一律不覆盖。"""
        assert (
            main_gui.choose_pythonnet_runtime(
                platform="win32", already_set=True, has_coreclr=True
            )
            is None
        )

    def test_non_windows_is_untouched(self) -> None:
        """Linux/macOS 上默认 Mono 才是对的，不能替用户改成 coreclr。"""
        assert (
            main_gui.choose_pythonnet_runtime(
                platform="linux", already_set=False, has_coreclr=True
            )
            is None
        )

    def test_windows_without_coreclr_is_untouched(self) -> None:
        """没装 .NET Core 时不能乱指定 —— 否则连本来可用的 netfx 也被堵死。"""
        assert (
            main_gui.choose_pythonnet_runtime(
                platform="win32", already_set=False, has_coreclr=False
            )
            is None
        )

    def test_find_desktop_runtime_requires_both_frameworks(self, tmp_path) -> None:
        """两个共享框架缺一不可：只查 NETCore.App 会把"窗体起不来"误判成可用。"""
        shared = tmp_path / "shared"
        (shared / "Microsoft.NETCore.App" / "8.0.30").mkdir(parents=True)
        (shared / "Microsoft.WindowsDesktop.App" / "8.0.30").mkdir(parents=True)
        assert main_gui.find_desktop_runtime(roots=[tmp_path]) == ("8.0.30", "8.0.30")

        # 只有 .NET Core、没有 WindowsDesktop ⇒ 判定不可用（WinForms 建不出来）
        (shared / "Microsoft.WindowsDesktop.App" / "8.0.30").rmdir()
        assert main_gui.find_desktop_runtime(roots=[tmp_path]) is None

    def test_find_desktop_runtime_rejects_old_versions(self, tmp_path) -> None:
        """只有 .NET 5 及以下不算：pythonnet 3.x 要求 ≥ 6（.NET Framework 4.x 更不算）。"""
        shared = tmp_path / "shared"
        (shared / "Microsoft.NETCore.App" / "5.0.17").mkdir(parents=True)
        (shared / "Microsoft.WindowsDesktop.App" / "5.0.17").mkdir(parents=True)
        assert main_gui.find_desktop_runtime(roots=[tmp_path]) is None

    def test_find_desktop_runtime_picks_highest_numeric_version(self, tmp_path) -> None:
        """版本按**数值**挑最高：10.0.11 比 8.0.30 新（字典序会把 8 当最高）。"""
        shared = tmp_path / "shared"
        for version in ("8.0.30", "10.0.11"):
            (shared / "Microsoft.NETCore.App" / version).mkdir(parents=True)
            (shared / "Microsoft.WindowsDesktop.App" / version).mkdir(parents=True)
        assert main_gui.find_desktop_runtime(roots=[tmp_path]) == ("10.0.11", "10.0.11")

    def test_write_runtime_config_lists_both_frameworks(self, tmp_path) -> None:
        """runtimeconfig 里必须有 WindowsDesktop（否则 System.Windows.Forms 找不到）。"""
        target = tmp_path / "sub" / "rc.json"  # 顺带验证父目录会自动创建
        assert main_gui.write_clr_runtime_config(target, "10.0.11", "8.0.30") == target

        options = json.loads(target.read_text(encoding="utf-8"))["runtimeOptions"]
        assert options["tfm"] == "net10.0"
        assert options["frameworks"] == [
            {"name": "Microsoft.WindowsDesktop.App", "version": "10.0.11"},
            {"name": "Microsoft.NETCore.App", "version": "8.0.30"},
        ]

    def test_prepare_clr_runtime_sets_env_and_loads_pythonnet(
        self, monkeypatch, tmp_path
    ) -> None:
        """完整链路：设环境变量 → 写配置 → 用 pythonnet.load 指认它。"""
        monkeypatch.delenv(main_gui.CLR_RUNTIME_ENV, raising=False)
        monkeypatch.setattr(
            main_gui, "find_desktop_runtime", lambda roots=None: ("10.0.11", "10.0.11")
        )
        written = tmp_path / "rc.json"
        monkeypatch.setattr(
            main_gui, "write_clr_runtime_config", lambda target, desktop, core: written
        )
        calls: list[tuple[str, str]] = []
        fake_pythonnet = SimpleNamespace(
            load=lambda runtime, **kwargs: calls.append((runtime, kwargs["runtime_config"]))
        )
        monkeypatch.setitem(sys.modules, "pythonnet", fake_pythonnet)

        assert main_gui.prepare_clr_runtime(platform="win32") == written
        assert os.environ[main_gui.CLR_RUNTIME_ENV] == "coreclr"
        assert calls == [("coreclr", str(written))]

    def test_prepare_clr_runtime_respects_user_override(self, monkeypatch) -> None:
        """用户自己指定了 PYTHONNET_RUNTIME → 一个字节都不许动。"""
        monkeypatch.setenv(main_gui.CLR_RUNTIME_ENV, "netfx")
        monkeypatch.setattr(
            main_gui, "find_desktop_runtime", lambda roots=None: ("10.0.11", "10.0.11")
        )
        assert main_gui.prepare_clr_runtime(platform="win32") is None
        assert os.environ[main_gui.CLR_RUNTIME_ENV] == "netfx"

    def test_prepare_clr_runtime_skips_non_windows(self, monkeypatch) -> None:
        monkeypatch.delenv(main_gui.CLR_RUNTIME_ENV, raising=False)
        monkeypatch.setattr(
            main_gui, "find_desktop_runtime", lambda roots=None: ("10.0.11", "10.0.11")
        )
        assert main_gui.prepare_clr_runtime(platform="linux") is None
        assert main_gui.CLR_RUNTIME_ENV not in os.environ

    def test_prepare_clr_runtime_skips_without_desktop_runtime(self, monkeypatch) -> None:
        """没装 .NET 桌面运行时时什么都别做（留着 netfx 默认那条路，别堵死）。"""
        monkeypatch.delenv(main_gui.CLR_RUNTIME_ENV, raising=False)
        monkeypatch.setattr(main_gui, "find_desktop_runtime", lambda roots=None: None)
        assert main_gui.prepare_clr_runtime(platform="win32") is None
        assert main_gui.CLR_RUNTIME_ENV not in os.environ


    def test_systemevents_path_from_tpa(self) -> None:
        """从 TPA（可解析程序集清单）里挑出 SystemEvents 的绝对路径。"""
        desktop_dll = (
            r"C:\Program Files\dotnet\shared\Microsoft.WindowsDesktop.App\10.0.11"
            r"\Microsoft.Win32.SystemEvents.dll"
        )
        listing = os.pathsep.join(
            [
                r"C:\Program Files\dotnet\shared\Microsoft.NETCore.App\10.0.11\System.Private.CoreLib.dll",
                desktop_dll,
            ]
        )
        assert main_gui._systemevents_path(listing) == desktop_dll

    def test_systemevents_path_missing(self) -> None:
        """清单里没有就返回 None（不能瞎猜路径）。"""
        assert main_gui._systemevents_path("") is None
        assert main_gui._systemevents_path(r"C:\somewhere\Other.dll") is None


if __name__ == "__main__":  # 支持 `python -m tests.test_main_gui`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
