"""tasks 包单元测试：结果对象、run 的异常策略、注册表、占位任务。

运行方式：

    python -m tests.test_tasks
    python -m pytest tests/test_tasks.py -q

【本文件要重点验证的一条设计】
``BaseTask.run()`` 对两类异常的处理**故意不同**：

- 业务异常（``CherrytaleError`` 子类）→ 转成失败的 ``TaskResult``，
  好让每日流程能继续跑后面的任务；
- 代码 bug（如 ``TypeError``）→ 原样抛出，绝不掩盖。

这是「容错」与「别掩盖 bug」之间的取舍，必须用测试把这条界线钉死，
否则以后有人为了「让日志好看」在里面加个 ``except Exception`` 就悄悄破坏了它。
"""

from __future__ import annotations

import logging

import pytest

import config
from client.exceptions import AuthenticationError, CherrytaleError, NotConfirmedError
from client.session import GameSession
from models.session_state import GameSessionState
from tasks.base import (
    BaseTask,
    TaskResult,
    available_tasks,
    create_task,
    get_task_class,
    register_task,
)

# 下面两行 import 的作用是**触发任务注册**（注册发生在模块导入时）。
# 若缺少它们，available_tasks() 里就不会出现 login / mail / daily_box。
import tasks.daily_box  # noqa: F401  (仅为副作用导入)
import tasks.login  # noqa: F401  (仅为副作用导入)


def make_session(*, logged_in: bool = False) -> GameSession:
    """构造一个不会联网的会话对象（域名是保留域名，绝不可能真的连上）。

    :param logged_in: 是否伪造登录态。此时会同时填上
        ``token_store``（平台令牌，供 ``requires_auth`` 检查）
        与 ``game_state``（游戏会话状态，供业务任务注入信封）。
    """
    session = GameSession("https://example.invalid", timeout=1.0)
    if logged_in:
        session.token_store.update(uid="u1", token="tok")
        session.set_game_state(GameSessionState(token="tok", player_id=1, server_id=1))
    return session


# ---------------------------------------------------------------------------
# 测试用任务样例
# ---------------------------------------------------------------------------
@register_task
class _OkTask(BaseTask):
    """永远成功（测试样例）。"""

    name = "test_ok"
    requires_auth = False

    def execute(self) -> TaskResult:
        return TaskResult(task=self.name, ok=True, message="一切正常", data={"level": 1})


@register_task
class _BusinessFailTask(BaseTask):
    """抛出业务异常（测试样例）。"""

    name = "test_business_fail"
    requires_auth = False

    def execute(self) -> TaskResult:
        raise NotConfirmedError("协议未确认，拒绝发包")


@register_task
class _BuggyTask(BaseTask):
    """抛出代码 bug（测试样例）。"""

    name = "test_buggy"
    requires_auth = False

    def execute(self) -> TaskResult:
        raise TypeError("这是代码 bug，必须原样抛出")


@register_task
class _AuthRequiredTask(BaseTask):
    """需要登录态（测试样例）。"""

    name = "test_auth_required"
    requires_auth = True

    def execute(self) -> TaskResult:
        return TaskResult(task=self.name, ok=True, message="已登录，执行成功")


# ---------------------------------------------------------------------------
# TaskResult
# ---------------------------------------------------------------------------
class TestTaskResult:
    def test_success_text(self) -> None:
        assert "✔" in str(TaskResult(task="t", ok=True, message="完成"))

    def test_failure_text(self) -> None:
        assert "✘" in str(TaskResult(task="t", ok=False, message="失败原因"))

    def test_default_message_when_success(self) -> None:
        assert "成功" in str(TaskResult(task="t", ok=True))

    def test_default_message_when_failure(self) -> None:
        assert "失败" in str(TaskResult(task="t", ok=False))

    def test_data_is_optional(self) -> None:
        assert TaskResult(task="t", ok=True).data == {}


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------
class TestBaseTask:
    def test_cannot_instantiate_abstract(self) -> None:
        """execute 未实现的类不允许被实例化，避免「空任务」混进流程。"""
        with pytest.raises(TypeError):
            BaseTask(make_session())  # type: ignore[abstract]

    def test_describe_uses_first_docstring_line(self) -> None:
        assert _OkTask(make_session()).describe() == "永远成功（测试样例）。"

    def test_repr_contains_class_and_name(self) -> None:
        text = repr(_OkTask(make_session()))
        assert "_OkTask" in text
        assert "test_ok" in text


# ---------------------------------------------------------------------------
# run()：日志与异常策略
# ---------------------------------------------------------------------------
class TestTaskRunPolicy:
    def test_success_returns_ok_result(self) -> None:
        result = _OkTask(make_session()).run()
        assert result.ok is True
        assert result.message == "一切正常"
        assert result.data == {"level": 1}

    def test_business_exception_becomes_failed_result(self) -> None:
        """业务异常不能让整个每日流程中断，因此转成失败结果。"""
        result = _BusinessFailTask(make_session()).run()
        assert result.ok is False
        assert "协议未确认" in result.message

    def test_business_exception_does_not_propagate(self) -> None:
        _BusinessFailTask(make_session()).run()  # 不抛异常即通过

    def test_code_bug_propagates(self) -> None:
        """代码 bug 必须原样抛出，绝不能被 except Exception 吞掉。"""
        with pytest.raises(TypeError, match="代码 bug"):
            _BuggyTask(make_session()).run()

    def test_start_and_finish_are_logged(self, caplog) -> None:
        """开始用 INFO；**完成行已降为 DEBUG**（2026-10-04 结果行去重）。

        ``run()`` 原先在 INFO 级再念一遍 ``✔ 任务 X 完成（1.2s）：…``，
        而入口层（CLI ``print(step.line)`` / Web ``_log(result.line)``）已经输出
        ``[✔] X: …`` —— 同一句话在日志流里出现两遍。现在完成行只在 DEBUG 里，
        见 :meth:`test_finish_line_still_exists_at_debug`。
        """
        with caplog.at_level(logging.INFO, logger="tasks"):
            _OkTask(make_session()).run()
        assert "开始任务 test_ok" in caplog.text
        assert "完成" not in caplog.text

    def test_finish_line_still_exists_at_debug(self, caplog) -> None:
        """去重**没有丢掉信息**：完成行（含耗时）在 DEBUG 里仍然打出来。"""
        with caplog.at_level(logging.DEBUG, logger="tasks"):
            _OkTask(make_session()).run()
        assert "完成" in caplog.text

    def test_failure_is_logged_as_warning(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="tasks"):
            _BusinessFailTask(make_session()).run()
        assert "失败" in caplog.text

    def test_precondition_blocks_unauthenticated_task(self) -> None:
        """需要登录却没有令牌时，应在执行前被拦下并给出明确原因。"""
        result = _AuthRequiredTask(make_session(logged_in=False)).run()
        assert result.ok is False
        assert "需要登录态" in result.message

    def test_precondition_passes_when_logged_in(self) -> None:
        result = _AuthRequiredTask(make_session(logged_in=True)).run()
        assert result.ok is True

    def test_check_preconditions_raises_authentication_error(self) -> None:
        task = _AuthRequiredTask(make_session(logged_in=False))
        with pytest.raises(AuthenticationError):
            task.check_preconditions()

    def test_auth_exception_is_cherrytale_error(self) -> None:
        """确认异常继承链正确，否则 run() 的兜底逻辑抓不住它。"""
        assert issubclass(AuthenticationError, CherrytaleError)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
class TestTaskRegistry:
    def test_sample_tasks_are_registered(self) -> None:
        names = available_tasks()
        for expected in ("test_ok", "test_business_fail", "test_buggy"):
            assert expected in names

    def test_placeholder_tasks_are_registered(self) -> None:
        assert "login" in available_tasks()
        assert "daily_box" in available_tasks()

    def test_get_task_class_returns_class(self) -> None:
        assert get_task_class("login").name == "login"

    def test_unknown_name_lists_alternatives(self) -> None:
        """报错要列出可用任务名，省得你回头翻代码找名字。"""
        with pytest.raises(KeyError, match="可用任务"):
            get_task_class("这个任务不存在")

    def test_create_task_returns_instance(self) -> None:
        task = create_task("login", make_session())
        assert isinstance(task, BaseTask)
        assert task.name == "login"

    def test_re_registering_same_class_is_idempotent(self) -> None:
        """同一个类被重复导入时不应报错，否则模块重载会炸。"""
        assert register_task(_OkTask) is _OkTask

    def test_duplicate_name_from_other_class_rejected(self) -> None:
        class DuplicatedName(BaseTask):
            name = "test_ok"  # 与 _OkTask 重名

            def execute(self) -> TaskResult:  # pragma: no cover - 不会被调用
                return TaskResult(task=self.name, ok=True)

        with pytest.raises(ValueError, match="已被"):
            register_task(DuplicatedName)

    def test_empty_name_rejected(self) -> None:
        class NoName(BaseTask):
            def execute(self) -> TaskResult:  # pragma: no cover - 不会被调用
                return TaskResult(task="", ok=True)

        with pytest.raises(ValueError, match="name 属性"):
            register_task(NoName)

    def test_available_tasks_returns_copy(self) -> None:
        """返回副本，避免外部代码意外篡改注册表。"""
        snapshot = available_tasks()
        snapshot["injected"] = _OkTask
        assert "injected" not in available_tasks()


# ---------------------------------------------------------------------------
# 占位任务（协议未确认时必须明确报错，绝不发包）
# ---------------------------------------------------------------------------
class TestPlaceholderTasks:
    def test_login_does_not_require_auth(self) -> None:
        """登录本身当然不需要先持有令牌。"""
        assert get_task_class("login").requires_auth is False

    def test_daily_box_requires_auth(self) -> None:
        """每日领取类任务都需要登录态（``daily`` 已删除，改由 ``daily_box`` 代表）。"""
        assert get_task_class("daily_box").requires_auth is True

    def test_login_without_any_platform_identity_is_blocked(
        self, monkeypatch, tmp_path
    ) -> None:
        """没有**任何**平台身份来源时，登录必须在**发包之前**被拦下，并说明怎么办。

        "任何来源"= 没有显式令牌、没有已保存账号、也没有账号密码。
        刻意钉成"不联网"的行为：登录有副作用，测试环境绝不能因为
        "本机恰好有 .auth_token / 有凭据文件"就真去发一个登录包 ——
        所以账号库与凭据文件都指到临时路径。
        """
        monkeypatch.setattr(config, "has_auth_token", lambda: False)
        monkeypatch.setattr(config, "PLATFORM_ACCOUNTS_FILE", tmp_path / "accounts.json")
        monkeypatch.setattr(config, "PLATFORM_CREDENTIAL_FILE", tmp_path / "none.txt")
        monkeypatch.delenv(config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME, raising=False)
        monkeypatch.delenv(config.PLATFORM_LOGIN_PASSWORD_ENV_NAME, raising=False)
        monkeypatch.delenv(config.PLATFORM_ACCOUNT_ENV_NAME, raising=False)

        result = create_task("login", make_session()).run()

        assert result.ok is False
        assert "无法确定要用哪个平台账号" in result.message
        assert config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME in result.message
        assert "--account" in result.message

    def test_login_dry_run_is_offline(self, monkeypatch) -> None:
        """演练模式**不调用平台接口**，直接产出将要发送的字节。

        这条防的是一种很隐蔽的退化：为了让 dry-run 也拿到 userId 而去调平台网关，
        结果"演练"变成"发了一半"—— 在无网络或令牌失效时还会莫名失败，
        让人误以为是字节组装错了。
        """
        monkeypatch.setattr(
            config, "has_auth_token", lambda: True
        )  # 即使有令牌也不该被使用
        result = create_task("login", make_session(), dry_run=True).run()
        assert result.ok is True
        assert "演练完成" in result.message
        assert result.data["body_size"] > 0

    # 【2026-09-24 删掉的两个用例】
    # * ``test_daily_declares_only_registered_steps``：守的是"daily 声明的每一步都已注册"。
    #   但 ``daily`` 这层"流程壳"已被删除（理由见 services/task_spec.py），
    #   这条约束本身不再存在；"注册表 ↔ 规格表"的一致性由
    #   tests/test_services_task_spec.py 守着。
    # * ``test_daily_without_token_is_blocked_earlier``：daily 的"未登录被前置检查拦下"。
    #   等价约束改由 ``mail`` 承担（同一套 requires_auth 前置检查，见 tasks/base.py）。
    def test_daily_box_without_token_is_blocked_earlier(self) -> None:
        """未登录时应在前置检查就被拦下（原来这条测的是 ``daily``）。

        用 ``daily_box`` 是因为本文件只为 ``login`` / ``daily_box`` 触发了注册
        （见文件顶部的副作用 import）。``requires_auth`` 默认 True（``tasks/base.py``），
        所以"未登录 → 前置检查拦下"这条约束对每个每日领取类任务都成立。
        """
        result = create_task("daily_box", make_session(logged_in=False)).run()
        assert result.ok is False
        assert "需要登录态" in result.message


if __name__ == "__main__":  # 支持 `python -m tests.test_tasks`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
