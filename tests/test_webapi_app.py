"""``webapi/app.py`` 的接口测试（**真实 HTTP，但全程离线**）。

【为什么用"uvicorn 线程 + requests"而不是 ``fastapi.testclient``】
``TestClient`` 需要额外依赖 ``httpx``（本项目刻意不引入新依赖）。
而"真起一个服务器 + 用 requests 打它"还有额外好处：
测到的是**真实的 HTTP 行为**（状态码、JSON 编码、静态目录），
连"uvicorn 是否正常启动"这件事都顺带验证了。

【离线保证】
所有请求都不出网：
- ``/api/run`` 一律用 ``dry_run=true``（演练模式不发包，见 ``tests/test_main.py`` 的同款断言）；
- 会话状态被 monkeypatch 成"没有"，因此也不依赖本机有没有 ``.session.json``。

运行方式：

    python -m tests.test_webapi_app
    python -m pytest tests/test_webapi_app.py -q
"""

from __future__ import annotations

import io
import json
import re
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

# 离线 HTTP 请求测试依赖
requests = pytest.importorskip("requests")

import config
from models.activity_cache import ActivityCache
from services import cancellation, runner, selection_state, spend_state
from webapi import app as webapp
from webapi import logstream, state


def _free_port() -> int:
    """取一个当前空闲的端口（测试服务器用）。

    【为什么要动态取端口】
    写死 8765 会和"用户自己开着网页版"撞车，也会让并行跑的测试互相抢占。
    让操作系统分配一个空闲端口最省心（这里 deliberately 不关心重用的极小概率）。
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class _Server:
    """把 WebApp 应用跑在后台线程里。

    :param application: 要跑的应用。
    :param port: 监听端口。
    """

    def __init__(self, application: object, port: int) -> None:
        self._server, self._app = webapp.create_server(
            host="127.0.0.1",
            port=port,
            application=application if isinstance(application, webapp.WebApp) else None,
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.base_url = f"http://127.0.0.1:{port}"

    def start(self, *, timeout: float = 30.0) -> None:
        """启动后台服务。"""
        self._thread.start()

    def stop(self) -> None:
        """请求退出并等线程结束。"""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=15)


@contextmanager
def _serve(application: object) -> Iterator[str]:
    """供单个用例临时起一个服务（例如"带令牌"的那组用例）。"""
    server = _Server(application, _free_port())
    server.start()
    try:
        yield server.base_url
    finally:
        server.stop()


@pytest.fixture()
def stream() -> logstream.LogStream:
    """测试自己的日志流（不碰全局状态）。"""
    return logstream.LogStream(capacity=500)


@pytest.fixture()
def bridge(stream: logstream.LogStream) -> Iterator[logstream.ThreadAwareStdout]:
    """安装 stdout 代理（与 ``web_server.py`` 的真实路径一致）。

    未被捕获的线程照旧写**真实控制台**（``sys.__stdout__``）：
    这样代理存在期间不会把 pytest 自己的输出吞掉。
    """
    saved = sys.stdout
    proxy = logstream.install_stdout_bridge(stream, original=sys.__stdout__ or sys.stdout)
    try:
        yield proxy
    finally:
        logstream.uninstall_stdout_bridge(proxy)
        sys.stdout = saved


@pytest.fixture()
def web_dir(tmp_path: Path) -> Path:
    """一个最小的前端目录（让"静态挂载"这件事在测试里可验证）。"""
    target = tmp_path / "web"
    target.mkdir()
    (target / "index.html").write_text("<!doctype html><title>测试页</title>", encoding="utf-8")
    return target


@pytest.fixture()
def base_url(
    monkeypatch: pytest.MonkeyPatch,
    stream: logstream.LogStream,
    bridge: logstream.ThreadAwareStdout,
    web_dir: Path,
    tmp_path: Path,
) -> Iterator[str]:
    """不带令牌的应用（等价于默认的"只服务本机"）。"""
    # 会话状态固定成"没有"：测试与这台机器上的 .session.json 无关
    monkeypatch.setattr(runner, "resolve_session_state", lambda: (None, "（无）"))
    # 活动区域缓存指到临时目录：**绝不碰工程根的真实 .activity_cache.json** ——
    # 否则测试会读到你本机的真实缓存，结果取决于"你这台机器上有没有数据"。
    monkeypatch.setattr(config, "ACTIVITY_CACHE_FILE", tmp_path / ".activity_cache.json")
    # 钻石总闸同理（2026-10-05）：POST /api/spend 会写盘，必须指到临时目录。
    monkeypatch.setattr(config, "SPEND_STATE_FILE", tmp_path / ".spend_state.json")
    # 任务勾选/参数同理（2026-10-05）：POST /api/selection 会写盘，同上。
    monkeypatch.setattr(config, "SELECTION_STATE_FILE", tmp_path / ".selection_state.json")
    application = webapp.create_app(stream=stream, bridge=bridge, web_dir=web_dir)
    with _serve(application) as url:
        yield url


# ---------------------------------------------------------------------------
# 轮询小工具
# ---------------------------------------------------------------------------
def _get(base_url: str, path: str, **params: object) -> requests.Response:
    """GET 一个接口（超时 10 秒，避免测试挂死）。"""
    return requests.get(f"{base_url}{path}", params=params or None, timeout=10)


def _wait_for_log_line(
    base_url: str, needle: str, *, timeout: float = 60.0
) -> list[str]:
    """轮询 ``/api/logs`` 直到出现含 ``needle`` 的行；返回收到过的所有消息。

    【为什么要轮询而不是 sleep 固定时间】
    固定 sleep 在慢机器上会偶发失败、在快机器上白等。轮询 + 上限最稳。
    """
    deadline = time.time() + timeout
    since = 0
    seen: list[str] = []
    while time.time() < deadline:
        payload = _get(base_url, "/api/logs", since=since).json()
        since = int(payload["next_seq"])
        seen.extend(line["message"] for line in payload["lines"])
        if any(needle in message for message in seen):
            return seen
        time.sleep(0.1)
    raise AssertionError(f"等待日志 {needle!r} 超时；最近收到：{seen[-20:]}")


def _wait_for_run_to_finish(base_url: str, *, timeout: float = 60.0) -> dict:
    """轮询 ``/api/status`` 直到运行结束；返回最终状态。"""
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        payload = _get(base_url, "/api/status").json()
        if payload.get("status") != "running":
            return payload
        time.sleep(0.1)
    raise AssertionError(f"等待运行结束超时；当前状态：{payload}")


# ---------------------------------------------------------------------------
# ① 只读接口
# ---------------------------------------------------------------------------
class TestReadOnlyEndpoints:
    def test_health(self, base_url: str) -> None:
        payload = _get(base_url, "/api/health").json()
        assert payload["ok"] is True
        assert payload["app_version"] == webapp.APP_VERSION
        assert payload["auth_required"] is False
        assert payload["session"]["has_session"] is False
        assert payload["running"] is False
        assert payload["run"]["status"] == "idle"

    def test_tasks_cover_all_groups_and_registered_tasks(self, base_url: str) -> None:
        payload = _get(base_url, "/api/tasks").json()

        group_keys = [group["key"] for group in payload["groups"]]
        # 分组按"风险"划分：自动（无消耗）/ 主动（有消耗或需参数）/ 待实现
        assert group_keys == ["auto", "manual", "planned"]

        tasks = {task["name"]: task for task in payload["tasks"]}
        # 已注册的任务必须**全部**出现在清单里（缺一个，网页上就少一个可选项）
        for name in runner.load_builtin_tasks():
            assert name in tasks, f"任务 {name} 没有出现在 /api/tasks 里"

        # 2026-09-24：原来的「每日任务流」(daily) 已删除 —— 它只是一层壳，
        # "每日四件套"现在由四个独立的零消耗任务承担（全部默认勾选）。
        # login_pass 已于 2026-10-03 按用户决策删除（逐天领取被服务端拒 -3，功能鸡肋）
        for name in ("mail", "free_gift", "daily_box"):
            assert tasks[name]["default_checked"] is True, f"{name} 应该默认勾选"
            assert tasks[name]["enabled"] is True
            assert tasks[name]["requires_auth"] is True
        # 只读数据类任务不再出现在任务清单里（改由「信息展示」面板展示；
        # 位置见规格表里的 `hidden=True`，与 login / handshake 同一机制）
        for name in ("wallet", "roles", "arena_status", "wudou_status", "yimo_status"):
            assert tasks[name]["hidden"] is True, f"{name} 应该隐藏（只在信息展示里出现）"
        assert tasks["login"]["enabled"] is True  # 曾经在这里踩过坑：被标成"未实现"
        assert tasks["login"]["hidden"] is True  # 登录交给专用界面，不在清单里显示
        assert tasks["handshake"]["hidden"] is True  # 握手是每个任务的自动前置，不该让人勾
        assert tasks["wallet"]["group"] == "auto"  # 只读钱包属于"自动任务"
        assert tasks["sweep_activity"]["group"] == "manual"
        assert tasks["sweep_activity"]["params"], "扫荡任务必须带参数 schema"
        # ★ 2026-10-06：sign_in 占位已删 —— 登录签到是内置钩子（tasks/login_signin.py），
        # 不进任务清单（否则前端会多出一张无法执行、也无开关的"每日签到"卡片）。
        assert "sign_in" not in tasks
        assert payload["suggested_dry_run"] is False
        assert "summary" in payload["spend"]

        # ★ 游戏日（2026-09-24）：网页的"每日任务自动执行"靠它判重。
        # 换日时刻是**游戏服务器的每天 05:00**（不是 00:00，也不是浏览器时区），
        # 口径只在 ``models/daily_reset.py`` 一处定义；接口如实端出来，前端不自己算。
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload["game_day"])
        assert payload["next_daily_reset"].endswith(f"{config.DAILY_RESET_HOUR:02d}:00")

        # ★ 活跃宝箱必须排在自动任务清单的**最后**（2026-09-24 用户要求）：
        # 网页是按这个顺序提交的，宝箱排在前面就会拿着"昨天的活跃点"去领
        # （刚过 05:00 打开页面时最明显 —— 服务端拒绝，用户以为宝箱坏了）。
        auto_names = [task["name"] for task in payload["tasks"] if task["group"] == "auto"]
        assert auto_names[-1] == "daily_box"
        # 它还是唯一"一天可以领多次"的自动任务：网页在每个主动任务执行后自动补跑
        # （见 web/assets/app.js 的 REPEATABLE_AUTO），所以默认勾上。
        assert tasks["daily_box"]["default_checked"] is True

    def test_accounts_returns_a_list(self, base_url: str) -> None:
        payload = _get(base_url, "/api/accounts").json()
        assert isinstance(payload["accounts"], list)
        assert isinstance(payload.get("zone_accounts"), list)
        assert isinstance(payload.get("platform_accounts"), list)
        for item in payload["accounts"]:
            assert set(item) >= {"index", "account", "user_id", "summary"}

    def test_static_index_is_served(self, base_url: str) -> None:
        response = requests.get(f"{base_url}/", timeout=10)
        assert response.status_code == 200
        assert "测试页" in response.text

    def test_docs_are_mounted_under_api(self, base_url: str) -> None:
        """交互式文档留在 /api/docs（手动试接口时有用）。"""
        assert requests.get(f"{base_url}/api/docs", timeout=10).status_code == 200


# ---------------------------------------------------------------------------
# ② 日志接口的游标语义
# ---------------------------------------------------------------------------
class TestLogCursor:
    def test_limit_does_not_skip_lines(
        self, base_url: str, stream: logstream.LogStream
    ) -> None:
        """``next_seq`` 必须停在"已发出的最后一行"，否则中间的行永远看不到。"""
        stream.append("INFO", "tasks", "第一行")
        stream.append("INFO", "tasks", "第二行")
        stream.append("INFO", "tasks", "第三行")

        first = _get(base_url, "/api/logs", since=0, limit=1).json()
        assert [line["message"] for line in first["lines"]] == ["第一行"]
        assert first["next_seq"] == first["lines"][-1]["seq"]

        second = _get(base_url, "/api/logs", since=first["next_seq"], limit=1).json()
        assert [line["message"] for line in second["lines"]] == ["第二行"]

        third = _get(base_url, "/api/logs", since=second["next_seq"]).json()
        assert [line["message"] for line in third["lines"]] == ["第三行"]
        assert third["stream_seq"] >= third["next_seq"]

    def test_no_new_lines_keeps_cursor(self, base_url: str) -> None:
        payload = _get(base_url, "/api/logs", since=42).json()
        assert payload["lines"] == []
        assert payload["next_seq"] == 42


# ---------------------------------------------------------------------------
# ③ 运行接口（全程离线：dry_run）
# ---------------------------------------------------------------------------
class TestRunFlow:
    def test_dry_run_through_http(self, base_url: str) -> None:
        """完整走一遍：POST /api/run → 轮询日志 → 轮询状态。

        这是"网页上一键运行"的最小可信证据：演练模式下必须能看到「未发送」
        （print 通道），同时 ``tasks`` 的日志（logging 通道）也要在，
        最后状态必须是 ``done`` 且那一步是 ok。
        """
        response = requests.post(
            f"{base_url}/api/run",
            json={"dry_run": True, "tasks": [{"name": "handshake", "params": {}}]},
            timeout=10,
        )
        assert response.status_code == 202
        assert response.json()["status"] == "running"

        messages = _wait_for_log_line(base_url, "未发送")
        assert any("handshake" in message for message in messages)

        final = _wait_for_run_to_finish(base_url)
        assert final["status"] == "done"
        assert final["error"] == ""
        assert [step["status"] for step in final["steps"]] == ["ok"]
        assert final["steps"][0]["title"] == "网关握手"
        assert final["dry_run"] is True
        assert "1/1 步成功" in final["summary"]

    def test_empty_task_list_is_rejected(self, base_url: str) -> None:
        response = requests.post(f"{base_url}/api/run", json={"tasks": []}, timeout=10)
        assert response.status_code == 400
        assert "没有选择任何任务" in response.json()["detail"]["problems"]

    def test_unknown_task_is_rejected_before_running(self, base_url: str) -> None:
        response = requests.post(
            f"{base_url}/api/run",
            json={"tasks": [{"name": "no-such-task", "params": {}}]},
            timeout=10,
        )
        assert response.status_code == 400
        problems = response.json()["detail"]["problems"]
        assert any("未找到任务" in problem for problem in problems)

    def test_bad_param_type_is_rejected(self, base_url: str) -> None:
        response = requests.post(
            f"{base_url}/api/run",
            json={"tasks": [{"name": "sweep_activity", "params": {"times": "abc"}}]},
            timeout=10,
        )
        assert response.status_code == 400
        problems = response.json()["detail"]["problems"]
        assert any("times" in problem for problem in problems)

    def test_stop_without_run_is_a_noop(self, base_url: str) -> None:
        """没有运行时点停止：不是错误，只是"没什么可停的"（HTTP 200）。"""
        response = requests.post(f"{base_url}/api/stop", timeout=10)
        assert response.status_code == 200
        assert response.json()["stopped"] is False

    def test_second_run_is_queued_not_rejected(
        self, base_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 2026-10-04：运行中再提交**排队**，而不是 409（用户需求）。

        【为什么这条判据变了，而"互斥"的要求没变】
        排队不等于并发：worker 只有一个线程，且每批开跑前照样抢运行闸门。
        所以"凭据环境变量 / 消费额度互相踩"这两条老顾虑依旧不成立 ——
        这里额外断言"两批都真的跑了、且是串行跑完的"。
        """
        original = runner.run_tasks
        overlap: list[int] = []
        running_now = [0]

        def slow_run_tasks(context: object, request: object, **kwargs: object) -> object:
            # 只为把"运行中"这个状态维持住 0.8 秒，好让第二次请求撞上它
            running_now[0] += 1
            overlap.append(running_now[0])
            time.sleep(0.8)
            try:
                return original(context, request, **kwargs)  # type: ignore[arg-type]
            finally:
                running_now[0] -= 1

        monkeypatch.setattr(runner, "run_tasks", slow_run_tasks)

        first = requests.post(
            f"{base_url}/api/run",
            json={"dry_run": True, "tasks": [{"name": "handshake", "params": {}}]},
            timeout=10,
        )
        assert first.status_code == 202

        second = requests.post(
            f"{base_url}/api/run",
            json={"dry_run": True, "tasks": [{"name": "handshake", "params": {}}]},
            timeout=10,
        )
        # 不再 409：返回 202 + "排在 1 批之后"
        assert second.status_code == 202
        assert second.json()["status"] == "queued"
        assert second.json()["queued_behind"] >= 1

        _wait_for_run_to_finish(base_url)
        # 两批都真的跑了，而且从没并发（峰值始终 ≤ 1）
        assert overlap == [1, 1]


# ---------------------------------------------------------------------------
# ③″ 闸门等待：静默 + 60 秒兜底（★ 2026-10-06 用户要求）
# ---------------------------------------------------------------------------
class TestGateWaitIsQuiet:
    """worker 在闸门外等待时**不该**刷日志面板。

    【背景（改动前必读）】
    早先的实现在等待 3 秒后就写一行 ``⏳ 正在等待其他请求释放运行闸门…``，
    用户反馈太吵 —— 正常的只读请求几秒就回来了，每点一次执行都可能看到一行
    莫名其妙的告警。现在改成：**只在等待满 60 秒时才写一行**（之后每 60 秒续报），
    其余时间只打心跳；心跳只更新看门狗状态，**不进用户日志流**。

    【为什么必须在这里钉死】
    这条行为没有别的回归网：那行文本在实现里只出现一次，
    以后有人"顺手加回一行提示"不会有任何测试变红。

    【怎么做到不睡觉】
    把 ``state.time.sleep`` 换成一个**纯计数替身**（不耗时），于是 0.2 秒的逻辑
    步长瞬间跑完；替身在第 N 次被调用时释放闸门，就能精确停在想要的等待时长上
    （``waited`` 每轮 +0.2，所以 N 次调用 = 逻辑等待 ``N * 0.2`` 秒）。
    """

    @staticmethod
    def _state(stream: logstream.LogStream) -> state.RunManager:
        return state.RunManager(stream=stream)

    @staticmethod
    def _drive(
        run_state: state.RunManager,
        *,
        ticks: int,
        token: cancellation.CancelToken | None = None,
    ) -> tuple[bool, list[str]]:
        """占用闸门后驱动一次 ``_acquire_gate_interruptible``。

        :param ticks: 逻辑轮询次数。替身在第 ``ticks`` 次被调用时放闸，
            因此该次之后循环再走一轮就会拿到闸门 —— 逻辑等待时长约为
            ``ticks * 0.2`` 秒；要落在某个整点上就多给 1（如 60 秒用 301）。
        :param token: 取消令牌；``None`` 表示新建一个（不会被喊停）。
        :return: ``(是否拿到闸门, 心跳记录列表)``。
        """
        assert run_state._gate.acquire(blocking=False)  # noqa: SLF001 - 模拟他人占闸
        token = token or cancellation.CancelToken()
        heartbeats: list[str] = []
        calls = {"n": 0}

        def fake_sleep(_seconds: float) -> None:
            calls["n"] += 1
            # 跑到第 ticks 次就放闸，让它正常收尾（返回 True）
            if calls["n"] >= ticks:
                run_state._gate.release()  # noqa: SLF001

        with mock.patch.object(state.time, "sleep", side_effect=fake_sleep), mock.patch.object(
            state, "_heartbeat", side_effect=heartbeats.append
        ):
            acquired = run_state._acquire_gate_interruptible(token)  # noqa: SLF001
        return acquired, heartbeats

    def test_below_threshold_stays_silent(self, stream: logstream.LogStream) -> None:
        """等待 10 秒（远不到 60 秒）：日志流**一行都不能有**，但心跳必须照打。"""
        run_state = self._state(stream)
        acquired, heartbeats = self._drive(run_state, ticks=50)  # 50 * 0.2 = 10 秒

        assert acquired is True
        assert [line.message for line in stream.snapshot()] == []
        assert heartbeats, "等待期间必须打心跳（看门狗靠它区分'在等'与'卡死'）"
        assert any("运行闸门" in note for note in heartbeats)

    def test_past_threshold_writes_exactly_one_line(
        self, stream: logstream.LogStream
    ) -> None:
        """等待 80 秒：恰好跨过一个 60 秒整点 → 只写一行。"""
        run_state = self._state(stream)
        acquired, _ = self._drive(run_state, ticks=400)  # 约 80 秒

        assert acquired is True
        messages = [line.message for line in stream.snapshot()]
        assert len(messages) == 1, f"80 秒只该写一行，实际：{messages}"
        assert "运行闸门" in messages[0] and "60" in messages[0]

    def test_three_minutes_writes_three_lines(self, stream: logstream.LogStream) -> None:
        """等待到 180 秒（含）：写 3 行（60/120/180），**不是**同一整点命中两遍。

        【为什么要专门断言"不重复"】
        早先的实现用 ``waited % 60 < 0.2`` 判据，浮点累加会把 180 秒落成
        180.00000000000003，导致同一整点连续命中两次（实测 300 秒内多出 2 行）。
        现在改成显式 ``next_report`` 阈值，这里把行数钉死。

        ``ticks=901`` 而不是 ``900``：替身在第 900 次放闸，第 900 次循环的
        ``waited`` 正好是 180.0 但那一轮已经不再检查日志 —— 多给 1 次才能让
        180 秒这一行落到日志里。
        """
        run_state = self._state(stream)
        acquired, _ = self._drive(run_state, ticks=901)

        assert acquired is True
        messages = [line.message for line in stream.snapshot()]
        assert len(messages) == 3, f"180 秒该写 3 行，实际 {len(messages)} 行：{messages}"
        assert [m.split("已等待运行闸门 ")[1].split(" 秒")[0] for m in messages] == [
            "60",
            "120",
            "180",
        ]

    def test_cancel_during_wait_is_observed(self, stream: logstream.LogStream) -> None:
        """等待期间被喊停：立刻返回 ``False``，且不写任何日志。"""
        run_state = self._state(stream)
        token = cancellation.CancelToken()
        token.request()

        acquired, heartbeats = self._drive(run_state, ticks=50, token=token)

        assert acquired is False
        assert [line.message for line in stream.snapshot()] == []
        assert heartbeats == [], "已经喊停了就不该再打心跳"


# ---------------------------------------------------------------------------
# ③′ 钻石总闸：保存接口 + start_run 合并语义（2026-10-05）
# ---------------------------------------------------------------------------
class TestSpendGate:
    """总闸的服务端持久化与运行时合并。

    【合并语义（唯一要背下来的规则）】
    - ``allow_diamond_synced=True``：前端已把服务端保存值还原进勾选框，
      ``allow_diamond`` 就是用户此刻的明确选择（含刚关闸）→ 照单全收；
    - ``allow_diamond_synced=False``（旧 JS / 还原失败）：
      ``allow_diamond=False`` 不足为信 → 用「已保存值 或 请求值」合并，
      保住"开过就保持开"。简单 OR 会把"刚关掉的闸"在竞态下又弹开，
      所以必须靠这个字段区分两种来源。
    """

    @pytest.fixture()
    def spend_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """总闸状态文件指到临时目录（某些用例不经 base_url 起服务）。"""
        target = tmp_path / ".spend_state.json"
        monkeypatch.setattr(config, "SPEND_STATE_FILE", target)
        return target

    @staticmethod
    def _capture_run_allow_diamond(monkeypatch: pytest.MonkeyPatch) -> dict:
        """把 /api/run 真正拿到的 ``request.allow_diamond`` 抓出来（不真跑任务）。"""
        captured: dict = {}

        def fake_run_tasks(
            context: object, request: runner.RunRequest, **kwargs: object
        ) -> runner.RunReport:
            captured["allow_diamond"] = request.allow_diamond
            return runner.RunReport(steps=[])

        monkeypatch.setattr(runner, "run_tasks", fake_run_tasks)
        return captured

    def test_post_spend_round_trips(self, base_url: str, spend_file: Path) -> None:
        """POST 保存 → /api/tasks 回传 → 文件确实是合法 JSON。"""
        response = requests.post(
            f"{base_url}/api/spend", json={"allow_diamond": True}, timeout=10
        )
        assert response.status_code == 200
        body = response.json()
        assert body["allow_diamond"] is True
        assert body["updated_at"]

        payload = _get(base_url, "/api/tasks").json()
        assert payload["spend"]["saved_allow_diamond"] is True
        assert json.loads(spend_file.read_text(encoding="utf-8"))["allow_diamond"] is True

        # 关回去也生效（"关了就关了"）
        requests.post(
            f"{base_url}/api/spend", json={"allow_diamond": False}, timeout=10
        )
        payload = _get(base_url, "/api/tasks").json()
        assert payload["spend"]["saved_allow_diamond"] is False

    def test_post_spend_rejects_non_bool(self, base_url: str) -> None:
        """看不懂的值必须 422（pydantic 宽松模式接受 "yes"/"1"，但不接受乱码）。"""
        response = requests.post(
            f"{base_url}/api/spend", json={"allow_diamond": "maybe"}, timeout=10
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        ("saved", "sent", "synced", "expected"),
        [
            # 前端已同步：用户的明确选择照单全收 —— 刚关闸立即跑也必须关
            (True, False, True, False),
            (False, True, True, True),
            # 前端未同步（旧 JS / 还原失败）：保住"开过就保持开"
            (True, False, False, True),
            (False, True, False, True),
        ],
    )
    def test_run_merges_saved_gate(
        self,
        base_url: str,
        spend_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        saved: bool,
        sent: bool,
        synced: bool,
        expected: bool,
    ) -> None:
        captured = self._capture_run_allow_diamond(monkeypatch)
        spend_state.set_allow_diamond(saved)

        response = requests.post(
            f"{base_url}/api/run",
            json={
                "dry_run": True,
                "allow_diamond": sent,
                "allow_diamond_synced": synced,
                "tasks": [{"name": "handshake", "params": {}}],
            },
            timeout=10,
        )
        assert response.status_code == 202
        _wait_for_run_to_finish(base_url)
        assert captured["allow_diamond"] is expected


# ---------------------------------------------------------------------------
# ③ selection：任务勾选/参数的服务端持久化（2026-10-05）
# ---------------------------------------------------------------------------
class TestSelection:
    """任务勾选/参数的保存与回传。

    【与总闸的语义差异】selection 不参与运行决策（运行请求自带完整
    tasks/params），所以没有 synced 合并标志 —— 存坏/存旧顶多回默认。
    服务端唯一要守的是**形状**：未知任务名/参数键在写盘前被白名单丢弃。
    """

    def test_post_selection_round_trips(self, base_url: str, tmp_path: Path) -> None:
        """POST 保存 → /api/tasks 回传 → 文件确实是合法 JSON。"""
        tasks = {
            "sweep_material": {"checked": True, "params": {"times": 5}},
            "push_main_stage": {"checked": False, "params": {"max_stages": 3}},
        }
        response = requests.post(
            f"{base_url}/api/selection",
            json={"tasks": tasks, "dry_run": True},
            timeout=10,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["saved_tasks"] == tasks
        assert body["saved_dry_run"] is True
        assert body["updated_at"]

        payload = _get(base_url, "/api/tasks").json()
        selection = payload["selection"]
        assert selection["saved_tasks"] == tasks
        assert selection["saved_dry_run"] is True
        assert selection["saved_updated_at"]  # 有值 = "存过"，前端据此以服务端为权威

        state_file = tmp_path / ".selection_state.json"
        assert json.loads(state_file.read_text(encoding="utf-8"))["tasks"] == tasks

    def test_tasks_reports_never_saved(self, base_url: str) -> None:
        """从未保存：saved_updated_at 为空串（前端保留 localStorage 兜底）。"""
        payload = _get(base_url, "/api/tasks").json()
        selection = payload["selection"]
        assert selection["saved_updated_at"] == ""
        assert selection["saved_tasks"] == {}
        assert selection["saved_dry_run"] is None

    def test_post_filters_unknown_task_and_param(self, base_url: str) -> None:
        """白名单：未知任务名整条丢弃、未知参数键丢弃，已知原样保留。"""
        response = requests.post(
            f"{base_url}/api/selection",
            json={
                "tasks": {
                    "mail": {"checked": True, "params": {}},
                    "no_such_task": {"checked": True, "params": {}},
                    "sweep_material": {
                        "checked": True,
                        "params": {"times": 3, "tims": 9},
                    },
                },
                "dry_run": False,
            },
            timeout=10,
        )
        assert response.status_code == 200
        saved = response.json()["saved_tasks"]
        assert set(saved) == {"mail", "sweep_material"}
        assert saved["sweep_material"]["params"] == {"times": 3}

    def test_post_selection_rejects_non_bool_dry_run(self, base_url: str) -> None:
        """看不懂的 dry_run 必须 422（pydantic 宽松模式接受 "yes"，不接受乱码）。"""
        response = requests.post(
            f"{base_url}/api/selection",
            json={"tasks": {}, "dry_run": "maybe"},
            timeout=10,
        )
        assert response.status_code == 422

    def test_state_module_reads_same_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """不经 HTTP 的直接调用也必须落到同一份配置路径（webapi 与模块的接线）。"""
        target = tmp_path / "direct.json"
        monkeypatch.setattr(config, "SELECTION_STATE_FILE", target)
        selection_state.save_selection(
            {"mail": {"checked": True, "params": {}}}, dry_run=False
        )
        assert target.is_file()
        assert selection_state.saved_tasks() == {"mail": {"checked": True, "params": {}}}


# ---------------------------------------------------------------------------
# ④ 接线：日志流与 stdout 代理必须共用同一个缓冲
# ---------------------------------------------------------------------------
class TestWiring:
    def test_stream_is_derived_from_bridge_when_omitted(self, web_dir: Path) -> None:
        """只传 ``bridge`` 不传 ``stream`` 时，应用内部必须与代理共用**同一个**缓冲。

        （真实踩过的坑：``web_server.py`` 一开始只传了 bridge，结果
        ``print`` 输出进了代理自己的缓冲、状态与说明进了另一个缓冲 ——
        网页日志窗里少了一半内容，而且不报任何错。）
        """
        proxy_stream = logstream.LogStream(capacity=200)
        proxy = logstream.ThreadAwareStdout(proxy_stream, io.StringIO())
        application = webapp.create_app(bridge=proxy, web_dir=web_dir)

        with _serve(application) as url:
            requests.post(
                f"{url}/api/run",
                json={"dry_run": True, "tasks": [{"name": "handshake", "params": {}}]},
                timeout=10,
            )
            _wait_for_run_to_finish(url)
            health = _get(url, "/api/health").json()

        assert proxy_stream.next_seq > 1, "状态/说明没有写进代理的缓冲"
        assert health["next_seq"] == proxy_stream.next_seq, "两个缓冲不是同一个"


# ---------------------------------------------------------------------------
# ⑤ 访问令牌（暴露到局域网时的唯一防线）
# ---------------------------------------------------------------------------
class TestToken:
    #: 令牌用**纯 ASCII**：浏览器对非 ASCII 的请求头会直接抛错
    #: （所以 web_server.py 的 --token 帮助里也建议用字母数字）。
    TOKEN = "secret-token-1"

    def test_api_requires_header_token(self, web_dir: Path) -> None:
        with _serve(webapp.create_app(token=self.TOKEN, web_dir=web_dir)) as url:
            assert requests.get(f"{url}/api/health", timeout=10).status_code == 401

            wrong = requests.get(
                f"{url}/api/health",
                headers={"X-Auth-Token": "guessed"},
                timeout=10,
            )
            assert wrong.status_code == 401

            ok = requests.get(
                f"{url}/api/health",
                headers={"X-Auth-Token": self.TOKEN},
                timeout=10,
            )
            assert ok.status_code == 200
            assert ok.json()["auth_required"] is True

    def test_query_token_is_accepted(self, web_dir: Path) -> None:
        """``?token=…`` 也接受：手机端直接点链接进来最省事。

        这里刻意用**中文令牌**验证查询参数路径 —— 中文走不了请求头（浏览器限制），
        但走 URL 参数没有问题，这条就是给那种场景兜底的。
        """
        with _serve(webapp.create_app(token="中文口令", web_dir=web_dir)) as url:
            assert _get(url, "/api/health", token="中文口令").status_code == 200
            assert _get(url, "/api/health", token="错的").status_code == 401

    def test_static_page_stays_public(self, web_dir: Path) -> None:
        """页面本身不校验令牌 —— 否则用户在页面上连输入令牌的机会都没有。

        页面只是一层壳，数据与操作都在 ``/api/*``（那些接口全部要令牌）。
        """
        with _serve(webapp.create_app(token=self.TOKEN, web_dir=web_dir)) as url:
            assert requests.get(f"{url}/", timeout=10).status_code == 200


# ---------------------------------------------------------------------------
# ⑥ 登录 / 选区两阶段 + 信息面板（用假的任务结果，**不联网**）
# ---------------------------------------------------------------------------
class TestLoginFlowEndpoints:
    @staticmethod
    def _fake_report(*, ok: bool = True, data: dict | None = None, message: str = "ok"):
        step = runner.StepResult(name="login", ok=ok, message=message, data=data or {})
        return runner.RunReport(steps=[step])

    def test_servers_endpoint_asks_for_list_only(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第一段必须带 ``plan_only``（否则会真的进区、写会话）。"""
        captured: dict = {}
        fake_report = self._fake_report  # 先取出：monkeypatch 后 self 会是 RunManager

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            captured["names"] = [task.name for task in request.tasks]
            captured["params"] = [dict(task.params) for task in request.tasks]
            captured["account"] = request.account
            return fake_report(
                data={
                    "servers": [
                        {
                            "id": 113,
                            "name": "伊莉莎白一世",
                            "label": "#113 伊莉莎白一世（组 -1）",
                            "has_character": True,
                            "group_id": -1,
                            "people_count": 12345,
                        }
                    ],
                    "suggested_server_id": 113,
                    "account_id": 10000004,
                    "user_id": "ER00000000-0000-0000-0000-000000000000",
                }
            )

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            payload = requests.post(
                f"{url}/api/login/servers", json={"account": "2"}, timeout=10
            ).json()

        assert captured["names"] == ["login"]
        assert captured["params"][0]["plan_only"] is True
        assert captured["account"] == "2"
        assert payload["servers"][0]["id"] == 113
        # 平台 userId 必须打码（页面内容经常被截图、贴群）
        assert payload["user_id"].endswith("(38 字符)")
        assert "1aac" not in payload["user_id"]

    def test_enter_endpoint_passes_server_id_and_returns_player(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}
        fake_report = self._fake_report  # 见上一条用例的说明

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            captured["params"] = [dict(task.params) for task in request.tasks]
            return fake_report(
                message="登录成功：playerId=10000003 @ 伊莉莎白一世(#113)",
                data={
                    "player_name": "真昼",
                    "fighting_force": 2518906,
                    "login_days": 241,
                    "server_id": 113,
                },
            )

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            payload = requests.post(
                f"{url}/api/login/enter",
                json={"account": "2", "server_id": 113},
                timeout=10,
            ).json()

        assert captured["params"][0]["server_id"] == 113
        assert not captured["params"][0].get("plan_only")
        assert payload["player"]["name"] == "真昼"
        assert payload["player"]["fighting_force"] == 2518906
        assert payload["player"]["login_days"] == 241

    def test_player_endpoint_merges_and_reports_partial_failure(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一个只读任务失败不影响其它：能显示多少显示多少。

        ★ 2026-09-24：这个接口从 2 个只读任务扩到 **5 个**（货币 / 竞技场 /
        天命对决 / 失控炼成阵 / 荣耀之巅只读）；
        ★ 2026-10-05：再 +1 —— **角色名册（``roles``）**，「角色与资源」面板的
        「我的角色」分组靠它。共 **6 个**。这里一并守住四件事：

        1. 请求里的**任务清单与顺序** —— 少一个就是界面上少一张卡片；
        2. 荣耀之巅的 ``battles`` 必须是 **0**（显式只读）—— 否则会**真开打**；
           它现在是该任务的参数（界面上叫「荣耀之巅 → 打几场」，不再是
           「运行选项 → 进阶」），界面上填了什么都不能被这个接口沿用；
        3. 失败的只读任务只进 ``errors``，其余字段照常返回；
        4. ``roles`` 的返回体要带上 ``source``（实时名册 / 登录快照）——
           界面据此提示"这份数据是什么时候的"，不写清楚会被当成实时数据。
        """
        captured: dict = {}

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            captured["names"] = [task.name for task in request.tasks]
            # ★ 2026-09-25：场数不再是运行级字段，而是 top_pvp_battle 的任务参数
            captured["top_pvp_params"] = dict(
                next(
                    (
                        task.params
                        for task in request.tasks
                        if task.name == "top_pvp_battle"
                    ),
                    {},
                )
            )
            return runner.RunReport(
                steps=[
                    runner.StepResult(
                        name="wallet",
                        ok=True,
                        message="ok",
                        data={
                            "coins": 37193710,
                            "diamonds": 3426,
                            "stamina": 220,
                            "item_count": 823,
                        },
                    ),
                    runner.StepResult(
                        name="roles",
                        ok=True,
                        message="角色 2 个（来源：实时名册（5012））",
                        data={
                            "source": "live",
                            "fallback_reason": "",
                            "role_count": 2,
                            "roles": (
                                {
                                    "role_sid": 155242087,
                                    "role_id": 130003000,
                                    "role_info_id": 133003000,
                                    "level": 50,
                                    "star": 5,
                                    "name": "庫爾蒂絲",
                                },
                                {
                                    "role_sid": 223860075,
                                    "role_id": 130007400,
                                    "role_info_id": 133007400,
                                    "level": 42,
                                    "star": 4,
                                    "name": "將臣",
                                },
                            ),
                        },
                    ),
                    runner.StepResult(name="arena_status", ok=False, message="没有登录态"),
                    runner.StepResult(
                        name="wudou_status",
                        ok=True,
                        message="ok",
                        data={"challenge_times": 3, "npc_times": 1, "my_ranking": 42},
                    ),
                    runner.StepResult(
                        name="yimo_status",
                        ok=True,
                        message="ok",
                        data={"remain_count": 2, "now_score": 880, "now_rank": 15},
                    ),
                    runner.StepResult(
                        name="top_pvp_battle",
                        ok=True,
                        message="只读完成（未发起任何挑战）",
                        data={"mode": "read_only", "my_ranking": 7, "my_points": 1234},
                    ),
                ]
            )

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            # ① 默认快照**不含**荣耀之巅（2026-10-04：它只在登录统一读取与
            #    真正操作过该任务之后才读，免得"每跑一个任务都读一遍"）
            _get(url, "/api/player").json()
            assert captured["names"] == [
                "wallet",
                "roles",
                "arena_status",
                "wudou_status",
                "yimo_status",
            ]

            # ② ``full=1`` 才是"连荣耀之巅一起读"的那份
            payload = _get(url, "/api/player", full=1).json()

        # ③ 任务清单与顺序（多一个少一个一眼可见）
        assert captured["names"] == [
            "wallet",
            "roles",
            "arena_status",
            "wudou_status",
            "yimo_status",
            "top_pvp_battle",
        ]
        # ② 只读保证：荣耀之巅的场数被**显式**钉成 0
        #    （不是"不传、靠 config 默认值"—— 那个默认值可以被环境变量改掉，
        #     改掉之后"刷新一下信息面板"就变成真开打了）
        assert captured["top_pvp_params"]["battles"] == 0
        assert captured["top_pvp_params"]["interval"] == 0
        # ③ 数据与逐项容错
        assert payload["game"]["coins"] == 37193710
        assert payload["game"]["diamonds"] == 3426
        assert payload["arena"]["challenge_count"] is None  # 这个任务失败了 → 显示 "—"
        assert payload["wudou"]["challenge_times"] == 3
        assert payload["wudou"]["my_ranking"] == 42
        assert payload["yimo"]["remain_count"] == 2
        assert payload["yimo"]["now_score"] == 880
        assert payload["top_pvp"]["mode"] == "read_only"
        assert payload["top_pvp"]["my_ranking"] == 7
        assert payload["top_pvp"]["my_points"] == 1234
        # ④ 角色：条数、来源标记、以及逐条的展示字段都要透出去
        assert payload["roles"]["count"] == 2
        assert payload["roles"]["source"] == "live"
        assert [item["name"] for item in payload["roles"]["items"]] == ["庫爾蒂絲", "將臣"]
        assert payload["roles"]["items"][0]["role_sid"] == 155242087
        assert payload["errors"] and "arena_status" in payload["errors"][0]

    def test_main_stage_status_endpoint(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """测试 /api/main_stage/status 接口返回主线最高通关进度。"""

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            assert len(request.tasks) == 1
            assert request.tasks[0].name == "main_stage_status"
            return runner.RunReport(
                steps=[
                    runner.StepResult(
                        name="main_stage_status",
                        ok=True,
                        message="当前已通关最高主线：第一部第1章 1-2-6 突如其來的天使",
                        data={
                            "highest_passed_id": 102000009,
                            "highest_passed_desc": "第一部第1章 1-2-6 突如其來的天使",
                            "highest_passed_area_id": 101000002,
                            "highest_passed_area_name": "突如其來的天使",
                            "next_section_id": 102000010,
                            "next_section_desc": "第一部第1章 1-3-1 翡翠國的危機",
                            "is_all_cleared": False,
                            "passed_count": 9,
                            "total_count": 537,
                        },
                    ),
                ]
            )

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            payload = _get(url, "/api/main_stage/status").json()

        assert payload["ok"] is True
        assert payload["highest_passed_id"] == 102000009
        assert "1-2-6" in payload["highest_passed_desc"]
        assert payload["next_section_id"] == 102000010
        # ★ 2026-10-05：展示给用户的关卡描述**不得夹带 9 位 sectionID**。
        # 本桩原先写的是 "1-2 突如其來的天使 102000010" —— 那是过时格式
        # （真实 friendly_name 形如「第一部第1章 1-2-6 突如其來的天使」），
        # 且 102000010 其实属于 1-3。留着会把错误格式固化给后来人。
        for field in ("highest_passed_desc", "next_section_desc"):
            assert not re.search(r"\d{6,}", payload[field]), (
                f"{field} 里出现了裸关卡 ID：{payload[field]!r}"
            )

    def test_banquet_status_endpoint(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``/api/banquet/status``：宴席双按钮的"两顿现在什么状态"（只读）。

        【★ 回归防线（2026-10-05）】
        第一版实现跑的是 ``bbq_energy`` + ``params={"eat": False}``，但 ``eat``
        不在规格参数里、会被 ``normalize_params`` 丢弃，runner 对缺失的 ``eat``
        默认 **True** —— 只读端点会真的发出 15003 吃掉一顿。本用例把
        "端点必须跑专用只读任务 ``bbq_status``"钉死。
        """
        captured: dict[str, object] = {}

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            assert len(request.tasks) == 1
            captured["name"] = request.tasks[0].name
            captured["params"] = dict(request.tasks[0].params)
            return runner.RunReport(
                steps=[
                    runner.StepResult(
                        name="bbq_status",
                        ok=True,
                        message="宴席两顿状态：早宴席开放中；可邀请好友 2 人",
                        data={
                            "meals": [
                                {
                                    "meal": "morning",
                                    "label": "早宴席",
                                    "group_id": 501000004,
                                    "window_state": "covering",
                                    "state": "ready",
                                    "edible": True,
                                    "invitable": True,
                                    "gap": False,
                                    "window_text": "06:10–13:59",
                                    "start_text": "06:10",
                                    "end_text": "13:59",
                                },
                                {
                                    "meal": "evening",
                                    "label": "晚宴席",
                                    "group_id": 501000005,
                                    "window_state": "not_started",
                                    "state": "not_started",
                                    "edible": False,
                                    "invitable": False,
                                    "gap": False,
                                    "window_text": "14:00–05:59",
                                    "start_text": "14:00",
                                    "end_text": "05:59",
                                },
                            ],
                            "friend_count": 2,
                            "sent_packets": (32007,),
                        },
                    ),
                ]
            )

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            payload = _get(url, "/api/banquet/status").json()

        # 端点跑的必须是只读专用任务，且不带任何参数（不给"能翻转 eat"的口子）
        assert captured["name"] == "bbq_status"
        assert captured["params"] == {}
        assert payload["ok"] is True
        meals = {item["meal"]: item for item in payload["meals"]}
        assert meals["morning"]["window_state"] == "covering"
        assert meals["evening"]["window_state"] == "not_started"
        # 服务端记账的"哪顿吃过了"一并下发（前端据它把按钮置灰）
        assert "meals" in payload["banquet"]
        assert "banquet_day" in payload["banquet"]
        assert payload["friend_count"] == 2

    def test_login_failure_maps_to_400(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """账号 / 凭据问题属于"这次登录没成功" → 400 + 任务自己的说明。"""
        fake_report = self._fake_report

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            return fake_report(ok=False, message="无法确定要用哪个平台账号。")

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            response = requests.post(
                f"{url}/api/login/servers", json={"account": "9"}, timeout=10
            )
        assert response.status_code == 400
        assert "无法确定要用哪个平台账号" in response.json()["detail"]

    def test_session_platform_id_is_masked(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """接口里的平台 userId 必须打码（命令行看完整值，网页看打码值）。"""

        class FakeState:
            player_id = 10000003
            server_id = 113
            server_name = "伊莉莎白一世"
            platform_user_id = "ER00000000-0000-0000-0000-000000000000"
            obtained_at = 1.0

            def describe(self) -> str:
                return "playerId=10000003 @ 伊莉莎白一世(#113)（已打码）"

        monkeypatch.setattr(
            runner, "resolve_session_state", lambda: (FakeState(), "测试来源")
        )
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            payload = _get(url, "/api/health").json()

        masked = payload["session"]["platform_user_id"]
        # 打码规则：前 10 字符 + 省略号 + 总长度
        assert masked == "ER00000000…(38 字符)"
        assert masked.endswith("(38 字符)")
        assert "0000-0000-0000-0000-000000000000" not in masked  # 中段不可见
        assert payload["session"]["server_id"] == 113  # 结构化字段照旧可用


# ---------------------------------------------------------------------------
# ④ 活动关卡预览（★ 2026-09-22 新增）
# ---------------------------------------------------------------------------
class TestActivityPreview:
    """``/api/activity/preview``：把"当期有哪些区、这区有哪些关"端给界面。

    【为什么这些用例都 monkeypatch ``run_inline``】
    与登录流程那组测试同一套路：**不打任何真实网络**，只验证
    "请求怎么翻译"（任务名 / 参数 / dry_run）与"响应怎么组装"（把 ``step.data`` 端出去）。
    真实任务的行为由 ``tests/test_sweep_task.py`` 覆盖。
    """

    @pytest.fixture(autouse=True)
    def _isolated_cache(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """把活动区域缓存指到临时目录（**每个用例都要**）。

        【为什么必须 autouse，不能只靠 base_url fixture】
        本类里有几个用例自己 ``create_app``（因为要测 409 / 400 那条路径），
        不经过 ``base_url``。若不隔离，服务层会读写**工程根的真实**
        ``.activity_cache.json`` —— 于是测试结果取决于"这台机器上有没有缓存、
        它新不新鲜"，出现过"本机验证过一次 → 单元测试莫名其妙变红"的假故障。
        """
        monkeypatch.setattr(
            config, "ACTIVITY_CACHE_FILE", tmp_path / ".activity_cache.json"
        )

    @staticmethod
    def _report(*, ok: bool = True, data: dict | None = None, message: str = "ok"):
        step = runner.StepResult(
            name="sweep_activity", ok=ok, message=message, data=data or {}
        )
        return runner.RunReport(steps=[step])

    def test_preview_is_read_only_and_returns_areas(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}
        fake_report = self._report  # 先取出：monkeypatch 后 self 会是 RunManager

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            captured["names"] = [task.name for task in request.tasks]
            captured["params"] = [dict(task.params) for task in request.tasks]
            captured["dry_run"] = request.dry_run
            return fake_report(
                data={
                    "mode": "read_only",
                    "open_areas": 1,
                    "areas": [
                        {
                            "index": 1,
                            "area_id": 128110053,
                            "name": "可疑的工作",
                            "progress": "5/5",
                            "start": "2026-09-23 21:00",
                            "end": "2026-10-07 21:00",
                        }
                    ],
                    "sent_packets": (11025,),
                }
            )

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            payload = _get(url, "/api/activity/preview").json()

        assert captured["names"] == ["sweep_activity"]
        # ★ 只读的两条硬约束：times=0，且**不是**演练模式。
        # 演练模式（dry_run=True）在任务里等同于"不联网、不发包"——
        # 那样调这个接口只会拿到一段说明文字，一个真实区域都看不到。
        assert captured["params"][0]["times"] == 0
        # ★ 只读预览模式下允许未通关关卡（allow_unpassed=True），以支持一关未过时的关卡候选展示
        assert captured["params"][0]["allow_unpassed"] is True
        assert captured["dry_run"] is False
        assert payload["areas"][0]["area_id"] == 128110053
        assert payload["sections"] == {}
        # 预览绝不能带上扫荡包（11009）
        assert 11009 not in payload["sent_packets"]

    def test_preview_with_area_returns_sections(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str | None] = []
        fake_report = self._report

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            calls.append(dict(request.tasks[0].params).get("area"))
            return fake_report(
                data={
                    "mode": "read_only",
                    # ★ 必须带 areas：服务层的顺序是"先确保区域新鲜，再拉该区关卡"，
                    # 拿不到区域时会**提前返回**（那是正确行为，这里要造完整的数据）
                    "areas": [
                        {"index": 1, "area_id": 128110053, "name": "可疑的工作"}
                    ],
                    "area_id": 128110053,
                    "sweepable": 1,
                    "blocked": 1,
                    "area": {"area_id": 128110053, "name": "可疑的工作"},
                    "sections": {
                        "sweepable": [
                            {
                                "index": 1,
                                "section_id": 129110539,
                                "name": "关卡9",
                                "energy_cost": 8,
                                "passed": True,
                            }
                        ],
                        "blocked": [{"section_id": 129110537, "reason": "不消耗体力"}],
                    },
                    "sent_packets": (11025, 11003),
                }
            )

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            payload = _get(url, "/api/activity/preview", area=128110053).json()

        # 缓存为空 → 先拉区域清单（area=None），再拉该区关卡（area="128110053"）。
        # 顺序不能反：区域换了之后旧的关卡号可能根本不存在。
        assert calls == [None, "128110053"]
        assert payload["area"]["name"] == "可疑的工作"
        assert payload["sections"]["sweepable"][0]["section_id"] == 129110539
        assert payload["sections"]["blocked"][0]["reason"] == "不消耗体力"
        assert 11009 not in payload["sent_packets"]

    def test_unsupported_scope_is_rejected(self, web_dir: Path) -> None:
        """回忆区 / 常驻区暂未接入：明确报 400，而不是静默按当期返回。"""
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            response = _get(url, "/api/activity/preview", scope="memory")

        assert response.status_code == 400
        assert "scope" in str(response.json()["detail"]["problems"])

    def test_busy_run_maps_to_409(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """预览与"一键运行"共用同一把闸门：运行中必须拒绝，而不是并发发包。"""

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            raise state.RunBusyError("上一次运行还没有结束，请等它跑完（或稍后再试）")

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            response = _get(url, "/api/activity/preview")

        assert response.status_code == 409


    def test_fresh_cache_never_touches_the_network(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """★ 缓存新鲜时**一个包都不发**（用户要求："不要每次都自动拉取"）。

        【为什么这条断言写法这么"狠"】
        直接把 ``run_inline`` 换成"一旦被调用就 AssertionError"的函数 ——
        这样"本可以避免的请求"不可能被静默放过，而不是只断言"响应看起来对"。
        """
        cache_file = tmp_path / ".activity_cache.json"
        monkeypatch.setattr(config, "ACTIVITY_CACHE_FILE", cache_file)
        cache = ActivityCache()
        cache.store_areas(
            [{"index": 1, "area_id": 128110053, "name": "可疑的工作", "progress": "5/5"}],
            player_id=0,
            server_id=0,
        )
        cache.store_sections(
            128110053, [{"section_id": 129110539, "energy_cost": 40, "passed": True}], []
        )
        cache.save()

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            raise AssertionError("缓存新鲜时不该发起任何运行（这条请求本可以避免）")

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            areas = _get(url, "/api/activity/preview").json()
            sections = _get(url, "/api/activity/preview", area=128110053).json()

        # 区域与关卡都必须来自缓存，且发包列表为空
        assert areas["source"] == "cache"
        assert areas["sent_packets"] == []
        assert areas["areas"][0]["area_id"] == 128110053
        assert sections["source"] == "cache"
        assert sections["sent_packets"] == []
        assert sections["sections"]["sweepable"][0]["section_id"] == 129110539
        # 状态行要能显示"上次更新 / 下次自动更新"
        assert areas["cache"]["fetched_at"]
        assert areas["cache"]["next_refresh_at"]

    def test_refresh_forces_a_network_call(
        self, web_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``?refresh=1``（「更新区域」按钮）必须**无视缓存**真的去拉一次。"""
        cache_file = tmp_path / ".activity_cache.json"
        monkeypatch.setattr(config, "ACTIVITY_CACHE_FILE", cache_file)
        stale = ActivityCache()
        stale.store_areas(
            [{"index": 1, "area_id": 111111111, "name": "旧区域"}],
            player_id=0,
            server_id=0,
        )
        stale.save()

        calls: list[dict] = []
        fake_report = self._report

        def fake_run_inline(self, request):  # type: ignore[no-untyped-def]
            calls.append(dict(request.tasks[0].params))
            return fake_report(
                data={
                    "areas": [{"index": 1, "area_id": 128110053, "name": "可疑的工作"}],
                    "sent_packets": (11025,),
                }
            )

        monkeypatch.setattr(state.RunManager, "run_inline", fake_run_inline)
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            payload = _get(url, "/api/activity/preview", refresh=1).json()

        assert len(calls) == 1
        assert calls[0]["times"] == 0  # 仍然只读
        assert payload["source"] == "network"
        assert payload["areas"][0]["area_id"] == 128110053  # 旧的 111111111 已被替换
        assert payload["sent_packets"] == [11025]
        assert 11009 not in payload["sent_packets"]
        assert cache_file.exists()

    def test_non_numeric_area_is_rejected(self, web_dir: Path) -> None:
        """区域只接受 areaID（数字）：网页是下拉，名称/编号留给命令行。"""
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            response = _get(url, "/api/activity/preview", area="可疑")

        assert response.status_code == 400
        assert "areaID" in str(response.json()["detail"]["problems"])

    def test_material_tree_endpoint(self, web_dir: Path) -> None:
        """/api/material/tree 端点返回按类别分组的素材关卡子目录树。"""
        with _serve(webapp.create_app(web_dir=web_dir)) as url:
            response = _get(url, "/api/material/tree")

        assert response.status_code == 200
        payload = response.json()
        assert "categories" in payload
        assert "job" in payload["categories"]
        assert "element" in payload["categories"]
        job_areas = payload["categories"]["job"]
        assert len(job_areas) == 7
        element_areas = payload["categories"]["element"]
        assert len(element_areas) == 6
        # 验证具有 sections 子目录
        assert len(job_areas[0]["sections"]) > 0

    def test_save_logs_and_open_folder(
        self, base_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """测试开发者日志保存与目录打开端点。"""
        monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)

        # 1. 默认保存（从 log_stream 获取内容）
        res = requests.post(f"{base_url}/api/logs/save", json={}, timeout=10)
        assert res.status_code == 200
        data = res.json()
        assert data["ok"] is True
        assert Path(data["filepath"]).exists()
        assert data["filename"].startswith("cherrytale-dev-log-")
        assert "can_open_folder" in data

        # 2. 携带前端自定义 content 保存
        custom_content = "22:00:00 [INFO] test: 自定义日志内容"
        res2 = requests.post(
            f"{base_url}/api/logs/save",
            json={"content": custom_content},
            timeout=10,
        )
        assert res2.status_code == 200
        data2 = res2.json()
        assert Path(data2["filepath"]).read_text(encoding="utf-8") == custom_content
        assert data2["line_count"] == 1

        # 3. 打开目录端点测试（mock 平台打开动作以防弹出文件管理器）
        if sys.platform == "win32":
            opened = []
            monkeypatch.setattr(webapp.os, "startfile", lambda p: opened.append(p))
            res3 = requests.post(f"{base_url}/api/logs/open_folder", timeout=10)
            assert res3.status_code == 200
            assert res3.json()["ok"] is True
            assert len(opened) == 1
        elif sys.platform == "darwin":
            opened = []
            monkeypatch.setattr(webapp.subprocess, "Popen", lambda args: opened.append(args))
            res3 = requests.post(f"{base_url}/api/logs/open_folder", timeout=10)
            assert res3.status_code == 200
            assert res3.json()["ok"] is True


if __name__ == "__main__":  # 支持 `python -m tests.test_webapi_app`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))




