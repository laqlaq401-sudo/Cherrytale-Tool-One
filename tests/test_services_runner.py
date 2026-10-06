"""``services/runner.py`` 的守护测试（离线，不联网）。

【它守的是什么】
1. **闸门**：钻石总闸与用途白名单两道门的行为（未授权时 ``spent`` 必须为 0 的语义）；
2. **参数翻译**：每个任务的专属 kwargs 名字必须与真实任务类一致 ——
   写错名字会 ``TypeError``（而不是被静默忽略），这条用真实类来验证；
3. **两个"临时状态"**：凭据用完必须从环境变量里消失、DEBUG 级别用完必须恢复；
4. **离线可跑**：``--dry-run`` 的整条链路（prepare → run_tasks）零网络流量。

运行方式（两种都支持）：

    python -m tests.test_services_runner
    python -m pytest tests/test_services_runner.py -q
"""

from __future__ import annotations

import logging
import os

import pytest

import config
from client.session import GameSession
from client.spending import SpendPolicy, SpendPurpose
from services import runner
from services.task_spec import normalize_params


@pytest.fixture()
def no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """把"会话状态来源"固定成"没有"。

    【为什么要固定】
    ``resolve_session_state()`` 会去读本机的 ``.session.json`` / 环境变量。
    如果测试依赖它的真实结果，就会变成"在我机器上过、在你机器上挂" ——
    这正是本项目反复强调要避免的那类测试。
    """
    monkeypatch.setattr(runner, "resolve_session_state", lambda: (None, "（无）"))


@pytest.fixture()
def context() -> runner.RunContext:
    """一个不依赖会话文件的运行上下文（只测"翻译/装配"，不会发包）。"""
    ctx = runner.RunContext(
        session=GameSession(),
        session_state=None,
        session_source="（测试）",
        spend_policy=SpendPolicy(),
        run_options=runner.RunOptions(),
    )
    try:
        yield ctx
    finally:
        ctx.close()


def _params(name: str, raw: dict[str, object] | None = None) -> dict[str, object]:
    """取值规范化后的参数（与真实调用路径完全一致的入口）。"""
    return normalize_params(name, raw)[0]


def _kwargs(
    context: runner.RunContext,
    name: str,
    raw: dict[str, object] | None = None,
    *,
    account: str | None = None,
    dry_run: bool = True,
) -> dict[str, object]:
    """按真实路径组装一次 kwargs（请求 → 参数规范化 → task_kwargs_for）。"""
    task = runner.TaskRequest(name=name, params=raw or {})
    request = runner.RunRequest(tasks=(task,), dry_run=dry_run, account=account)
    return runner.task_kwargs_for(
        request, task, context, _params(name, raw), runner.resolve_task_class(name)
    )


# ---------------------------------------------------------------------------
# ① 从"任务参数"派生的判断
# ---------------------------------------------------------------------------
class TestRequestDerivations:
    def test_invite_friends_comes_from_task_params(self) -> None:
        """用途授权只有一个来源：宴席任务的参数（不是 RunRequest 上的另一个字段）。"""
        quiet = runner.RunRequest(tasks=(runner.TaskRequest("bbq_energy"),))
        loud = runner.RunRequest(
            tasks=(runner.TaskRequest("bbq_energy", {"invite_friends": True}),)
        )
        assert quiet.wants_invite_friends is False
        assert loud.wants_invite_friends is True

    def test_requested_purposes_adds_bbq_invite_only_when_asked(self) -> None:
        quiet = runner.RunRequest(tasks=(runner.TaskRequest("bbq_energy"),))
        loud = runner.RunRequest(
            tasks=(runner.TaskRequest("bbq_energy", {"invite_friends": True}),)
        )
        assert SpendPurpose.BBQ_INVITE not in quiet.requested_purposes()
        assert SpendPurpose.BBQ_INVITE in loud.requested_purposes()


# ---------------------------------------------------------------------------
# ② 校验
# ---------------------------------------------------------------------------
class TestValidation:
    def test_empty_task_list_is_a_problem(self) -> None:
        assert runner.validate_request(runner.RunRequest(tasks=())) == ["没有选择任何任务"]

    def test_known_task_has_no_problem(self) -> None:
        request = runner.RunRequest(tasks=(runner.TaskRequest("mail"),))
        assert runner.validate_request(request) == []

    def test_unknown_task_is_reported_with_options(self) -> None:
        request = runner.RunRequest(tasks=(runner.TaskRequest("no-such-task"),))
        problems = runner.validate_request(request)
        assert len(problems) == 1
        assert "未找到任务" in problems[0]
        assert "mail" in problems[0]  # 消息里必须列出可选任务

    def test_placeholder_task_is_reported_as_unimplemented(self, monkeypatch) -> None:
        """规格表里 enabled=False 的条目必须报"尚未实现"（而不是"未找到"）。

        ★ 2026-10-06：最后一个真实占位 ``sign_in`` 已删除（登录签到转为
        内置钩子，见 ``tasks/login_signin.py``）。分支本身仍保留 ——
        将来新增占位时它是唯一的安全网 —— 所以这里注入一个假占位来测。
        """
        from services import task_spec
        from services.task_spec import TaskSpec

        monkeypatch.setitem(
            task_spec.TASK_SPECS,
            "fake_placeholder",
            TaskSpec(
                name="fake_placeholder",
                group="planned",
                title="（测试占位）",
                summary="（测试占位）暂未实现",
                enabled=False,
            ),
        )
        request = runner.RunRequest(tasks=(runner.TaskRequest("fake_placeholder"),))
        problems = runner.validate_request(request)
        assert len(problems) == 1
        assert "尚未实现" in problems[0]

    def test_bad_param_type_is_reported(self) -> None:
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("sweep_activity", {"times": "abc"}),)
        )
        problems = runner.validate_request(request)
        assert len(problems) == 1
        assert "times" in problems[0]

    def test_resolve_task_class_raises_own_error_type(self) -> None:
        """名字写错必须抛 :class:`UnknownTaskError`（入口据此给出 400 / 退出码 2）。"""
        with pytest.raises(runner.UnknownTaskError):
            runner.resolve_task_class("no-such-task")


# ---------------------------------------------------------------------------
# ③ 参数翻译（kwargs 名字必须与真实任务类一致）
# ---------------------------------------------------------------------------
class TestTaskKwargs:
    def test_common_kwargs_always_present(self, context: runner.RunContext) -> None:
        assert _kwargs(context, "mail") == {
            "dry_run": True,
            "spend_policy": context.spend_policy,
        }

    def test_login_gets_account(self, context: runner.RunContext) -> None:
        kwargs = _kwargs(context, "login", {"account": "2"}, account="2")
        assert kwargs["account"] == "2"

    def test_sweep_maps_allow_unpassed_to_only_passed(
        self, context: runner.RunContext
    ) -> None:
        """``--allow-unpassed`` 与任务要的 ``only_passed`` 方向相反，转换只做一次。"""
        strict = _kwargs(context, "sweep_activity", {"times": 3, "area": "3"})
        assert strict["area"] == "3"
        assert strict["times"] == 3
        assert strict["section"] is None
        assert strict["only_passed"] is True  # 默认只扫已通关

        loose = _kwargs(
            context, "sweep_activity", {"allow_unpassed": True, "times": "1"}
        )
        assert loose["only_passed"] is False
        assert loose["times"] == 1

    def test_sweep_zero_times_stays_zero(self, context: runner.RunContext) -> None:
        """0 = 只读，绝不能被"顺手"改成 1（那会真的扣体力）。"""
        assert _kwargs(context, "sweep_activity")["times"] == 0

    def test_prefetch_sections_reaches_the_task_kwarg(
        self, context: runner.RunContext
    ) -> None:
        """★ 2026-10-06：内部参数 ``prefetch_sections`` 必须真的传到任务构造函数上。

        【为什么值得单独一条】
        这里的 ``if "prefetch_sections" in params`` 曾经是**死分支**：规格表没登记
        这个键 → ``normalize_params`` 先把它丢了 → 判断永远为假 →
        活动缓存服务的"一次预热全部区域关卡"从来没生效过（用户只看到一行
        "忽略了不认识的参数：prefetch_sections"）。
        所以这条从**真实入口**走一遍（请求 → 规范化 → 翻译），把整条链路钉住；
        顺带守住"没提供时也要显式传 False"（任务形参有默认值，但显式更不易漂）。
        """
        assert _kwargs(context, "sweep_activity")["prefetch_sections"] is False
        kwargs = _kwargs(context, "sweep_activity", {"prefetch_sections": True})
        assert kwargs["prefetch_sections"] is True
        # 翻译出来的名字必须与真实签名一致（写错这里就会 TypeError）
        from tasks.base import create_task

        task = create_task("sweep_activity", context.session, **kwargs)
        assert task.prefetch_sections is True

    def test_run_options_only_for_declaring_tasks(self, context: runner.RunContext) -> None:
        """运行参数只发给声明了 ``accepts_run_options`` 的任务（否则会 TypeError）。"""
        assert "run_options" not in _kwargs(context, "mail")
        assert "run_options" not in _kwargs(context, "sweep_activity")
        assert _kwargs(context, "top_pvp_battle")["run_options"] is context.run_options

    def test_bbq_energy_defaults_to_eat(self, context: runner.RunContext) -> None:
        """主动任务已移除确认吃宴席开关，默认执行即吃（eat=True）。"""
        assert _kwargs(context, "bbq_energy")["eat"] is True
        assert _kwargs(context, "bbq_energy", {"invite_friends": True})["eat"] is True

    def test_push_main_stage_kwargs(self, context: runner.RunContext) -> None:
        """测试 push_main_stage 任务参数映射到 kwargs。"""
        kwargs = _kwargs(
            context,
            "push_main_stage",
            {
                "max_stages": 5,
                "delay_min": 10.0,
                "delay_max": 20.0,
            },
        )
        assert kwargs["max_stages"] == 5
        assert kwargs["delay_min"] == 10.0
        assert kwargs["delay_max"] == 20.0

    def test_wrong_kwarg_name_is_not_silently_ignored(self, context: runner.RunContext) -> None:
        """说明"点名式"的价值：名字写错会立刻 TypeError，而不是静默忽略。

        如果任务构造函数里有 ``**kwargs``，写错的名字会被悄悄吃掉 ——
        用户看到的现象是"我设了参数却没生效"，极难排查。
        真实类的签名没有 ``**kwargs``，所以这里能守住。
        """
        from tasks.base import create_task

        with pytest.raises(TypeError):
            create_task(
                "wallet",
                context.session,
                dry_run=True,
                spend_policy=context.spend_policy,
                bogus_option=True,
            )


# ---------------------------------------------------------------------------
# ④ 装配阶段（闸门 / 运行参数 / 说明文字）
# ---------------------------------------------------------------------------
class TestPrepare:
    def test_gate_closed_by_default(self, no_session: None) -> None:
        request = runner.RunRequest(tasks=(runner.TaskRequest("bbq_energy"),))
        ctx = runner.prepare(request)
        try:
            assert ctx.spend_policy.allow_diamond is False
            assert SpendPurpose.BBQ_INVITE not in ctx.spend_policy.allowed_purposes
            assert any("消费许可" in line for line in ctx.notes)
        finally:
            ctx.close()

    def test_allow_diamond_opens_master_gate(self, no_session: None) -> None:
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("bbq_energy"),), allow_diamond=True
        )
        ctx = runner.prepare(request)
        try:
            assert ctx.spend_policy.allow_diamond is True
            assert any("已显式开启钻石总闸" in line for line in ctx.notes)
        finally:
            ctx.close()

    def test_invite_without_master_gate_cannot_spend(self, no_session: None) -> None:
        """两道门是**与**关系：只授权用途、不开总闸，依旧一分钱都花不出去。"""
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("bbq_energy", {"invite_friends": True}),)
        )
        ctx = runner.prepare(request)
        try:
            assert SpendPurpose.BBQ_INVITE in ctx.spend_policy.allowed_purposes
            assert ctx.spend_policy.allows_purpose(SpendPurpose.BBQ_INVITE) is False
            assert any("仍然不会花" in line for line in ctx.notes)
        finally:
            ctx.close()

    def test_emit_receives_exactly_the_recorded_lines(self, no_session: None) -> None:
        """出口（命令行 print / 网页日志流）与记录（context.notes）必须完全一致。"""
        lines: list[str] = []
        request = runner.RunRequest(tasks=(runner.TaskRequest("handshake"),))
        ctx = runner.prepare(request, emit=lines.append)
        try:
            assert lines == ctx.notes
            assert lines  # 至少要说话（"未找到会话状态"那条）
        finally:
            ctx.close()

    def test_missing_session_note_points_to_login(self, no_session: None) -> None:
        ctx = runner.prepare(runner.RunRequest(tasks=(runner.TaskRequest("mail"),)))
        try:
            assert ctx.session_state is None
            assert any("未找到游戏会话状态" in line for line in ctx.notes)
            assert any("main.py run login" in line for line in ctx.notes)
        finally:
            ctx.close()

    def test_run_options_read_only_default(
        self, no_session: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不填场数 = config 的值；config 为 0 时必须是"只读"。"""
        monkeypatch.setattr(config, "TOP_PVP_BATTLES", 0)
        request = runner.RunRequest(tasks=(runner.TaskRequest("top_pvp_battle"),))
        ctx = runner.prepare(request)
        try:
            assert ctx.run_options.battles == 0
            assert ctx.run_options.is_read_only is True
            assert ctx.run_options.max_battles == config.TOP_PVP_MAX_BATTLES
        finally:
            ctx.close()

    def test_explicit_battles_override_config(self, no_session: None) -> None:
        """场数 / 间隔由**任务参数**给出（★ 2026-09-25 归位后不再走运行级字段）。

        网页上填的就是这两个值，所以测试也照 UI 的路径来提交 ——
        顺带保证"字符串形式的场数"（``<input type="number">`` 交上来的样子）
        也会被正确转换：``"5"`` 与 ``"0.25"`` 都必须生效。
        """
        request = runner.RunRequest(
            tasks=(
                runner.TaskRequest(
                    "top_pvp_battle", params={"battles": "5", "interval": "0.25"}
                ),
            )
        )
        ctx = runner.prepare(request)
        try:
            assert ctx.run_options.battles == 5
            assert ctx.run_options.effective_battles <= 5
            assert ctx.run_options.interval_seconds == 0.25
        finally:
            ctx.close()

    def test_battles_ignored_when_task_does_not_declare_it(
        self, no_session: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """别的任务带上 ``battles`` 不该影响运行参数（只有声明的任务才读）。

        这是"参数归属"最实际的收益：``normalize_params`` 会把扫荡任务不认识的
        ``battles`` 丢掉，于是"给扫荡任务顺手加个 --battles"不会悄悄改变荣耀之巅的场数。
        """
        monkeypatch.setattr(config, "TOP_PVP_BATTLES", 0)
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("sweep_activity", params={"battles": 9}),)
        )
        ctx = runner.prepare(request)
        try:
            assert ctx.run_options.battles == 0
            assert ctx.run_options.is_read_only is True
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# ⑤ 执行阶段（离线可跑：dry-run 全链路）
# ---------------------------------------------------------------------------
class TestRunTasks:
    def test_dry_run_handshake_is_offline_and_ok(self, no_session: None) -> None:
        """整条链路（prepare → run_tasks）在演练模式下**零网络流量**。"""
        request = runner.RunRequest(tasks=(runner.TaskRequest("handshake"),), dry_run=True)
        ctx = runner.prepare(request)
        try:
            report = runner.run_tasks(ctx, request)
        finally:
            ctx.close()

        assert report.ok is True
        assert report.steps[0].ok is True
        assert "未发送" in report.steps[0].message
        assert report.steps[0].line.startswith("[✔] handshake:")
        assert report.spend_policy.spent_so_far == 0

    def test_step_line_keeps_task_result_format(self, no_session: None) -> None:
        """结果行必须与 ``TaskResult.__str__`` 同格式（CLI 输出因此没有变化）。"""
        request = runner.RunRequest(tasks=(runner.TaskRequest("handshake"),), dry_run=True)
        ctx = runner.prepare(request)
        try:
            report = runner.run_tasks(ctx, request)
        finally:
            ctx.close()
        step = report.steps[0]
        assert step.line == f"[✔] {step.name}: {step.message}"

    def test_failure_does_not_stop_later_steps(self, no_session: None) -> None:
        """"邮件已领空"不该导致后面的步骤也不做（与 ``tasks/daily.py`` 同一策略）。"""
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("arena_status"), runner.TaskRequest("handshake")),
            dry_run=True,
        )
        ctx = runner.prepare(request)
        try:
            report = runner.run_tasks(ctx, request)
        finally:
            ctx.close()

        assert len(report.steps) == 2
        assert report.steps[0].ok is False  # 没有登录态 → 前置检查就拦下了
        assert report.steps[1].ok is True   # 后面的步骤照常执行
        assert report.ok is False
        assert report.summary().startswith("1/2 步成功")
        assert [step.name for step in report.failed] == ["arena_status"]

    def test_unknown_task_aborts_before_any_step(self, no_session: None) -> None:
        """名字写错必须在**跑之前**发现：否则前几步已经真的领了奖励。"""
        started: list[str] = []
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("handshake"), runner.TaskRequest("no-such-task")),
            dry_run=True,
        )
        ctx = runner.prepare(request)
        try:
            with pytest.raises(runner.UnknownTaskError):
                runner.run_tasks(ctx, request, on_start=started.append)
        finally:
            ctx.close()
        assert started == []

    def test_uncaught_exception_is_contained_to_one_step(
        self, no_session: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 2026-10-03 兜底钉子：单任务的未捕获异常只毁掉**那一行**。

        实测事故：free_gift 的 ``EnvelopeError``（模型解码异常，不属于
        ``CherrytaleError``）曾把整批运行炸断，排在后面的活跃宝箱根本没跑。
        现在 runner 循环里兜底合成失败步骤，后续任务照常执行。
        """
        import tasks.probe as probe_module  # noqa: F401  导入即注册 handshake

        task_class = runner.resolve_task_class("handshake")

        def broken_execute(self: object) -> object:
            raise ValueError("模拟模型解码崩溃（EnvelopeError 同类：非业务异常）")

        monkeypatch.setattr(task_class, "execute", broken_execute)
        # _dry_run 也在同一类上（dry_run 分支会先走它），一并替换以防绕过
        monkeypatch.setattr(task_class, "_dry_run", broken_execute, raising=False)

        request = runner.RunRequest(
            tasks=(runner.TaskRequest("handshake"), runner.TaskRequest("handshake")),
            dry_run=True,
        )
        ctx = runner.prepare(request)
        try:
            report = runner.run_tasks(ctx, request)
        finally:
            ctx.close()

        assert len(report.steps) == 2
        assert report.steps[0].ok is False
        assert "未捕获异常" in report.steps[0].message
        assert "模拟模型解码崩溃" in report.steps[0].message
        # 第二步是新构造的任务实例，monkeypatch 仍生效 → 同样失败但**没有中断**
        assert report.steps[1].name == "handshake"
        assert report.ok is False

    def test_progress_callbacks_fire_in_order(self, no_session: None) -> None:
        started: list[str] = []
        finished: list[str] = []
        request = runner.RunRequest(tasks=(runner.TaskRequest("handshake"),), dry_run=True)
        ctx = runner.prepare(request)
        try:
            runner.run_tasks(
                ctx,
                request,
                on_start=started.append,
                on_finish=lambda step: finished.append(step.name),
            )
        finally:
            ctx.close()
        assert started == ["handshake"]
        assert finished == ["handshake"]

    def test_notes_are_collected_into_report(self, no_session: None) -> None:
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("top_pvp_battle"),), dry_run=True
        )
        ctx = runner.prepare(request)
        try:
            report = runner.run_tasks(ctx, request)
        finally:
            ctx.close()
        assert any("运行参数" in line for line in report.notes)

    def test_credentials_never_leak(self, no_session: None) -> None:
        """密码不能出现在说明文字、结果消息或环境变量里（安全承诺的机器化验证）。"""
        secret = "超级秘密口令-不该出现在任何地方"
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("mail"),),
            credentials=("u@example.com", secret),
        )
        ctx = runner.prepare(request)
        try:
            report = runner.run_tasks(ctx, request)  # 无登录态会失败，但流程要完整走完
        finally:
            ctx.close()

        joined = "\n".join(report.notes) + "\n".join(
            step.message for step in report.steps
        )
        assert secret not in joined
        assert secret not in os.environ.values()  # 用完必须从环境变量里消失


# ---------------------------------------------------------------------------
# ⑥ 两个"临时状态"：凭据与日志级别
# ---------------------------------------------------------------------------
class TestTemporaryState:
    def test_credentials_are_injected_then_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME, raising=False)
        monkeypatch.delenv(config.PLATFORM_LOGIN_PASSWORD_ENV_NAME, raising=False)

        with runner._temporary_credentials(("a@example.com", "pw")):
            assert os.environ[config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME] == "a@example.com"
            assert os.environ[config.PLATFORM_LOGIN_PASSWORD_ENV_NAME] == "pw"

        assert config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME not in os.environ
        assert config.PLATFORM_LOGIN_PASSWORD_ENV_NAME not in os.environ

    def test_pre_existing_env_is_restored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """原本就有凭据环境变量时，必须恢复成**原值**（而不是删掉）。"""
        monkeypatch.setenv(config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME, "原账号")
        monkeypatch.setenv(config.PLATFORM_LOGIN_PASSWORD_ENV_NAME, "原密码")

        with runner._temporary_credentials(("临时账号", "临时密码")):
            assert os.environ[config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME] == "临时账号"

        assert os.environ[config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME] == "原账号"
        assert os.environ[config.PLATFORM_LOGIN_PASSWORD_ENV_NAME] == "原密码"

    def test_none_credentials_change_nothing(self) -> None:
        before = dict(os.environ)
        with runner._temporary_credentials(None):
            pass
        assert dict(os.environ) == before

    def test_verbose_raises_level_then_restores(self) -> None:
        root = logging.getLogger()
        before = root.level
        with runner._temporary_log_level(True):
            assert root.level == logging.DEBUG
        assert root.level == before

    def test_non_verbose_does_not_touch_level(self) -> None:
        """不 verbose 时不许"顺手"改成 INFO —— 那会覆盖入口层的设置。"""
        root = logging.getLogger()
        before = root.level
        with runner._temporary_log_level(False):
            assert root.level == before


# ---------------------------------------------------------------------------
# ⑦ 给 /api/health 用的会话摘要
# ---------------------------------------------------------------------------
class TestDescribeSession:
    def test_without_session(self, no_session: None) -> None:
        info = runner.describe_session()
        assert info["has_session"] is False
        assert info["summary"] == ""

    def test_with_state_uses_masked_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只能用 ``describe()`` 的**打码**摘要；同时给出结构化字段供界面使用。"""

        class FakeState:
            player_id = 10000003
            server_id = 113
            server_name = "测试区服"
            platform_user_id = "ER-TEST"
            obtained_at = 1.5

            def describe(self) -> str:
                return "playerId=1 @ 测试区服(#1)（已打码）"

        monkeypatch.setattr(
            runner, "resolve_session_state", lambda: (FakeState(), "会话文件 x")
        )
        info = runner.describe_session()
        assert info["has_session"] is True
        assert "已打码" in str(info["summary"])
        # 给程序用的字段：界面靠它们显示"上次进的区"、发请求时做一致性检查
        assert info["player_id"] == 10000003
        assert info["server_id"] == 113
        assert info["server_name"] == "测试区服"
        assert info["platform_user_id"] == "ER-TEST"
        # 令牌绝不出现在这里（describe() 已打码，且我们不额外透传任何令牌字段）
        assert "token" not in {key.lower() for key in info}


if __name__ == "__main__":  # 支持 `python -m tests.test_services_runner`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))



