"""执行调度入口：CLI 与 Web 共用同一份「装配 + 执行 + 汇总」逻辑。

【为什么需要这一层（而不是让 Web 自己拼一遍）】
"跑一个任务"原本完整地写在 ``main.py`` 的 ``cmd_run()`` 里。Web 层如果照抄一遍，
就会出现**两份**决定"会不会花钻石""battles=0 是不是只读"的代码 ——
这类分叉最危险的表现是：命令行安全，网页上却真的把资源花掉了，而且不报错。
抽到这里之后，两个入口都只是"翻译 + 渲染"：

    main.py  ── argparse → RunRequest ─┐
                                        ├─→ services/runner.py ─→ tasks/ ─→ client/
    webapi/  ── JSON     → RunRequest ─┘

【数据流】
    RunRequest（结构化意图）
        ↓ prepare()   ← 会话状态、钻石闸门、运行参数
    RunContext（本次运行的全部上下文）
        ↓ run_tasks() ← 逐任务：参数规范化 → 组装 kwargs → 执行
    RunReport（每步的结果 + 说明文字）

【三条不可动摇的约定】
1. **默认只读**：``battles=0`` / ``times=0`` 不消耗任何资源；
2. **花钱要两道门**：总闸（:attr:`RunRequest.allow_diamond`）与用途白名单
   （见 :meth:`RunRequest.requested_purposes`）都过才放行，未授权时请求字节
   根本不会被组装 —— 与 ``client/spending.py`` 的 fail-closed 哲学一致；
3. **凭据只活在内存里**：账号密码通过环境变量临时注入（见
   :func:`_temporary_credentials`），运行结束立刻恢复，不落盘、不回显、不入日志。

.. note::
   本模块**不做任何打印**。所有"要给人看的话"都通过 ``emit`` 回调交出去：
   CLI 传 ``print``、Web 传日志流。这样同一句话不会在两个入口里各写一份
   （也就不会出现"命令行说 A、网页说 B"）。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import config
from client.session import GameSession
from client.session_store import resolve_session_state
from client.spending import SpendPolicy, SpendPurpose, purposes_from_config
from models.session_state import GameSessionState
from services import cancellation, task_registry
from services.task_spec import get_spec, normalize_params
from tasks.base import BaseTask, RunOptions, TaskResult, create_task, get_task_class
from client.exceptions import SessionKickedError

_LOGGER: Final[logging.Logger] = logging.getLogger("services.runner")

#: 「说明文字」的去处。CLI 传 ``print``，Web 传日志流的写入函数。
NoteSink = Callable[[str], None]

#: 单个任务开始 / 结束时通知入口层（Web 用它更新步骤状态）。
StartSink = Callable[[str], None]
FinishSink = Callable[["StepResult"], None]


class UnknownTaskError(LookupError):
    """任务名不在注册表里。

    【为什么单独定义一个异常，而不是直接抛 ``KeyError``】
    两个入口对"任务名写错"的处理**必须**与"任务执行失败"区分开：
    CLI 要返回退出码 ``2``（失败是 ``1``），Web 要返回 HTTP 400（失败是 200 + 失败步骤）。
    有自己的类型，入口层才能用 ``except`` 精确捕获，而不必去猜 KeyError 从哪来。
    """


# ---------------------------------------------------------------------------
# 输入：结构化的"用户意图"
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TaskRequest:
    """一次运行里的单个任务。

    :param name: 注册名（如 ``"sweep_activity"``）。
    :param params: 任务级参数。允许是"脏"的原始 dict（直接从 JSON 来也行）——
        执行前会由 :func:`services.task_spec.normalize_params` 规范化：
        补默认值、做类型转换、丢弃任务不认识的键。
    """

    name: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunRequest:
    """一次运行的完整意图（CLI 与 Web 都构造它）。

    :param tasks: 要按顺序执行的任务（"一键运行"= 勾选多个 → 串行执行）。
    :param dry_run: 演练模式：只组装字节、**不联网**。第一次跑务必先用它。
    :param allow_diamond: 钻石总闸（**默认关闭**）。关闭时花钻石的请求连字节都不组装。
    :param account: 账号选择器（序号 / 邮箱 / userId），只有 ``login`` 会用到；
        其它任务只用它做"当前会话属于哪个号"的一致性提示。
    :param credentials: ``(邮箱, 密码)`` —— **只在内存里**。运行期间临时注入
        环境变量供 ``client/credentials.py`` 读取，运行结束立刻恢复。
    :param verbose: 本次运行是否临时打开 DEBUG 日志（用完恢复，不影响别的运行）。

    【为什么没有 ``battles`` / ``interval`` 字段（2026-09-25）】
    它们现在是荣耀之巅的**任务级参数**（见 ``services/task_spec.py`` 的
    ``BATTLES_PARAM`` 与 :attr:`requested_battles`）：与 :attr:`wants_invite_friends`
    同一套做法 —— "只有某一个任务认识的参数，就只从那个任务的参数里读"，
    不在本类上再放一份同义字段。两处各存一份的结果必然是
    "界面上填了、字段却没传"这类沉默的错（也正是"战斗次数跑到别的板块"的由来）。
    """

    tasks: Sequence[TaskRequest]
    dry_run: bool = False
    allow_diamond: bool = False
    account: str | None = None
    credentials: tuple[str, str] | None = None
    verbose: bool = False

    # ------------------------------------------------------------------
    # 派生判断（只写一次，两个入口共用）
    # ------------------------------------------------------------------
    @property
    def wants_invite_friends(self) -> bool:
        """本次是否显式授权了「宴席邀请好友」这个花钱用途。

        【为什么从任务参数里推导，而不是在 RunRequest 上再放一个同义字段】
        命令行里 ``--invite-friends`` 只有一个旗标，Web 表单里也只有一个复选框。
        如果 RunRequest 上再存一份，就变成"同一件事有两个来源"，
        迟早出现"复选框勾了、字段没传"的不一致。所以它**只**来自宴席任务的参数，
        而规格表（``services/task_spec.py``）已经声明了 ``invite_friends``。

        【★ 2026-10-05：三个开关任一为真即视为"授权了 BBQ_INVITE 用途"】
        网页把宴席拆成早/晚两个按钮、各带一个邀请开关（``invite_morning`` /
        ``invite_evening``）。**用途级白名单是整次运行的**，所以这里取三者之或；
        至于"到底哪一顿真的能邀请"，由任务层按该顿窗口 + 该顿开关再判一次
        （见 ``tasks/bbq.py::_execute_meal``）—— 两层各管一件事：
        这里管"这次运行允不允许花这个用途的钻石"，任务层管"这一顿该不该邀请"。
        """
        return any(
            bool(task.params.get(key))
            for task in self.tasks
            for key in ("invite_friends", "invite_morning", "invite_evening")
        )

    @property
    def wants_buy_energy(self) -> bool:
        """本次是否显式授权了「购买体力」这个花钱用途（购买次数 > 0）。"""
        return any(
            int(task.params.get("times") or 0) > 0
            for task in self.tasks
            if task.name == "buy_energy"
        )

    # ------------------------------------------------------------------
    # 竞技类运行参数（★ 2026-09-25：从"运行级字段"改为"任务参数派生"）
    # ------------------------------------------------------------------
    # 【为什么这样读，而不是在本类上放 battles / interval 字段】
    # 见类 docstring：只有荣耀之巅认识这两个参数，所以唯一来源就是那张卡片
    # 提交的任务参数。本类只做"翻译"，不做第二份判断。
    #
    # 【为什么必须走 ParamSpec.coerce】
    # 网页表单交上来的是**字符串**（``<input type="number">`` 的 value），
    # 直接 ``int()`` 会漏掉规格表已定义好的语义（"空串 = 没填"、"3.0 → 3"、
    # "bool 不算整数"…）。规格表是参数面的唯一来源，取值也一并用它的规则，
    # 这样 CLI 与网页两条路径得到的运行参数必然一致。
    #
    # 【多个任务都声明同名参数时怎么办】
    # 以**第一个声明的任务**为准（当前只有 ``top_pvp_battle`` 声明）。
    # 将来真出现第二个时，正确做法是让它们语义一致、或显式写明以谁为准，
    # 而不是在这里悄悄相加或取最危险的那个。
    def _requested_task_param(self, name: str) -> Any:
        """在"声明了该参数"的任务里取第一个值；没有任何任务声明时返回 ``None``。

        ``None`` 的含义由下游决定（对 ``battles`` / ``interval`` 而言 =
        "没填 → 用 ``config`` 里的默认值"）。
        """
        for task in self.tasks:
            spec = get_spec(task.name)
            if spec is None:
                continue  # 未登记规格的任务：它不可能声明任务参数
            param_spec = spec.param(name)
            if param_spec is None:
                continue  # 该任务没有这个参数（例如扫荡任务没有 battles）
            return param_spec.coerce(task.params.get(name))
        return None

    @property
    def requested_battles(self) -> int | None:
        """荣耀之巅本次要打几场；``None`` = 没填 → 用 ``config.TOP_PVP_BATTLES``。"""
        return self._requested_task_param("battles")

    @property
    def requested_interval(self) -> float | None:
        """荣耀之巅每场之间的间隔秒数；``None`` = 没填 → 用 config 的值。"""
        return self._requested_task_param("interval")

    def requested_purposes(self) -> frozenset[SpendPurpose]:
        """本次运行允许的**花钱用途**集合 = 配置里的用途 + 本次显式授权的用途。"""
        purposes = set(purposes_from_config())
        if self.wants_invite_friends:
            purposes.add(SpendPurpose.BBQ_INVITE)
        if self.wants_buy_energy:
            purposes.add(SpendPurpose.BUY_ENERGY)
        return frozenset(purposes)


# ---------------------------------------------------------------------------
# 输出：单步结果与整次运行报告
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StepResult:
    """单个任务的执行结果（比 ``TaskResult`` 多一个耗时，方便 UI 显示进度）。"""

    name: str
    ok: bool
    message: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0

    @property
    def line(self) -> str:
        """与 ``tasks.base.TaskResult.__str__`` **逐字一致**的一行结果。

        【为什么刻意保持同格式】
        改造前 CLI 打印 ``print(result)``，格式是 ``[✔] 任务名: 说明``。
        由本属性生成同一行，既让现有测试里"stdout 里要有某些字样"的断言继续成立，
        也保证用户看到的输出不会因为这次重构而变样。
        """
        mark = "✔" if self.ok else "✘"
        return f"[{mark}] {self.name}: {self.message or ('成功' if self.ok else '失败')}"

    @staticmethod
    def from_task_result(result: TaskResult, *, elapsed: float) -> "StepResult":
        """由任务自己的返回值构造（唯一入口，避免两处字段对不齐）。"""
        return StepResult(
            name=result.task,
            ok=result.ok,
            message=result.message,
            data=dict(result.data),
            elapsed=elapsed,
        )


@dataclass
class RunReport:
    """一次运行的总报告。

    :param steps: 每一步的结果（顺序 = :attr:`RunRequest.tasks` 的顺序）。
    :param notes: 本次运行产出过的全部"说明文字"（CLI 打印过、Web 回显过）。
    :param spend_policy: 收尾时的消费策略；``spend_policy.spent_so_far``
        就是本次实际花掉的钻石数（跨多个任务累计，见 :func:`run_tasks`）。
    """

    steps: list[StepResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    spend_policy: SpendPolicy = field(default_factory=lambda: SpendPolicy())

    @property
    def ok(self) -> bool:
        """**全部步骤都成功**才算成功（与 ``tasks/daily.py`` 的汇总口径一致）。"""
        return bool(self.steps) and all(step.ok for step in self.steps)

    @property
    def succeeded(self) -> int:
        """成功的步骤数。"""
        return sum(1 for step in self.steps if step.ok)

    @property
    def failed(self) -> list[StepResult]:
        """失败的步骤（UI 用红色标出来，不必再遍历一遍）。"""
        return [step for step in self.steps if not step.ok]

    def summary(self) -> str:
        """一行汇总，例如 ``3/4 步成功（失败：sweep_activity）``。"""
        if not self.steps:
            return "没有执行任何任务"
        text = f"{self.succeeded}/{len(self.steps)} 步成功"
        if self.failed:
            text += "（失败：" + "，".join(step.name for step in self.failed) + "）"
        return text


# ---------------------------------------------------------------------------
# 上下文：装配阶段产出、执行阶段消费
# ---------------------------------------------------------------------------
@dataclass
class RunContext:
    """一次运行的全部上下文（由 :func:`prepare` 产出，交给 :func:`run_tasks`）。

    :param session: 底层 HTTP 会话（Cookie / 连接池 / 令牌都在它手里）。
    :param session_state: 载入到的游戏会话状态（``None`` = 没找到，需登录的任务会报 401）。
    :param session_source: 会话状态的来源描述（环境变量 / config_local.py / 会话文件）。
    :param spend_policy: 本次运行的消费策略（两道门都体现在这个对象上）。
    :param run_options: 运行参数（打几场 / 间隔多久）。
    :param notes: 本次运行说过的全部"给人看的话"（顺序即产生顺序）。
    """

    session: GameSession
    session_state: GameSessionState | None
    session_source: str
    spend_policy: SpendPolicy
    run_options: RunOptions
    notes: list[str] = field(default_factory=list)

    def close(self) -> None:
        """释放底层 HTTP 会话。

        【为什么入口层要显式调用】
        进程退出时操作系统会回收一切，所以旧版命令行从没关心过这件事；
        但 Web 服务是**长驻进程**，每次运行都新建一个 ``GameSession``，
        不关就等于把连接池一层层堆在内存里 —— 几十次运行之后症状是"越跑越慢"，
        而且极难联想到是这里。因此两个入口都在 ``finally`` 里调用它。
        """
        self.session.close()


def _note(context: RunContext, text: str, emit: NoteSink | None) -> None:
    """记录一句"给人看的话"：进 ``context.notes``，并可选地发给出口。

    :param context: 本次运行的上下文。
    :param text: 要说的那句话（中文、面向使用者，**不允许包含任何凭据**）。
    :param emit: 出口；``None`` 表示只记录、不输出。
    """
    context.notes.append(text)
    if emit is not None:
        emit(text)


# ---------------------------------------------------------------------------
# 两个"临时状态"的守护：凭据与日志级别
# ---------------------------------------------------------------------------
@contextmanager
def _temporary_credentials(credentials: tuple[str, str] | None) -> Iterator[None]:
    """把账号密码临时放进环境变量，供 ``client/credentials.py`` 读取。

    【为什么走环境变量，而不是给任务加参数】
    ``tasks/login.py`` 取凭据的优先级（环境变量 > 凭据文件）已经固定在
    ``client/credentials.py`` 里并有测试覆盖。为了 Web 表单再给任务加一条注入路径，
    等于让"凭据从哪来"多一个分支，还会把密码带进任务对象（更容易出现在日志/异常里）。
    环境变量是**最短、且不新增泄露面**的做法。

    【为什么必须用完就恢复】
    环境变量是**进程级**的：Web 服务长驻，如果留着不删，下一次运行、
    甚至另一个请求都会"继承"上一次的密码。所以 finally 里一定恢复原值
    （原本不存在就删除）。整个过程密码不会出现在任何持久化位置，
    也不会出现在 :attr:`RunContext.notes` 里。
    """
    if credentials is None:
        yield
        return

    account, password = credentials
    account_name = config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME
    password_name = config.PLATFORM_LOGIN_PASSWORD_ENV_NAME
    previous = {account_name: os.environ.get(account_name), password_name: os.environ.get(password_name)}

    os.environ[account_name] = account
    os.environ[password_name] = password
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _temporary_log_level(verbose: bool) -> Iterator[None]:
    """按需把根日志器临时调成 DEBUG，结束后恢复。

    【为什么只在 verbose=True 时才动，而不是"不 verbose 就设 INFO"】
    日志级别是**入口层**的决定：CLI 由 ``main.setup_logging()`` 统一设置
    （受 ``--verbose`` 与 ``config.LOG_LEVEL`` 共同影响），Web 由服务启动参数决定。
    这里若顺手改成 INFO，就会把入口层的设置覆盖掉（例如服务端明明想开 DEBUG）。
    所以本函数只做"临时提高"，绝不"顺手降低"。
    """
    if not verbose:
        yield
        return

    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root.setLevel(previous_level)


# ---------------------------------------------------------------------------
# 任务注册表
# ---------------------------------------------------------------------------
#: 导入清单在 ``services/task_registry.py`` 里（三个地方共用，见那个模块的说明）。
#: 这里重新导出，是为了保持既有调用点（``runner.load_builtin_tasks()``）不变。
load_builtin_tasks = task_registry.load_builtin_tasks





# ---------------------------------------------------------------------------
# 装配阶段
# ---------------------------------------------------------------------------
def describe_session() -> dict[str, Any]:
    """只读地描述当前游戏会话状态（**已打码**，可直接回给浏览器或终端）。

    :return: 包含两类字段的 dict：

        * 给人看的：``summary``（**打码后**的一行摘要）、``source``（来自哪个文件/环境变量）；
        * 给程序用的：``has_session`` / ``player_id`` / ``server_id`` / ``server_name`` /
          ``platform_user_id`` / ``obtained_at``。

    【为什么要分成"给人看"和"给程序用"两部分】
    界面要显示"上次进的区是哪个"（给选区页高亮）、也要显示角色摘要。
    如果只给一串拼好的中文，前端就得去**解析中文**才能拿到区服编号 ——
    那种做法在文案一改就崩，而且没人会想到是这里的问题。

    【安全】
    ``summary`` 走 ``GameSessionState.describe()``（**打码**）；
    ``platform_user_id`` 是"哪个平台账号"，不是令牌，登录日志里本来就打印它。
    **令牌本身永远不进这个返回值**。
    """
    state, source = resolve_session_state()
    if state is None:
        return {
            "has_session": False,
            "source": source,
            "summary": "",
            "player_id": 0,
            "player_name": "",
            "server_id": 0,
            "server_name": "",
            "platform_user_id": "",
            "obtained_at": 0.0,
        }
    player_name = str(getattr(state, "player_name", "") or "")
    platform_user_id = str(getattr(state, "platform_user_id", "") or "")
    if not player_name and platform_user_id:
        try:
            from client.account_store import AccountStore

            # 【为什么是 ``.get()`` 而不是 ``.accounts``（2026-10-04 安全审计 P2）】
            # ``AccountStore.load()`` 返回的是 ``dict[user_id, SavedAccount]``，
            # 早先这里写的是 ``.load().accounts.get(...)`` —— 每次都会
            # ``AttributeError``，再被下面的兜底 ``except`` 吞掉。结果是
            # "角色名"**从来没能从账号库补出来**：会话摘要里那一栏永远是空的，
            # 而界面上看不出任何异常（静默失效）。
            saved = AccountStore().load().get(platform_user_id)
            if saved and saved.game.get("player_name"):
                player_name = str(saved.game["player_name"])
        except Exception as exc:  # noqa: BLE001
            # 账号库读不出来不该影响"描述会话"这件小事，但要留痕（DEBUG），
            # 不再无声吞掉 —— 上面那个缺陷正是被"静默兜底"藏了很久。
            _LOGGER.debug("从账号库补角色名失败（忽略，不影响会话描述）：%s", exc)
    return {
        "has_session": True,
        "source": source,
        "summary": state.describe() if hasattr(state, "describe") else "",
        "player_id": int(getattr(state, "player_id", 0)),
        "player_name": player_name,
        "server_id": int(getattr(state, "server_id", 0)),
        "server_name": getattr(state, "server_name", ""),
        "platform_user_id": platform_user_id,
        "obtained_at": float(getattr(state, "obtained_at", 0.0)),
    }


def prepare(request: RunRequest, *, emit: NoteSink | None = None) -> RunContext:
    """装配一次运行需要的全部上下文：会话状态、消费闸门、运行参数。

    :param request: 结构化的运行意图。
    :param emit: 说明文字的出口（CLI 传 ``print``，Web 传日志流）。
    :return: :class:`RunContext`（调用方负责 ``close()``）。

    【与改造前的 ``main.cmd_run()`` 逐条对应】
    顺序、措辞、判断条件都刻意保持一致，因为本函数被命令行与网页**共用**：
    任何"顺手优化"都会同时改变两边行为 —— 这正是本层存在的意义
    （想改只改一次，但也就必须一次改对）。

    【为什么"装配"与"执行"分开】
    ① Web 需要在真正开跑**之前**就把上下文回显给用户（例如"消费许可：…"）；
    ② 测试可以只测装配，不触发任何网络请求。
    """
    # ① 会话状态：跨进程复用登录结果（来源优先级见 client/session_store.py）
    session = GameSession()
    state, source = resolve_session_state()
    if state is not None:
        session.set_game_state(state)

    # ② 消费许可：两道门 —— 总闸与用途白名单（**与**关系，fail-closed）
    policy_from_config = SpendPolicy.from_config()
    spend_policy = SpendPolicy(
        allow_diamond=policy_from_config.allow_diamond or request.allow_diamond,
        allowed_purposes=request.requested_purposes(),
        max_diamond_per_call=policy_from_config.max_diamond_per_call,
        max_diamond_per_run=policy_from_config.max_diamond_per_run,
    )

    # ③ 运行参数：没给就用 config 的值（0 = 只读，见 tasks/base.py 的 RunOptions 说明）
    #
    # ★ 2026-09-25：来源改成**任务参数**（``requested_battles`` 从荣耀之巅卡片提交的
    # params 里读）。这样"界面上填的场数"只有一条路径进入运行参数，
    # 不会出现"运行级字段与任务参数各存一份、以谁为准"的分叉。
    requested_battles = request.requested_battles
    requested_interval = request.requested_interval
    run_options = RunOptions(
        battles=(
            config.TOP_PVP_BATTLES if requested_battles is None else requested_battles
        ),
        max_battles=config.TOP_PVP_MAX_BATTLES,
        interval_seconds=(
            config.TOP_PVP_INTERVAL_SECONDS
            if requested_interval is None
            else requested_interval
        ),
    )

    context = RunContext(
        session=session,
        session_state=state,
        session_source=source,
        spend_policy=spend_policy,
        run_options=run_options,
    )

    # ④ 告知（顺序与改造前一致：会话 → 账号一致性 → 消费许可）
    if state is not None:
        _note(context, f"已载入游戏会话状态（来源：{source}）：{state.describe()}", emit)
    else:
        _note(
            context,
            "提示：未找到游戏会话状态"
            f"（环境变量 {config.SESSION_TOKEN_ENV_NAME} / 会话文件 / config_local.py 都没有）。"
            "需登录的任务会报 401；先跑 `python main.py run login`（会自动写入会话文件）。",
            emit,
        )

    _warn_account_mismatch(context, request, emit)

    if request.allow_diamond:
        _note(context, f"⚠ 已显式开启钻石总闸：{spend_policy.describe()}", emit)
    if request.wants_invite_friends:
        gate = (
            "总闸已开 → 会真的花钻石"
            if spend_policy.allow_diamond
            else "但总闸未开 → 仍然不会花"
        )
        _note(context, f"⚠ 已授权用途「宴席邀请好友」（{gate}）", emit)
    _note(context, f"消费许可：{spend_policy.describe()}", emit)

    return context


def _warn_account_mismatch(
    context: RunContext, request: RunRequest, emit: NoteSink | None
) -> None:
    """多账号下最容易搞混的一件事：会话状态其实属于**另一个号**。

    :param context: 运行上下文（需要里面的会话状态）。
    :param request: 运行意图（需要 ``account`` 选择器）。
    :param emit: 说明文字的出口。

    【为什么只在"用户明确选了号"且"本次不是登录"时才提示】
    ① 没选号就没有可矛盾的对象；② 登录本来就会覆盖会话状态，
    这时提示"会话属于别人"纯属噪音。
    另外只在**读得到会话状态**时才比较 —— 读不到时，上面那句
    "未找到游戏会话状态"已经把问题说清楚了。
    """
    selector = (request.account or "").strip()
    state = context.session_state
    if not selector or state is None:
        return
    if any(task.name == "login" for task in request.tasks):
        return

    # 局部导入：只有走到这一步才需要它们，也让模块导入保持轻量
    from client.account_store import AccountStore
    from client.exceptions import CherrytaleError

    try:
        picked = AccountStore().pick(selector)
    except CherrytaleError as exc:
        _note(context, f"⚠ 指定的账号不可用：{exc}", emit)
        return

    if (
        picked.user_id
        and state.platform_user_id
        and picked.user_id != state.platform_user_id
    ):
        _note(
            context,
            f"⚠ 当前会话属于 {state.platform_user_id}，而你选的是 "
            f"{picked.account}（{picked.user_id}）。\n"
            "   要切换账号请先跑："
            "python main.py run login --account <序号|邮箱>",
            emit,
        )


# ---------------------------------------------------------------------------
# 校验：入口层据此给出 400 / 提示，而不必真的跑一遍
# ---------------------------------------------------------------------------
def resolve_task_class(name: str) -> type[BaseTask]:
    """按名字取任务类；不存在时抛 :class:`UnknownTaskError`。

    【为什么要包一层】
    ``tasks.base.get_task_class`` 抛的是 ``KeyError``：它的消息会被 ``str()`` 加上引号，
    类型上也与"业务失败"混在一起。包成自己的异常类型后，入口层可以精确区分
    "名字写错"（CLI 退出码 2 / HTTP 400）与"任务执行失败"（CLI 退出码 1 / 失败步骤）。
    消息本身**原样保留**（里面已经列出了全部可选任务名），所以用户看到的提示不变。
    """
    try:
        return get_task_class(name)
    except KeyError as exc:
        raise UnknownTaskError(str(exc)) from exc


def validate_request(request: RunRequest) -> list[str]:
    """检查一次运行请求是否可执行；返回**问题清单**（空列表 = 可以跑）。

    :param request: 运行意图。
    :return: 中文问题描述列表，可直接显示给用户。

    【为什么不在这里检查"有没有登录态"】
    那是 ``tasks/base.py`` 的 ``check_preconditions()`` 的职责，而且它会给出
    "请先跑 login"这样的**操作指引**。在这里再判一次，等于同一条规则写两遍，
    两边迟早不一致。本函数只做"开跑之前就能确定"的检查：
    任务名对不对、是不是尚未实现的占位项、参数能不能转成目标类型。
    """
    problems: list[str] = []

    # 注册表必须已填充，否则每个任务名都会被判成"未找到"（见 load_builtin_tasks）
    load_builtin_tasks()

    if not request.tasks:
        problems.append("没有选择任何任务")

    for task in request.tasks:
        spec = get_spec(task.name)
        if spec is not None and not spec.enabled:
            problems.append(
                f"{task.name}（{spec.title}）尚未实现：协议未确认"
            )
            continue
        try:
            resolve_task_class(task.name)
        except UnknownTaskError as exc:
            problems.append(str(exc))
            continue
        try:
            normalize_params(task.name, task.params)
        except ValueError as exc:
            problems.append(f"{task.name}：{exc}")

    return problems


# ---------------------------------------------------------------------------
# 执行阶段
# ---------------------------------------------------------------------------
def task_kwargs_for(
    request: RunRequest,
    task: TaskRequest,
    context: RunContext,
    params: Mapping[str, Any],
    task_class: type[BaseTask],
) -> dict[str, Any]:
    """把"结构化参数"翻译成任务构造函数认识的 kwargs。

    :param request: 运行意图（运行级参数在它上面）。
    :param task: 本次要跑的任务。
    :param context: 运行上下文（消费策略与运行参数在它上面）。
    :param params: **已规范化**的任务参数（见 ``services.task_spec.normalize_params``）。
    :param task_class: 任务类（用它判断是否接受运行参数）。
    :return: 可直接 ``create_task(name, session, **kwargs)`` 的字典。

    【为什么每个任务的专属参数要在这里逐个点名】
    因为这些名字必须与任务构造函数的签名一致，而签名只存在于任务代码里。
    点名式的好处是：谁改了任务签名，谁就会在"跑这个任务"时立刻看到 ``TypeError``，
    而不是被一个静默忽略的默认值糊过去（后者才是真正难查的那类问题）。
    ``tests/test_services_runner.py`` 会用真实任务类逐一校验这些名字。

    【``only_passed`` 为什么要取反】
    对外暴露的是 ``--allow-unpassed``（"允许未通关"），而任务要的是
    ``only_passed``（"只扫已通关"）—— 两个词方向相反。为不改变既有用法，
    对外的语义保持不变，转换只在这里做一次。
    """
    kwargs: dict[str, Any] = {
        # 这两个是 tasks/base.py 的 BaseTask.__init__ 认得的公共参数
        "dry_run": request.dry_run,
        "spend_policy": context.spend_policy,
    }

    if task.name == "login":
        kwargs["account"] = request.account
        # 选区 / 只拉列表：名字与 services/task_spec.py 的登录规格一致。
        # ``params`` 已被 normalize_params 规范化过，所以这里可以直接取；
        # ``or None`` 把"空串"（界面没填）也统一成"没指定"。
        kwargs["server_id"] = params.get("server_id") or None
        kwargs["plan_only"] = bool(params.get("plan_only"))
    elif task.name == "sweep_activity":
        kwargs["area"] = params["area"]
        kwargs["times"] = int(params["times"] or 0)
        kwargs["section"] = params["section"]
        kwargs["only_passed"] = not bool(params["allow_unpassed"])
        if "prefetch_sections" in params:
            kwargs["prefetch_sections"] = bool(params["prefetch_sections"])
    elif task.name == "sweep_material":
        kwargs["category"] = params.get("category") or "all"
        kwargs["area"] = params.get("area")
        kwargs["times"] = int(params.get("times") or 0)
        kwargs["section"] = params.get("section")
        kwargs["only_passed"] = not bool(params.get("allow_unpassed"))
    # ``top_pvp_battle`` 这里**没有专属分支**（2026-09-25）：它只靠上面那两个公共
    # kwargs（``dry_run`` / ``spend_policy``）与 ``run_options`` 工作 ——
    # 原来的 ``keep_battle_animation`` 参数已按需求删除（行为固定为跳过），
    # 场数 / 间隔都在 ``RunOptions`` 里，所以没有关键字需要在这里翻译。
    elif task.name == "bbq_energy":
        # 主动任务已移除确认吃宴席开关，默认执行即吃；若显式传入按传入值
        kwargs["eat"] = bool(params.get("eat", True)) or request.wants_invite_friends
        # ★ 2026-10-05：指定顿（网页早/晚两个按钮）。
        # ``meal`` 缺省为 None ⇒ 不传该 kwarg，老路径行为逐字节不变。
        meal = params.get("meal") or None
        if meal is not None:
            kwargs["meal"] = meal
            # 该顿自己的邀请开关。**只翻译"用户的选择"**，窗口合法性由任务层判
            # （"窗不覆盖优先于开关开着"，见 tasks/bbq.py::_execute_meal）。
            meal_keys = {"morning": "invite_morning", "evening": "invite_evening"}
            kwargs["invite"] = bool(params.get(meal_keys.get(meal, "")))
    elif task.name == "buy_energy":
        kwargs["times"] = int(params.get("times") or 0)
        if "max_cost" in params:
            kwargs["max_cost"] = int(params.get("max_cost") or 0)
    elif task.name == "market_buy":
        # 四个"买不买"开关逐个点名：名字与 MarketBuyTask.__init__ 的形参一致，
        # 谁改了签名，这里就会在"跑这个任务"时立刻 TypeError（而不是被默认值糊过去）。
        kwargs["buy_blood_diamond"] = bool(params.get("buy_blood_diamond"))
        kwargs["buy_amber"] = bool(params.get("buy_amber"))
        kwargs["buy_chocolate"] = bool(params.get("buy_chocolate"))
        kwargs["buy_fragrance"] = bool(params.get("buy_fragrance"))
    elif task.name == "push_main_stage":
        if "target_section" in params:
            kwargs["target_section"] = params.get("target_section") or None
        if "target_area" in params:
            kwargs["target_area"] = params.get("target_area") or None
        kwargs["max_stages"] = int(params.get("max_stages") or 0)
        kwargs["delay_min"] = float(params.get("delay_min") or 15.0)
        kwargs["delay_max"] = float(params.get("delay_max") or 25.0)

    if getattr(task_class, "accepts_run_options", False):
        kwargs["run_options"] = context.run_options

    return kwargs


def _bbq_mode_note(kwargs: Mapping[str, Any], spend_policy: SpendPolicy) -> str:
    """宴席模式的说明文字（与改造前 ``main.cmd_run()`` 的三句话逐字一致）。"""
    if not kwargs.get("eat"):
        return "宴席模式：只读（不会发送 15003；要真的吃请加 --eat）"
    if spend_policy.allows_purpose(SpendPurpose.BBQ_INVITE):
        return "宴席模式：邀请好友（最高档，会花钻石）"
    return "宴席模式：免费档（自己吃，不花钻石）"


def _notify_step_done(
    step: StepResult,
    *,
    on_finish: FinishSink | None,
    emit: NoteSink | None,
) -> None:
    """把"这一步已结束"的通知交给入口层（``on_finish`` 与 ``emit`` **二选一**）。

    【为什么要有它（2026-10-04 安全审计 P1）】
    ``run_tasks`` 的两条兜底分支（用户中断 / 未捕获异常）原先只调 ``emit``，
    而 Web 入口把步骤从 ``running`` 改成 ``ok``/``fail`` 的**唯一**通路是
    ``on_finish`` —— 漏掉它，这一步就会在进度条上永远转圈，
    即便整批早已结束（即审计里说的"无响应的盲区"）。

    【为什么是二选一而不是都调】
    Web 侧的 ``on_finish``（``webapi/state.py::RunManager._on_step_finish``）
    内部已经会输出同一行结果；这里若再 ``emit`` 一次，同一句话会出现两遍。
    CLI 侧没有 ``on_finish``，因此仍走 ``emit``，输出格式与从前逐字一致。
    """
    if on_finish is not None:
        on_finish(step)
    elif emit is not None:
        emit(step.line)


def run_tasks(
    context: RunContext,
    request: RunRequest,
    *,
    emit: NoteSink | None = None,
    on_start: StartSink | None = None,
    on_finish: FinishSink | None = None,
    report: RunReport | None = None,
) -> RunReport:
    """按顺序执行 :attr:`RunRequest.tasks`，返回总报告。

    :param context: :func:`prepare` 产出的上下文。
    :param request: 运行意图。
    :param emit: 说明文字的出口（CLI 传 ``print``，Web 传日志流）。
    :param on_start: 每个任务**开始前**的回调（Web 用它把该步标成"运行中"）。
    :param on_finish: 每个任务**结束后**的回调（Web 用它写出该步的状态与耗时）。
    :param report: 复用的报告对象（Web 传进来是为了"边跑边看"：进度回调里
        就能读到已完成的步骤）；``None`` 表示新建。
    :return: :class:`RunReport`。
    :raises UnknownTaskError: 只要有任务名不在注册表里就立刻抛出 ——
        **在跑任何一个任务之前**就检查完，避免"跑到第 3 个才发现名字写错"
        （此时前两个已经真的领了奖励，用户却以为整批失败）。

    【为什么某一步失败不中断后面的步骤】
    与 ``tasks/daily.py`` 的策略一致："邮件已领空"不该导致后面的签到也不做。
    每个失败都会如实记进报告（``report.failed``），由入口层去呈现。
    真正的代码 bug 依旧会抛出去（``BaseTask.run`` 只吞业务异常）——
    但只抛到**这一层为止**：2026-10-03 实测 free_gift 的 ``EnvelopeError``
    （模型解码异常，不属于 ``CherrytaleError``）曾把整批运行炸断，
    排在后面的活跃宝箱根本没执行，用户还以为宝箱坏了。所以循环里加了
    最后一道兜底：单个任务的未捕获异常被合成一条失败步骤，
    后续任务照常执行；``SessionKickedError`` 仍在 ``_run_single`` 内处理（自动重登）。

    【消费额度为什么要在步骤之间"接回来"】
    ``SpendPolicy`` 是不可变对象，"已花多少"记在对象里。任务构造时拿到的是当前策略，
    执行中若获批花钱会返回**新对象**放在 ``task.spend_policy`` 上。
    这里把它接回上下文，下一次任务构造时就带着"已花 N 钻"，
    ``max_diamond_per_run`` 才能真正在一整批任务里累加
    （与 ``tasks/daily.py`` 对子任务的处理完全同源）。
    """
    # 注册表必须已填充：否则 resolve_task_class 会把每个名字都判成"未找到"
    # （导入是幂等的，重复调用没有代价；见 load_builtin_tasks）
    load_builtin_tasks()

    active_report = RunReport() if report is None else report
    active_report.spend_policy = context.spend_policy

    # ① 先把**所有**任务名校验一遍（见 docstring 里"跑一半才发现写错"的说明）
    resolved: list[tuple[TaskRequest, type[BaseTask]]] = [
        (task, resolve_task_class(task.name)) for task in request.tasks
    ]

    # ② 凭据（若有）只在本次运行期间存在；DEBUG 级别同理，都在 with 退出时恢复
    with _temporary_credentials(request.credentials), _temporary_log_level(request.verbose):
        for task, task_class in resolved:
            # ★ 2026-10-04：**任务边界**是取消的最主要检查点。
            # 用户点「停止执行」之后，至多再跑完手上的这一个任务就会停下 ——
            # 一个任务通常只是一两个请求，这点延迟用户察觉不到。
            cancellation.raise_if_cancelled()
            try:
                step = _run_single(
                    context,
                    request,
                    task,
                    task_class,
                    emit=emit,
                    on_start=on_start,
                    on_finish=on_finish,
                )
            except SessionKickedError:
                # 会话被顶：_run_single 内部已尝试自动重登重试；走到这里说明
                # 恢复失败/被禁用 —— 保持原有语义，整批终止（后续任务都要重登才有意义）
                raise
            except cancellation.TaskAborted as exc:
                # ★ 2026-10-04：用户在**任务内部**（例如扫到第 7 关时）点了停止。
                # 把它翻译成一条"没跑完"的步骤写进报告（界面因此看得出来
                # 这一步是被中断的，而不是失败），再抛出去让整批收尾。
                step = StepResult(
                    name=task.name,
                    ok=False,
                    message=f"已中断（{exc}）：本任务未完成的部分没有执行",
                )
                active_report.steps.append(step)
                _notify_step_done(step, on_finish=on_finish, emit=emit)
                raise
            except Exception as exc:  # noqa: BLE001 —— 兜底见 docstring 的 2026-10-03 说明
                step = StepResult(
                    name=task.name,
                    ok=False,
                    message=f"任务内部未捕获异常：{exc!r}",
                )
                _LOGGER.exception("任务 %s 抛出未捕获异常（已兜底，继续后续任务）", task.name)
                _notify_step_done(step, on_finish=on_finish, emit=emit)
            active_report.steps.append(step)

    active_report.spend_policy = context.spend_policy
    active_report.notes = list(context.notes)
    return active_report


def _recover_kicked_session(context: RunContext, *, emit: NoteSink | None) -> bool:
    """会话被其他设备登录顶掉后的自动恢复。

    用**当前会话绑定账号**（按 platform_user_id 查账号库）免密重登一次 ——
    新设备挤掉旧设备是服务端既定行为，这里让"被挤的一方"自动跟随最新登录。
    ``login`` 任务成功后会调 ``session.set_game_state(...)`` 更新**同一个**
    会话对象，随后 ``_run_single`` 的重试自然带上新令牌。

    :return: 是否恢复成功；``False`` = 无法自动恢复（无账号绑定 / 重登失败），
        调用方按普通失败处理。
    """
    state = context.session.game_state
    if state is None or not state.platform_user_id:
        return False
    from client.account_store import AccountStore

    account = AccountStore().find_account(state.platform_user_id)
    if account is None:
        return False
    display = account.account or f"{state.platform_user_id[:10]}…"
    _note(
        context,
        f"检测到会话被其他设备的登录顶掉，正在用保存的账号免密重新登录（{display}）…",
        emit,
    )
    login_request = RunRequest(
        tasks=(
            TaskRequest(
                name="login",
                params={
                    "account": state.platform_user_id,
                    "server_id": state.server_id,
                },
            ),
        ),
        account=state.platform_user_id,
    )
    report = run_tasks(context, login_request, emit=emit)
    if not report.ok:
        return False
    _note(context, "自动重新登录成功，重试当前任务。", emit)
    return True


def _run_single(
    context: RunContext,
    request: RunRequest,
    task: TaskRequest,
    task_class: type[BaseTask],
    *,
    emit: NoteSink | None,
    on_start: StartSink | None,
    on_finish: FinishSink | None,
    allow_kick_recovery: bool = True,
) -> StepResult:
    """执行单个任务：规范化参数 → 补齐说明 → 组装 kwargs → 执行 → 记耗时。"""
    params, dropped = normalize_params(task.name, task.params)
    if dropped:
        # 命令行路径不会走到这里（argparse 只有一个任务的旗标）；
        # 这条提示主要服务 Web/脚本调用方：填了不生效比报错更难查。
        _note(context, f"⚠ {task.name} 忽略了不认识的参数：{'、'.join(sorted(dropped))}", emit)

    if on_start is not None:
        on_start(task.name)

    kwargs = task_kwargs_for(request, task, context, params, task_class)

    if task.name == "bbq_energy":
        _note(context, _bbq_mode_note(kwargs, context.spend_policy), emit)
    if "run_options" in kwargs:
        _note(context, f"运行参数：{context.run_options.describe()}", emit)

    started = time.perf_counter()
    task_instance = create_task(task.name, context.session, **kwargs)
    try:
        result = task_instance.run()
    except SessionKickedError:
        # 会话被其他设备的登录顶掉（多端登录同一账号的必然现象）：
        # 用保存的账号免密重登一次，再重试本任务 —— 对使用者透明。
        # 只恢复一次：重试中再被踢（罕见的多端互踢竞争）就按普通失败上报。
        if not allow_kick_recovery or not _recover_kicked_session(context, emit=emit):
            raise
        started = time.perf_counter()
        task_instance = create_task(task.name, context.session, **kwargs)
        result = task_instance.run()
    elapsed = time.perf_counter() - started

    # 接回可能被更新过的消费策略（见 run_tasks 的说明）
    context.spend_policy = task_instance.spend_policy

    step = StepResult.from_task_result(result, elapsed=elapsed)
    if on_finish is not None:
        on_finish(step)
    return step



