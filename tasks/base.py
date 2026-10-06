"""任务基类、统一返回值与注册表。

【这一层存在的意义】
如果每个业务动作都各写各的，你很快会得到一堆「长得都不一样」的函数：
有的返回 bool，有的抛异常，有的打印日志后返回 None。
之后想串起「登录 → 签到 → 领取奖励」这样的每日流程时，
只能写一大堆 if/else 去适配每种风格。

本模块提供三个统一约定：

1. :class:`TaskResult`：所有任务都返回同一种结果对象；
2. :class:`BaseTask`：所有任务都实现 ``execute()``，由基类的 ``run()`` 统一
   负责日志、异常翻译与耗时统计；
3. :func:`register_task`：任务自动登记到注册表，``main.py`` 因此可以按名字调度，
   新增任务时不需要改入口文件的任何代码。
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Mapping

from client.exceptions import AuthenticationError, CherrytaleError, SpendBlockedError, SessionKickedError
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendKind, SpendPolicy, SpendPurpose

# ---------------------------------------------------------------------------
# 奖励 / 数量的呈现层（实现在 models/reward.py，这里只做再导出）
# ---------------------------------------------------------------------------
# 【为什么实现放在 models/ 而不是本文件】
# 分层方向是 tasks/ → models/：模型层描述"协议里的数据长什么样"，
# 顺带描述"它该怎么被念出来"最自然；反过来让 models/ 依赖 tasks/ 会形成循环导入。
#
# 【为什么在 tasks/base.py 里再导出一次】
# 全项目的调用点历来都写 `from tasks.base import reward_text`。
# 保留这组名字，是为了让"口径改造"只改语义、不改 import 路径 ——
# 否则每个任务文件都要动一次 import，纯属噪音。
#
# 【★ 2026-10-04 语义变更（重要）】
# ``reward_text()`` 现在**必须**显式传 ``scope``（数量口径），
# 且 ``scope=UNKNOWN`` 时返回空串。这不是"少显示点东西"，
# 而是把"我们其实不知道这个数字是增量还是余额"如实说出来 ——
# 详见 models/reward.py 的模块文档（Q-021 真机结论）。
from models.reward import (
    QuantityScope,
    count_item_kinds,
    count_items,
    cost_phrase,
    item_name,
    reward_phrase,
    reward_text,
)

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks")


@dataclass(frozen=True)
class TaskResult:
    """任务的统一返回值。

    :param task: 任务名（与注册名一致，便于日志里一眼看清是哪个任务）。
    :param ok: 是否成功。
    :param message: 人类可读的结果描述（成功时的简短说明，失败时的错误原因）。
    :param data: 需要往上传递的结构化数据（例如登录后拿到的 uid / token）。
    """

    task: str
    ok: bool
    message: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        mark = "✔" if self.ok else "✘"
        return f"[{mark}] {self.task}: {self.message or ('成功' if self.ok else '失败')}"


@dataclass(frozen=True)
class RunOptions:
    """一次运行的「用户意图」参数（场数、间隔…）。

    【为什么要有这个对象，而不是每个任务各加几个构造参数】
    竞技板块有四个模式，将来每个都要"打几场 / 间隔多久"；如果每个任务各写一遍
    ``battles``、``interval`` 参数，命令行入口就得为每个任务写一段透传代码，
    早晚漏掉一个。统一成一个对象后，``main.py`` 只需要判断
    "这个任务声明了 :attr:`BaseTask.accepts_run_options` 吗"，然后传一次。

    【默认值为什么全是"什么都不做"】
    ``battles=0`` = 只读。竞技类请求一发出就消耗玩家真实次数，
    所以"没配置"绝不能等于"随便打"（fail-closed 的同一套哲学，见
    ``client/spending.py`` 的钻石闸门）。

    :param battles: 用户想打几场（0 = 只读，不发任何消耗次数的请求）。
    :param max_battles: 硬上限（0 = 不额外限制）。与 ``battles`` 是**两道独立防线**：
        ``battles`` 防程序乱跑，``max_battles`` 防人手动输入时多打一个 0。
    :param interval_seconds: 每场之间的间隔秒数（0 = 不等待）。
    """

    #: 用户想打几场（0 = 只读）
    battles: int = 0
    #: 硬上限（0 = 不额外限制）
    max_battles: int = 0
    #: 每场间隔秒数
    interval_seconds: float = 0.0

    @property
    def is_read_only(self) -> bool:
        """本次是否**只读**（一场都不打）。"""
        return self.effective_battles == 0

    @property
    def effective_battles(self) -> int:
        """实际要打的场数 = ``min(battles, max_battles)``（上限为 0 时表示不限制）。

        【为什么把裁剪放在这里，而不是让任务各写一遍】
        两个地方各写一遍 ``min(...)``，早晚有一处写成 ``max(...)`` 或忘了裁剪；
        放在对象上之后，"要打几场"全项目只有一个答案。
        """
        if self.max_battles <= 0:
            return max(self.battles, 0)
        return max(min(self.battles, self.max_battles), 0)

    def was_capped(self) -> bool:
        """用户给的场数是否被硬上限裁剪过（用于提示"为什么没打完"）。"""
        return self.battles > self.effective_battles

    def describe(self) -> str:
        """一行摘要（启动日志与演练输出用）。"""
        if self.is_read_only:
            return "只读模式（battles=0）：不会发送任何消耗次数的请求"
        text = f"计划打 {self.effective_battles} 场（间隔 {self.interval_seconds:g}s）"
        if self.was_capped():
            text += f"；注意：你要求 {self.battles} 场，已被硬上限 {self.max_battles} 场裁剪"
        return text


class BaseTask(ABC):
    """所有业务任务的基类。

    子类只需做两件事：

    1. 声明 :attr:`name`（供注册表与命令行调度使用）；
    2. 实现 :meth:`execute`。

    **不要在** :meth:`execute` 里自己写 try/except 去吞异常：
    基类的 :meth:`run` 已经统一处理了「已定义的业务异常」，
    而真正的代码 bug 应该原样抛出，这样你才能看到完整的堆栈。
    """

    #: 任务名，必须唯一且非空（注册时会校验）
    name: ClassVar[str] = ""

    #: 是否需要登录态。为 ``True`` 时，未登录会在执行前被拦下。
    requires_auth: ClassVar[bool] = True

    #: 本任务是否存在**会花掉钻石**的路径（例如宴席里"花钻石邀请好友"）。
    #:
    #: 【为什么要做成"类级声明"而不是只靠任务里自己判断】
    #: 任务内部的花钱分支可能藏在多层 if / 多个档位里，只要有一处忘了判断，
    #: 就会在开关关闭时照发不误。声明在类上之后，:meth:`check_preconditions`
    #: 会在任务开始前统一过闸门 —— 这道防线不依赖任务作者的自觉。
    #:
    #: .. note::
    #:    声明为 ``True`` 只表示"存在花钱路径"，**不代表**一定会花：
    #:    默认开关关闭时，付费路径会被跳过，任务仍可完成免费部分。
    spends_diamond: ClassVar[bool] = False

    #: 本任务**存在花钻石路径**的用途清单（见 ``client/spending.py`` 的 ``SpendPurpose``）。
    #:
    #: 【与 :attr:`spends_diamond` 的分工 —— 这是 2026-09-21 新增的一层】
    #: ``spends_diamond = True`` 的语义是"**整个任务**都可能花钱"，所以开关关闭时
    #: 任务会被直接拦死；而宴席拆成两步之后，花不花钱只取决于其中一个分支：
    #: 邀请好友要花钱、确认吃宴席免费。把整任务拦死会让"只想吃免费档"也跑不了。
    #:
    #: 于是用途清单的语义是：**"某个分支会花钱"**，未授权时
    #: :meth:`check_preconditions` 只提示"该分支将被跳过"，任务本体照常执行；
    #: 硬保证交给组包前的 :meth:`request_diamond_spend` —— 未授权 ⇒ 请求字节
    #: 从未被构造。两道合起来，既不会误拦免费路径，也不会漏放花钱路径。
    diamond_purposes: ClassVar[tuple[SpendPurpose, ...]] = ()

    #: 需要在 ``python main.py tasks`` 里额外提示的一句话（默认无）。
    #:
    #: 为什么要有它：任务的"需要主动开启"这类信息写不进类型系统，
    #: 但用户必须在跑之前看到，否则只会觉得"这任务好像什么都没做"。
    opt_in_hint: ClassVar[str] = ""

    #: 本任务是否接受**运行参数**（:class:`RunOptions`：打几场、间隔多久…）。
    #:
    #: 【为什么做成类级声明】
    #: ``main.py`` 要为所有任务提供一个统一的命令行入口，而"场数/间隔"这类参数
    #: 只对竞技类任务有意义（日常任务是"每天一次"的固定流程，没有场数概念）。
    #: 声明为 ``True`` 的任务才会收到 ``run_options``：其它任务既不会被塞进
    #: 不认识的参数，命令行也不必为每个任务写一段 if。
    accepts_run_options: ClassVar[bool] = False

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        run_options: RunOptions | None = None,
    ) -> None:
        self.session = session
        self.logger = logger or _LOGGER
        #: 消费许可策略（见 ``client/spending.py``）。
        #:
        #: ``None`` 表示用 ``config`` 里的全局开关构造 —— 也就是**默认禁止花钻石**。
        #: 将来的设置界面只需构造好 ``SpendPolicy`` 传进来，
        #: 本类与各任务**不需要任何改动**：这就是"留一个接口"的落点。
        self.spend_policy = spend_policy if spend_policy is not None else SpendPolicy.from_config()
        #: 演练模式：**只组装、不发送**。
        #:
        #: 为什么放在基类而不是各任务自己定义？
        #:   1. 每个会发包的任务都需要它（否则"第一次发"永远无法预演）；
        #:   2. 统一放在基类，`main.py run --dry-run` 才能对所有任务一视同仁 ——
        #:      否则每加一个任务都要改一次命令行入口，早晚漏掉一个。
        self.dry_run = dry_run
        #: 运行参数（打几场 / 间隔多久）。仅在任务声明 ``accepts_run_options = True``
        #: 时由 ``main.py`` 传入具体值；其它情况是"什么都不做"的默认对象
        #: （``battles=0`` = 只读），所以任务里可以**无条件**使用它，不必判空。
        self.run_options = run_options if run_options is not None else RunOptions()

    # ------------------------------------------------------------------
    # 子类实现
    # ------------------------------------------------------------------
    @abstractmethod
    def execute(self) -> TaskResult:
        """执行任务并返回结果（由子类实现）。

        :raises client.exceptions.CherrytaleError: 业务/网络失败时抛出，
            基类会把它转成失败的 :class:`TaskResult`。
        """

    # ------------------------------------------------------------------
    # 框架提供
    # ------------------------------------------------------------------
    def describe(self) -> str:
        """一句话描述本任务（用于 ``--list`` 之类的展示）。"""
        doc = (type(self).__doc__ or "").strip().splitlines()
        return doc[0] if doc else f"任务 {self.name}"

    def check_preconditions(self) -> None:
        """执行前的检查：先看登录态，再过**消费闸门**。

        :raises client.exceptions.AuthenticationError: 需要登录但没有可用令牌。
        :raises client.exceptions.SpendBlockedError: 任务存在钻石消耗路径，
            但钻石使用开关处于关闭状态。

        .. note::
           第二道检查是"类级"的：只看 :attr:`spends_diamond` 这个声明，
           不关心任务内部究竟会走哪条分支。这样即使任务作者在某个 if 里
           漏了判断，也会在**任何请求发出之前**被拦下。

        登录检查用的是 :meth:`client.session.GameSession.has_auth_context`：
        "游戏会话状态（1004 令牌）或平台令牌，任一有效即放行"。
        它只负责挡住"完全没登录过"，真正的硬保证在 :meth:`open_game_client`
        —— 那里没有有效状态就**构造不出**可发包的客户端。
        """
        if self.requires_auth and not self.session.has_auth_context():
            raise AuthenticationError(
                401, f"任务 {self.name!r} 需要登录态，但当前令牌为空或已过期"
            )

        if self.spends_diamond:
            # amount=0：这里拦的是"存在花钱路径"这件事本身，不是某一笔具体金额，
            # 所以不参与累计上限（charge 时 amount=0 也不会改变累计值）。
            try:
                self.spend_policy.check(
                    kind=SpendKind.DIAMOND,
                    amount=0,
                    reason=f"任务 {self.name!r} 存在钻石消耗路径（例如宴席花钻石邀请好友）",
                )
            except SpendBlockedError as exc:
                # 演练模式（``--dry-run``）**不发送任何东西**，所以允许继续 ——
                # 而且演练的价值恰恰在于"让人看清关闭状态下会发生什么"。
                # 真实执行时这里一律抛出，绝不放过。
                if not self.dry_run:
                    raise
                self.logger.warning(
                    "（演练）本任务存在花钱路径，但钻石使用开关关闭：%s", exc
                )

        # 用途级声明：**只提示、不拦任务**。
        # 【为什么不抛异常】声明"某个分支会花钱"不等于"整个任务都会花钱"：
        # 宴席任务在没开邀请权限时仍然要能只读跑完（这正是把 bbq 从日常流程里
        # 拆出来、改成"主动选择的开关模块"的前提）。硬拦改由组包前的
        # request_diamond_spend() 负责：未授权 ⇒ 请求字节从未被构造出来。
        not_allowed = [
            purpose
            for purpose in self.diamond_purposes
            if not self.spend_policy.allows_purpose(purpose)
        ]
        for purpose in not_allowed:
            self.logger.warning(
                "用途「%s」未获授权，对应步骤将被跳过（不会花任何钻石）。当前策略：%s",
                purpose.name,
                self.spend_policy.describe(),
            )

    def open_game_client(self) -> GameClient:
        """打开一个**带会话状态**的游戏网关客户端（业务任务都该用它）。

        :return: 已注入 ``token``（信封字段 2）与 ``player_id``（字段 8）的
            ``GameClient``；记得用 ``with`` 关闭。
        :raises client.exceptions.AuthenticationError: 非演练模式下没有可用的
            游戏会话状态时。

        用法::

            with self.open_game_client() as game:
                view = game.send(packet, expect=SomeRes)

        【为什么不能直接写 ``GameClient(...)``】
        那样构造出来的信封是**空的**：没有令牌、没有 playerId。而实测服务端只会
        回一句含糊的"包异常"，排查方向会被彻底带偏（去找字段编号、找 vCode，
        其实只是没带令牌）。统一走本方法，这类问题在"发出去之前"就会被拦住。

        【演练模式（--dry-run）为什么放行】
        ``--dry-run`` 的承诺是"只组装、不发送"，空令牌不会对服务端造成任何影响；
        而且演练的价值恰恰包括"看清没有令牌时字节长什么样"。真实执行时**绝不**放行
        —— 宁可看不到输出，也不发一个必然被拒的包（既浪费一次风控暴露，又毫无信息量）。
        """
        state = self.session.game_state
        if state is not None and state.is_valid():
            return GameClient.for_session(self.session, logger=self.logger)
        if self.dry_run:
            self.logger.info(
                "（演练）尚无游戏会话状态：组装出的信封不会带令牌（不会发包）"
            )
            return GameClient(logger=self.logger)
        raise AuthenticationError(
            401,
            f"任务 {self.name!r} 需要游戏会话状态（1004 令牌），但当前没有。\n"
            "  先登录一次即可：python main.py run login"
            "（成功后会写入 .session.json，后续命令自动复用）\n"
            "  或临时用环境变量：CHERRYTALE_SESSION_TOKEN=<令牌>",
        )

    def request_diamond_spend(
        self,
        amount: int,
        reason: str,
        purpose: SpendPurpose | None = None,
    ) -> None:
        """在**真正组包之前**申请一笔钻石消费。

        :param amount: 预计消耗的钻石数。
        :param reason: 这笔钱花在哪（会进异常信息与日志，便于事后审计）。
        :param purpose: 用途（见 :class:`~client.spending.SpendPurpose`）。
            **新代码应当总是传它** —— 传了才会同时受"用途白名单"管辖；
            ``None`` 只是为了不破坏既有调用点的旧行为（只受总闸管辖）。
        :raises client.exceptions.SpendBlockedError: 钻石开关关闭、用途未授权，
            或超出限额时。

        【为什么必须"先申请、后组包"】
        如果先组包再检查，请求字节就已经存在了 —— 一旦某处漏了检查或异常被
        顺手 catch 掉，字节就可能被发出去。把检查放在最前面，
        "开关关闭"就等于"字节从未被构造"，这才是真正的硬保证。

        【怎么用】

        .. code-block:: python

            def invite_friends(self, menu_id: int, friend_ids: list[int]) -> None:
                # ① 先过闸门（没授权就会抛异常 —— 请求字节根本不会被构造）
                self.request_diamond_spend(
                    amount=31,
                    reason="宴席邀请好友（最高档）",
                    purpose=SpendPurpose.BBQ_INVITE,
                )
                # ② 过了闸门才允许组包
                packet = BBQFriendsPacket.for_friends(group_id=menu_id, friend_ids=friend_ids)
                # 走到这里才说明闸门已放行，可以发送了

        .. note::
           获批后累计值会记进 ``self.spend_policy``（返回新对象并覆盖），
           因此同一流程里多笔消费会共同受 ``max_diamond_per_run`` 约束。
        """
        if self.dry_run:
            # 演练模式：只报告"这笔本来会被申请"，不改变策略状态，
            # 免得演练一次就把累计额度吃掉。
            self.logger.info(
                "（演练）将申请钻石消费：%d 钻 —— %s（用途 %s）；当前策略：%s",
                amount,
                reason,
                purpose.name if purpose is not None else "未指定",
                self.spend_policy.describe(),
            )
            return

        self.spend_policy = self.spend_policy.check(
            kind=SpendKind.DIAMOND, amount=amount, reason=reason, purpose=purpose
        )
        self.logger.info(
            "✔ 钻石消费已获准：%d 钻 —— %s（本次流程累计 %d 钻）",
            amount,
            reason,
            self.spend_policy.spent_so_far,
        )

    def run(self) -> TaskResult:
        """带日志与异常处理的执行入口（调用方应该用这个，而不是 ``execute``）。

        异常策略：

        - ``CherrytaleError``（我们自己定义的异常）→ 转成失败的 ``TaskResult``，
          让每日流程可以继续跑下一个任务，不至于因为一个任务失败就全中断；
        - 其它异常（例如 ``TypeError``）→ **原样抛出**。
          那说明代码有 bug，掩盖它只会让你以后更难查。
        """
        started = time.perf_counter()
        self.logger.info("▶ 开始任务 %s（%s）", self.name, self.describe())
        try:
            self.check_preconditions()
            result = self.execute()
        except SessionKickedError:
            # 会话被其他设备登录顶掉：**不**转成失败结果 —— 交给 runner 统一
            # 自动免密重登并重试当前任务（见 SessionKickedError 的文档）。
            # 在这里吞掉的话，恢复逻辑就永远没有介入的机会。
            raise
        except CherrytaleError as exc:
            elapsed = time.perf_counter() - started
            # ★ 2026-10-04 去重（D 组）：结果行由**入口层**统一输出
            # （CLI `print(step.line)` / Web `_log(result.line)`，格式 `[✘] 任务名: 原因`）。
            # 这里再念一遍整句就会让日志流里每步出现两遍 —— 所以只留
            # "哪个任务失败 + 耗时"这个入口层没有的信息，并保持 WARNING 级别
            # （前端据此标黄/标红，失败需要显眼）。
            self.logger.warning("✘ 任务 %s 失败（耗时 %.2fs）", self.name, elapsed)
            return TaskResult(task=self.name, ok=False, message=str(exc))

        elapsed = time.perf_counter() - started
        # 成功路径同理降为 DEBUG：`[✔] 任务名: 文案` 已由入口层输出。
        self.logger.debug("✔ 任务 %s 完成（%.2fs）：%s", self.name, elapsed, result.message)
        return result

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"


# ---------------------------------------------------------------------------
# 任务注册表
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, type[BaseTask]] = {}


def register_task(cls: type[BaseTask]) -> type[BaseTask]:
    """把任务类登记进注册表（用作装饰器）。

    :raises ValueError: 任务名为空，或与**另一个来源**的任务重名时。

    **为什么重名检查要区分「来源」而不是简单比较类对象？**
    同一个模块可能被导入两次，此时 Python 会创建两个不同的类对象：

    - 用 ``python -m tests.test_tasks`` 运行时，本文件先以 ``__main__`` 被导入，
      pytest 随后又会以 ``tests.test_tasks`` 收集它（模块名不同、类限定名相同）；
    - 直接 ``python tests/test_tasks.py`` 也有同样效果；
    - 交互式环境里的 ``importlib.reload()`` 同理。

    若只比较「是不是同一个类对象」或「模块名是否相同」，上述场景都会被误判成
    重名冲突而直接报错。因此判定规则定为：

        **类限定名相同，且（模块名相同 或 其中一方是 __main__）** → 视为同一个任务

    真正的冲突（两个不同模块各自定义了同名任务）仍然会被拦截，因为
    ``main.py`` 是按名字调度的，重名会让其中一个任务永远无法被调用，
    而这种 bug 靠肉眼极难发现。
    """
    if not cls.name:
        raise ValueError(f"{cls.__name__} 必须声明非空的 name 属性")

    existing = _REGISTRY.get(cls.name)
    if existing is not None and existing is not cls:
        same_origin = existing.__qualname__ == cls.__qualname__ and (
            existing.__module__ == cls.__module__
            or "__main__" in (existing.__module__, cls.__module__)
        )
        if not same_origin:
            raise ValueError(
                f"任务名 {cls.name!r} 已被 {existing.__module__}."
                f"{existing.__qualname__} 占用，不能重复注册"
            )

    _REGISTRY[cls.name] = cls
    return cls


def available_tasks() -> dict[str, type[BaseTask]]:
    """返回所有已注册任务（名字 → 类）的副本。"""
    return dict(_REGISTRY)


def get_task_class(name: str) -> type[BaseTask]:
    """按名字取任务类。

    :raises KeyError: 名字不存在时，异常信息会列出所有可选名字。
    """
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        options = ", ".join(sorted(_REGISTRY)) or "<尚未注册任何任务>"
        raise KeyError(f"未找到任务 {name!r}；可用任务：{options}") from exc


def create_task(name: str, session: GameSession, **kwargs: Any) -> BaseTask:
    """按名字创建任务实例。"""
    return get_task_class(name)(session, **kwargs)
