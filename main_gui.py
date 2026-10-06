"""PC 独立窗口：用 pywebview 开一个原生窗口，展示**同一个**网页。

用法::

    python main_gui.py                        # 无边框窗口（默认，自绘关闭/最小化按钮）
    python main_gui.py --no-frameless          # 用系统标题栏的普通窗口（更稳）
    python main_gui.py --host 0.0.0.0 --token 口令   # 顺便让手机也能访问
    python main_gui.py --no-window             # 只起服务、不开窗口（等价于 web_server.py）

【为什么不把 HTML 直接塞进窗口，而是先起本地服务】
页面要用 ``/api/*``（任务清单、状态、日志）。pywebview 也有 JS↔Python 桥，
但走那条路等于"为桌面端再写一套后端通道"，网页版与窗口版的行为会立刻分叉。
起本地服务 + 窗口指向 ``http://127.0.0.1:port`` 之后，两个入口**共用同一份代码**：
你在浏览器里看到的、在窗口里看到的、手机上看到的，都是同一个页面、同一套逻辑。

【关闭窗口必须把服务也停掉】
uvicorn 跑在后台线程里。窗口关闭后主线程返回，如果不显式设置
``server.should_exit``，进程会留在后台 —— 用户看到的现象是
"窗口明明关了，下次启动却报端口被占用"。所以这里用 ``finally`` 保证停止。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Final, Sequence
from urllib.parse import quote

import config
from webapi import app as webapp
from webapi import logstream

_LOGGER: Final[logging.Logger] = logging.getLogger("main")

#: 窗口默认尺寸（1180×860 在 1080p 屏上不会顶到边，留出拖动与任务栏空间）。
WINDOW_WIDTH: Final[int] = 1180
WINDOW_HEIGHT: Final[int] = 860

#: 窗口最小尺寸：再小就无法容纳参数表单（手机上另有网页版，不受此限制）。
WINDOW_MIN_SIZE: Final[tuple[int, int]] = (420, 640)

#: 暗色背景：窗口出现到页面绘制完成之间会露出底色，用白色会闪一下眼睛。
WINDOW_BG: Final[str] = "#0d0f14"

#: Edge / Chrome 的常见安装位置（回退方案用；``--app`` 模式开窗口）。
_BROWSER_CANDIDATES: Final[tuple[str, ...]] = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

#: 无边框窗口的自绘控制按钮。
#:
#: 【为什么由桌面壳注入，而不是写进 web/】
#: 网页版（手机 / 浏览器）没有"窗口"这个概念。把关闭按钮写进页面，
#: 手机端就会出现一个点了没反应的按钮。由桌面壳在页面加载完成后注入，
#: 职责就干净了：**网页只关心网页，窗口装饰归窗口**。
#:
#: 按钮调用 ``window.pywebview.api.*`` —— 那是 pywebview 提供的桥，
#: 对应本文件里的 :class:`DesktopChrome`。加了存在性判断，
#: 万一桥没就绪也只是按钮无效，不会抛错影响页面。
_WINDOW_CONTROLS_JS: Final[str] = """
(() => {
  if (document.getElementById("win-controls")) return;

  const ICONS = {
    min: `<svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="2.5" y1="8" x2="13.5" y2="8"/></svg>`,
    max: `<svg class="win-icon-max" viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="2.5" width="11" height="11" rx="2"/></svg>`,
    restore: `<svg class="win-icon-restore" viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4.5" y="1.5" width="10" height="10" rx="1.5"/><path d="M1.5 4.5v10h10"/></svg>`,
    close: `<svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="3" y1="3" x2="13" y2="13"/><line x1="13" y1="3" x2="3" y2="13"/></svg>`,
  };

  const make = (type, title, handler) => {
    const btn = document.createElement("button");
    btn.innerHTML = ICONS[type];
    btn.title = title;
    btn.type = "button";
    btn.className = `iconbtn win-btn win-btn--${type}`;
    btn.setAttribute("aria-label", title);

    btn.addEventListener("click", () => {
      btn.classList.add("win-btn--active-anim");
      setTimeout(() => btn.classList.remove("win-btn--active-anim"), 260);

      if (type === "min") {
        document.documentElement.classList.add("win-minimizing");
      } else if (type === "max") {
        const isMax = btn.classList.toggle("is-maximized");
        btn.innerHTML = isMax ? ICONS.restore : ICONS.max;
        btn.title = isMax ? "还原" : "最大化";
        btn.setAttribute("aria-label", btn.title);
        document.documentElement.classList.add("win-maximizing-transition");
        setTimeout(() => {
          document.documentElement.classList.remove("win-maximizing-transition");
        }, 260);
      } else if (type === "close") {
        document.documentElement.classList.add("win-closing");
      }

      const invoke = () => {
        const api = window.pywebview && window.pywebview.api;
        if (api && typeof api[handler] === "function") {
          api[handler]();
          return true;
        }
        return false;
      };

      const delay = type === "min" ? 180 : type === "close" ? 150 : 0;
      setTimeout(() => {
        if (invoke()) return;

        btn.disabled = true;
        let tries = 0;
        const timer = setInterval(() => {
          if (invoke() || ++tries >= 20) {
            clearInterval(timer);
            btn.disabled = false;
          }
        }, 150);
      }, delay);
    });
    return btn;
  };

  window.addEventListener("focus", () => {
    if (document.documentElement.classList.contains("win-minimizing")) {
      document.documentElement.classList.remove("win-minimizing");
      document.documentElement.classList.add("win-restoring");
      setTimeout(() => {
        document.documentElement.classList.remove("win-restoring");
      }, 250);
    }
  });

  const box = document.createElement("div");
  box.id = "win-controls";
  box.appendChild(make("min", "最小化", "minimize"));
  box.appendChild(make("max", "最大化 / 还原", "toggle_maximize"));
  box.appendChild(make("close", "关闭", "close"));

  const dock = document.getElementById("win-dock");
  if (dock) {
    dock.appendChild(box);
  } else {
    box.style.cssText =
      "position:fixed;top:8px;right:10px;z-index:99;display:flex;gap:10px;align-items:center;";
    document.body.appendChild(box);
  }

  // ------------------------------------------------------------------
  // 边缘拖拽缩放把手（无边框窗口没有系统缩放边框，只能自己补）
  //
  // mousedown 时 stopPropagation 拦在 target 阶段：pywebview 的 easy_drag
  // 监听挂在 window 冒泡上，收不到这个事件就不会"一边缩放一边拖动窗口"。
  // 之后的拖拽循环全部在 Python 侧（DesktopChrome.begin_edge_resize），
  // 不逐帧走 JS 桥 —— 每次桥调用都是一次跨语言往返，逐帧调会卡。
  // ------------------------------------------------------------------
  const edges = [
    ["n", "ns-resize", "top:-3px;left:10px;right:10px;height:6px;"],
    ["s", "ns-resize", "bottom:-3px;left:10px;right:10px;height:6px;"],
    ["w", "ew-resize", "left:-3px;top:10px;bottom:10px;width:6px;"],
    ["e", "ew-resize", "right:-3px;top:10px;bottom:10px;width:6px;"],
    ["nw", "nwse-resize", "top:-3px;left:-3px;width:14px;height:14px;"],
    ["ne", "nesw-resize", "top:-3px;right:-3px;width:14px;height:14px;"],
    ["sw", "nesw-resize", "bottom:-3px;left:-3px;width:14px;height:14px;"],
    ["se", "nwse-resize", "bottom:-3px;right:-3px;width:14px;height:14px;"],
  ];
  for (const [edge, cursor, pos] of edges) {
    const handle = document.createElement("div");
    handle.className = "win-resize-edge";
    handle.dataset.edge = edge;
    handle.style.cssText = "position:fixed;z-index:98;" + pos + "cursor:" + cursor + ";";
    handle.addEventListener("mousedown", (event) => {
      event.stopPropagation();
      event.preventDefault();
      const api = window.pywebview && window.pywebview.api;
      if (api && typeof api.begin_edge_resize === "function") api.begin_edge_resize(edge);
    });
    document.body.appendChild(handle);
  }
})();
"""


# ----------------------------------------------------------------------
# 屏幕几何：工作区查询与窗口尺寸（纯函数，可单测）
# ----------------------------------------------------------------------
def _query_screen_workarea():
    """查主显示器的屏幕尺寸与工作区矩形。

    :return: ``(screen, work)``；``screen = (宽, 高)``，
        ``work = (left, top, right, bottom)``（去掉任务栏后的可用区域，
        侧边任务栏时 left/top 不为 0）。非 Windows 或查询失败 → ``(None, None)``。

    【为什么要查工作区】pywebview 把窗口摆在**屏幕**正中（不是工作区正中），
    默认 1180×860 在 125% 缩放的屏（有效高只有 ~864px）上底边正好压进
    任务栏 —— 这就是"窗口和任务栏重合"的来源。
    """
    if sys.platform != "win32":
        return None, None
    try:
        import ctypes

        class _Rect(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_long)
                for name in ("left", "top", "right", "bottom")
            ]

        user32 = ctypes.windll.user32
        screen = (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
        rect = _Rect()
        # SPI_GETWORKAREA = 0x0030：主显示器去掉任务栏后的矩形
        if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return None, None
        work = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if min(screen) <= 0 or work[2] - work[0] <= 0 or work[3] - work[1] <= 0:
            return None, None
        return screen, work
    except Exception:  # pragma: no cover - 无显示环境 / 非 Windows
        return None, None


def _fit_window_size(
    screen: tuple[int, int] | None, work: tuple[int, int, int, int] | None
) -> tuple[int, int]:
    """算"绝不压到任务栏"的初始窗口尺寸（居中于屏幕的前提下）。

    居中于屏幕的窗口整体落在工作区内需要**两个方向**同时满足：
    底边不越工作区底 ``h <= 2×work.bottom − 屏高``、
    顶边不越工作区顶 ``h <= 屏高 − 2×work.top``（任务栏在顶部时靠后者起作用；
    宽度同理）。极端小屏下保底 :data:`WINDOW_MIN_SIZE`
    （宁可轻微压线，也不能开不出一个能用的窗口）。
    """
    if screen is None or work is None:
        return WINDOW_WIDTH, WINDOW_HEIGHT
    limit_w = min(2 * work[2] - screen[0], screen[0] - 2 * work[0])
    limit_h = min(2 * work[3] - screen[1], screen[1] - 2 * work[1])
    width = max(min(WINDOW_WIDTH, limit_w), WINDOW_MIN_SIZE[0])
    height = max(min(WINDOW_HEIGHT, limit_h), WINDOW_MIN_SIZE[1])
    return width, height


def _edge_geometry(
    edge: str,
    left: int,
    top: int,
    width: int,
    height: int,
    dx: int,
    dy: int,
    min_w: int,
    min_h: int,
) -> tuple[int, int, int, int]:
    """算边缘拖拽缩放后的窗口矩形（纯函数）。

    :param edge: ``n/s/e/w`` 或其组合（``ne/nw/se/sw``）——
        字母出现在名字里就表示"这一侧跟着光标走"。
    :param dx/dy: 光标相对拖拽起点的位移（屏幕坐标）。
    :return: ``(left, top, width, height)``。西/北缘缩放时位置与尺寸
        联动（对侧边界钉住不动），并钳到最小尺寸。
    """
    new_w, new_h = width, height
    if "e" in edge:
        new_w = max(min_w, width + dx)
    if "s" in edge:
        new_h = max(min_h, height + dy)
    if "w" in edge:
        new_w = max(min_w, width - dx)
        left = left + (width - new_w)  # 右边界钉住：宽度被钳制时位置跟着回弹
    if "n" in edge:
        new_h = max(min_h, height - dy)
        top = top + (height - new_h)
    return left, top, new_w, new_h


def _fix_point_for(edge: str):
    """边缘缩放时应固定的角（pywebview ``FixPoint`` 的位组合）。

    拖哪条边/角，对侧的角就是钉死的基准点。import 失败返回 ``None``，
    调用方退回 resize 的默认固定点（北西角）—— 只影响西/北缘的观感，
    不影响功能。"""
    try:
        from webview.window import FixPoint

        return {
            "e": FixPoint.NORTH | FixPoint.WEST,
            "s": FixPoint.NORTH | FixPoint.WEST,
            "se": FixPoint.NORTH | FixPoint.WEST,
            "w": FixPoint.NORTH | FixPoint.EAST,
            "sw": FixPoint.NORTH | FixPoint.EAST,
            "n": FixPoint.SOUTH | FixPoint.WEST,
            "ne": FixPoint.SOUTH | FixPoint.WEST,
            "nw": FixPoint.SOUTH | FixPoint.EAST,
        }[edge]
    except Exception:  # pragma: no cover - pywebview 未安装 / 版本差异
        return None


class ServerThread:
    """把 HTTP 服务跑在后台线程里（窗口关闭后由 :meth:`stop` 收尾）。

    :param application: WebApp 实例。
    :param host: 监听地址。
    :param port: 监听端口。
    """

    def __init__(self, application: object, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._server, self._app = webapp.create_server(
            host=host,
            port=port,
            application=application if isinstance(application, webapp.WebApp) else None,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="cherrytale-http", daemon=True
        )

    @property
    def is_alive(self) -> bool:
        """服务线程是否还在跑。"""
        return self._thread.is_alive()

    def start(self, *, timeout: float = 30.0) -> None:
        """启动并监听连接。"""
        self._thread.start()

    def stop(self) -> None:
        """请求退出并等线程结束（最多 15 秒）。"""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=15)


class DesktopChrome:
    """暴露给页面的最小桌面能力（只给无边框模式用）。

    pywebview 会把本对象的**公开方法**挂到 ``window.pywebview.api`` 上，
    供注入的按钮调用（见 :data:`_WINDOW_CONTROLS_JS`）。

    【持有窗口的字段为什么必须叫 ``_window``（2026-09-22 修，别改回 ``window``）】

    pywebview 在注入 ``window.pywebview`` 之前，会**递归遍历本对象的公开属性**
    来生成 JS 可调的 API 表（``webview/util.py::inject_pywebview()``）：

    * 它会跳过 ``_`` 开头的名字；
    * 但对"非可调用、且像对象"的属性会**继续往里爬**（``get_functions()``）。

    于是过去那个公开的 ``window`` 会让它爬进 pywebview 的 ``Window`` →
    ``native``（WinForms 的 .NET 窗体对象）→ ``AccessibilityObject`` → ``Owner``
    → ``AccessibleRole``（枚举值）→ 枚举成员 ``Alert`` → 又是 ``Alert`` → ……
    （pythonnet 对 .NET 枚举取同名成员会返回同一个值，因此永不穷尽），
    一路撞到 Python 的递归上限，打印出几百个 ``.Alert`` 的
    ``RecursionError: maximum recursion depth exceeded``，同时把 GUI 线程
    卡住数秒（表现是"窗口刚出来时白屏/无响应，随后自己恢复"）。

    实测 pywebview **6.2.1（当前最新版）仍然如此**，升级无效；真正的规避就是
    让扫描在第一层就跳过它 —— 即下面这个 ``_window``。
    页面要用的 ``minimize`` / ``close`` 等是**方法**，不参与递归，照旧被收集。
    """

    def __init__(self) -> None:
        self._window: Any = None
        # 最大化状态由**自己**维护（见 toggle_maximize：不用系统最大化）
        self._maximized: bool = False
        self._saved_bounds: tuple[int, int, int, int] | None = None

    def _attach_window(self, window: Any) -> None:
        """绑定 pywebview 的窗口对象（由 :func:`run_window` 在创建窗口后调用）。

        :param window: ``webview.create_window()`` 返回的窗口。

        【为什么方法名也以 ``_`` 开头】
        它是给 Python 侧用的，不该出现在页面的 ``window.pywebview.api`` 里；
        以 ``_`` 开头会被 pywebview 的属性扫描跳过 —— 与字段同样的道理。
        """
        self._window = window

    def minimize(self) -> None:
        """最小化窗口。"""
        if self._window is not None:
            self._window.minimize()

    def close(self) -> None:
        """关闭窗口（主线程的 ``webview.start()`` 随即返回，服务在 finally 里停掉）。"""
        if self._window is not None:
            self._window.destroy()

    # ------------------------------------------------------------------
    # 最大化与边缘缩放（2026-09-30：无边框窗口的两大补课）
    # ------------------------------------------------------------------
    def toggle_maximize(self) -> None:
        """在工作区范围内最大化 / 还原（带平滑缓动过渡动画）。

        【为什么不用 ``window.maximize()``】WinForms 对无边框窗体
        （``FormBorderStyle.None``）的系统最大化是**铺满整屏**的 —— 任务栏
        会被盖住，这正是本次要修的"窗口和任务栏重合"。所以这里手动把窗口
        move+resize 到工作区矩形（任务栏之外），再点一次恢复原位。
        通过异步插值线程执行平滑缓动，避免生硬瞬跳。
        """
        if self._window is None:
            return
        _screen, work = _query_screen_workarea()
        if work is None:
            _LOGGER.warning("无法查询工作区，最大化不可用（可手动拉伸窗口）")
            return
        try:
            form = self._window.native  # WinForms Form（仅读当前 bounds）
            cur_bounds = (
                int(form.Left),
                int(form.Top),
                int(form.Width),
                int(form.Height),
            )
        except Exception:  # pragma: no cover - 取不到原生窗体就放弃记忆位置
            cur_bounds = (work[0] + 50, work[1] + 50, WINDOW_WIDTH, WINDOW_HEIGHT)

        if self._maximized:
            target_bounds = self._saved_bounds or (
                work[0] + 50,
                work[1] + 50,
                WINDOW_WIDTH,
                WINDOW_HEIGHT,
            )
            self._saved_bounds = None
            self._maximized = False
        else:
            self._saved_bounds = cur_bounds
            target_bounds = (
                work[0],
                work[1],
                work[2] - work[0],
                work[3] - work[1],
            )
            self._maximized = True

        threading.Thread(
            target=self._animate_bounds_transition,
            args=(cur_bounds, target_bounds),
            daemon=True,
        ).start()

    def _animate_bounds_transition(
        self,
        start_bounds: tuple[int, int, int, int],
        target_bounds: tuple[int, int, int, int],
        duration: float = 0.16,
        steps: int = 10,
    ) -> None:
        """多帧平滑缓动插值，实现窗口从普通尺寸到最大化（及还原）的平滑过渡。"""
        dt = duration / steps
        for i in range(1, steps + 1):
            progress = i / steps
            ease = 1.0 - (1.0 - progress) ** 3
            cur_l = int(start_bounds[0] + (target_bounds[0] - start_bounds[0]) * ease)
            cur_t = int(start_bounds[1] + (target_bounds[1] - start_bounds[1]) * ease)
            cur_w = int(start_bounds[2] + (target_bounds[2] - start_bounds[2]) * ease)
            cur_h = int(start_bounds[3] + (target_bounds[3] - start_bounds[3]) * ease)
            try:
                if self._window is not None:
                    self._window.move(cur_l, cur_t)
                    self._window.resize(cur_w, cur_h)
            except Exception:
                break
            time.sleep(dt)

    def begin_edge_resize(self, edge: str) -> None:
        """边缘拖拽缩放入口（页面注入把手 mousedown 时调用，见 _WINDOW_CONTROLS_JS）。

        立刻返回、另开线程跑拖拽循环（:meth:`_edge_resize_loop`）——
        桥调用若被轮询占住，页面其它 JS↔Python 通信会被堵住。
        最大化状态下忽略（先把窗口还原再拉伸）。
        """
        if self._window is None or self._maximized:
            return
        if edge not in {"n", "s", "e", "w", "ne", "nw", "se", "sw"}:
            return
        threading.Thread(
            target=self._edge_resize_loop, args=(edge,), daemon=True
        ).start()

    def _edge_resize_loop(self, edge: str) -> None:
        """拖拽循环：Win32 ``GetCursorPos`` 轮询光标直到左键松开。

        几何计算在 :func:`_edge_geometry`（纯函数），落窗走
        ``window.move`` + ``window.resize``（pywebview 内部转 UI 线程，
        从本线程调用是安全的；直接改 WinForms 控件反而会跨线程崩）。
        """
        try:
            import ctypes

            form = self._window.native
            start = (
                int(form.Left),
                int(form.Top),
                int(form.Width),
                int(form.Height),
            )
        except Exception:  # pragma: no cover - 取不到原生窗体就放弃
            return

        class _Point(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        user32 = ctypes.windll.user32
        point = _Point()
        user32.GetCursorPos(ctypes.byref(point))
        start_x, start_y = point.x, point.y
        fix_point = _fix_point_for(edge)

        # VK_LBUTTON = 0x01；GetAsyncKeyState 最高位 = 当前按下
        while user32.GetAsyncKeyState(0x01) & 0x8000:
            user32.GetCursorPos(ctypes.byref(point))
            dx, dy = point.x - start_x, point.y - start_y
            left, top, width, height = _edge_geometry(
                edge, *start, dx, dy, *WINDOW_MIN_SIZE
            )
            try:
                if (left, top) != (start[0], start[1]):
                    self._window.move(left, top)
                if (width, height) != (start[2], start[3]):
                    if fix_point is not None:
                        self._window.resize(width, height, fix_point)
                    else:  # pragma: no cover - 老版本 pywebview 的退化路径
                        self._window.resize(width, height)
            except Exception:  # pragma: no cover - 窗口正在关闭等
                break
            time.sleep(0.016)  # ~60fps：人眼跟手，又不至于把 CPU 跑满


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="main_gui.py",
        description=(
            "米娅小助手的桌面窗口版：内部起一个仅本机可访问的服务，"
            "再用 pywebview 打开同一个页面。"
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument(
        "--token",
        default=None,
        metavar="口令",
        help="访问令牌；用 --host 暴露到局域网时**必须**给出（建议用字母数字）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出 DEBUG 级别日志",
    )
    parser.add_argument(
        "--frameless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "无边框窗口（默认开启）：没有系统标题栏，改用页面右上角的自绘"
            "「最小化 / 关闭」按钮，窗口内任意空白处都可拖动"
            "（代价是多挂一个 JS 桥对象；pywebview 启动时会对它做一次属性扫描，"
            "已用私有命名的 _window 规避其递归缺陷；想完全绕开用 --no-frameless）"
        ),
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="只起服务、不开窗口（等价于 web_server.py，方便用浏览器访问或让手机连）",
    )
    return parser


# ----------------------------------------------------------------------
# CLR 运行时选择：让 pywebview 的 Windows 后端走 .NET Core，而不是 .NET Framework
# ----------------------------------------------------------------------
#: pythonnet 读这个环境变量来选 CLR：``coreclr`` = .NET 6+、``netfx`` = .NET Framework 4.x、
#: ``mono`` = Mono。**必须在 `import clr` 之前设好** —— pywebview 的 Windows 后端
#: 是在 `webview.start()` 里才第一次 `import clr`。
CLR_RUNTIME_ENV: Final[str] = "PYTHONNET_RUNTIME"

#: Windows 上需要**同时**存在的两种 .NET「共享框架」：
#:
#: * ``Microsoft.NETCore.App`` —— pythonnet 的 ``Python.Runtime.dll``
#:   （一个 .NETStandard2.0 程序集）要靠它加载；
#: * ``Microsoft.WindowsDesktop.App`` —— pywebview 的窗口本身是 WinForms 窗体，
#:   ``System.Windows.Forms`` 在这个框架里。
_DOTNET_FRAMEWORKS: Final[tuple[str, ...]] = (
    "Microsoft.NETCore.App",
    "Microsoft.WindowsDesktop.App",
)

#: pythonnet 3.x 要求的最低 .NET 大版本（.NET 5 之后统一叫 ".NET N"，6 以前不满足）。
_MIN_DOTNET_MAJOR: Final[int] = 6


def _dotnet_roots() -> list[Path]:
    """列出本机可能的 dotnet 安装根目录（只查文件系统，不跑子进程）。

    :return: 真实存在的目录，按"最可能是主安装"排序并去重。

    【为什么不跑 ``dotnet --list-runtimes``】
    那是启动一个进程（本机实测 ~1s 且依赖 PATH）；窗口启动路径上不该有这种开销。
    直接看 ``<root>/shared/<框架>/<版本>/`` 这些文件夹，既快又不依赖 dotnet CLI
    是否在 PATH 上（Windows 上装了运行时不一定装了 CLI）。
    """
    candidates: list[str | None] = [
        os.environ.get("DOTNET_ROOT"),
        r"C:\Program Files\dotnet",
        r"C:\Program Files (x86)\dotnet",
    ]
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        # 用户级安装（如 dotnet-install.ps1 的 -InstallDir）常见落点
        candidates.append(str(Path(local_appdata) / "Microsoft" / "dotnet"))

    roots: list[Path] = []
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _numeric_version(name: str) -> tuple[int, ...]:
    """把 ``"10.0.11"`` 变成 ``(10, 0, 11)``，供**数值**排序用。

    【为什么不能直接字符串排序】字典序下 ``"10.0.11" < "8.0.30"``，
    会把 .NET 8 当成"版本最高的那个"，从而给 .NET 宿主一份要它降级的配置。
    """
    return tuple(int(part) for part in name.split(".") if part.isdigit())


def _framework_versions(root: Path, framework: str) -> list[str]:
    """列出 ``<root>/shared/<framework>/`` 下的版本号（数值升序）。

    :param root: dotnet 安装根目录。
    :param framework: 共享框架名（见 :data:`_DOTNET_FRAMEWORKS`）。
    :return: 版本字符串列表；目录不存在时为空列表。
    """
    folder = root / "shared" / framework
    if not folder.is_dir():
        return []
    return sorted(
        (child.name for child in folder.glob("*") if child.is_dir()),
        key=_numeric_version,
    )


#: pywebview 的 ``winforms.py`` 要用、却没自己 AddReference 的那个程序集。
#: 它在 .NET Core 里是独立程序集（.NET Framework 里 SystemEvents 直接住在
#: System.Windows.Forms.dll 里，所以当年不需要单独加）。
SYSTEMEVENTS_DLL: Final[str] = "Microsoft.Win32.SystemEvents.dll"


def _systemevents_path(listing: str) -> str | None:
    """从"可解析程序集清单"里挑出 SystemEvents 的绝对路径（纯函数，便于单测）。

    :param listing: ``AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES")`` 的原始值
        （一个用 ``os.pathsep`` 拼起来的长字符串）。
    :return: 命中的绝对路径；没有则 ``None``。
    """
    for entry in listing.split(os.pathsep):
        if entry.endswith(SYSTEMEVENTS_DLL):
            return entry
    return None


def _add_systemevents_assembly() -> str | None:
    """把 SystemEvents 程序集装进运行时（pywebview 的 winforms 后端需要）。

    :return: 实际 AddReference 的路径；取不到/加不上时 ``None``。

    【为什么必须补这一下（实测）】pywebview 的 ``winforms.py`` 只 AddReference 了
    ``System.Windows.Forms`` 等 4 个框架程序集，紧接着就
    ``from Microsoft.Win32 import SystemEvents``。.NET Framework 有 GAC 兜着，
    .NET Core 下 pythonnet **不会**自动把它找出来，于是报::

        ImportError: cannot import name 'SystemEvents' from 'Microsoft.Win32'

    【为什么按**绝对路径**加】按名字 ``clr.AddReference('Microsoft.Win32.SystemEvents')``
    在 .NET Core 下会以 ``AttributeError: 'NoneType' object has no attribute 'GetMethod'``
    告终（实测）；从宿主给的 TPA 清单里取路径才稳。
    """
    try:
        import clr
        from System import AppContext
    except Exception as exc:  # pragma: no cover - 没有 CLR 时才会走到
        _LOGGER.debug("无法导入 clr/System：%s", exc)
        return None

    path = _systemevents_path(str(AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES") or ""))
    if path is None:
        _LOGGER.debug("TPA 里没有 %s，跳过（pywebview 可能仍会缺 SystemEvents）", SYSTEMEVENTS_DLL)
        return None
    try:
        clr.AddReference(path)
    except Exception as exc:  # pragma: no cover - 依赖具体环境
        _LOGGER.warning("AddReference(%s) 失败：%s", path, exc)
        return None
    return path


def find_desktop_runtime(roots: Sequence[Path] | None = None) -> tuple[str, str] | None:
    """找一套能跑 pywebview 的 .NET 桌面运行时。

    :param roots: 待查的 dotnet 安装根目录；``None`` = 用 :func:`_dotnet_roots` 自动探测。
    :return: ``(WindowsDesktop 版本, NETCore 版本)``；没有合格组合时 ``None``。

    【为什么两个框架都要有（缺一不可）】
    只满足 ``Microsoft.NETCore.App``：``Python.Runtime.dll`` 能起来，但 pywebview 建
    WinForms 窗体时那句 ``clr.AddReference('System.Windows.Forms')`` 会抛
    ``FileNotFoundException``（2026-10-03 真机实测到的第二个错）；
    只满足 WindowsDesktop：连 pythonnet 自己都加载不了。少查一个，就是把"报错 A"
    换成"报错 B"。
    """
    for root in _dotnet_roots() if roots is None else roots:
        desktop = _framework_versions(root, _DOTNET_FRAMEWORKS[1])
        core = _framework_versions(root, _DOTNET_FRAMEWORKS[0])
        if not desktop or not core:
            continue
        if _numeric_version(desktop[-1])[0] < _MIN_DOTNET_MAJOR:
            continue
        if _numeric_version(core[-1])[0] < _MIN_DOTNET_MAJOR:
            continue
        return desktop[-1], core[-1]
    return None


def choose_pythonnet_runtime(
    *,
    platform: str,
    already_set: bool,
    has_coreclr: bool,
) -> str | None:
    """决定要不要替 pythonnet 指定 CLR 运行时（纯函数，便于单测）。

    :param platform: :data:`sys.platform` 的值（``"win32"`` = Windows）。
    :param already_set: 环境里是否**已经**有 ``PYTHONNET_RUNTIME``。
    :param has_coreclr: 是否装了可用的 .NET (Core) 桌面运行时。
    :return: 要写入的值（``"coreclr"``），或 ``None`` = **不干预**。

    【为什么 Windows 上必须显式指定 coreclr（本项目 2026-10-03 实测结论）】
    pythonnet 官方文档写明「Windows 默认用 .NET Framework（netfx），
    Linux/macOS 默认 Mono」。但 pythonnet 3.1.0 的 wheel 里
    ``runtime/Python.Runtime.dll`` 只有 **.NETStandard v2.0** 那一份
    （旁边是 ``Python.Runtime.deps.json`` —— .NET Core 的加载清单），
    netfx 那套加载方式在这份程序集里找不到入口点，于是报::

        Failed to resolve Python.Runtime.Loader.Initialize
        from …\\pythonnet\\runtime\\Python.Runtime.dll

    实测（``tools/probe_clr.py``，python 3.14.3 + pythonnet 3.1.0 + clr_loader 0.3.1）：
    默认 / netfx / mono 三种模式**全部 FAIL**，只有 ``PYTHONNET_RUNTIME=coreclr``
    能起来（CLR 报 10.0.11）。所以这里替用户把默认值改对。

    **用户自己设过就一律不覆盖** —— 他可能就是要用 netfx 或 Mono；
    而"没装 .NET Core"的机器上也不能乱指定，否则连 netfx 这条路都被堵死。
    """
    if already_set:
        return None
    if platform != "win32":
        return None
    if not has_coreclr:
        return None
    return "coreclr"


def write_clr_runtime_config(
    target: Path,
    desktop_version: str,
    core_version: str,
) -> Path | None:
    """写一份「把 Windows 桌面框架也装进进程」的 runtimeconfig.json。

    :param target: 写到哪个路径（建议 ``<工程根>/tmp/clr-runtimeconfig.json``，已 gitignore）。
    :param desktop_version: ``Microsoft.WindowsDesktop.App`` 的版本（如 ``"10.0.11"``）。
    :param core_version: ``Microsoft.NETCore.App`` 的版本。
    :return: 写好的路径；目标不可写（如 exe 装在只读目录）时返回 ``None``。

    【为什么非写这一份不可（2026-10-03 真机实测）】
    只设 ``PYTHONNET_RUNTIME=coreclr`` 时 pythonnet 确实能加载 CLR，但 .NET 宿主
    只装了 ``Microsoft.NETCore.App``（控制台运行时）—— pywebview 里那句
    ``clr.AddReference('System.Windows.Forms')`` 于是抛::

        System.IO.FileNotFoundException: Could not load file or assembly
        'System.Windows.Forms, Culture=neutral, PublicKeyToken=null'

    runtimeconfig 就是「告诉 .NET 宿主该把哪些共享框架装进这个进程」的清单，
    而 ``System.Windows.Forms`` 在 **Microsoft.WindowsDesktop.App** 里。
    完整实测矩阵见 ``tools/probe_clr.py``（探针里有一份等价实现 —— 刻意不 import
    本模块，好让"窗口起不来"的时候探针仍然能跑）。
    """
    payload = {
        "runtimeOptions": {
            "tfm": f"net{desktop_version.split('.', 1)[0]}.0",
            # 同大版本内允许向上滚动到本机实际装着的补丁版本
            "rollForward": "latestMinor",
            "frameworks": [
                {"name": _DOTNET_FRAMEWORKS[1], "version": desktop_version},
                {"name": _DOTNET_FRAMEWORKS[0], "version": core_version},
            ],
        }
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        _LOGGER.warning("写 runtimeconfig 失败（%s）：%s", target, exc)
        return None
    return target


def prepare_clr_runtime(*, platform: str | None = None) -> Path | None:
    """启动 pywebview 之前把 CLR 环境准备好（环境变量 + runtimeconfig + pythonnet.load）。

    :param platform: 覆盖平台判断（测试用）；``None`` = 取 :data:`sys.platform`。
    :return: 实际用到的 runtimeconfig 路径；``None`` = 没动环境，原因有三类：
        「不是 Windows」「用户自己指定了 ``PYTHONNET_RUNTIME``」「本机没有合格的
        .NET 桌面运行时」—— 三种情况都必须保持 pythonnet 的默认行为，不能硬来。

    调用时机**必须早于** ``webview.start()``：pythonnet 只在"第一次 ``import clr``"
    那一刻决定用哪个运行时、读哪份 runtimeconfig，之后再改就没用了。

    .. warning::
       **这是"必要但不充分"的一半（2026-10-03 实测）**：本函数跑通之后
       ``import clr`` 与 ``System.Windows.Forms`` 都没问题了，但 pywebview 6.2.1 的
       ``winforms.py`` 仍在**导入期**失败 —— 它的 ``OpenFolderDialog`` 类体里调用了
       .NET Framework 专有的 ``System.Windows.Forms.FileDialogNative+IFileDialog``
       （.NET Core 的 WinForms 没有这个类型）⇒ ``None.GetMethod`` 报 AttributeError。
       所以 ``main()`` 里保留了"回退浏览器 --app 窗口"这条路；将来 pywebview 修掉那处，
       本函数就是缺失的另一半。完整证据链见 ``notes/web_layer.md``「§十二补 4」。
    """
    desktop = find_desktop_runtime()
    choice = choose_pythonnet_runtime(
        platform=platform or sys.platform,
        already_set=bool(os.environ.get(CLR_RUNTIME_ENV)),
        has_coreclr=desktop is not None,
    )
    if choice is None or desktop is None:
        return None

    # ① 环境变量：兜底用（万一下面 pythonnet.load 那条路没走成，至少 CLR 能起来）
    os.environ[CLR_RUNTIME_ENV] = choice

    # ② runtimeconfig：优先工程 tmp/；写不进去（exe 安装目录只读）就退到系统临时目录
    desktop_version, core_version = desktop
    config_path = write_clr_runtime_config(
        config.PROJECT_ROOT / "tmp" / "clr-runtimeconfig.json", desktop_version, core_version
    )
    if config_path is None:
        config_path = write_clr_runtime_config(
            Path(tempfile.gettempdir()) / "cherrytale-clr-runtimeconfig.json",
            desktop_version,
            core_version,
        )
    if config_path is None:
        return None

    # ③ 明确告诉 pythonnet 用哪份配置 —— 这一步才是让 WinForms 真正可用的关键
    #    （实测：只做 ①② 仍会 FileNotFoundException）
    try:
        import pythonnet  # 由 pywebview 带来；没装 pywebview 时根本走不到这里

        pythonnet.load(choice, runtime_config=str(config_path))
    except Exception as exc:  # pragma: no cover - 依赖具体环境
        _LOGGER.warning("pythonnet.load(%s, runtime_config=…) 失败：%s", choice, exc)
        return None

    # ④ 补上 pywebview 要用、却自己没 AddReference 的 SystemEvents
    systemevents = _add_systemevents_assembly()
    _LOGGER.debug("CLR 就绪：%s + %s + SystemEvents=%s", choice, config_path, systemevents)
    return config_path


def open_browser_window(url: str) -> bool:
    """回退方案：用 Edge/Chrome 的 ``--app`` 模式开一个"像独立窗口"的浏览器。

    :param url: 要打开的地址。
    :return: 是否成功唤起了浏览器。

    【为什么选 ``--app``】
    它会开一个没有地址栏与标签栏的窗口，观感与独立窗口接近，而且**零新增依赖**
    （Edge 是 Windows 自带的）。

    【为什么必须给一个独立的 ``--user-data-dir``】
    否则命令行会"交给已在运行的 Edge 处理"，结果是**在现有窗口里开一个新标签页**，
    而不是一个新窗口 —— 那就完全不是"独立窗口"了。
    这个目录放在 ``tmp/`` 下（已在 .gitignore 里），不会污染用户的浏览配置。
    """
    profile = config.PROJECT_ROOT / "tmp" / "gui-browser-profile"
    for candidate in _BROWSER_CANDIDATES:
        if not Path(candidate).is_file():
            continue
        try:
            subprocess.Popen(
                [
                    candidate,
                    f"--app={url}",
                    f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
                    f"--user-data-dir={profile}",
                    # 【为什么必须带这两个开关（2026-10-03 白窗口事故）】
                    # 这个 user-data-dir 是**本工具专用的全新 profile**，Edge 第一次
                    # 用它启动时会走"首次运行"流程：窗口先弹出来、内容却是白的
                    # （真机实测：15:00 与 15:25 两次白窗口；而且一旦这个"卡在首次
                    # 运行里"的实例还活着，后续启动只会把 URL 转发给它，白屏会一直
                    # 复现 —— 当时进程表里找不到任何带 --app= 的进程，正是转发的特征）。
                    # 这两个开关让 Edge 跳过首次运行 / 默认浏览器检查，直接建窗口
                    # 加载页面。验收方式：删掉 tmp/gui-browser-profile 后从零启动，
                    # 窗口应直接显示界面（见 notes/web_layer.md「§十二补 5」）。
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                close_fds=True,
            )
        except OSError as exc:  # pragma: no cover - 极少见（权限/被杀软拦截）
            _LOGGER.warning("调用 %s 失败：%s", candidate, exc)
            continue
        return True

    # 一个都没找到：退回默认浏览器（会带地址栏，但至少能打开）
    return webbrowser.open(url)


def run_window(url: str, *, frameless: bool, title: str = "米娅小助手") -> None:
    """打开 pywebview 窗口（阻塞到窗口关闭）。

    :param url: 窗口打开的地址（若设了令牌，会带上 ``?token=…``）。
    :param frameless: 是否无边框。
    :raises ImportError: 没装 pywebview 时抛出（由调用方负责回退）。

    【无边框模式为什么必须 ``easy_drag=True``】
    没有系统标题栏之后窗口就"没地方可拖"，会被钉死在屏幕中央。
    ``easy_drag`` 让窗口内**非交互区域**都能拖动，同时不影响按钮与输入框的点击。

    【令牌为什么走 URL 参数】
    页面里的 JS 会把 ``?token=`` 读进 sessionStorage 并立刻把地址栏抹掉
    （见 ``web/assets/app.js``）。窗口是我们自己创建的，不存在"令牌被浏览器历史
    留存"的问题；而走 URL 能省掉"用户还得手动填一次令牌"。
    """
    # 【必须在 import webview 之前】pythonnet 只在"第一次 import clr"那一刻决定
    # 用哪个 CLR、读哪份 runtimeconfig（见 prepare_clr_runtime 的说明）
    prepare_clr_runtime()
    import webview

    # 初始尺寸按工作区收紧：pywebview 把窗口居中于**屏幕**而非工作区，
    # 默认 1180×860 在 125% 缩放的屏上底边正好压进任务栏（见 _fit_window_size）
    screen, work = _query_screen_workarea()
    width, height = _fit_window_size(screen, work)

    shell = DesktopChrome() if frameless else None
    window = webview.create_window(
        title,
        url,
        width=width,
        height=height,
        min_size=WINDOW_MIN_SIZE,
        frameless=frameless,
        easy_drag=frameless,
        background_color=WINDOW_BG,
        js_api=shell,
    )

    if shell is not None:
        # 用私有方法绑定（见 DesktopChrome 的 docstring：公开属性会让 pywebview
        # 的属性扫描爬进 .NET 对象图并递归爆栈）
        shell._attach_window(window)
        # 每次页面加载完成都重新注入窗口控制（两页跳转后 DOM 会重建，
        # 见 _wire_window_events 的说明）；必须在 start() 之前订阅
        _wire_window_events(window)
        webview.start()
    else:
        webview.start()


def _wire_window_events(window: Any) -> None:
    """订阅窗口事件（必须在 ``webview.start()`` 之前，否则会漏掉首次加载）。

    【为什么把注入挂在 loaded 事件上，而不是只在首屏注入一次（2026-10-01 修）】
    前端拆成 login.html / index.html 两页之后，登录与退出都会**真实跳转**；
    每次跳转 DOM 全部重建，一次性注入的窗口按钮与缩放把手随之消失 ——
    用户重新登录后右上角就"空了"。pywebview 在每次导航完成时都会触发
    ``events.loaded``（on_navigation_completed → inject_pywebview → set），
    处理器里的注入脚本自带 ``#win-controls`` 去重：同文档重复触发安全，
    跨导航必然重新注入。
    """
    window.events.loaded += lambda: _inject_window_controls(window)


def _inject_window_controls(window: Any) -> None:
    """给无边框窗口注入自绘的「最小化 / 最大化 / 关闭」按钮与缩放把手。

    由 :func:`_wire_window_events` 在**每次**页面加载完成后调用（脚本自身
    去重）。注入失败不影响页面本身（用户还可以用 ``Alt+F4`` 关闭窗口），
    所以这里只记日志。
    """
    try:
        window.evaluate_js(_WINDOW_CONTROLS_JS)
    except Exception:  # pragma: no cover - 依赖具体 GUI 后端，失败也无害
        _LOGGER.warning("注入窗口控制按钮失败（可用 Alt+F4 关闭窗口）")


def wait_until_stopped(server: ServerThread) -> None:
    """阻塞到服务线程结束（服务在别的线程里跑，主线程只能等）。

    :param server: 已启动的服务线程包装对象。

    .. note::
       ``Ctrl+C`` 会在这里抛出 ``KeyboardInterrupt``，由 :func:`main` 捕获后
       走统一的 ``finally`` 收尾。
    """
    while server.is_alive:
        time.sleep(0.5)


def main(argv: Sequence[str] | None = None) -> int:
    """程序主入口。

    :param argv: 命令行参数（``None`` 表示取 ``sys.argv[1:]``）。
    :return: 进程退出码（``0`` 正常 / ``2`` 参数有问题 / ``1`` 启动失败）。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # 复用命令行入口的两个工具函数：编码修复与日志格式只写一遍
    from main import force_utf8_output, setup_logging
    # 复用 web_server 的"绑定地址 / 令牌"规则：两个入口对同一件事的说法必须一致
    from web_server import LOOPBACK_HOSTS, describe_urls

    force_utf8_output()
    setup_logging(args.verbose)

    if args.host not in LOOPBACK_HOSTS and not args.token:
        print(
            "✘ 拒绝启动：--host 已经暴露到局域网，但没有给 --token。\n"
            "  这个服务能在你的账号上做真实操作；没有令牌等于同网人人可用。\n"
            "  正确用法：python main_gui.py --host 0.0.0.0 --token 自定义口令"
        )
        return 2

    stream = logstream.LogStream()
    handler = logstream.attach_to_root(stream, level=logging.INFO)
    bridge = logstream.install_stdout_bridge(stream)
    application = webapp.create_app(token=args.token, stream=stream, bridge=bridge)

    server = ServerThread(application, args.host, args.port)
    try:
        server.start()
    except OSError as exc:
        print(f"✘ 启动失败：{exc}\n  换一个端口试试：python main_gui.py --port {args.port + 1}")
        logstream.uninstall_stdout_bridge(bridge)
        logstream.detach(handler)
        return 1

    url = f"http://127.0.0.1:{args.port}/"
    if args.token:
        # 中文令牌必须编码（见 build_parser 里"建议用字母数字"的提醒）
        url += f"?token={quote(args.token)}"

    print("米娅小助手已启动（窗口与网页共用同一套任务逻辑）：")
    for line in describe_urls(args.host, args.port, args.token):
        print(line)
    # 见 web_server.py 里同一处说明：重定向到文件时 stdout 是块缓冲，不刷新会丢提示
    if sys.stdout is not None and hasattr(sys.stdout, "flush"):
        sys.stdout.flush()


    try:
        if args.no_window:
            print("  （--no-window：只起服务，用浏览器打开上面的地址即可）")
            wait_until_stopped(server)
        else:
            try:
                run_window(url, frameless=args.frameless)
            except ImportError:
                print(
                    "提示：未安装 pywebview，改用 Edge/Chrome 的 --app 模式开独立窗口。\n"
                    "     想用原生窗口：python -m pip install pywebview==6.2.1"
                )
                if not open_browser_window(url):
                    print(f"✘ 也没能唤起浏览器，请手动打开：{url}")
                wait_until_stopped(server)
            except Exception as exc:
                # 【为什么 catch 到 Exception（2026-10-03）】pywebview 的 Windows 后端
                # 在初始化期可能抛出三种长相不同的异常：pythonnet 的 RuntimeError、
                # pywebview 自己的 WebViewException、以及底层的 .NET 异常。
                # 对使用者来说它们是同一件事 —— "原生窗口没起来"。所以这里统一收口：
                # 记一条带堆栈的日志（排障用），给一句人话，然后退回浏览器 --app 窗口
                # （服务已经在跑，功能一个都不少；抛堆栈只会让初学者卡住）。
                _LOGGER.exception("pywebview 原生窗口初始化失败")
                print(
                    "提示：原生窗口没起来，改用 Edge/Chrome 的 --app 模式。\n"
                    f"      底层报错：{exc}\n"
                    "      这不是本工具的 bug：pywebview 6.2.1 的 WinForms 后端是按 .NET\n"
                    "      Framework 写的（用到 FileDialogNative+IFileDialog），而 pythonnet\n"
                    "      3.1.0（唯一支持 Python 3.14 的版本）只带 .NET Core 程序集。\n"
                    "      诊断/证据：.venv/Scripts/python.exe tools/probe_clr.py\n"
                    "      完整定位见 notes/web_layer.md「§十二补 4」"
                )
                if not open_browser_window(url):
                    print(f"✘ 也没能唤起浏览器，请手动打开：{url}")
                wait_until_stopped(server)
    except KeyboardInterrupt:
        print("已停止。")
    finally:
        # 关窗口 = 停服务：不然进程会留在后台占着端口（见模块文档的说明）
        server.stop()
        logstream.uninstall_stdout_bridge(bridge)
        logstream.detach(handler)
    return 0


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())

