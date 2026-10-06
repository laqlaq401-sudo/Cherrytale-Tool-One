"""任务规格表：CLI 与 Web 共用的「参数面」唯一来源。

【为什么要有这个模块】
在它出现之前，「每个任务接受哪些参数」只存在于 ``main.py`` 的 ``argparse``
定义里。Web 前端要画表单就得**再抄一遍**，而抄错的表现是
「网页上填了参数、后端悄悄忽略」—— 这种 bug 几乎不会被发现。

抽成规格表之后：

    main.py（CLI） ─┐
                    ├─→ services/task_spec.py（参数面唯一来源）
    webapi/（HTTP）─┘

``tests/test_services_task_spec.py`` 还会做机器化校验：本文件里每个
:class:`ParamSpec` 的 :attr:`~ParamSpec.name` 必须真实出现在
``main.build_parser()`` 解析出的字段集合中。任何一边加了参数却忘了另一边，
测试立刻变红 —— 这比"人工记得同步"可靠得多。

【三类参数，别混在一起】
1. **任务级参数**（:data:`TASK_SPECS` 里的 ``params``）：只对某个任务有意义，
   例如扫荡的 ``--times``、宴席的 ``--eat``；
2. **运行级参数**（:data:`RUN_LEVEL_PARAMS`）：所有任务共用，例如 ``--dry-run``
   （2026-09-25 起只剩"演练"与"钻石总闸"两项：``battles`` / ``interval`` 已归位成
   荣耀之巅的任务级参数 —— 判据就是下面这条"是不是每个任务都认得它"）；
3. **凭据类参数**（账号 / 密码）：只在内存里传递，绝不落盘、绝不入日志，
   因此相关约束写在 ``services/runner.py``，不在本文件。

【默认值为什么全写成「什么都不做」】
``times=0`` / ``battles=0`` 表示只读：**不填就等于不发消耗类请求**。
这与 ``tasks/base.py`` 的 ``RunOptions``、``client/spending.py`` 的钻石闸门
是同一套 fail-closed 哲学，规格表只是把它如实描述出来，供 UI 展示与校验。

.. warning::
   规格表里**没有**任何"执行逻辑"：它只说"有什么参数、默认多少、
   值长什么样"，怎么用这些参数由 ``services/runner.py`` 决定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Mapping

from models.activity_stage import SWEEP_UNTIL_EMPTY

if TYPE_CHECKING:  # 仅用于类型检查，运行时不导入，避免多余依赖
    import argparse

    from tasks.base import BaseTask

_LOGGER: Final[logging.Logger] = logging.getLogger("services.task_spec")


# ---------------------------------------------------------------------------
# 类型别名与分组
# ---------------------------------------------------------------------------
#: 参数的值类型。故意只保留这四种：再复杂的结构（列表 / 对象）用命令行表达
#: 都会变得别扭，真需要时应该新增一个任务，而不是给参数加结构。
ParamKind = Literal["int", "float", "str", "bool"]

#: 分组键。UI 按这个顺序展示卡片，命令行入口不使用它。
#:
#: 【为什么从"按领取方式分"改成"按风险分"】
#: 旧分组是 ``session / daily / claim / readonly / advanced``：那是**开发者视角**
#: （这个任务是干什么的）。使用者关心的是完全不同的一件事 ——
#: **"我能不能无脑全勾上"**。所以现在只有三档：
#:
#: * ``auto``（自动任务）：纯领取 / 只读，不消耗任何资源 → 默认勾选；
#: * ``manual``（主动任务）：会消耗资源或需要参数 → 默认不勾、运行前二次确认；
#: * ``planned``（待实现）：协议还没确认，只占位。
GroupKey = Literal["auto", "manual", "planned"]

#: ``(分组键, 中文标题, 一句话说明)`` —— 顺序即 UI 展示顺序。
GROUPS: Final[tuple[tuple[GroupKey, str, str], ...]] = (
    ("auto", "自动任务", "纯领取 / 只读：不消耗任何资源，可以无脑跑（默认勾选）"),
    ("manual", "主动任务", "会消耗资源或需要参数：默认不勾选，运行前会二次确认"),
    ("planned", "待实现", "协议尚未确认，仅作占位（不会被执行）"),
)


@dataclass(frozen=True)
class ParamSpec:
    """一个参数的完整描述（既是 CLI 旗标的说明，也是 Web 表单字段的定义）。

    :param name: 结构化键名。与 ``argparse`` 的 ``dest`` **保持一致**，
        这样"CLI 参数 ↔ 结构化参数"的翻译不需要额外映射表，
        一致性测试也能直接用集合比对。
    :param kind: 值类型（见 :data:`ParamKind`）。
    :param default: 默认值。**None 表示"没填"**，具体含义由任务决定
        （例如扫荡的 ``times=None`` 与 ``times=0`` 在下游都等价于"只读"）。
    :param label: 中文短标签（Web 表单左侧文字）。
    :param hint: 一句话说明（Web 帮助文本；风格与 CLI 的 ``--help`` 一致）。
    :param cli_flag: 命令行旗标写法，如 ``"--times"``。用于文档与一致性测试。
    :param danger: 该参数**会消耗玩家资源**（次数 / 体力 / 扫荡券 / 钻石）时置 True。
        UI 用它决定是否必须二次确认；这与"默认只读"是两道独立的防线。
    :param from_config: 默认值的来源说明（例如 ``"TOP_PVP_BATTLES"``），
        用于告诉用户"不填会发生什么"。
    :param choices: 固定档位（可选项集合）。非空时界面渲染成下拉，空则表示自由输入。
        命令行**不受影响** —— 档位只约束界面，不改变 ``coerce`` 的行为。
    :param options_source: 动态候选来源标识（如 ``"activity_areas"``）。
        非空表示"这个参数的候选要联网从游戏里拉"，由 ``webapi/`` 的预览接口按标识提供；
        空串表示没有候选。**有候选也不排除手输**（见字段注释）。
    :param allow_manual: 是否允许用户**手动填值**。``False`` 时界面只画下拉
        （没有候选就无法提交），命令行不受影响 —— 见字段注释。
    """

    name: str
    kind: ParamKind
    default: Any
    label: str
    hint: str
    cli_flag: str
    danger: bool = False
    from_config: str = ""
    #: 固定档位（可选项集合）。非空时 UI 渲染成**下拉**而不是自由输入框。
    #:
    #: 【为什么要档位而不是自由数字（2026-09-22 新增）】
    #: 真实客户端的扫荡面板只有 **1 / 5 / 10** 三档（``StageModule.eSweepType``，
    #: 见 ``models/activity_stage.py::SWEEP_TIME_CHOICES``）。给一个自由数字框，
    #: 用户既不知道该填几，填了也会被 ``normalize_sweep_times`` 悄悄向下归位 ——
    #: 界面直接给档位，才是"所见即所发"。
    #:
    #: .. note::
    #:    命令行**不受影响**：``--times`` 仍是普通 int。档位只约束界面展示，
    #:    与 ``coerce`` 无关（所以 ``ParamKind`` 不需要新增 "choice"）。
    #:    两处取值的一致性由 ``tests/test_services_task_spec.py`` 机器校验。
    choices: tuple[Any, ...] = ()
    #: 档位值的**自定义显示文案**（``{值: 文案}``）。空时前端按类型自动生成
    #: （``0`` → "0（只读：什么都不扫）"、``5`` → "5 次"）。
    #:
    #: 【为什么需要它（2026-10-04）】
    #: 同一个数值在不同任务里含义不同：哨兵 ``-1`` 在活动关是"扫荡到清空体力"、
    #: 在素材关是"扫荡到次数耗尽"。文案必须随任务变化，所以下放到参数规格里，
    #: 而不是让前端按 ``-1`` 猜。
    choice_labels: Mapping[Any, str] = field(default_factory=dict)
    #: 动态候选来源标识（如 ``"activity_areas"`` / ``"activity_sections"``）。
    #:
    #: 【为什么登记"标识"而不是"候选本身"】
    #: 候选必须联网从游戏里拉（当期活动区按周轮换，实测起止时间相差 14 天），
    #: 写死在规格表里等于把会过期的数据当常量。这里只说明"去哪一类候选里取"，
    #: 由 ``webapi/`` 的预览接口按标识返回当时的真实列表（见 ``notes/web_layer.md``）。
    #: 空串表示该参数没有候选，界面照旧渲染成输入框。
    #:
    #: .. important::
    #:    有候选**不等于**只能从候选里选：手动填 areaID / sectionID 始终可用 ——
    #:    候选只是省去"翻日志抄 ID"，不是一层新限制。
    options_source: str = ""
    #: 是否允许**手动填值**（默认允许）。``False`` 时界面只画下拉。
    #:
    #: 【为什么需要这个开关（2026-09-22 用户要求）】
    #: 活动区域 / 关卡号是"每期都换的随机 ID"，让用户手输等于让他去别处抄数字 ——
    #: 抄错的表现是"任务跑完什么都没发生（服务端拒绝）"，排查成本极高。
    #: 所以这两个参数明确 ``allow_manual=False``：**只能从候选里选**，
    #: 没有候选（没登录 / 缓存为空）时界面会说清"为什么选不了"，而不是给一个
    #: 看似能用、填错却很难查的输入框。
    #:
    #: .. note::
    #:    命令行**不受影响**：``--area`` 依旧支持 areaID / 清单编号 / 名称关键字。
    #:    这个开关只约束网页表单（``main.py`` 是手写 argparse，不读规格表）。
    allow_manual: bool = True
    #: 是否为**内部参数**：只由**进程内调用方**设置，界面不渲染、命令行不暴露。
    #:
    #: 【为什么需要这一类（★ 2026-10-06）】
    #: 有些键是任务构造函数**真的收**的，但属于"实现细节"而非"用户选项"，
    #: 例如 ``sweep_activity`` 的 ``prefetch_sections``（缓存预热：刷新区域时
    #: 顺带把所有区域的关卡拉回来，由 ``services/activity_cache_service.py``
    #: 设置）。它必须被规格表**声明**，否则 ``normalize_params`` 会把它当
    #: "不认识的参数"丢掉 —— 于是 ``services/runner.py`` 里那段翻译代码
    #: 永远走不到，功能**静默失效**，用户只看到一行看不懂的告警。
    #: 但它又**不该**画在表单里：多一个没人知道该怎么选的开关，只是噪音。
    #:
    #: .. important::
    #:    ``hidden`` 只改变"露出方式"，不改变"是否算声明过的参数"：
    #:    :func:`normalize_params` 照旧收下它（缺省取 :attr:`default`），
    #:    所以"结构化参数"与"命令行参数"的**键集合仍然完全一致**
    #:    （命令行取不到 → 默认值）。两条路径的形状一致性由
    #:    ``tests/test_services_task_spec.py::TestInternalParams`` 钉住。
    #:
    #: .. note::
    #:    ``cli_flag`` 留空是这类参数的正常状态 —— 命令行**没有**对应旗标
    #:    （"不能有隐形旗标"那条守卫因此是**反向**判据：内部参数不许出现在
    #:    命令行的字段名里，见 ``test_spec_param_names_exist_in_cli``）。
    hidden: bool = False

    def coerce(self, value: Any) -> Any:
        """把外部输入（命令行字符串 / HTTP JSON）转成本参数期望的类型。

        :param value: 原始值；``None`` 与空字符串都表示"没填"。
        :return: 转换后的值；"没填"时返回 :attr:`default`。
        :raises ValueError: 值无法按 :attr:`kind` 解释时
            （错误信息用中文写明"哪个参数、期望什么、收到什么"，
            让用户不必去翻代码）。

        【为什么 bool 要单独处理】
        Python 里 ``True`` 是 ``int`` 的子类。如果先按 int 分支处理，
        ``--times true`` 会变成 ``times=1`` 这种荒唐结果而不报错。
        所以 :attr:`kind` 为数值型时**显式拒绝** bool，避免"静默算错"。

        【为什么空字符串等于"没填"】
        命令行的 ``--area ""`` 与 HTTP 表单里的空输入框，语义都是
        "用户没提供这个值"。统一成 ``None`` 之后，下游任务只需判断
        ``is None`` 一种情况，不必同时处理 ``None`` 和 ``""``。
        """
        if value is None:
            return self.default

        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            text = str(value).strip().lower()
            if text in ("", "0", "false", "no", "off"):
                return False
            if text in ("1", "true", "yes", "on"):
                return True
            raise ValueError(f"参数 {self.name} 需要布尔值，收到 {value!r}")

        if self.kind == "int":
            if isinstance(value, bool):  # 见上方说明：bool 先拦住
                raise ValueError(f"参数 {self.name} 需要整数，收到布尔值 {value!r}")
            if isinstance(value, int):
                return value
            if isinstance(value, float) and value.is_integer():
                # JSON 里 3.0 与 3 都可能出现，整数漂移不该让用户困惑
                return int(value)
            text = str(value).strip()
            if not text:
                return self.default
            try:
                return int(text, 10)
            except ValueError as exc:
                raise ValueError(f"参数 {self.name} 需要整数，收到 {value!r}") from exc

        if self.kind == "float":
            if isinstance(value, bool):
                raise ValueError(f"参数 {self.name} 需要小数，收到布尔值 {value!r}")
            if isinstance(value, (int, float)):
                return float(value)
            text = str(value).strip()
            if not text:
                return self.default
            try:
                return float(text)
            except ValueError as exc:
                raise ValueError(f"参数 {self.name} 需要小数，收到 {value!r}") from exc

        # 剩下的就是字符串：空串按"没填"处理（见 docstring）
        text = str(value).strip()
        return text or None

    def describe(self) -> str:
        """一行摘要（调试日志与 UI 提示都能用）。"""
        parts = [f"{self.cli_flag}={self.default!r}（{self.label}）"]
        if self.danger:
            parts.append("会消耗资源")
        if self.from_config:
            parts.append(f"默认取自 config.{self.from_config}")
        return "；".join(parts)


@dataclass(frozen=True)
class TaskSpec:
    """一个任务"在 UI 里长什么样、接受哪些参数"。

    :param name: 注册名，必须与 ``tasks/base.py`` 里 ``@register_task`` 的
        ``name`` 完全一致。
    :param group: 所属分组（见 :data:`GROUPS`），决定 UI 卡片位置。
    :param title: 中文标题（UI 显示用）。
    :param summary: 一句话说明（尽量与任务类文档首行一致，但更适合新手阅读）。
    :param params: 该任务接受的**任务级**参数（不含运行级参数）。
    :param default_checked: UI 初次打开时是否默认勾选。
    :param enabled: ``False`` 表示"占位项"：协议未确认，UI 灰显、后端拒绝执行。
    :param once_per_day: **每个游戏日只自动执行一次**（2026-10-04 用户需求）。
        为 ``True`` 的任务，服务端会把"今天跑过"记进
        ``services/daily_state.py`` 的记账文件，**不依赖浏览器 localStorage**
        （那份记录换端口 / 换入口就会丢，详见该模块的文档）。
        本游戏日内重复登录不会再自动跑它；卡片上的「执行」小按钮属于手动路径，
        不受这条限制。
        ★ 2026-10-06 追加：自动执行时它**只跟"启动"这一类触发走**
        （进主界面 / 登录成功 / 切区，以及启动时撞 409 后的补跑），
        网页端"勾选变动"不会再捎带提交它们 —— 用户要求"只有第一次登录工具时执行"。
        详见 :data:`ONCE_PER_DAY_TASKS`。
    :param hidden: 是否**不出现在任务清单里**。例如 ``login``：它由"登录 / 选区"
        专用界面负责，留在清单里反而会让人以为"勾上它就能登录"。
        （CLI 不受影响：``python main.py run login`` 照常可用。）

    【运行期属性为什么不写在这里】
    ``requires_auth`` / ``spends_diamond`` / ``diamond_purposes`` /
    ``accepts_run_options`` 这些**约束**由任务类自己声明（见 ``tasks/base.py``）。
    如果在规格表里再抄一遍，两处早晚不一致。所以 :func:`describe_tasks`
    在运行时把两者合并：**规格表负责"显示什么"，任务类负责"真实约束"**。
    """

    name: str
    group: GroupKey
    title: str
    summary: str
    params: tuple[ParamSpec, ...] = ()
    default_checked: bool = False
    enabled: bool = True
    #: 每个游戏日只自动执行一次（见 :data:`services.daily_state` 的说明）。
    once_per_day: bool = False
    hidden: bool = False

    def param(self, name: str) -> ParamSpec | None:
        """按名取参数规格；该任务不接受此参数时返回 ``None``。"""
        for spec in self.params:
            if spec.name == name:
                return spec
        return None

    @property
    def param_names(self) -> tuple[str, ...]:
        """本任务接受的全部参数名（顺序即声明顺序，Web 表单按此渲染）。"""
        return tuple(spec.name for spec in self.params)

    @property
    def consumes(self) -> bool:
        """是否存在"会消耗玩家资源"的参数（UI 据此要求二次确认）。"""
        return any(spec.danger for spec in self.params)


# ---------------------------------------------------------------------------
# 运行级参数：所有任务都可能用到（与 CLI 的全局旗标一一对应）
# ---------------------------------------------------------------------------
DRY_RUN_PARAM: Final[ParamSpec] = ParamSpec(
    name="dry_run",
    kind="bool",
    default=False,
    label="演练模式",
    hint="只组装并打印将要发送的字节，不联网、不发包（第一次跑务必先用它）",
    cli_flag="-n/--dry-run",
)

ALLOW_DIAMOND_PARAM: Final[ParamSpec] = ParamSpec(
    name="allow_diamond",
    kind="bool",
    default=False,
    label="允许花钻石（总闸）",
    hint="默认关闭。关闭时任何花钻石的请求连字节都不会被组装出来",
    cli_flag="--allow-diamond",
)

# ★ 2026-09-25：这里**只剩**真正全局的两项。
#
# 【为什么 ``battles`` / ``interval`` 被搬走了】
# 它们原本登记在这里，于是网页把"场数 / 间隔"画成了「运行选项」卡片里的一个
# 「进阶：场数与间隔（只对「荣耀之巅」等任务生效）」折叠块 —— 用户看到的是
# "我点的是荣耀之巅，战斗次数怎么跑到别的板块去了"。而这两个值全项目只有
# 荣耀之巅一个消费者（``tasks/base.py::RunOptions``），登记成运行级本就名不副实。
# 现在它们是 ``top_pvp_battle`` 的任务级参数（见下方 ``BATTLES_PARAM``），
# 由规格表驱动、自动出现在那张卡片的参数区里（前端零特例、不写死任务名）。
# 命令行**不受影响**：``--battles`` / ``--interval`` 仍是 ``run`` 子命令的旗标。
RUN_LEVEL_PARAMS: Final[tuple[ParamSpec, ...]] = (
    DRY_RUN_PARAM,
    ALLOW_DIAMOND_PARAM,
)


# ---------------------------------------------------------------------------
# 任务级参数：只对某一个任务有意义
# ---------------------------------------------------------------------------
#: ``login``：账号选择器（序号 / 邮箱 / userId）。
#: 为什么它是"任务级"而不是运行级：只有 login 任务的构造函数认识它，
#: 其它任务收到会直接 TypeError（见 ``main.py`` 里"按声明过滤"的先例）。
ACCOUNT_PARAM: Final[ParamSpec] = ParamSpec(
    name="account",
    kind="str",
    default=None,
    label="账号选择器",
    hint="账号序号 / 邮箱 / userId",
    cli_flag="--account",
)

#: ``login``：强制进入的区服编号（网页「选区」界面点的那一个）。
SERVER_ID_PARAM: Final[ParamSpec] = ParamSpec(
    name="server_id",
    kind="int",
    default=None,
    label="区服编号",
    hint="指定进入的区服编号",
    cli_flag="--server-id",
)

#: ``login``：只拉区服列表就返回（登录 / 选区两段式流程的**第一段**）。
PLAN_ONLY_PARAM: Final[ParamSpec] = ParamSpec(
    name="plan_only",
    kind="bool",
    default=False,
    label="只拉区服列表",
    hint="仅查询区服列表，不进入游戏",
    cli_flag="--plan-only",
)

#: ``bbq_energy``：确认吃宴席（发送 15003）。
EAT_PARAM: Final[ParamSpec] = ParamSpec(
    name="eat",
    kind="bool",
    default=False,
    label="确认吃宴席",
    hint="确认享用宴席",
    cli_flag="--eat",
    danger=True,
)

#: ``bbq_energy``：花钻石邀请好友（钻石闸门的**用途**开关，隐含吃）。
#: 【历史用途保留】整场（无 ``meal``）与 CLI 的 ``--invite-friends`` 仍走它；
#: 网页的早/晚两个按钮改用下面两个**分顿**开关。
INVITE_FRIENDS_PARAM: Final[ParamSpec] = ParamSpec(
    name="invite_friends",
    kind="bool",
    default=False,
    label="邀请好友（花钻石）",
    hint="邀请好友（消耗钻石）",
    cli_flag="--invite-friends",
    danger=True,
    from_config="BBQ_INVITE_FRIENDS",
)

#: ``bbq_energy``：指定要吃的**哪一顿**（``morning`` 早宴席 / ``evening`` 晚宴席）。
#:
#: 【为什么是参数而不是写死的两个任务名（2026-10-05）】
#: 前端把「执行」拆成早 / 晚两个按钮，但**任务仍是同一个** ``bbq_energy`` ——
#: 两个按钮只差这一个参数。做成参数（而不是新注册一个 ``bbq_energy_evening`` 任务）
#: 有三个好处：钱包 / 闸门 / 结果展示 / 记账都只有一份实现；CLI 与网页共用同一入口；
#: 将来加"第三顿"只是多一个取值。
#:
#: 【默认 None = 整场（旧行为）】
#: 不填该参数时，``bbq_energy`` 的执行路径**与改造前逐字节相同** ——
#: 既有的 CLI 用法与自动化不受任何影响。只有网页点了具体某一顿才带上它。
MEAL_PARAM: Final[ParamSpec] = ParamSpec(
    name="meal",
    kind="str",
    default=None,
    label="宴席时段",
    hint="不填 = 整场；morning = 早宴席；evening = 晚宴席",
    cli_flag="--meal",
    choices=("morning", "evening"),
    choice_labels={"morning": "早宴席", "evening": "晚宴席"},
)

#: ``bbq_energy``：**早宴席**那个按钮自己的邀请开关（网页用；与晚宴席互不影响）。
INVITE_MORNING_PARAM: Final[ParamSpec] = ParamSpec(
    name="invite_morning",
    kind="bool",
    default=False,
    label="早宴席·邀请好友（花钻石）",
    hint="早宴席是否花钻石邀请好友；仅早宴席的时间窗内有效",
    cli_flag="--invite-morning",
    danger=True,
)

#: ``bbq_energy``：**晚宴席**那个按钮自己的邀请开关（网页用；与早宴席互不影响）。
INVITE_EVENING_PARAM: Final[ParamSpec] = ParamSpec(
    name="invite_evening",
    kind="bool",
    default=False,
    label="晚宴席·邀请好友（花钻石）",
    hint="晚宴席是否花钻石邀请好友；仅晚宴席的时间窗内有效",
    cli_flag="--invite-evening",
    danger=True,
)

#: ``sweep_activity``：目标活动区域。
#:
#: ``options_source`` 让网页把它渲染成**下拉**：点「拉取」时后端会发 11025（只读），
#: 把当期开放区域连同进度 / 起止时间一起填进选项（见 ``webapi/app.py`` 的预览接口）。
#: 手输 areaID 依旧可用（候选只是省去抄 ID）。
AREA_PARAM: Final[ParamSpec] = ParamSpec(
    name="area",
    kind="str",
    default=None,
    label="活动区域",
    hint="选择目标活动区域",
    cli_flag="--area",
    options_source="activity_areas",
    allow_manual=False,
)

#: ``sweep_activity``（降临/活动关）：**每个关卡**最多扫几次。
#:
#: 【★ 2026-10-04：档位从 1/5/10 改为 1/5/「扫荡到清空体力」】
#: 真实需求是"远超 10 次"的扫荡，而客户端一次操作最多也就 5 次（抓包见
#: ``notes/sweep_capture.md`` §五：一次操作 = 一个 ``times=N`` 的包）。所以：
#: - ``1`` / ``5``：与原来一样，一次标准操作；
#: - ``-1``（:data:`models.activity_stage.SWEEP_UNTIL_EMPTY`）：**反复发「扫荡 5 次」
#:   直到体力清空**（服务端回体力不足即停），而不是把一个 50 次塞进一个包。
#: 保留了 ``0``（只读）作为安全默认 —— 否则默认值会变成"1 次"，太危险。
ACTIVITY_TIMES_PARAM: Final[ParamSpec] = ParamSpec(
    name="times",
    kind="int",
    default=0,
    label="每关扫荡次数",
    hint="0 为只读；可选 1 / 5 / 扫荡到清空体力",
    cli_flag="--times",
    danger=True,
    from_config="SWEEP_MAX_TIMES",
    choices=(0, 1, 5, SWEEP_UNTIL_EMPTY),
    choice_labels={SWEEP_UNTIL_EMPTY: "扫荡到清空体力"},
)

#: ``sweep_material``（素材/元素关）：**每个关卡**最多扫几次。
#:
#: 【与活动关的唯一区别在文案】素材关**不消耗体力**（消耗每日次数），所以哨兵
#: ``-1`` 的含义是"扫荡到次数耗尽"（服务端回 ``TimesIsFull = -7``），
#: 而不是"清空体力"。因此这里**必须**是与 :data:`ACTIVITY_TIMES_PARAM` 分开的
#: 一个规格（共用会把文案说错）。
MATERIAL_TIMES_PARAM: Final[ParamSpec] = ParamSpec(
    name="times",
    kind="int",
    default=0,
    label="每关扫荡次数",
    hint="0 为只读；可选 1 / 5 / 扫荡到次数耗尽",
    cli_flag="--times",
    danger=True,
    from_config="SWEEP_MAX_TIMES",
    choices=(0, 1, 5, SWEEP_UNTIL_EMPTY),
    choice_labels={SWEEP_UNTIL_EMPTY: "扫荡到次数耗尽"},
)

#: ``sweep_activity``：只扫指定的一个关卡。
SECTION_PARAM: Final[ParamSpec] = ParamSpec(
    name="section",
    kind="str",
    default=None,
    label="指定关卡",
    hint="选择指定关卡（不指定则扫全部关卡）",
    cli_flag="--section",
    options_source="activity_sections",
    allow_manual=False,
)

#: ``sweep_activity``：是否允许首次通关简单关卡。
ALLOW_UNPASSED_PARAM: Final[ParamSpec] = ParamSpec(
    name="allow_unpassed",
    kind="bool",
    default=False,
    label="允许首次通关简单关卡",
    hint="自动攻打未通关的简单关卡，首通拿三星后继续扫荡",
    cli_flag="--allow-unpassed",
)

#: ``sweep_activity``：**内部参数** —— 只读发现区域时，顺带把所有区域的关卡一并拉回来。
#:
#: 【谁在用它（★ 2026-10-06）】
#: 只有 ``services/activity_cache_service.py::_fetch(..., prefetch_sections=True)``：
#: 网页点「更新区域」/ 缓存过期时刷新一次，就把**所有**当期区域的关卡候选
#: 一次填进缓存，之后用户在「活动区域 / 指定关卡」下拉里换区**一个包都不用发**。
#: 不预热的话，每选一个新区域都要再跑一次 ``11025 + 11003``（两个只读包）。
#:
#: 【为什么是 hidden 而不是普通参数】
#: 它是"缓存预热"这种实现细节，对使用者没有意义 —— 画成开关只会让人纠结
#: "要不要勾"。命令行同理不给旗标（``cli_flag=""``）。
#: 但它**必须登记**：不登记就会被 :func:`normalize_params` 当未知键丢掉，
#: 于是 ``services/runner.py`` 里那段翻译永远走不到、预热静默失效
#: （2026-10-06 实测：用户只看到一行"忽略了不认识的参数：prefetch_sections"）。
PREFETCH_SECTIONS_PARAM: Final[ParamSpec] = ParamSpec(
    name="prefetch_sections",
    kind="bool",
    default=False,
    label="预拉取全部区域关卡",
    hint="内部使用：刷新区域时顺带把每个区域的关卡候选也存进缓存",
    cli_flag="",
    hidden=True,
)

#: ``sweep_material``：素材关卡大类筛选。
CATEGORY_PARAM: Final[ParamSpec] = ParamSpec(
    name="category",
    kind="str",
    default="all",
    label="素材大类",
    hint="选择素材分类",
    cli_flag="--category",
    choices=("all", "job", "element"),
)

#: ``sweep_material``：素材目标区域。
MATERIAL_AREA_PARAM: Final[ParamSpec] = ParamSpec(
    name="area",
    kind="str",
    default=None,
    label="目标区域",
    hint="选择目标素材区域（不指定则展示目录/全部扫荡）",
    cli_flag="--area",
    options_source="material_areas",
    allow_manual=False,
)

#: ``sweep_material``：指定关卡编号。
MATERIAL_SECTION_PARAM: Final[ParamSpec] = ParamSpec(
    name="section",
    kind="str",
    default=None,
    label="指定关卡",
    hint="选择指定关卡（不指定则默认扫该区最高难度）",
    cli_flag="--section",
    options_source="material_sections",
    allow_manual=False,
)

#: ``buy_energy``：用钻石购买体力次数（0 = 只读）。
BUY_ENERGY_TIMES_PARAM: Final[ParamSpec] = ParamSpec(
    name="times",
    kind="int",
    default=0,
    label="购买次数",
    hint="购买体力次数（每次 120 体力）",
    cli_flag="--times",
    danger=True,
    from_config="BUY_ENERGY_TIMES",
    choices=(0, 1, 2, 3, 5, 10),
)

#: ``buy_energy``：钻石消耗上限（0 = 不限制）。
BUY_ENERGY_MAX_COST_PARAM: Final[ParamSpec] = ParamSpec(
    name="max_cost",
    kind="int",
    default=0,
    label="钻石消耗上限",
    hint="最多允许消耗的钻石数（0 为不限）",
    cli_flag="--max-cost",
    danger=True,
    from_config="BUY_ENERGY_MAX_COST",
)

#: ``top_pvp_battle``：本次最多打几场（0 = 只读）。
#:
#: 【它为什么不登记成"运行级参数"（2026-09-25 归位）】
#: 语义上它像"这一次运行打几场"，但**只有荣耀之巅认识它**。登记成运行级时，
#: 网页会把它画到「运行选项」卡片的「进阶」折叠块里 —— 用户点开的是荣耀之巅，
#: 参数却在别处，于是有了"战斗次数被划到进阶"这个反馈。
#: 改成任务级之后：卡片里直接可见、填了会进二次确认（``danger=True`` 使
#: ``TaskSpec.consumes`` 为真）、卡片上还会出现「有消耗」徽标。
#: 命令行用法一字未变（``--battles`` 仍是 ``run`` 子命令的旗标，
#: ``task_params_from_cli`` 按同名 ``dest`` 自动取到）。
BATTLES_PARAM: Final[ParamSpec] = ParamSpec(
    name="battles",
    kind="int",
    default=None,
    label="打几场",
    hint="挑战场数（0 为只读查询）",
    cli_flag="--battles",
    danger=True,
    from_config="TOP_PVP_BATTLES",
)

#: ``top_pvp_battle``：两场之间的间隔秒数。
INTERVAL_PARAM: Final[ParamSpec] = ParamSpec(
    name="interval",
    kind="float",
    default=None,
    label="每场间隔（秒）",
    hint="战斗间隔时间（秒，默认 15 秒）",
    cli_flag="--interval",
    from_config="TOP_PVP_INTERVAL_SECONDS",
)

#: ``push_main_stage``：指定目标关卡（sectionID）。
#:
#: 【★ 2026-10-05 去掉 hint 里的长 ID】原文案举例写的是一个 9 位 sectionID ——
#: 用户既看不懂也抄不对（抄错的表现是"任务跑完什么都没发生"）。
#: 现改为按"第几章 第几关"描述；真要填 ID 时可从网页下拉里直接选，不必手抄。
#: 注：本参数当前**未挂在 push_main_stage 的 params 上**（见 TASK_SPECS），
#: 保留定义只是为将来恢复手填入口时留位。
TARGET_SECTION_PARAM: Final[ParamSpec] = ParamSpec(
    name="target_section",
    kind="int",
    default=None,
    label="目标关卡",
    hint="指定推到哪一关停止（如「1-3」）；不填则不限",
    cli_flag="--target-section",
)

#: ``push_main_stage``：指定目标章节（areaID 或名称前缀如 1-2）。
#:
#: 【★ 2026-10-05 去掉 hint 里的长 ID】同 TARGET_SECTION_PARAM：
#: 原先举例是个 9 位 areaID，换成用户认得的章节号。
TARGET_AREA_PARAM: Final[ParamSpec] = ParamSpec(
    name="target_area",
    kind="str",
    default=None,
    label="目标章节",
    hint="指定推到哪一小章停止（如「1-2」）；不填则不限",
    cli_flag="--target-area",
)

#: ``push_main_stage``：本次最多推进关卡数。
MAX_STAGES_PARAM: Final[ParamSpec] = ParamSpec(
    name="max_stages",
    kind="int",
    default=0,
    label="最多推进关卡数",
    hint="0 为不限关数，推至体力耗尽或遇校验关卡",
    cli_flag="--max-stages",
    danger=True,
)

#: ``push_main_stage``：每关模拟战斗耗时下限（秒）。
DELAY_MIN_PARAM: Final[ParamSpec] = ParamSpec(
    name="delay_min",
    kind="float",
    default=15.0,
    label="单关最小延迟",
    hint="单关模拟耗时下限（秒）",
    cli_flag="--delay-min",
)

#: ``market_buy``：是否购买血钻（``itemID`` 200200000）。
#:
#: 【为什么 4 个开关都标 ``danger=True``】
#: 一般市集用**金币**结算（``StoreSettingsData.currencyIcon`` = 200000050），
#: 不是钻石 —— 但它照样会消耗资源。规格表里 ``danger=True`` 的作用有两层：
#: ① 卡片上显示「⚠ 消耗资源」，② ``TaskSpec.consumes`` 为真、进入"有消耗"名单。
#: 真正扣钱的判据永远是服务端的 ``nowBuyCount/maxBuyCount``，见
#: ``tasks/market_buy.py``。
MARKET_BLOOD_DIAMOND_PARAM: Final[ParamSpec] = ParamSpec(
    name="buy_blood_diamond",
    kind="bool",
    default=True,
    label="购买血钻",
    hint="买满一般市集的血钻，每日限购",
    cli_flag="--buy-blood-diamond",
    danger=True,
)

#: ``market_buy``：是否购买琥珀（``itemID`` 200200003）。
MARKET_AMBER_PARAM: Final[ParamSpec] = ParamSpec(
    name="buy_amber",
    kind="bool",
    default=True,
    label="购买琥珀",
    hint="买满一般市集的琥珀，每日限购",
    cli_flag="--buy-amber",
    danger=True,
)

#: ``market_buy``：是否购买爱心巧克力（``itemID`` 200600001）。
MARKET_CHOCOLATE_PARAM: Final[ParamSpec] = ParamSpec(
    name="buy_chocolate",
    kind="bool",
    default=True,
    label="购买爱心巧克力",
    hint="买满一般市集的爱心巧克力（好感度 +50）",
    cli_flag="--buy-chocolate",
    danger=True,
)

#: ``market_buy``：是否购买幸福香氛（``itemID`` 200600002）。
MARKET_FRAGRANCE_PARAM: Final[ParamSpec] = ParamSpec(
    name="buy_fragrance",
    kind="bool",
    default=True,
    label="购买幸福香氛",
    hint="买满一般市集的幸福香氛（好感度 +100）",
    cli_flag="--buy-fragrance",
    danger=True,
)

#: ``push_main_stage``：每关模拟战斗耗时上限（秒）。
DELAY_MAX_PARAM: Final[ParamSpec] = ParamSpec(
    name="delay_max",
    kind="float",
    default=25.0,
    label="单关最大延迟",
    hint="单关模拟耗时上限（秒）",
    cli_flag="--delay-max",
)

#: ``top_pvp_battle``：是否保留战斗演出 —— **已删除（2026-09-25）**。
#:
#: 【为什么删掉这个参数】
#: 需求是"不要这个开关，行为固定为跳过战斗演出"。它的三层实现都已撤掉：
#: 规格表参数（原 ``KEEP_ANIMATION_PARAM``）、命令行旗标
#: （``--keep-battle-animation``）、任务形参（``keep_battle_animation``）。
#: 剩下唯一的"反悔通道"是环境变量 ``CHERRYTALE_TOP_PVP_SKIP_BATTLE=0``
#: （见 ``config.py`` 与 ``tasks/top_pvp.py::_ensure_skip_battle_enabled``）：
#: **只影响"要不要去改服务端那个开关"，界面上没有任何开关可点** ——
#: 这是刻意的："少一个开关"比"多一个看起来能用、填错却很难查的选项"更安全。


# ---------------------------------------------------------------------------
# 任务规格总表（顺序 = UI 展示顺序）
# ---------------------------------------------------------------------------
#: 每个已注册任务都必须在这里出现，否则 ``/api/tasks`` 里看不到它。
#: 这条约束由 ``tests/test_services_task_spec.py`` 用
#: ``main.load_builtin_tasks()`` 的注册表做集合比对来强制。
TASK_SPECS: Final[dict[str, TaskSpec]] = {
    # ---------------- ⓪ 账号会话（其它任务的前置；由专用界面负责，不在清单里显示） ----------------
    "login": TaskSpec(
        name="login",
        group="auto",
        title="登录 / 换号",
        summary="登录游戏并同步账号会话",
        params=(ACCOUNT_PARAM, SERVER_ID_PARAM, PLAN_ONLY_PARAM),
        hidden=True,
    ),
    # ---------------- ① 每日领取（纯领取，无花钱路径） ----------------
    "mail": TaskSpec(
        name="mail",
        group="auto",
        title="邮件一键领取",
        summary="领取全部未读邮件",
        default_checked=True,
    ),
    # ---------------- ② 只读数据（交给「信息展示」面板，不在任务清单里） ----------------
    "wallet": TaskSpec(
        name="wallet",
        group="auto",
        title="金币 / 钻石查询",
        summary="查询金币、钻石与体力数据",
        hidden=True,
    ),
    "roles": TaskSpec(
        name="roles",
        group="auto",
        title="角色名册查询",
        summary="拉取角色（英雄）清单：先试 5011 实时名册，拿不到就用登录快照",
        hidden=True,
    ),
    "free_gift": TaskSpec(
        name="free_gift",
        group="auto",
        title="特惠礼包免费领取",
        summary="商城每日免费礼包",
        default_checked=True,
        # 2026-10-04 用户需求：同一个游戏日只自动跑一次
        once_per_day=True,
    ),
    # ---------------- ③ 只读体检（零消耗） ----------------
    "handshake": TaskSpec(
        name="handshake",
        group="auto",
        title="网关握手",
        summary="网关连通性握手",
        hidden=True,
    ),
    "arena_status": TaskSpec(
        name="arena_status",
        group="auto",
        title="竞技场状态",
        summary="查询竞技场挑战状态",
        hidden=True,
    ),
    "wudou_status": TaskSpec(
        name="wudou_status",
        group="auto",
        title="天命对决状态",
        summary="查询天命对决挑战状态",
        hidden=True,
    ),
    "yimo_status": TaskSpec(
        name="yimo_status",
        group="auto",
        title="失控炼成阵状态",
        summary="查询失控炼成阵挑战状态",
        hidden=True,
    ),
    "main_stage_status": TaskSpec(
        name="main_stage_status",
        group="auto",
        title="主线关卡进度状态",
        summary="查询主线关卡推进进度",
        hidden=True,
    ),
    # ★ 2026-10-05：宴席双按钮的"两顿现在什么状态"只读查询（/api/banquet/status 用）。
    # 隐藏的理由与上面四个只读状态任务相同：它不消耗任何东西、不给人勾选。
    "bbq_status": TaskSpec(
        name="bbq_status",
        group="auto",
        title="宴席两顿状态",
        summary="查询早/晚宴席的开放时段与当前状态",
        hidden=True,
    ),
    # ---------------- ③ 工会 ----------------
    "alliance_sign_in": TaskSpec(
        name="alliance_sign_in",
        group="auto",
        title="工会签到",
        summary="每日自动签到",
        default_checked=True,
        # 2026-10-04 用户需求：同一个游戏日只自动跑一次
        once_per_day=True,
    ),
    "alliance_mining_personal": TaskSpec(
        name="alliance_mining_personal",
        group="auto",
        title="工会个人挖矿",
        summary="收获矿产并开启空闲矿",
        default_checked=True,
        once_per_day=True,
    ),
    "alliance_mining_team": TaskSpec(
        name="alliance_mining_team",
        group="auto",
        title="工会团体挖矿",
        summary="参与团体矿位并开启",
        default_checked=True,
        once_per_day=True,
    ),
    # （2026-10-04：工会捐献原本声明在这里，为了让"主动任务"连成一段、
    #   便于维护展示顺序，已整体移到下面的 ④ 进阶操作里 —— 顺序的唯一
    #   来源应该是那个分组，而不是夹在工会任务中间。）
    "grand_line_supply": TaskSpec(
        name="grand_line_supply",
        group="auto",
        title="辉煌航迹搜集物资",
        summary="领取累计搜集奖励",
        default_checked=True,
    ),
    "daily_free_draw": TaskSpec(
        name="daily_free_draw",
        group="auto",
        title="每日免费抽卡",
        summary="自动抽取每日免费卡池（零钻石消耗）",
        default_checked=True,
        # 2026-10-06 用户需求：同一个游戏日只自动跑一次（任务类文档自己也写着
        # "每日只执行一次" —— 之前只是规格表漏登记）
        once_per_day=True,
    ),
    "top_pvp_box": TaskSpec(
        name="top_pvp_box",
        group="auto",
        title="荣耀之巅宝箱",
        summary="领取荣耀之巅每日宝箱",
        default_checked=True,
        # 2026-10-06 用户需求：同一个游戏日只自动跑一次（同上，补齐漏登记）
        once_per_day=True,
    ),
    "yimo_box": TaskSpec(
        name="yimo_box",
        group="auto",
        title="失控炼成阵宝箱",
        summary="领取全服通关宝箱（3 份全满后自动停止）",
        default_checked=True,
    ),
    "daily_box": TaskSpec(
        name="daily_box",
        group="auto",
        title="日常 / 周常活跃宝箱",
        summary="仅领取已达标的宝箱",
        default_checked=True,
    ),
    # ---------------- ④ 进阶操作（需参数 / 有消耗） ----------------
    #
    # 【★ 2026-10-04：组内顺序 = 用户指定的展示顺序】
    # 降临扫荡 → 荣耀之巅 → 宴席 → 购买体力 → 素材关卡 → 一般市集购买
    # → 工会金币捐献 → 主线。
    # UI 的组内顺序就是这里的声明顺序（``describe_tasks`` 只按 group 做稳定
    # 排序，不打乱组内先后），所以要改顺序只动这一段，**不要**去改前端。
    "sweep_activity": TaskSpec(
        name="sweep_activity",
        group="manual",
        title="降临 / 活动关卡扫荡",
        summary="按所选区域批量扫荡，消耗体力与扫荡券",
        params=(
            AREA_PARAM,
            ACTIVITY_TIMES_PARAM,
            SECTION_PARAM,
            ALLOW_UNPASSED_PARAM,
            # ★ 2026-10-06：内部参数（界面不画、命令行没有旗标），
            # 由 services/activity_cache_service.py 设置。见它的定义处说明。
            PREFETCH_SECTIONS_PARAM,
        ),
    ),
    "top_pvp_battle": TaskSpec(
        name="top_pvp_battle",
        group="manual",
        title="荣耀之巅",
        summary="自动匹配并完成挑战，消耗挑战次数",
        params=(BATTLES_PARAM, INTERVAL_PARAM),
    ),
    "bbq_energy": TaskSpec(
        name="bbq_energy",
        group="manual",
        title="宴席",
        summary="享用体力，可邀请好友（花钻石）",
        params=(
            MEAL_PARAM,
            INVITE_MORNING_PARAM,
            INVITE_EVENING_PARAM,
            INVITE_FRIENDS_PARAM,
        ),
    ),
    "buy_energy": TaskSpec(
        name="buy_energy",
        group="manual",
        title="购买体力（钻石）",
        summary="每次 120 点",
        params=(BUY_ENERGY_TIMES_PARAM,),
    ),
    "sweep_material": TaskSpec(
        name="sweep_material",
        group="manual",
        title="素材关卡与元素试炼",
        summary="不消耗体力，消耗每日挑战次数",
        params=(
            CATEGORY_PARAM,
            MATERIAL_AREA_PARAM,
            MATERIAL_TIMES_PARAM,
            MATERIAL_SECTION_PARAM,
            ALLOW_UNPASSED_PARAM,
        ),
    ),
    "market_buy": TaskSpec(
        name="market_buy",
        group="manual",
        title="一般市集购买",
        summary="按每日限购买满（消耗金币）",
        params=(
            MARKET_BLOOD_DIAMOND_PARAM,
            MARKET_AMBER_PARAM,
            MARKET_CHOCOLATE_PARAM,
            MARKET_FRAGRANCE_PARAM,
        ),
    ),
    "alliance_donate": TaskSpec(
        name="alliance_donate",
        group="manual",
        title="工会金币捐献（默认满档）",
        summary="消耗 25,000 金币",
    ),
    "push_main_stage": TaskSpec(
        name="push_main_stage",
        group="manual",
        title="主线关卡通关",
        summary="自动推进并领取章节满星宝箱，消耗体力",
        params=(
            MAX_STAGES_PARAM,
            DELAY_MIN_PARAM,
            DELAY_MAX_PARAM,
        ),
    ),
}


#: 运行级参数的名字集合（用于把"贴在某个任务上的 dry_run"识别出来，不算未知键）。
RUN_LEVEL_PARAM_NAMES: Final[frozenset[str]] = frozenset(
    spec.name for spec in RUN_LEVEL_PARAMS
)

#: **每个游戏日只自动执行一次**的任务（2026-10-04 用户需求）。
#:
#: 成员（截至 2026-10-06，共 6 个）：特惠礼包 ``free_gift``、工会签到
#: ``alliance_sign_in``、个人/团体挖矿 ``alliance_mining_personal`` /
#: ``alliance_mining_team``、每日免费抽卡 ``daily_free_draw``、
#: 荣耀之巅宝箱 ``top_pvp_box`` —— 都是"每天就有一次"的领取类操作。
#: （后两个是 2026-10-06 补登记的：它们的任务类文档自己就写着"每日一次"，
#:   只是当初漏了在规格表上打勾，代价是换端口 / 换入口时会重复跑一遍。）
#:
#: 【自动执行的触发时机（2026-10-06 用户要求：只在第一次登录工具时执行）】
#: 名单里的任务**只跟着"启动"这一类触发走**（进主界面 / 登录成功 / 切区，
#: 以及启动时撞 409 后的那次补跑）。网页端后续的"勾选变动"**不会**再把它们
#: 捎带提交（见 ``web/assets/app.js::runAutomation`` 的 ``ONCE_PER_DAY_TRIGGERS``）——
#: 用户的原话是"只有第一次登录工具时执行"。
#: ⚠️ 卡片上的「执行」小按钮走 ``origin="manual"`` 的手动路径，
#: **不受这条限制**：任何时候点它都照跑，优先级最高。
#:
#: 【为什么要从规格表派生，而不是让调用各自写一份名单】
#: 记账在服务端（``services/daily_state.py``）、提示在前台，两边各抄一份名单
#: 迟早不一致：多写一个任务 = 它永远不会被记账（天天重跑）；
#: 漏写一个 = 它跑过一次当天就不再自动执行。名单只有这一个来源。
ONCE_PER_DAY_TASKS: Final[frozenset[str]] = frozenset(
    name for name, spec in TASK_SPECS.items() if spec.once_per_day
)


def is_once_per_day(task_name: str) -> bool:
    """这个任务是否属于"每游戏日只自动跑一次"（见 :data:`ONCE_PER_DAY_TASKS`）。"""
    return task_name in ONCE_PER_DAY_TASKS

#: 已经因为"缺规格"警告过的任务名（避免每次请求都刷屏）。
_WARNED_MISSING_SPEC: set[str] = set()


# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------
def get_spec(task_name: str) -> TaskSpec | None:
    """按注册名取规格；未登记时返回 ``None``（由调用方决定报错还是兜底）。"""
    return TASK_SPECS.get(task_name)


def group_of(task_name: str) -> GroupKey:
    """任务所属分组；未登记的任务归入 ``"manual"``。

    【为什么未登记时兜底而不是报错】
    规格表是"展示层元数据"，不是执行权限表。将来有人新增了任务、忘了登记规格，
    正确行为是"UI 里照样能看到并执行它（归到主动任务、默认不勾选）"，
    而不是整个页面报错。**兜底到 ``manual`` 而不是 ``auto``**：
    默认勾选的名单必须是"确定无消耗"的，没登记的任务不该被自动选上。
    至于"忘了登记"，由一致性测试在开发期就红掉，不该由运行期的使用者承担。
    """
    spec = TASK_SPECS.get(task_name)
    return spec.group if spec is not None else "manual"


def normalize_params(
    task_name: str, raw: Mapping[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    """把外部传入的参数规范化成"该任务真正接受的一小撮键"。

    :param task_name: 注册名。
    :param raw: 原始参数（可能来自 HTTP JSON，也可能来自命令行）。
    :return: ``(规范化后的参数, 被丢弃的键名列表)``。

    **返回值一定包含该任务声明的全部参数**（缺的填默认值），
    与 :func:`task_params_from_cli` 的形状完全一致 —— 这样下游（runner）
    无论从哪条路径来的参数，都可以直接 ``params["times"]`` 取值，
    不必到处写 ``params.get("times", 0)``（那种写法漏一处就是缺陷）。

    【为什么要丢弃未知键，而不是原样透传】
    透传的后果不是"多一个参数"，而是任务构造函数直接 ``TypeError`` ——
    那是"还没发请求就崩"的最差失败模式（``main.py`` 里"按声明过滤"是同一考虑）。
    所以这里与 CLI 保持完全一致的策略：**只给任务它声明过的东西**；
    被丢弃的键会返回给调用方并记一条 WARNING，避免"网页上填了却没生效"的沉默。

    【为什么类型不对时抛 ValueError，而不是"退回默认值"】
    ``--times abc`` 静默变成 0（只读）看起来"安全"，但用户以为自己扫了；
    反过来静默变成 1 就直接扣掉体力。两种猜测都在骗人，所以宁可报错。
    """
    provided = dict(raw or {})
    params: dict[str, Any] = {}
    dropped: list[str] = []

    if TASK_SPECS.get(task_name) is not None:
        for param_spec in TASK_SPECS[task_name].params:
            params[param_spec.name] = param_spec.coerce(provided.get(param_spec.name))

    for key in provided:
        if TASK_SPECS.get(task_name) is not None and key in params:
            continue
        # 运行级参数允许"贴在某个任务上"传入（例如 {"dry_run": true}），
        # 它们由 services/runner 统一处理，不算未知键。
        if key in RUN_LEVEL_PARAM_NAMES:
            continue
        dropped.append(key)

    if dropped:
        _LOGGER.warning(
            "任务 %s 忽略了不认识的参数：%s", task_name, "、".join(sorted(dropped))
        )
    return params, dropped


def task_params_from_cli(namespace: argparse.Namespace, task_name: str) -> dict[str, Any]:
    """从 ``argparse`` 解析结果里取出**该任务声明的**任务级参数。

    :param namespace: ``main.build_parser().parse_args(...)`` 的结果。
    :param task_name: 注册名。
    :return: 结构化参数 dict（键名与命令行 ``dest`` 相同）。

    【为什么用 ``getattr(..., None)`` 而不是读 ``namespace.__dict__``】
    两个原因：① 某个旗标只存在于别的子命令时，这里不会炸；
    ② 未登记规格的任务直接返回空 dict，把报错留给任务构造函数 ——
    与改造前的失败方式保持一致（``KeyError`` / ``TypeError`` 由上层处理）。
    """
    spec = TASK_SPECS.get(task_name)
    if spec is None:
        return {}
    return {
        param_spec.name: param_spec.coerce(getattr(namespace, param_spec.name, None))
        for param_spec in spec.params
    }


def run_level_from_cli(namespace: argparse.Namespace) -> dict[str, Any]:
    """从 ``argparse`` 解析结果里取出运行级参数（演练 / 钻石总闸）。

    .. note::
       ``battles`` / ``interval`` **不在这里**（2026-09-25 归位）：它们已经是
       荣耀之巅的任务级参数，由 :func:`task_params_from_cli` 取出。命令行的
       ``--battles`` / ``--interval`` 旗标仍属 ``run`` 子命令，所以用法不变。
    """
    return {
        spec.name: spec.coerce(getattr(namespace, spec.name, None))
        for spec in RUN_LEVEL_PARAMS
    }


def groups_view() -> list[dict[str, str]]:
    """分组元数据（UI 的分节标题用它渲染）。"""
    return [
        {"key": key, "title": title, "summary": summary}
        for key, title, summary in GROUPS
    ]


def param_view(param: ParamSpec) -> dict[str, Any]:
    """把 :class:`ParamSpec` 转成 JSON 可序列化的 dict（前端表单直接消费）。"""
    return {
        "name": param.name,
        "kind": param.kind,
        "default": param.default,
        "label": param.label,
        "hint": param.hint,
        "cli_flag": param.cli_flag,
        "danger": param.danger,
        "from_config": param.from_config,
        # 界面渲染提示（2026-09-22 新增）：非空 ``choices`` → 下拉；
        # 非空 ``options_source`` → 下拉 + 「拉取」按钮。两者都空 → 照旧输入框。
        "choices": list(param.choices),
        # 档位文案（键统一转成字符串，因为 JSON 的键只能是字符串）。
        # 空 dict 表示"前端按类型自动生成文案"。
        "choice_labels": {str(key): text for key, text in param.choice_labels.items()},
        "options_source": param.options_source,
        # 是否允许手输：``False`` 时前端只画下拉（活动区域 / 指定关卡就是这种）
        "allow_manual": param.allow_manual,
        # 是否**内部参数**（前端据此决定"不画它"，见 ParamSpec.hidden 的说明）。
        # 必须带出去：漏了它，前端会把内部参数当普通参数画成表单里的一行。
        "hidden": param.hidden,
    }


def describe_tasks(
    registry: Mapping[str, type[BaseTask]] | None = None,
) -> list[dict[str, Any]]:
    """把「规格表 + 任务类真实约束」合并成前端可直接渲染的结构。

    :param registry: 已注册任务（名字 → 类）。``None`` 表示取
        ``tasks.base.available_tasks()``（会**先触发内置任务的导入**，
        见下）；显式传参是为了让测试能塞假注册表。
    :return: 任务描述列表，顺序 = :data:`GROUPS` 顺序 + 规格表内的声明顺序
        （Python 的排序是稳定的，所以组内顺序 = 声明顺序）。

    【两侧信息各自只有一份来源】
    - **显示信息**（标题 / 说明 / 参数 / 默认勾选）来自规格表；
    - **约束信息**（需登录 / 是否可能花钻石 / 花钱用途 / 是否接受运行参数）
      直接读任务类上的类级声明，绝不抄第二份。
    """
    from services import task_registry  # 局部导入：避免模块级循环依赖

    # 【为什么必须显式加载内置任务】
    # 注册是"导入模块时的副作用"：服务进程刚启动时注册表是**空的**，
    # 若直接读 available_tasks()，每个任务都会被判成"未注册"→ enabled=False
    # → 网页上复选框全灰、login 被标成"未实现"。详见 services/task_registry.py。
    registered: dict[str, type[BaseTask]] = dict(
        task_registry.load_builtin_tasks() if registry is None else registry
    )
    group_order = {key: index for index, (key, _, _) in enumerate(GROUPS)}

    views: list[dict[str, Any]] = [
        _task_view(spec, registered.get(spec.name)) for spec in TASK_SPECS.values()
    ]

    # 兜底：已注册但没登记规格的任务也照样显示（见 group_of 的说明）。
    # 这样"新增任务忘了登记"只会让它暂时缺少中文标题，而不会从页面上消失。
    for name in sorted(registered):
        if name in TASK_SPECS:
            continue
        if name not in _WARNED_MISSING_SPEC:
            _WARNED_MISSING_SPEC.add(name)
            _LOGGER.warning(
                "任务 %s 未在 services/task_spec.py 登记规格，UI 已按兜底方式显示", name
            )
        views.append(_task_view(_fallback_spec(name, registered[name]), registered[name]))

    views.sort(key=lambda view: group_order.get(str(view["group"]), 99))
    return views


def _fallback_spec(name: str, task_class: type[BaseTask]) -> TaskSpec:
    """给"注册了但没登记规格"的任务造一个兜底规格（进"进阶操作"、默认不勾选）。"""
    doc_lines = (task_class.__doc__ or "").strip().splitlines()
    return TaskSpec(
        name=name,
        group="manual",
        title=name,
        summary=doc_lines[0].strip() if doc_lines else "",
    )


def _task_view(spec: TaskSpec, task_class: type[BaseTask] | None) -> dict[str, Any]:
    """合并单个任务的「显示信息 + 真实约束」，产出给 UI 的 dict。"""
    doc_lines = (task_class.__doc__ or "").strip().splitlines() if task_class else []
    return {
        "name": spec.name,
        "group": spec.group,
        "title": spec.title,
        "summary": spec.summary,
        "doc_summary": doc_lines[0].strip() if doc_lines else "",
        "params": [param_view(param) for param in spec.params],
        "default_checked": spec.default_checked,
        "enabled": spec.enabled and task_class is not None,
        "hidden": spec.hidden,
        # ★ 2026-10-06：网页端要知道"哪些任务属于每游戏日只自动跑一次"，
        # 才能在"勾选变动"这条触发路径上把它们排除掉（用户要求这些任务
        # **只在第一次登录工具时执行**，见 ONCE_PER_DAY_TASKS 的说明）。
        # 这份名单的唯一来源是规格表，前端不得自己再抄一份。
        "once_per_day": spec.once_per_day,
        "consumes": spec.consumes,
        "registered": task_class is not None,
        "requires_auth": bool(getattr(task_class, "requires_auth", False)),
        "spends_diamond": bool(getattr(task_class, "spends_diamond", False)),
        "diamond_purposes": [
            # 用枚举**名字**（如 "BBQ_INVITE"）而不是数值：UI 直接显示给用户看，
            # "0" 这种数值对理解毫无帮助（SpendPurpose 是 IntEnum）。
            str(getattr(purpose, "name", purpose))
            for purpose in getattr(task_class, "diamond_purposes", ())
        ],
        "opt_in_hint": str(getattr(task_class, "opt_in_hint", "")),
        "accepts_run_options": bool(getattr(task_class, "accepts_run_options", False)),
    }







