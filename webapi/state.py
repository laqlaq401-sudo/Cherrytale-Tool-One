"""运行状态：网页上的"进度条" + "同一时刻只跑一次"的保证。

【为什么要有状态对象，而不是让前端自己看日志】
日志是"流水账"，进度是"状态"。用户想知道的是"现在跑到第几个任务、哪个成功了"，
而不是从几百行日志里自己数。两者分开之后：日志可以随便刷屏（DEBUG 时），
进度始终是干净的一小块 JSON。

【★ 2026-10-04：从"拒绝"改成"排队"（用户需求）】
用户要的是"点了就执行，上一个没跑完就排着，按顺序全部跑完"，而不是弹一个
"上一次还没结束"。上面两条硬理由依然成立，所以排队**没有放松任何互斥**：

1. worker 只有**一个**线程，队列里的运行严格串行执行 —— 并发那个前提不存在；
2. 每个请求依旧先抢那把 ``_gate`` 再执行（只读请求也抢同一把），
   所以"异步运行 vs 只读请求"的互斥原样保留；
3. 取消令牌是"当前运行"一元的（见 ``services/cancellation.py``）：
   ``/api/stop`` 只会作用于正在跑的那一批，**队列里剩下的照常跑**
   （用户 2026-10-04 选定的语义）。

``RunBusyError`` / HTTP 409 因此只剩一个场景：worker 正卡在"等闸门"
（例如信息面板刷新占着它）时来了一个**同步只读**请求。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from services import banquet_state, cancellation, daily_state, runner, task_spec
from services.task_spec import get_spec
from webapi import logstream
from webapi import app as _app_module

_LOGGER: Final[logging.Logger] = logging.getLogger("webapi.state")

#: worker 在闸门外等多久才往日志面板写一行（秒）。**只影响日志，不影响等待本身**。
#:
#: 为什么是 60 秒：正常的只读请求（刷新信息面板等）几秒就回来了，够不到这个门槛；
#: 一旦够到，说明确实有东西长时间占着闸门（典型是 Android 网络超时 30s×N），
#: 这时候用户有权在日志里看到"它在等什么"。改小会让正常使用被刷屏（实测 3 秒太吵）。
_GATE_WAIT_LOG_EVERY_SECONDS: Final[float] = 60.0


def _heartbeat(note: str = "") -> None:
    """给卡死看门狗打一次心跳（★ 2026-10-06 新增）。

    看门狗此前只看"HTTP 请求在飞的数量"，而"点了执行、进度条一动不动"这种
    最典型的卡死恰恰是 ``POST /api/run`` 早就返回、只有 **worker 线程**卡住的形态
    —— 那时看门狗永远不触发（见 ``webapi/app.py::note_work_progress`` 的说明）。

    这里在**任务边界**打心跳：一次运行本来就是一两个请求一个任务，
    用任务边界当节拍既贴合业务，又不会因为"某个请求慢"就误报。
    """
    try:
        _app_module.note_work_progress(note)
    except Exception:  # noqa: BLE001 - 心跳失败绝不能影响任务本身
        pass


def _note_run_active(delta: int) -> None:
    """登记"worker 手上有没有一批在跑"（供看门狗判断忙不忙）。"""
    try:
        with _app_module._ACTIVITY_LOCK:  # noqa: SLF001 - 同一模块内的协作，见上
            _app_module._ACTIVITY["run_active"] = max(  # noqa: SLF001
                0, int(_app_module._ACTIVITY.get("run_active", 0)) + delta  # noqa: SLF001
            )
            _app_module._ACTIVITY["last_progress"] = time.time()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass

#: 单个步骤的状态。``pending`` = 还没轮到，``running`` = 正在跑，
#: ``skipped`` = **用户中断后没有执行**（2026-10-04 新增，与"失败"区分开：
#: 它不是做错了，而是压根没做）。
StepStatus = Literal["pending", "running", "ok", "fail", "skipped"]

#: 一次运行的整体状态。
RunStatus = Literal["idle", "running", "done"]


@dataclass
class _QueuedRun:
    """队列里的**一批**运行（一次 ``/api/run`` 提交 = 一批）。

    :param run_id: 这一批的编号。
    :param request: 原始意图。
    :param steps: 它自己的进度步骤（还没有轮到执行时就可以先摆出来给用户看）。
    """

    run_id: str
    request: runner.RunRequest
    steps: list[StepState] = field(default_factory=list)


class RunBusyError(RuntimeError):
    """已经有一次运行在进行中（入口层据此返回 HTTP 409）。"""


class InvalidRunError(ValueError):
    """请求本身不可执行（例如任务名写错、参数类型不对）。

    :param problems: 中文问题清单，入口层直接回给前端展示。

    【为什么继承 ``ValueError`` 还要单独定义】
    继承 ``ValueError`` 表达"调用方给的输入有问题"（与"服务端出错"区分开）；
    单独定义则是为了让入口层能用一行 ``except`` 精确捕获并转成 HTTP 400 ——
    不必去猜某个 ValueError 是不是从别处冒出来的。
    """

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("；".join(problems))
        #: 问题清单（可能多条，例如"勾了两个任务，名字都写错了"）。
        self.problems: list[str] = list(problems)


@dataclass
class StepState:
    """一个任务在进度条里的状态。

    :param name: 注册名（如 ``sweep_activity``）。
    :param title: 中文标题（来自 ``services/task_spec.py``，给用户看的）。
    :param status: 见 :data:`StepStatus`。
    :param message: 成功/失败的说明（来自任务自己的返回值）。
    :param elapsed: 耗时（秒）。
    :param data: 任务返回的结构化数据（供前端即时状态联动）。
    :param finished_at: 这一步**结束时的服务端钟点**（``HH:MM:SS``）；还没结束是空串。
    """

    name: str
    title: str
    status: StepStatus = "pending"
    message: str = ""
    elapsed: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    step_id: str = ""
    #: ★ 2026-10-06：结束时刻由**服务端**盖章，前端照抄显示。
    #:
    #: 【为什么不是前端自己取"现在"（用户报的"瞬间同时执行"假象）】
    #: 运行结果流水以前给每行盖的是**前端渲染那一刻**的时间戳。当页面是在
    #: 运行**之后**才加载/刷新的（``/api/status`` 会把最近 10 批已完成步骤一并
    #: 回填，见 :meth:`status`），十几行结果会在同一个瞬间被渲染出来 ——
    #: 于是一批**严格串行、耗时合计约 30 秒**的任务，在界面上全显示成同一个
    #: 秒数，看起来像"瞬间同时执行"。时间必须由真正知道它的人来记。
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成 JSON 可序列化的 dict。"""
        return {
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "message": self.message,
            "elapsed": round(self.elapsed, 2),
            "data": self.data,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "finished_at": self.finished_at,
        }


def _title_of(task_name: str) -> str:
    """取任务的中文标题；未登记规格时退回注册名（照样能用，只是不够漂亮）。"""
    spec = get_spec(task_name)
    return spec.title if spec is not None else task_name


class RunManager:
    """持有"当前 / 上一次运行"的状态，并保证同一时刻只有一次运行。

    :param stream: 日志流（状态变更与任务输出都写进去）。
    :param bridge: stdout 代理；传入后 worker 线程的 ``print`` 才会进日志流
        （``None`` 表示不捕获 —— 测试与命令行场景用得上）。
    """

    def __init__(
        self,
        stream: logstream.LogStream,
        *,
        bridge: logstream.ThreadAwareStdout | None = None,
    ) -> None:
        self._stream = stream
        self._bridge = bridge
        self._lock = threading.Lock()
        #: "同一时刻只跑一次"的**闸门**（异步运行与同步只读请求共用）。
        #:
        #: 【为什么不能只看 ``_status``】
        #: ``_status == "running"`` 只覆盖"异步运行"；而同步只读请求
        #: （拉区服列表、刷新信息面板）不写 status。两者若各判各的，
        #: 就会出现"异步运行 + 同步请求"同时跑 —— 凭据是进程级环境变量、
        #: 消费额度是每次运行独立的，正是我们最不想要的并发。
        #: 真正的互斥放在这把锁上，``_status`` 只负责"给用户看进度"。
        self._gate = threading.Lock()
        #: ★ 2026-10-04：**待执行队列**。``self._queue[0]`` = 正在执行的那一批，
        #: 其余是"已经在后台排着队"的。worker 只有一个线程，严格串行。
        #:
        #: 【为什么不再抛 RunBusyError 拒绝，而是排队】
        #: 用户要的就是"点十个就按顺序跑十个"。排队没有放松任何互斥
        #: （worker 单线程、每个请求开跑前照样抢 ``_gate``），
        #: 却把"点了没反应 + 一句解释"换成了"点了我知道你会跑"。
        self._queue: list[_QueuedRun] = []
        #: 已跑完的批次（只保留最近若干批）：队列跑空之后，进度卡片还能回看
        #: "刚才都做了什么"，而不是只剩最后一批。
        self._recent: list[list[StepState]] = []
        self._worker_thread: threading.Thread | None = None
        self._run_id: str | None = None
        self._status: RunStatus = "idle"
        self._started_at = ""
        self._finished_at = ""
        self._steps: list[StepState] = []
        self._summary = ""
        self._error = ""
        self._dry_run = False
        self._diamond_spent = 0

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        """当前是否有运行在进行中（含"队列里还有排队的"）。"""
        with self._lock:
            return bool(self._queue)

    @property
    def queue_length(self) -> int:
        """后台排着队还没跑的**批数**（不含正在跑的那一批）。"""
        with self._lock:
            return max(len(self._queue) - 1, 0)

    def status(self) -> dict[str, Any]:
        """状态快照（``GET /api/status`` 直接返回它）。

        ``next_seq`` 一并带上，前端因此不必额外再请求一次日志接口就能知道进度。

        【为什么 steps 要做"扁平化"】
        排着队的那几批也有步骤要展示（用户的预期是"我点了三个，进度里就显示
        三个"）。所以这里把"正在跑的那一批 + 排队批次的 pending 步骤"拼成一条
        列表给用户看 —— 顺序正是它们真正会被执行的顺序。
        """
        with self._lock:
            running = bool(self._queue)
            steps: list[dict[str, Any]] = []
            # 已经跑完的批次（最近的最多 10 批）→ 正在跑的 → 排队中的。
            # 【为什么空闲时不再加 ``_steps``】跑完的批次已经被收进 `_recent`，
            # 而 `_steps` 仍指向同一份列表 —— 再拼一次就是"每个步骤出现两遍"。
            for done in self._recent[-10:]:
                steps.extend(step.to_dict() for step in done)
            if running:
                steps.extend(step.to_dict() for step in self._steps)
                for queued_item in self._queue[1:]:
                    steps.extend(step.to_dict() for step in queued_item.steps)
            return {
                "run_id": self._run_id,
                "status": "running" if running else self._status,
                "running": running,
                "dry_run": self._dry_run,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "steps": steps,
                "summary": self._summary,
                "error": self._error,
                "diamond_spent": self._diamond_spent,
                # 后台排着队还没开跑的批数（前端据此显示"还有 N 批在排队"）
                "queued_runs": max(len(self._queue) - 1, 0),
                # ★ 2026-10-04：本游戏日服务端已记账的自动任务。
                # 轮询带着它，前端在运行中途/结束那一刻就能更新"今天跑过哪些"，
                # 不必再去请求一次 /api/tasks（那份清单本身也很少变）。
                "auto_done": list(daily_state.completed_names()),
                # ★ 2026-10-05：宴席"哪顿吃过了"也随轮询带给前端 ——
                # 理由与 auto_done 相同：吃完那一刻就该把按钮置灰，
                # 不必等下一次 /api/tasks。
                "banquet": banquet_state.describe(),
                "next_seq": self._stream.next_seq,
            }

    # ------------------------------------------------------------------
    # 启动一次运行
    # ------------------------------------------------------------------
    def start(self, request: runner.RunRequest) -> tuple[str, int]:
        """校验请求、**入队**并返回 ``(run_id, 前面还有几批)``。

        :param request: 运行意图（由入口层从 JSON / 命令行翻译而来）。
        :return: ``(本次运行的 run_id, 排在本批次之前的批数)``。
            ``0`` 表示"立刻开始"，``1`` 表示"前面还有一批在跑"。
        :raises InvalidRunError: 请求不可执行（任务名写错、参数类型不对…）。

        .. note::
           校验放在**入队之前**：这样"任务名写错"能立刻拿到 HTTP 400，
           而不是先返回 202、再在状态里出现一个失败步骤 —— 后者对前端更难处理，
           对用户也更难理解（"点了运行，过一会儿才说名字错了"）。

        【为什么现在不再抛 RunBusyError（2026-10-04）】
        以前"忙"= HTTP 409 + 前端弹窗。用户明确要求改成：**直接挂在后台排队**，
        按顺序全部跑完。互斥由"单 worker 线程 + 每批开跑前抢闸门"保证，
        与排队并不冲突（见 :attr:`_gate` 的说明）。
        """
        problems = runner.validate_request(request)
        if problems:
            raise InvalidRunError(problems)

        with self._lock:
            run_id = uuid.uuid4().hex[:12]
            # 尚未开跑的步骤先按 pending 摆好：即便还在排队，进度视图里也能
            # 看见"我点的东西已经排上了"（见 status 的扁平化说明）。
            steps = [
                StepState(
                    name=task.name,
                    title=_title_of(task.name),
                    run_id=run_id,
                    step_id=f"{run_id}_{idx}_{task.name}",
                )
                for idx, task in enumerate(request.tasks)
            ]
            # 排在本批**之前**的批数（含正在跑的那一批）：
            # 队列为空 → 0（立刻开始）；队列里有一批 → 1（它跑完就轮到本批）。
            item = _QueuedRun(run_id=run_id, request=request, steps=steps)
            position = len(self._queue)
            self._queue.append(item)
            if position == 0:
                # ★ 2026-10-04：队列本来是空的 → 本批就是"接下来要跑的那一批"，
                # 立刻把它摆进全局进度视图（``_claim_locked``）。
                # 【为什么不能等 worker 自己去认领】worker 可能还没被操作系统
                # 调度到（尤其 ``_spawn_worker`` 是持锁 ``start()``，新线程一醒来
                # 就卡在同一把锁上）。这段窗口里 ``_steps`` 还是空的，
                # ``status()`` 就只看得见"排队的批次"、看不见刚提交的这一批 ——
                # 用户点了执行却看到进度里没有它。这里先摆好，worker 稍后
                # 再认领一次是幂等的（见 ``_claim_locked``）。
                self._claim_locked(item)
            worker_alive = (
                self._worker_thread is not None and self._worker_thread.is_alive()
            )

        if not worker_alive:
            self._spawn_worker(run_id)

        mode = "演练模式（不会发任何请求）" if request.dry_run else "真实执行"
        if position == 0:
            self._log(f"▶ 已受理运行 {run_id}：{mode}，共 {len(request.tasks)} 个任务")
        else:
            self._log(
                f"⏳ 运行 {run_id} 已排入队列（前面还有 {position} 批）："
                f"{mode}，共 {len(request.tasks)} 个任务"
            )
        return run_id, position

    def _claim_locked(self, item: "_QueuedRun") -> None:
        """把"当前进度视图"指向这一批（**必须在持锁状态下调用**）。

        ``start()`` 与 ``_worker_loop`` 共用同一段认领逻辑：
        - ``start()`` 用它让**刚受理**的批次立刻出现在进度里（见那里的说明）；
        - ``_worker_loop`` 在真正开跑前再确认一次。

        重复认领同一批是幂等的（只是把同样的值再写一遍），所以两处都调它是安全的。
        """
        self._steps = item.steps
        self._run_id = item.run_id
        self._status = "running"
        self._started_at = time.strftime("%H:%M:%S")
        self._finished_at = ""
        self._summary = ""
        self._error = ""
        self._dry_run = item.request.dry_run
        self._diamond_spent = 0

    def _spawn_worker(self, run_id: str) -> None:
        """起那个唯一的 worker 线程（串行消费整个队列）。

        【★ 2026-10-04：``start()`` 必须在**持锁状态**下调用】
        旧写法是"锁内置句柄 → 出锁再 ``start()``"。``Thread.is_alive()`` 在
        ``start()`` 返回前一直是 ``False``，所以那条缝里若又来一个提交，
        双检会误判成"没有存活线程"从而**再起一个 worker** —— 两个 worker
        并行消费队列，"同一时刻只跑一次"的前提（凭据是进程级环境变量、
        消费额度按运行累计）就被打破了。
        持锁 ``start()`` 是安全的：新线程的第一件事是抢同一把锁，
        它只会稍等片刻，不构成死锁。
        """
        with self._lock:
            # 双检：可能另一个线程刚起好（HTTP 请求是并发的）
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"cherrytale-run-{run_id}",
                # 守护线程：服务停止时不为了"等任务跑完"而卡住进程退出。
                daemon=True,
            )
            self._worker_thread = thread
            # 在锁内 start：句柄登记与线程真正开跑之间不留缝（见 docstring）。
            thread.start()

    # ------------------------------------------------------------------
    # ★ 中断（2026-10-04 新增）
    # ------------------------------------------------------------------
    def stop(self, reason: str = cancellation.DEFAULT_REASON) -> dict[str, Any]:
        """中断**当前正在跑的那一批**（队列里剩下的照常跑）。

        :return: 给人看的结果 dict；``stopped=False`` 表示"现在没有可中断的运行"。

        【为什么不清空队列（用户 2026-10-04 选定）】
        用户的语义是"停手上的这一件"，不是"取消我排的所有东西"：
        排队是他自己点的，停掉当前一件之后，下一件照常开始才符合预期。
        “都别跑了”另有它的表达方式 —— 那是再点上实实在在的那张卡片。

        【为什么已经标记了 pending 步骤为 skipped】
        当前这批剩下的任务不会被执行了，把状态从 pending 改成 skipped，
        用户才能在进度里一眼看出"这几个是我自己停掉的"，而不是一直转圈。
        """
        token = cancellation.request_cancel(reason)
        if token is None:
            return {"stopped": False, "message": "当前没有正在运行的任务。"}

        skipped: list[str] = []
        with self._lock:
            # 只动**正在跑那一批**的 pending 步骤；正在跑的那一个由它自己
            # 的任务边界决定如何结束（runner 会把中断翻译成一条失败步骤）。
            for step in self._steps:
                if step.status == "pending":
                    step.status = "skipped"
                    step.message = "已中止（用户点了停止执行）"
                    skipped.append(step.title or step.name)

        self._log(f"⏹ 已请求中断：{reason}（未执行的 {len(skipped)} 个任务已标记跳过）")
        return {
            "stopped": True,
            "message": "已请求中断：正在跑的任务会在边界处停下，队列里的照常执行。",
            "skipped": skipped,
            "queued_runs": self.queue_length,
        }

    def _acquire_gate(self) -> bool:
        """尝试独占"运行闸门"（非阻塞）。

        :return: ``True`` = 抢到了（用完必须 ``release``）；``False`` = 已有别人在跑。
        """
        return self._gate.acquire(blocking=False)

    def run_inline(self, request: runner.RunRequest) -> runner.RunReport:
        """**同步**跑一次（在调用者线程里），用于 1~2 秒的只读请求。

        :param request: 运行意图（通常是 ``plan_only`` 的 login，或若干只读查询）。
        :return: :class:`services.runner.RunReport`（直接读 ``steps[*].data`` 即可）。
        :raises InvalidRunError: 请求不可执行（任务名写错、参数类型不对…）。
        :raises RunBusyError: 已有运行在进行（含后台的异步运行）。

        【为什么必须有它】
        "拉区服列表""刷新信息面板"这类请求要在**一次 HTTP 响应里**带回结果，
        不能像"一键运行"那样先返回 ``run_id`` 再轮询 —— 否则前端要多三四次往返，
        还得自己把"轮询状态"与"取结果"对上，复杂度全无必要。
        但它们**仍然必须**走同一把闸门：理由见 :attr:`_gate` 的说明。

        【为什么它不写运行状态（status / steps）】
        它是一次"只读问答"，不是用户眼里的"一次运行"。写进去会让进度条突然
        出现一堆用户没点过的步骤。所以只在**日志流**里留痕。
        """
        problems = runner.validate_request(request)
        if problems:
            raise InvalidRunError(problems)

        if not self._acquire_gate():
            raise RunBusyError("上一次运行还没有结束，请等它跑完（或稍后再试）")

        context: runner.RunContext | None = None
        try:
            with self._capture():
                context = runner.prepare(request, emit=self._log)
                return runner.run_tasks(context, request, emit=self._log)
        finally:
            if context is not None:
                context.close()
            self._gate.release()

    # ------------------------------------------------------------------
    # worker：真正干活的线程（★ 2026-10-04：串行消费整个队列）
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        """把队列里的批次**逐个**跑完；**任何时候都不许把异常抛给线程边界**。

        【为什么这里要兜住所有异常】
        线程里未捕获的异常只会打印到 stderr，前端永远等不到"结束"信号，
        表现为"进度条卡在运行中"。所以无论发生什么，最后都要把状态推进到
        ``done`` 并放开一切资源。

        【为什么由它去 pop 队列而不是一次性取完】
        用户可以在队列跑的过程中**继续点执行** —— 那些新批次会追加进来，
        本循环照跑不误。好处是"再点一次"不需要判断"worker 还活着吗"。

        【★ 2026-10-04 修复：退场必须与"队列为空"同锁原子完成】
        旧写法是"在锁内发现队列为空 → 跳出循环 → 在 ``finally`` 里另取一次锁
        把 ``_worker_thread`` 置空"。这两步之间有一个窗口：``start()`` 此时入队，
        并在锁内看到**旧线程仍存活**（``is_alive()`` 为真）于是不再起新线程；
        而旧线程紧接着退出并把 ``_worker_thread`` 置空 ——
        结果是这一批**永远留在队列里**（表现为"提交了却没反应"）。
        现在把"发现队列为空"与"置空线程句柄 + 状态收尾"放在**同一把锁**内，
        ``start()`` 的入队与存活判断也在同一把锁内，两者因此不可能交错：
        要么 worker 看到新批次继续消费，要么它已彻底退场、由 ``start()`` 起新线程。
        """
        while True:
            with self._lock:
                if not self._queue:
                    # 队列已空：在同一把锁内完成退场标记（见 docstring 的竞态说明）。
                    self._worker_thread = None
                    self._status = "done"
                    self._finished_at = time.strftime("%H:%M:%S")
                    return
                item = self._queue[0]
                # 认领这一批：把全局的"当前进度"指向它的步骤列表
                # （与 start() 共用同一段逻辑，重复认领幂等）。
                self._claim_locked(item)
            try:
                self._execute_one(item)
            except Exception:  # noqa: BLE001
                # ``_execute_one`` 内部已兜底，这里只防"兜底本身又炸"：
                # 记一笔继续消费队列，绝不把异常抛给线程边界（那会让队列滞留）。
                _LOGGER.exception("运行 %s 的 worker 循环出现未捕获异常", item.run_id)
            finally:
                with self._lock:
                    if self._queue and self._queue[0] is item:
                        self._queue.pop(0)
                    # 留一份"跑完了什么"供进度卡片回看（只留最近 10 批）
                    self._recent.append(item.steps)
                    del self._recent[:-10]

    def _execute_one(self, item: "_QueuedRun") -> None:
        """执行队列里的**一批**；结束时一定会把状态写清楚。

        【取消令牌的生命周期为什么包在这里】
        每一批都换一个新令牌：上一批被中断的痕迹绝不能带进下一批
        （否则用户停了 A，紧接着的 B 一启动就立刻被自己停掉）。
        ``finally`` 里摘令牌，保证异常路径也不会留下脏状态。
        """
        token = cancellation.install_token()
        context: runner.RunContext | None = None
        acquired = False
        # ★ 2026-10-06：登记"worker 手上有活"，看门狗因此能看见
        # "HTTP 都返回了、只有 worker 卡住"这种卡死（见 _heartbeat 的说明）。
        _note_run_active(1)
        try:
            acquired = self._acquire_gate_interruptible(token)
            if acquired:
                with self._capture():
                    context = runner.prepare(item.request, emit=self._log)
                    report = runner.run_tasks(
                        context,
                        item.request,
                        emit=self._log,
                        on_start=self._on_step_start,
                        on_finish=self._on_step_finish,
                    )
                self._finish(report)
            elif token.requested:
                # 排在这批之前的用户先点了停止 —— 干脆就没轮到它开跑
                self._abort_all(item, "已中止（用户点了停止执行）")
        except cancellation.TaskAborted as exc:
            self._finish_aborted(item, str(exc))
        except runner.UnknownTaskError as exc:  # 理论上已在 start() 拦下，兜底用
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - 见 _worker_loop：绝不让异常逃出线程
            _LOGGER.exception("运行 %s 失败", item.run_id)
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            if context is not None:
                context.close()
            # 放开运行闸门：无论成功、失败还是异常都必须放开，
            # 否则之后谁都跑不了（症状是"点了没反应"，而且没有任何报错）。
            if acquired:
                self._gate.release()
            cancellation.uninstall_token(token)
            _note_run_active(-1)

    def _acquire_gate_interruptible(self, token: cancellation.CancelToken) -> bool:
        """申请闸门，期间可被中断。

        :return: 是否拿到了闸门（``False`` = 拿到了之前用户喊停了）。

        【为什么不能干脆一把 ``acquire()`` 阻塞到底】
        闸门常被只读请求占着（刷新信息面板几秒就回来了）。这时候用户点了停止，
        线程还在傻等锁 —— 停在这一轮的是"等待"，而不是"任务"。
        所以每隔 0.2 秒试一次，顺带看看有没有被喊停。

        【★ 2026-10-06 晚：绝大多数等待**不写日志**，只打心跳】
        最初（当天早些时候）曾在等待 3 秒后往日志面板写一行"正在等待其他请求释放
        运行闸门"。实测这行太吵：正常的只读请求（刷新信息面板等）都在几秒级就回来了，
        用户每点一次执行就可能看到一行莫名其妙的告警，而它并不代表任何问题。

        现在改成**静默等待**，只在以下两处留痕，都不进用户日志面板：

        1. 每次轮询都 ``_heartbeat(...)`` —— 刷新看门狗 ``last_progress``，
           并把 ``_ACTIVITY["last_note"]`` 记成"正在等待运行闸门（已等 N 秒）"。
           真挂死时看门狗的堆栈转储会带上这一行，排障时能立刻区分
           "卡在网络上"与"卡在闸门上"（见 ``webapi/app.py::note_work_progress``）；
        2. 等待**超过 60 秒**才写一行 ``WARNING``，之后每 60 秒续报一次。
           60 秒这个门槛是刻意选的：正常只读请求够不到，一旦够到就说明真的出问题了
           （只读请求占着闸门 + Android 网络超时 30s×N），此时用户有权知道它在等什么。
        这个 60 秒档是**唯一**可能的日志输出；正常使用下日志面板完全看不到等待痕迹。
        """
        waited = 0.0
        #: 下一次该写日志的等待时刻。用**显式阈值**而不是 ``waited % 60 < 0.2``：
        #: 浮点累加 0.2 会让 ``waited`` 落成 180.00000000000003 之类，
        #: 于是 180 秒那次**同一个整点命中两遍**（实测 300 秒内多出 2 行）。
        next_report = _GATE_WAIT_LOG_EVERY_SECONDS
        while not token.requested:
            if self._gate.acquire(blocking=False):
                # 等到闸门本身**不写日志**：用户没看到"在等"，就不该突然看到"等到了"。
                _heartbeat("已获取运行闸门")
                return True
            time.sleep(0.2)
            waited += 0.2
            _heartbeat(f"正在等待运行闸门（已等 {waited:.0f} 秒）")
            # 每满 60 秒留一行（第一行落在 60 秒；之后 120、180…）。
            if waited >= next_report:
                next_report += _GATE_WAIT_LOG_EVERY_SECONDS
                self._log(
                    f"⏳ 已等待运行闸门 {waited:.0f} 秒，"
                    "有其他请求（刷新信息面板等）正在占用，跑完就会自动继续",
                    level="WARNING",
                )
        return False

    def _abort_all(self, item: "_QueuedRun", message: str) -> None:
        """这批压根没开始就中止了：所有步骤标成 skipped 并写日志。"""
        for step in item.steps:
            if step.status in ("pending", "running"):
                step.status = "skipped"
                step.message = message
        with self._lock:
            self._finished_at = time.strftime("%H:%M:%S")
            self._summary = message
        self._log(f"⏹ 运行 {item.run_id} 已中止（未执行）：{message}")

    def _capture(self) -> Any:
        """返回"捕获当前线程 print"的上下文（没装代理时是个空上下文）。"""
        import contextlib

        if self._bridge is None:
            return contextlib.nullcontext()
        return self._bridge.capture_current_thread()

    # ------------------------------------------------------------------
    # 回调与收尾
    # ------------------------------------------------------------------
    def _log(
        self,
        text: str,
        *,
        level: str = logstream.USER_LEVEL,
        logger_name: str = "run",
    ) -> None:
        """把一句话写进日志流。

        :param text: 文本（多行会被拆成多行，与真实终端里的观感一致）。
        :param level: 日志级别；默认 ``USER``（普通文本，前端不着色）。
        :param logger_name: 来源名；默认 ``run``（表示"这是运行流程在说话"）。

        这个方法同时充当 ``services/runner`` 的 ``emit`` 出口 ——
        runner 把"给人看的话"交出来，这里决定它们去哪里。
        """
        for line in str(text).splitlines() or [""]:
            self._stream.append(level, logger_name, line)

    def _on_step_start(self, name: str) -> None:
        """某个任务开始：把该步标成"运行中"（前端据此高亮当前步骤）。"""
        # ★ 2026-10-06：任务边界即心跳 —— 看门狗据此区分"跑得慢"与"真卡死"
        _heartbeat(f"正在执行 {_title_of(name)}")
        with self._lock:
            for step in self._steps:
                if step.name == name and step.status == "pending":
                    step.status = "running"
                    return

    def _on_step_finish(self, result: runner.StepResult) -> None:
        """某个任务结束：写入该步状态，并把**与命令行逐字相同**的结果行记进日志。

        ★ 2026-10-04：顺带完成"每游戏日一次"的**服务端记账**
        （``services/daily_state``）：这是为了让"今天跑过没有"不再依赖浏览器的
        localStorage —— 那份记录换个端口就没了（详见该模块的文档）。

        【为什么放在这里而不是前端】
        服务器才是"账号"这个概念所在的地方；放在这里，无论从哪个窗口 /
        哪个端口提交，同一个角色同一个游戏日都只会自动跑一次。

        【为什么演练模式不记账】
        演练一包不发，如果也记一笔，今天真要领的时候会被判成"已经跑过"
        —— 那是最容易让人以为工具坏了的一种假象。
        """
        with self._lock:
            for step in self._steps:
                if step.name == result.name and step.status in ("pending", "running"):
                    step.status = "ok" if result.ok else "fail"
                    step.message = result.message
                    step.elapsed = result.elapsed
                    step.data = dict(result.data)
                    # ★ 2026-10-06：结束时刻由服务端盖章（前端照抄显示，
                    # 不再用"渲染那一刻"当时间戳 —— 见 StepState.finished_at）。
                    step.finished_at = time.strftime("%H:%M:%S")
                    break
            dry_run = self._dry_run
        self._log(result.line)
        # ★ 2026-10-06：任务结束同样打一次心跳（见 _on_step_start 的说明）
        _heartbeat(f"{_title_of(result.name)} 结束")

        if not dry_run and task_spec.is_once_per_day(result.name):
            try:
                done = daily_state.mark_completed([result.name])
                _LOGGER.info(
                    "记账：%s 本游戏日已完成（共 %d 个：%s）",
                    result.name,
                    len(done),
                    "、".join(done) or "（无）",
                )
            except Exception as exc:  # noqa: BLE001 - 记账失败不该影响运行本身
                _LOGGER.warning("自动任务记账失败（不影响本次运行）：%s", exc)

    def _finish(self, report: runner.RunReport) -> None:
        """正常收尾：写汇总、记下本次花掉的钻石。"""
        with self._lock:
            self._status = "done"
            self._finished_at = time.strftime("%H:%M:%S")
            self._summary = report.summary()
            self._diamond_spent = report.spend_policy.spent_so_far
            spent = self._diamond_spent

        self._log(f"■ 运行结束：{report.summary()}")
        if spent:
            # 花钱是"不可逆"的动作，单独用 WARNING 提醒（前端会标黄/标红）
            self._log(f"⚠ 本次运行共消耗 {spent} 钻石", level="WARNING")

    def _finish_aborted(self, item: "_QueuedRun", reason: str) -> None:
        """**用户中断**后的收尾（2026-10-04 新增）。

        【为什么它不复用 :meth:`_fail`】
        中断**不是失败** —— 任务是好好的，只是用户让它别跑了。
        用 ERROR 级别会把运行日志标红，用户事后回看会以为这一步出了岔子。
        所以这里写 WARNING + 一个"⏹"记号，并把没轮到的步骤标成 skipped。
        """
        remaining = 0
        for step in item.steps:
            if step.status in ("pending", "running"):
                step.status = "skipped"
                step.message = "已中止（用户点了停止执行）"
                remaining += 1
        with self._lock:
            self._finished_at = time.strftime("%H:%M:%S")
            self._summary = f"已被用户中断：{reason}"
        self._log(
            f"⏹ 已中断：{reason}（本批还有 {remaining} 个任务未执行，"
            f"队列中还有 {self.queue_length} 批照常执行）",
            level="WARNING",
        )

    def _fail(self, reason: str) -> None:
        """异常收尾：把状态推到 ``done`` 并记下原因（绝不让前端一直等）。"""
        with self._lock:
            self._status = "done"
            self._finished_at = time.strftime("%H:%M:%S")
            self._error = reason
            if not self._summary:
                self._summary = "运行异常结束"
            for step in self._steps:
                if step.status == "running":
                    step.status = "fail"
                    step.message = step.message or reason

        self._log(f"✘ 运行异常结束：{reason}", level="ERROR")


