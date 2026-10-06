"""``services/task_spec.py`` 的守护测试（三条守门）。

【它守的是什么】
1. **覆盖**：``tasks/`` 里注册的每个任务都必须在规格表里有登记 ——
   新增任务忘了登记 UI，这里立刻变红（比"人工记得同步"可靠）；
2. **不漂移**：规格表的参数名与 ``main.build_parser()`` 的字段名**双向一致** ——
   无论哪一边加了旗标而另一边没跟上，都会失败；
3. **转换正确**：字符串/JSON 值转成正确的类型，且危险情况必须报错
   （``True`` 不能冒充整数、``"abc"`` 不能静默变成 0）。

运行方式（两种都支持，与仓库其它测试一致）：

    python -m tests.test_services_task_spec
    python -m pytest tests/test_services_task_spec.py -q
"""

from __future__ import annotations

import os

import pytest

import config
import main
from services.task_spec import (
    GROUPS,
    RUN_LEVEL_PARAMS,
    TASK_SPECS,
    ParamSpec,
    group_of,
    normalize_params,
    run_level_from_cli,
    task_params_from_cli,
)

#: 命令行里"非参数"的字段：位置参数、子命令名、调度函数、全局 -v。
#: 它们不需要出现在规格表里（``tests/test_main.py`` 已经在测它们）。
_NON_PARAM_DESTS = frozenset({"task", "func", "command", "verbose"})


def _run_dests() -> set[str]:
    """``python main.py run`` 子命令能解析出的全部字段名（= argparse 的 ``dest``）。

    直接解析真实解析器最不容易"测了个假的"：它是 ``main.py`` 那份定义的直接产物。
    """
    return set(vars(main.build_parser().parse_args(["run", "handshake"])))


def _builtin_registry() -> dict[str, type]:
    """只保留 ``tasks/`` 包里注册的任务 —— 排除**测试自己造**的假任务。

    【为什么必须过滤】
    pytest 在**收集阶段**就会导入所有测试模块，而 ``tests/test_tasks.py``
    在模块级用 ``@register_task`` 造了几个假任务（``test_ok`` / ``test_buggy`` …），
    它们会留在全局注册表里。若直接拿整张表做集合比对，本文件会因为这些
    "别人的测试数据"而失败 —— 那是测试互相污染，不是真的漏登记。

    判据用 ``__module__``：真正内置任务的模块名一定是 ``tasks.xxx``。
    （本函数顺带保证"过滤没有把所有任务都滤掉"，否则守卫会形同虚设。）
    """
    registry = {
        name: task_class
        for name, task_class in main.load_builtin_tasks().items()
        if getattr(task_class, "__module__", "").startswith("tasks.")
    }
    assert registry, "过滤后没有任何内置任务：说明过滤判据写错了，而不是任务真的为空"
    return registry


def _param(name: str) -> ParamSpec:
    """按名取参数规格：先找运行级参数，再找荣耀之巅的任务参数（找不到直接失败）。

    ★ 2026-09-25：``battles`` / ``interval`` 已归位成荣耀之巅的**任务级**参数
    （见 ``TASK_SPECS["top_pvp_battle"]``），所以这里要两处都找 ——
    否则本文件里那几条取值测试会因为"换个归属"而全部报"参数不存在"。
    """
    for spec in RUN_LEVEL_PARAMS:
        if spec.name == name:
            return spec
    from_top_pvp = TASK_SPECS["top_pvp_battle"].param(name)
    if from_top_pvp is not None:
        return from_top_pvp
    raise AssertionError(f"参数 {name!r} 既不在运行级、也不在荣耀之巅的参数里")


# ---------------------------------------------------------------------------
# ① 覆盖
# ---------------------------------------------------------------------------
class TestCoverage:
    def test_every_registered_task_has_spec(self) -> None:
        """注册表里的内置任务一个都不能少，否则网页上看不到它。"""
        missing = sorted(set(_builtin_registry()) - set(TASK_SPECS))
        assert not missing, f"以下任务没有登记规格，请补 services/task_spec.py：{missing}"

    def test_placeholder_tasks_are_not_registered(self) -> None:
        """占位项（enabled=False）必须**没有**真的注册，否则会被执行。"""
        registry = _builtin_registry()
        for name, spec in TASK_SPECS.items():
            if not spec.enabled:
                assert name not in registry, f"{name} 尚未实现，不该出现在注册表里"

    def test_spec_name_matches_dict_key(self) -> None:
        """键与 ``name`` 不一致是最阴的一类笔误（按名执行时会取错）。"""
        for key, spec in TASK_SPECS.items():
            assert spec.name == key, f"规格键 {key} 与 spec.name {spec.name} 不一致"

    def test_every_group_key_exists(self) -> None:
        keys = {key for key, _, _ in GROUPS}
        for spec in TASK_SPECS.values():
            assert spec.group in keys, f"{spec.name} 的分组 {spec.group} 未定义"

    def test_titles_and_summaries_are_not_empty(self) -> None:
        for name, spec in TASK_SPECS.items():
            assert spec.title.strip(), f"{name} 缺少中文标题"
            assert spec.summary.strip(), f"{name} 缺少一句话说明"

    def test_params_have_no_duplicates(self) -> None:
        for name, spec in TASK_SPECS.items():
            names = list(spec.param_names)
            assert len(names) == len(set(names)), f"{name} 有重复参数：{names}"

    def test_group_of_falls_back_for_unknown_task(self) -> None:
        """没登记规格的任务不该让页面报错，而是归到「主动任务」（默认不勾选）。"""
        assert group_of("mail") == "auto"
        assert group_of("sweep_activity") == "manual"
        assert group_of("这个任务不存在") == "manual"


# ---------------------------------------------------------------------------
# ② CLI / 规格表双向一致
# ---------------------------------------------------------------------------
class TestCliAlignment:
    def test_spec_param_names_exist_in_cli(self) -> None:
        """规格表里的每个参数名，命令行都必须真的能解析出来。

        ★ 2026-10-06：**内部参数**（``hidden=True``）反过来判 ——
        它们由进程内调用方设置，界面不渲染、命令行也不给旗标，
        所以"必须能在命令行解析出来"对它们是错的判据。留一个洞不如
        换成一条**同样严格的反向断言**：内部参数不许出现在命令行字段里
        （否则就成了"命令行有个谁也说不清该不该用的隐形开关"）。
        """
        dests = _run_dests()
        for spec in TASK_SPECS.values():
            for param in spec.params:
                if param.hidden:
                    assert param.name not in dests, (
                        f"{spec.name} 的内部参数 {param.name} 不该出现在命令行里"
                    )
                    continue
                assert param.name in dests, f"{spec.name} 的参数 {param.name} 不在 CLI 里"

    def test_run_level_param_names_exist_in_cli(self) -> None:
        dests = _run_dests()
        for param in RUN_LEVEL_PARAMS:
            assert param.name in dests, f"运行级参数 {param.name} 不在 CLI 里"

    def test_no_cli_flag_left_unmapped(self) -> None:
        """反向：命令行里的每个旗标都要在规格表里有归宿，不能有"隐形参数"。"""
        mapped = {param.name for param in RUN_LEVEL_PARAMS}
        for spec in TASK_SPECS.values():
            mapped |= set(spec.param_names)
        unmapped = _run_dests() - mapped - set(_NON_PARAM_DESTS)
        assert not unmapped, f"这些命令行旗标没有登记到规格表：{sorted(unmapped)}"


# ---------------------------------------------------------------------------
# ②' 荣耀之巅的参数归位（★ 2026-09-25）
# ---------------------------------------------------------------------------
class TestTopPvpParamPlacement:
    """守住"荣耀之巅的全部可调项都长在它自己的卡片里"这条产品要求。

    【为什么必须有这组断言】
    用户反馈"战斗次数被划到进阶"：根因是 ``battles`` / ``interval`` 被登记成
    **运行级**参数，于是网页把它们画进了「运行选项」卡片里的
    「进阶：场数与间隔」折叠块（那两块 UI 的渲染完全由规格表驱动）。
    修好之后必须留下回归断言 —— 否则下次谁"顺手"把它们挪回运行级，
    界面上会静悄悄地退回原样，而没有任何测试会变红。
    """

    def test_battles_and_interval_are_not_run_level(self) -> None:
        names = {param.name for param in RUN_LEVEL_PARAMS}
        assert "battles" not in names, "battles 又变成运行级参数了：它会跑出荣耀之巅卡片"
        assert "interval" not in names, "interval 又变成运行级参数了：同上"
        # 运行级只剩这两项：多一项就说明"某个任务专属的参数又被塞进来了"
        assert names == {"dry_run", "allow_diamond"}

    def test_top_pvp_owns_its_full_param_set(self) -> None:
        """场数 / 间隔 —— 荣耀之巅的可调项一个都不许跑到别的卡片里。

        ★ 2026-09-25 第二批：原第三项「保留战斗演出」按需求删除，
        战斗演出行为固定为跳过（见 ``tasks/top_pvp.py``）。
        """
        assert TASK_SPECS["top_pvp_battle"].param_names == ("battles", "interval")

    def test_keep_animation_flag_is_gone(self) -> None:
        """「保留战斗演出」的三层实现都要消失：规格参数、CLI 旗标、任务形参。

        【为什么必须连 CLI 那一层一起断言】
        ``--keep-battle-animation`` 一旦残留，用户仍能让"跳过演出"失效 ——
        而界面上已经没有这个开关，于是变成"看不见的开关还能被命令行打开"，
        比原来更难理解。任务形参由 ``tests/test_services_runner.py`` 的
        kwargs 校验兜住（名字写错就是 TypeError）。
        """
        assert "keep_battle_animation" not in _run_dests()
        assert TASK_SPECS["top_pvp_battle"].param("keep_battle_animation") is None

    def test_interval_default_is_15_seconds(self) -> None:
        """每场间隔默认 15 秒（2026-09-25 需求）。

        环境变量 ``CHERRYTALE_TOP_PVP_INTERVAL`` 可以覆盖默认值，
        所以先隔离它 —— 否则"在别人机器上跑"会因为环境不同而假失败。
        """
        if os.getenv(config.TOP_PVP_INTERVAL_ENV_NAME):
            pytest.skip("本机用环境变量覆盖了间隔，这里测不了默认值")
        assert config.TOP_PVP_INTERVAL_SECONDS == 15.0
        interval = TASK_SPECS["top_pvp_battle"].param("interval")
        assert interval is not None
        # 不填 = 用配置值（所以界面上的默认行为就是 config 里那个数）
        assert interval.coerce(None) is None
        assert "15" in interval.hint, "hint 里要写明默认 15 秒，否则用户在界面看不出默认值"

    def test_top_pvp_is_marked_as_consuming(self) -> None:
        """填了场数必须触发二次确认（``battles.danger`` → ``spec.consumes``）。"""
        assert TASK_SPECS["top_pvp_battle"].consumes is True
        battles = TASK_SPECS["top_pvp_battle"].param("battles")
        assert battles is not None and battles.danger is True
        # 间隔只是节奏控制，不该跟着标"消耗资源"（否则确认框里多一条噪音）
        interval = TASK_SPECS["top_pvp_battle"].param("interval")
        assert interval is not None and interval.danger is False


# ---------------------------------------------------------------------------
# ③ 取值转换
# ---------------------------------------------------------------------------
class TestCoerce:
    def test_int_accepts_string_and_integral_float(self) -> None:
        times = TASK_SPECS["sweep_activity"].param("times")
        assert times is not None
        assert times.coerce("2") == 2
        assert times.coerce(2.0) == 2
        assert times.coerce(3) == 3

    def test_int_rejects_bool_and_garbage(self) -> None:
        """bool 是 int 的子类：不显式拦住就会把 True 静默当成 1（真扣体力）。"""
        times = TASK_SPECS["sweep_activity"].param("times")
        assert times is not None
        with pytest.raises(ValueError):
            times.coerce(True)
        with pytest.raises(ValueError):
            times.coerce("abc")

    def test_empty_string_means_not_provided(self) -> None:
        area = TASK_SPECS["sweep_activity"].param("area")
        assert area is not None
        assert area.coerce("") is None
        assert area.coerce(None) is None
        assert area.coerce("  ") is None

    def test_bool_accepts_common_spellings(self) -> None:
        invite = TASK_SPECS["bbq_energy"].param("invite_friends")
        assert invite is not None
        assert invite.coerce("true") is True
        assert invite.coerce("0") is False
        assert invite.coerce(1) is True
        assert invite.coerce(None) is False
        with pytest.raises(ValueError):
            invite.coerce("大概吧")

    def test_float_param(self) -> None:
        # 取自「荣耀之巅」卡片的「每场间隔」（2026-09-25 起它不再是运行级参数）
        interval = _param("interval")
        assert interval.coerce("1.5") == 1.5
        assert interval.coerce(2) == 2.0
        # 不填 = None = "用 config 里的值"（与 0 的语义完全不同）
        assert interval.coerce(None) is None
        # 网页表单交上来的是字符串，空串也必须等于"没填"（否则会拿 "" 去 float()）
        assert interval.coerce("") is None

    def test_battles_default_none_means_use_config(self) -> None:
        # 同上：它现在是「荣耀之巅」卡片的「打几场」
        battles = _param("battles")
        assert battles.default is None
        assert battles.coerce(None) is None
        assert battles.coerce(0) == 0
        assert battles.coerce("3") == 3

    def test_danger_flags_mark_resource_consumers(self) -> None:
        """会消耗资源的参数必须标出来（UI 据此要求二次确认）。"""
        times = TASK_SPECS["sweep_activity"].param("times")
        invite = TASK_SPECS["bbq_energy"].param("invite_friends")
        area = TASK_SPECS["sweep_activity"].param("area")
        assert times is not None and times.danger is True
        assert invite is not None and invite.danger is True
        assert area is not None and area.danger is False


# ---------------------------------------------------------------------------
# ④ 规范化与命令行取值
# ---------------------------------------------------------------------------
class TestNormalize:
    def test_unknown_key_is_dropped(self) -> None:
        """未知键必须被丢掉：透传会让任务构造函数直接 TypeError。"""
        params, dropped = normalize_params("mail", {"bogus": 1})
        assert params == {}
        assert dropped == ["bogus"]

    def test_run_level_key_is_not_treated_as_unknown(self) -> None:
        params, dropped = normalize_params("mail", {"dry_run": "1"})
        assert params == {}
        assert dropped == []

    def test_declared_param_is_kept_and_coerced(self) -> None:
        """返回值是**完整参数集**（缺的补默认值），形状与 CLI 路径一致。"""
        params, dropped = normalize_params("sweep_activity", {"times": "2", "area": "3"})
        assert params == {
            "area": "3",
            "times": 2,
            "section": None,
            "allow_unpassed": False,
            # ★ 2026-10-06：内部参数（界面不画）也算"声明过的参数"，
            # 所以它必须出现在这里 —— 缺了它就说明它又被当未知键丢了。
            "prefetch_sections": False,
        }
        assert dropped == []

    def test_defaults_applied_when_missing(self) -> None:
        params, _ = normalize_params("bbq_energy", {})
        assert params == {
            "meal": None,
            "invite_morning": False,
            "invite_evening": False,
            "invite_friends": False,
        }

    def test_cli_task_params(self) -> None:
        ns = main.build_parser().parse_args(
            ["run", "sweep_activity", "--times", "2", "--area", "3"]
        )
        assert task_params_from_cli(ns, "sweep_activity") == {
            "area": "3",
            "times": 2,
            "section": None,
            "allow_unpassed": False,
            # ★ 2026-10-06：命令行**没有**这个旗标（内部参数），取默认值 ——
            # 键集合与结构化路径完全一致，这正是 TestInternalParams 守的性质。
            "prefetch_sections": False,
        }

    def test_cli_task_params_for_login(self) -> None:
        ns = main.build_parser().parse_args(["run", "login", "--account", "2"])
        assert task_params_from_cli(ns, "login") == {
            "account": "2",
            "server_id": None,
            "plan_only": False,
        }

    def test_cli_login_plan_only_and_server_id(self) -> None:
        """登录 / 选区两段式：命令行也要能表达"只拉列表"与"强制进某一区"。"""
        ns = main.build_parser().parse_args(
            ["run", "login", "--plan-only", "--server-id", "113"]
        )
        assert task_params_from_cli(ns, "login") == {
            "account": None,
            "server_id": 113,
            "plan_only": True,
        }

    def test_cli_run_level_defaults(self) -> None:
        """运行级参数只剩"演练 / 钻石总闸"（★ 2026-09-25 归位后）。"""
        ns = main.build_parser().parse_args(["run", "handshake"])
        assert run_level_from_cli(ns) == {
            "dry_run": False,
            "allow_diamond": False,
        }

    def test_cli_run_level_overrides(self) -> None:
        """``--battles`` / ``--interval`` 仍是 ``run`` 的旗标，但归荣耀之巅任务参数。

        ★ 2026-09-25：这两个值不再进 ``run_level_from_cli``，而是由
        ``task_params_from_cli(ns, "top_pvp_battle")`` 取出 —— 这正是
        "命令行用法不变、界面上参数却出现在荣耀之巅卡片里"能同时成立的原因。
        """
        ns = main.build_parser().parse_args(
            ["run", "top_pvp_battle", "--dry-run", "--battles", "3", "--interval", "0.5"]
        )
        assert run_level_from_cli(ns) == {"dry_run": True, "allow_diamond": False}
        assert task_params_from_cli(ns, "top_pvp_battle") == {
            "battles": 3,
            "interval": 0.5,
        }

    def test_unregistered_task_returns_empty(self) -> None:
        ns = main.build_parser().parse_args(["run", "handshake"])
        assert task_params_from_cli(ns, "no-such-task") == {}

    def test_both_paths_produce_same_shape(self) -> None:
        """同一组参数：命令行路径与结构化路径必须产出**完全相同**的 dict。

        这是"CLI 与 Web 不会漂移"最直接的一条断言：只要有人给其中一条
        路径加逻辑而忘了另一条，这里立刻失败。
        """
        from_cli = task_params_from_cli(
            main.build_parser().parse_args(["run", "bbq_energy", "--invite-friends"]), "bbq_energy"
        )
        from_struct = normalize_params(
            "bbq_energy", {"invite_friends": True, "dry_run": False}
        )[0]
        assert from_cli == from_struct == {
            "meal": None,
            "invite_morning": False,
            "invite_evening": False,
            "invite_friends": True,
        }

    def test_meal_and_per_meal_invites_round_trip(self) -> None:
        """★ 2026-10-05：网页早/晚双按钮的参数要能同时从两条路径走到位。"""
        ns = main.build_parser().parse_args(
            ["run", "bbq_energy", "--meal", "evening", "--invite-evening"]
        )
        from_cli = task_params_from_cli(ns, "bbq_energy")
        from_struct = normalize_params(
            "bbq_energy",
            {"meal": "evening", "invite_evening": True, "dry_run": False},
        )[0]
        assert from_cli == from_struct == {
            "meal": "evening",
            "invite_morning": False,
            "invite_evening": True,
            "invite_friends": False,
        }


# ---------------------------------------------------------------------------
# ⑤ 给 UI 的合并结果（规格表 + 任务类真实约束）
# ---------------------------------------------------------------------------
class TestDescribeTasks:
    # ------------------------------------------------------------------
    # 先来两条"必须在全新进程里跑"的守卫（原因见各用例的 docstring）
    # ------------------------------------------------------------------
    def _fresh_process(self, code: str) -> str:
        """在**全新解释器**里执行一段代码，返回它的 stdout。

        【为什么必须开子进程】
        pytest 在**收集阶段**就会 import 所有测试模块，而 ``tests/test_tasks.py``
        在后者的模块级注册了几个假任务 —— 也就是说本进程的注册表"恰好"是满的。
        于是"忘了加载任务注册表"这类缺陷在进程内**根本看不出来**：
        真实踩过一次 —— 网页上的任务清单把 ``login`` 标成"未实现"、复选框全灰，
        而当时所有单测都是绿的。
        子进程能还原"服务刚启动、还什么都没导入"的真实状态。
        """
        import subprocess
        import sys
        from pathlib import Path

        project_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_describe_tasks_loads_registry_in_fresh_process(self) -> None:
        """新进程里第一次调用 ``describe_tasks()`` 就必须认得出所有内置任务。"""
        import json

        code = (
            "import json;"
            "from services import task_spec;"
            "views={t['name']: t for t in task_spec.describe_tasks()};"
            "print(json.dumps({k: [views[k]['enabled'], views[k]['requires_auth'],"
            " views[k]['accepts_run_options']]"
            " for k in ('login','daily_box','handshake','top_pvp_battle')}))"
        )
        data = json.loads(self._fresh_process(code))
        assert data["login"] == [True, False, False]
        assert data["daily_box"] == [True, True, False]
        assert data["handshake"] == [True, False, False]
        assert data["top_pvp_battle"] == [True, True, True]

    def test_api_tasks_payload_is_correct_in_fresh_process(self) -> None:
        """``/api/tasks`` 的数据在新进程里也要正确（前端复选框直接依赖它）。"""
        import json

        # 不引入 httpx 依赖（那会是新依赖）：直接调用规格层拿"接口返回的那份数据"
        code = (
            "import json;"
            "from services import task_spec;"
            "views={t['name']: t for t in task_spec.describe_tasks()};"
            "sweep=views['sweep_activity']['params'];"
            "print(json.dumps([views['login']['enabled'], views['login']['title'],"
            " [p['name'] for p in sweep], views['bbq_energy']['diamond_purposes']]))"
        )
        data = json.loads(self._fresh_process(code))
        assert data[0] is True
        assert data[1] == "登录 / 换号"
        # ★ 2026-10-06：末尾多出的是**内部参数** ``prefetch_sections``。
        # 它必须出现在这份 payload 里：前端正是靠随行的 ``hidden`` 标记
        # 决定"不画它、也不把它塞进请求体"（``web/assets/app.js::visibleParams``）。
        assert data[2] == [
            "area",
            "times",
            "section",
            "allow_unpassed",
            "prefetch_sections",
        ]
        assert data[3] == ["BBQ_INVITE"]

    def _views(self) -> dict[str, dict[str, object]]:
        from services.task_spec import describe_tasks

        return {view["name"]: view for view in describe_tasks(main.load_builtin_tasks())}

    def test_merges_runtime_constraints_from_task_class(self) -> None:
        views = self._views()
        assert views["handshake"]["requires_auth"] is False
        assert views["daily_box"]["requires_auth"] is True
        assert views["top_pvp_battle"]["accepts_run_options"] is True

    def test_diamond_purposes_are_readable_names(self) -> None:
        """宴席的"花钱用途"要用枚举名显示（0 这种数值对用户毫无意义）。"""
        views = self._views()
        assert views["bbq_energy"]["diamond_purposes"] == ["BBQ_INVITE"]

    def test_opt_in_hint_is_carried_over(self) -> None:
        views = self._views()
        hint = str(views["bbq_energy"]["opt_in_hint"])
        assert "--eat" in hint

    def test_placeholder_is_disabled_and_unregistered(self) -> None:
        """★ 2026-10-06：``sign_in`` 占位已删除（登录签到转为内置钩子实现）。

        它不再是任务（见 ``tasks/login_signin.py``），所以规格表里
        **不该再有这个名字** —— 若谁把它加回去，就会在前端多出一张
        无法执行的"每日签到"卡片。
        """
        views = self._views()
        assert "sign_in" not in views

    def test_once_per_day_flag_reaches_the_view(self) -> None:
        """★ 2026-10-06：``once_per_day`` 必须送到前端。

        网页端拿它来实现在"勾选变动"这条路径上摘掉这批任务
        （用户要求它们"只在第一次登录工具时执行"，见
        ``web/assets/app.js::ONCE_PER_DAY_TRIGGERS``）。字段一旦丢了，
        前端只能靠 if 名字列表猜，迟早和后端名单漂移。
        """
        from services import task_spec

        views = self._views()
        for name in task_spec.ONCE_PER_DAY_TASKS:
            assert views[name]["once_per_day"] is True, f"{name} 应当把标记带到 view"
        assert views["daily_box"]["once_per_day"] is False
        assert views["mail"]["once_per_day"] is False

    def test_views_sorted_by_group_order(self) -> None:
        from services.task_spec import describe_tasks

        order = [key for key, _, _ in GROUPS]
        seen = [order.index(str(view["group"])) for view in describe_tasks()]
        assert seen == sorted(seen)

    def test_unregistered_task_falls_back_to_manual(self) -> None:
        """注册了但没登记规格的任务照样显示（只是进"主动任务"、默认不勾选）。

        兜底到 ``manual`` 而不是 ``auto`` 是刻意的：**默认勾选的名单只能放
        "确定无消耗"的任务**，没登记过的任务不该被自动选上。
        """

        class FakeTask:
            requires_auth = True
            __doc__ = "假任务：仅用于验证兜底显示"

        from services.task_spec import describe_tasks

        views = {view["name"]: view for view in describe_tasks({"fake_task": FakeTask})}
        assert views["fake_task"]["group"] == "manual"
        assert views["fake_task"]["default_checked"] is False
        assert views["fake_task"]["doc_summary"] == "假任务：仅用于验证兜底显示"


# ---------------------------------------------------------------------------
# ⑤ 「界面怎么渲染」的提示必须送到前端（★ 2026-09-22 新增）
# ---------------------------------------------------------------------------
class TestParamViewHints:
    """``choices`` / ``options_source`` 是界面渲染下拉的依据，不能丢在规格表里。

    【为什么要有这组测试】
    「活动区域 / 指定关卡」从"手输 ID"变成"下拉选择"，靠的就是这两个字段。
    如果 ``param_view()`` 忘了把它们带出去，前端只会拿到一个普通输入框 ——
    表现是"功能没生效"，而后端**其余测试全是绿的**（参数确实传给了任务）。
    所以这里直接守 ``/api/tasks`` 实际消费的那份 dict。
    """

    @staticmethod
    def _task_param(task: str, name: str) -> ParamSpec:
        for spec in TASK_SPECS[task].params:
            if spec.name == name:
                return spec
        raise AssertionError(f"任务 {task} 没有参数 {name}")

    def test_sweep_times_choices_match_the_real_client(self) -> None:
        """扫荡档位必须与 ``models.activity_stage`` 那份**完全一致**（含 0 = 只读）。

        两边各写一份、谁也不管谁，是这类"界面与协议对不上"的老毛病；
        这里做一次机器比对，以后任何一边改了而另一边没跟，立刻变红。

        ★ 2026-10-04：档位由 ``1/5/10`` 改为 ``1/5/「扫荡到清空体力」``，
        所以这里比的是 ``(0, 1, 5, SWEEP_UNTIL_EMPTY)``；哨兵**不**在
        ``SWEEP_TIME_CHOICES`` 里（那是"线上真正会发的 times"）。
        """
        from models.activity_stage import SWEEP_UNTIL_EMPTY

        assert self._task_param("sweep_activity", "times").choices == (
            0,
            1,
            5,
            SWEEP_UNTIL_EMPTY,
        )
        # 素材关同样带哨兵，但文案不同（"次数耗尽"而非"清空体力"）。
        assert self._task_param("sweep_material", "times").choices == (
            0,
            1,
            5,
            SWEEP_UNTIL_EMPTY,
        )

    def test_sweep_times_choice_labels_distinguish_the_sentinel(self) -> None:
        """同一个 ``-1`` 在活动/素材里文案必须不同（清空体力 vs 次数耗尽）。"""
        from models.activity_stage import SWEEP_UNTIL_EMPTY

        activity = self._task_param("sweep_activity", "times")
        material = self._task_param("sweep_material", "times")
        assert activity.choice_labels[SWEEP_UNTIL_EMPTY] == "扫荡到清空体力"
        assert material.choice_labels[SWEEP_UNTIL_EMPTY] == "扫荡到次数耗尽"

    def test_sweep_area_and_section_declare_candidate_sources(self) -> None:
        """区域 / 关卡必须声明"候选去哪拉"，否则前端只能画输入框。"""
        assert (
            self._task_param("sweep_activity", "area").options_source == "activity_areas"
        )
        assert (
            self._task_param("sweep_activity", "section").options_source
            == "activity_sections"
        )

    def test_param_view_carries_the_hints(self) -> None:
        from services.task_spec import param_view

        times = param_view(self._task_param("sweep_activity", "times"))
        assert times["choices"] == [0, 1, 5, -1]
        # 档位文案要一并带出去，否则前端不知道 -1 该显示成什么。
        assert times["choice_labels"] == {"-1": "扫荡到清空体力"}
        assert times["options_source"] == ""

        area = param_view(self._task_param("sweep_activity", "area"))
        assert area["choices"] == []
        assert area["options_source"] == "activity_areas"

    def test_hints_do_not_leak_into_coercion(self) -> None:
        """档位只是**界面提示**：命令行传 3 依然被接受（由任务自己向下归一）。"""
        assert self._task_param("sweep_activity", "times").coerce(3) == 3

    def test_activity_area_and_section_forbid_manual_input(self) -> None:
        """活动区域 / 指定关卡**不允许手输**（2026-09-22 用户要求）。

        这两项是"每期都换的随机 ID"，手输等于让用户去别处抄数字，抄错的症状
        （服务端静默拒绝）极难排查。所以规格里明确 ``allow_manual=False``，
        前端只画下拉；命令行不受影响（``main.py`` 不读规格表）。
        """
        assert self._task_param("sweep_activity", "area").allow_manual is False
        assert self._task_param("sweep_activity", "section").allow_manual is False
        # 次数是档位下拉，但**不禁止**手输语义（它没有候选来源，只是值域受限）
        assert self._task_param("sweep_activity", "times").allow_manual is True

    def test_sweep_material_area_and_section_declare_candidate_sources(self) -> None:
        """素材区域与关卡必须声明候选来源且禁止手动输入。"""
        area_spec = self._task_param("sweep_material", "area")
        section_spec = self._task_param("sweep_material", "section")
        assert area_spec.options_source == "material_areas"
        assert area_spec.allow_manual is False
        assert section_spec.options_source == "material_sections"
        assert section_spec.allow_manual is False

    def test_param_view_carries_allow_manual(self) -> None:
        from services.task_spec import param_view

        assert param_view(self._task_param("sweep_activity", "area"))["allow_manual"] is False
        assert (
            param_view(self._task_param("sweep_activity", "times"))["allow_manual"] is True
        )
        assert param_view(self._task_param("sweep_material", "area"))["allow_manual"] is False
        assert param_view(self._task_param("sweep_material", "section"))["allow_manual"] is False


# ---------------------------------------------------------------------------
# ⑤' 内部参数（★ 2026-10-06）
# ---------------------------------------------------------------------------
class TestInternalParams:
    """守住"内部参数既进得去、又漏不出来"。

    【为什么要有这组测试（2026-10-06 实测）】
    ``services/activity_cache_service.py`` 用 ``prefetch_sections=True`` 让
    ``sweep_activity`` 在刷新区域时顺带把**所有区域**的关卡候选一次拉回来
    （这样用户之后在「活动区域 / 指定关卡」下拉里换区不用再发包）。
    但规格表里**没有**登记这个键，于是 :func:`normalize_params` 把它当
    "不认识的参数"丢掉并打了一条 WARNING：

        ⚠ sweep_activity 忽略了不认识的参数：prefetch_sections

    后果不是"多一行提示"，而是功能**静默失效**：
    ``services/runner.py`` 里 ``if "prefetch_sections" in params`` 那段翻译
    永远走不到，预热从来没发生过 —— 用户每选一个新区域都要多发两个只读包。

    【为什么是 hidden 而不是普通参数】
    它是"缓存预热"这种实现细节，对使用者没有意义：画成开关只会让人纠结
    "要不要勾"。所以界面不画、命令行不给旗标，但它**仍算声明过的参数** ——
    :func:`normalize_params` 照旧收下它，两条路径的键集合保持一致。
    """

    @staticmethod
    def _param(task: str, name: str) -> ParamSpec:
        for spec in TASK_SPECS[task].params:
            if spec.name == name:
                return spec
        raise AssertionError(f"任务 {task} 没有参数 {name}")

    def test_prefetch_sections_is_declared_as_internal(self) -> None:
        param = self._param("sweep_activity", "prefetch_sections")
        assert param.kind == "bool"
        assert param.default is False
        assert param.hidden is True
        assert param.cli_flag == ""  # 命令行没有这个旗标（见上面的反向守卫）

    def test_prefetch_sections_is_kept_by_normalization(self) -> None:
        """★ 核心回归：它必须**活着**穿过 normalize_params，且不再报"不认识"。"""
        params, dropped = normalize_params("sweep_activity", {"prefetch_sections": True})
        assert dropped == [], "内部参数被当成未知键丢掉了（功能会静默失效）"
        assert params["prefetch_sections"] is True
        # 其余声明参数照旧带默认值：返回值形状不变
        assert params["times"] == 0
        assert params["area"] is None
        assert params["allow_unpassed"] is False

    def test_default_is_false_when_not_provided(self) -> None:
        """没人设置时取默认值（普通调用方 / 命令行路径都是这一种）。"""
        params, dropped = normalize_params("sweep_activity", {"times": 1})
        assert dropped == []
        assert params["prefetch_sections"] is False

    def test_cli_path_produces_the_same_shape(self) -> None:
        """命令行永远给不出这个键 → 取默认值；两条路径的键集合必须一致。"""
        ns = main.build_parser().parse_args(["run", "sweep_activity", "--times", "1"])
        from_cli = task_params_from_cli(ns, "sweep_activity")
        from_struct = normalize_params("sweep_activity", {"times": 1})[0]
        assert from_cli == from_struct
        assert from_cli["prefetch_sections"] is False

    def test_param_view_marks_it_hidden(self) -> None:
        """前端靠 ``hidden`` 决定"不画它"（``web/assets/app.js::visibleParams``）。"""
        from services.task_spec import param_view

        assert param_view(self._param("sweep_activity", "prefetch_sections"))["hidden"] is True
        # 普通参数照旧 False（漏了这条，前端会把所有参数都藏起来）
        assert param_view(self._param("sweep_activity", "times"))["hidden"] is False


if __name__ == "__main__":  # 支持 `python -m tests.test_services_task_spec`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))


