"""会话被挤（SessionKickedError）的自动恢复测试 —— 全程离线。

【场景】同一个账号在另一台设备上登录，服务端会把旧设备的会话令牌顶掉
（99004 code=-5 "token is Invalid"，2026-10-02 实测定性）。被挤的一方
下一次发包就会撞上 :class:`SessionKickedError`。

【期望行为】runner 捕获后：用保存的账号免密重登一次（login 任务会把
新会话写回同一个 ``GameSession`` 对象）→ 自动重试当前任务 → 对使用者
透明，最终结果是成功。

【离线保证】``create_task`` 与 ``AccountStore`` 都被替换成桩：
任务不发任何网络请求，账号库不读真实文件。

运行方式::

    python -m pytest tests/test_session_kick.py -q
"""

from __future__ import annotations

import pytest

from client.exceptions import SessionKickedError
from client.session import GameSession
from models.session_state import GameSessionState
from services import runner
from tasks.base import TaskResult


UID = "ER_test_user_0000000000000000000000000000"


def _state(token: str) -> GameSessionState:
    return GameSessionState(
        token=token,
        player_id=1,
        player_name="测试角色",
        time_stamp_token=0,
        account_id=1,
        server_id=113,
        server_name="测试服",
        platform_user_id=UID,
        obtained_at=0.0,
    )


class _FakeAccount:
    account = "tester@example.com"

    @staticmethod
    def is_expired() -> bool:
        return False

    @staticmethod
    def can_refresh() -> bool:
        return True


class _FakeAccountStore:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def find_account(self, selector: str) -> _FakeAccount | None:
        return _FakeAccount() if selector == UID else None


def test_kicked_session_auto_relogins_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第一次执行被踢 → 自动免密重登 → 重试成功，且全程只有一次重试。"""
    calls = {"business": 0, "login": 0}

    class FakeBusinessTask:
        name = "wallet"
        spend_policy = runner.SpendPolicy()

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def run(self) -> TaskResult:
            calls["business"] += 1
            if calls["business"] == 1:
                raise SessionKickedError(
                    packet_id=11011, code=-5, detail="token is Invalid"
                )
            return TaskResult(task=self.name, ok=True, message="重试成功")

    class FakeLoginTask:
        name = "login"
        spend_policy = runner.SpendPolicy()

        def __init__(self, session: GameSession) -> None:
            self.session = session

        def run(self) -> TaskResult:
            calls["login"] += 1
            # 真实 login 的关键副作用：把新会话写回同一个 GameSession 对象
            self.session.set_game_state(_state("fresh-token-2"))
            return TaskResult(task=self.name, ok=True, message="登录成功")

    def fake_create_task(name: str, session: GameSession, **kwargs: object):
        return FakeLoginTask(session) if name == "login" else FakeBusinessTask()

    monkeypatch.setattr(runner, "create_task", fake_create_task)
    monkeypatch.setattr("client.account_store.AccountStore", _FakeAccountStore)

    session = GameSession()
    session.set_game_state(_state("old-token"))
    context = runner.RunContext(
        session=session,
        session_state=session.game_state,
        session_source="test",
        spend_policy=runner.SpendPolicy(),
        run_options=runner.RunOptions(),
    )
    request = runner.RunRequest(tasks=(runner.TaskRequest(name="wallet"),))
    report = runner.run_tasks(context, request, emit=lambda text: None)

    assert calls == {"business": 2, "login": 1}, "应重试一次业务任务、重登一次"
    assert report.ok is True
    assert report.steps[-1].ok is True
    # 重试拿到的是 login 写回的新状态（新令牌）
    assert session.game_state is not None and session.game_state.token == "fresh-token-2"
    assert any("顶掉" in note for note in report.notes)


def test_kicked_session_without_saved_account_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """账号库里找不到绑定账号时：无法自动恢复，按普通失败上报，不崩溃。"""
    from client.exceptions import SessionKickedError

    class FakeBusinessTask:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def run(self) -> TaskResult:
            raise SessionKickedError(
                packet_id=11011, code=-5, detail="token is Invalid"
            )

    class _EmptyStore:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def find_account(self, selector: str) -> None:
            return None

    monkeypatch.setattr(
        runner, "create_task", lambda name, session, **kwargs: FakeBusinessTask()
    )
    monkeypatch.setattr("client.account_store.AccountStore", _EmptyStore)

    session = GameSession()
    session.set_game_state(_state("old-token"))
    context = runner.RunContext(
        session=session,
        session_state=session.game_state,
        session_source="test",
        spend_policy=runner.SpendPolicy(),
        run_options=runner.RunOptions(),
    )
    request = runner.RunRequest(tasks=(runner.TaskRequest(name="wallet"),))

    with pytest.raises(SessionKickedError):
        runner.run_tasks(context, request, emit=lambda text: None)
