"""运行中断令牌（``services/cancellation.py``）。

跑法::

    python -m pytest tests/test_services_cancellation.py -q
"""

from __future__ import annotations

import threading
import time

import pytest

from services import cancellation


def test_idle_token_never_cancels() -> None:
    """命令行 / 没有运行时：取消检查必须是空操作。"""
    cancellation.raise_if_cancelled()  # 不该抛
    assert cancellation.current_token().requested is False


def test_request_then_raise() -> None:
    token = cancellation.install_token("测试")
    try:
        assert cancellation.current_token() is token
        cancellation.request_cancel("用户点了停止执行")
        assert token.requested is True
        with pytest.raises(cancellation.TaskAborted):
            cancellation.raise_if_cancelled()
    finally:
        cancellation.uninstall_token(token)
    # 摘掉之后回到"永不取消"
    assert cancellation.current_token().requested is False


def test_request_cancel_without_run_returns_none() -> None:
    """没有运行在跑时，/api/stop 拿到的是 None（界面据此说"没什么可停的"）。"""
    assert cancellation.request_cancel() is None


def test_sleep_interruptible_returns_immediately_when_cancelled() -> None:
    token = cancellation.install_token()
    try:
        def cancel_later() -> None:
            time.sleep(0.05)
            token.request()

        threading.Thread(target=cancel_later, daemon=True).start()
        started = time.monotonic()
        with pytest.raises(cancellation.TaskAborted):
            cancellation.sleep_interruptible(30)
        # 30 秒的等待，被中断后应当远早于它结束
        assert time.monotonic() - started < 2
    finally:
        cancellation.uninstall_token(token)


def test_sleep_interruptible_completes_when_not_cancelled() -> None:
    started = time.monotonic()
    cancellation.sleep_interruptible(0.25)
    assert time.monotonic() - started >= 0.2


def test_new_run_gets_a_fresh_token() -> None:
    """上一批被中断的痕迹绝不能带进下一批（否则新的一启动就被自己停掉）。"""
    first = cancellation.install_token()
    first.request()
    cancellation.uninstall_token(first)

    second = cancellation.install_token()
    try:
        assert second.requested is False
    finally:
        cancellation.uninstall_token(second)


def test_uninstall_only_removes_own_token() -> None:
    """收尾慢一拍时，不能把新运行刚装上的令牌给摘了。"""
    first = cancellation.install_token()
    second = cancellation.install_token()
    cancellation.uninstall_token(first)
    assert cancellation.current_token() is second
    cancellation.uninstall_token(second)
    assert cancellation.current_token().requested is False
