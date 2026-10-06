"""运行中断：用户点「停止执行」时的那条唯一通路（2026-10-04 新增）。

【为什么要有这个模块】
在此之前 ``/api/stop`` 是一个**故意返回 501** 的占位接口 —— 当时的判断是
"领取类请求发出后无法撤销，给一个 Stop 按钮等于给人错觉"。这个判断只对了一半：

- 一次**请求**确实撤不回（服务端可能已经发过奖）；
- 但一次**运行**是由很多个请求串起来的，真正让人难受的恰恰是"跑不完的部分"：
  几十关主线、几十场扫荡、连打 N 场的荣耀之巅 —— 中途想停却只能等到跑完。

所以这里做的是**协作式取消**：给当前运行发一个信号，任务在安全的边界
（任务与任务之间、每一关/每一场之间、以及所有等待处）检查它并自己停下来。
已发出的请求照旧让它发完 —— 那是不可逆的部分，本模块**不假装**能撤回来。

【为什么是"全局当前令牌"而不是层层传参】
同一时刻只有一次运行在跑（``webapi/state.py`` 的运行闸门保证），
而"要中断它"这件事是由 HTTP 请求（``/api/stop``）发起的 —— 那个请求拿不到
worker 线程里的任何对象。用一个模块级的"当前令牌"，STOP 请求才能找到它。
命令行（``python main.py run …``）永远不装令牌，于是取到的是
:data:`_IDLE`（永不取消），行为与旧版完全一致。

【线程安全】
``threading.Event`` 本身是线程安全的；令牌的"安装/摘除"用一把锁保护，
避免"旧运行的令牌还没摘、新运行就装上"这种错配。
"""

from __future__ import annotations

import threading
import time
from typing import Final

#: 默认中断原因（用户从界面上点的那一下）。
DEFAULT_REASON: Final[str] = "用户点了「停止执行」"


class TaskAborted(RuntimeError):
    """任务被用户中断。

    【为什么继承 RuntimeError 而不是自己造一个 Exception 子类】
    它要能被 ``services/runner.py`` 的兜底 ``except Exception`` 正常拦下
    （一个任务的中止不该让整批崩掉），同时又要**明显有别于**
    ``CherrytaleError``（那是"服务端回了错误码"，这不是）。
    """


class CancelToken:
    """一次运行对应的中断信号。

    【协作式，不是抢占式】
     Python 没有安全的"杀线程"手段：强行让线程消失会让 socket / HTTP 连接
     停在半成品状态（服务端已经收了一半的包）。所以这里只提供
     "有没有人喊停"这个标记，由任务在**自己知道安全**的时刻去检查。
    """

    def __init__(self, reason: str = DEFAULT_REASON) -> None:
        self._event = threading.Event()
        self._reason = reason

    @property
    def requested(self) -> bool:
        """是否已经喊停。"""
        return self._event.is_set()

    @property
    def reason(self) -> str:
        """喊停的原因（写进结果消息，用户因此知道"是被我停掉的"）。"""
        return self._reason

    def request(self, reason: str = DEFAULT_REASON) -> None:
        """喊停（幂等：重复调用没有副作用）。"""
        self._reason = reason
        self._event.set()

    def raise_if_requested(self) -> None:
        """已经喊停就抛出 :class:`TaskAborted`。"""
        if self._event.is_set():
            raise TaskAborted(self._reason)

    def wait(self, seconds: float) -> bool:
        """可被打断的等待。

        :param seconds: 要等多久。
        :return: ``True`` = 等待期间被喊停（调用方应当立刻退出循环）；
            ``False`` = 正常等到头。

        【为什么不用 ``time.sleep``】
        ``time.sleep`` 期间线程对"喊停"毫无反应：荣耀之巅两场之间要等 15 秒，
        用户在第 2 秒点了停止，却要等满 15 秒才停 —— 那和"停不掉"没区别。
        """
        return self._event.wait(seconds)

    def __repr__(self) -> str:  # pragma: no cover - 排障用
        return f"CancelToken(requested={self.requested}, reason={self.reason!r})"


class _IdleToken(CancelToken):
    """永远不会被取消的令牌（命令行 / 没有运行时的默认值）。"""

    def request(self, reason: str = DEFAULT_REASON) -> None:
        # 刻意什么都不做：没有运行可停的时候，喊停不该留下任何痕迹，
        # 否则下一次运行一启动就会立刻被"上一笔账"中止。
        return


#: 没有运行时的占位令牌（见类说明）。
_IDLE: Final[CancelToken] = _IdleToken()

_current: CancelToken = _IDLE
_lock: Final[threading.Lock] = threading.Lock()


def current_token() -> CancelToken:
    """当前运行的中断令牌；没有运行在跑时返回 :data:`_IDLE`。"""
    with _lock:
        return _current


def install_token(reason: str = DEFAULT_REASON) -> CancelToken:
    """为一次新的运行装上令牌（由 worker 线程在开跑前调用）。"""
    token = CancelToken(reason)
    with _lock:
        # noqa 里的全局变量是刻意的：见模块文档"为什么是全局当前令牌"
        global _current  # noqa: PLW0603
        _current = token
    return token


def uninstall_token(token: CancelToken) -> None:
    """运行结束时摘掉令牌。

    【为什么要比对 `token` 再摘】
    万一新运行已经装上了自己的令牌（例如收尾慢了一拍），
    这里不该把新的那个摘掉 —— 那会让新运行"点停止毫无反应"。
    """
    with _lock:
        global _current  # noqa: PLW0603
        if _current is token:
            _current = _IDLE


def request_cancel(reason: str = DEFAULT_REASON) -> CancelToken | None:
    """给当前运行喊停（``/api/stop`` 的唯一动作）。

    :return: 被喊停的令牌；没有运行在跑时返回 ``None``
        （调用方据此答复"当前没有可中断的运行"）。
    """
    with _lock:
        token = _current
    if token is _IDLE:
        return None
    token.request(reason)
    return token


def raise_if_cancelled() -> None:
    """任务边界的统一检查点：被喊停就抛 :class:`TaskAborted`。

    用法（在任务的循环里每一轮都问一次）::

        for section in sections:
            raise_if_cancelled()      # ← 这里
            do_one(section)
    """
    current_token().raise_if_requested()


def sleep_interruptible(seconds: float) -> None:
    """可中断的等待：期间被喊停就抛 :class:`TaskAborted`。

    :param seconds: 要等几秒（``<= 0`` 时也会检查一次是否已被喊停）。

    任务里所有"模拟真人操作节奏"的延时都应当换成它 ——
    那些延时恰恰是最容易被用户喊停的时间窗口。
    """
    token = current_token()
    if seconds <= 0:
        token.raise_if_requested()
        return
    # 切片等待：每 0.2 秒醒一次看有没有被喊停。
    # 为什么是 0.2 秒：既让人感觉"点了立刻停"，又不至于在几十秒的等待里
    # 空转几千次（一次 Event.wait 的代价可以忽略）。
    deadline = time.monotonic() + float(seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if token.wait(min(0.2, remaining)):
            token.raise_if_requested()
