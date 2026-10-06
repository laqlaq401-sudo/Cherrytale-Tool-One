"""``webapi/android_app.py`` 的接口测试（真实 HTTP，全程离线）。

【为什么单独测这套服务器】
安卓端用的是标准库实现（``ThreadingHTTPServer``），与桌面的 fastapi 版是
**两条** HTTP 装配线。业务层（``state`` / ``runner``）虽然共用，但"路由 →
鉴权 → JSON 形状"这一层必须在桌面侧就能验证，否则每次封包都要装到手机上
才能发现问题。

【离线保证】（与 ``tests/test_webapi_app.py`` 同款）
- ``/api/run`` 用不存在的任务名 —— 在装配阶段就被 ``InvalidRunError`` 拦下，
  不发任何网络请求，也不写会话；
- 会话状态被 monkeypatch 成固定值，不依赖本机的 ``.session.json``。

运行方式：

    python -m pytest tests/test_android_app.py -q
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

requests = pytest.importorskip("requests")

from webapi import android_app, logstream


def _free_port() -> int:
    """取一个当前空闲的端口（写死会和网页版/手机版撞车）。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


@contextmanager
def _server(**kwargs) -> Iterator[tuple[str, android_app.AndroidApp]]:
    """起一个测试服务器（后台线程），结束时关闭。"""
    port = kwargs.pop("port", _free_port())
    server, app = android_app.create_server(port=port, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """把会话状态固定成"没有登录"（与真机是否登录过无关）。"""
    monkeypatch.setattr(
        android_app.runner,
        "describe_session",
        lambda: {"has_session": False, "platform_user_id": ""},
    )


def test_health_shape(no_session: None) -> None:
    """就绪探针：字段形状与 fastapi 版一致，前端首屏靠它路由。"""
    with _server() as (base, _app):
        resp = requests.get(f"{base}/api/health", timeout=5)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["auth_required"] is False
    assert body["next_screen"] == "login"
    assert "next_seq" in body and "run" in body


def test_unknown_api_path_single_wrapped_detail() -> None:
    """404 的错误体是 ``{"detail": ...}`` 一层包裹（对齐 fastapi 的错误格式）。"""
    with _server() as (base, _app):
        resp = requests.get(f"{base}/api/nothing", timeout=5)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_stop_without_run_is_a_noop() -> None:
    """★ 2026-10-04：/api/stop 不再是 501 占位。

    没有运行在跑时它只是"没什么可停的"（HTTP 200 + ``stopped=False``），
    与桌面版 ``webapi/app.py`` 的行为一致。
    """
    with _server() as (base, _app):
        resp = requests.post(f"{base}/api/stop", timeout=5)
    assert resp.status_code == 200
    assert resp.json()["stopped"] is False


def test_run_unknown_task_rejected_offline(no_session: None) -> None:
    """不存在的任务名 → 400 + problems；全程离线（装配阶段即失败）。"""
    with _server() as (base, _app):
        resp = requests.post(
            f"{base}/api/run",
            json={"tasks": [{"name": "__no_such_task__", "params": {}}]},
            timeout=10,
        )
    assert resp.status_code == 400
    assert "problems" in resp.json()["detail"]


def test_token_required_and_accepted() -> None:
    """令牌鉴权：缺头 401；带对 X-Auth-Token 头 → 正常返回。

    【为什么令牌用 ASCII】HTTP 头的取值只允许 latin-1 编码，中文令牌
    走不了请求头 —— 与 fastapi 版的实际情况一致（网页上中文口令实际
    通过 ``?token=`` 查询参数传递，下面顺带验证这条路）。
    """
    with _server(token="test-token-123") as (base, _app):
        missing = requests.get(f"{base}/api/health", timeout=5)
        assert missing.status_code == 401
        assert "访问令牌不正确" in missing.json()["detail"]

        ok = requests.get(
            f"{base}/api/health",
            headers={"X-Auth-Token": "test-token-123"},
            timeout=5,
        )
        assert ok.status_code == 200
        assert ok.json()["auth_required"] is True


def test_token_via_query_param() -> None:
    """``?token=`` 查询参数同样能通过鉴权（中文口令走这条路）。"""
    with _server(token="中文口令") as (base, _app):
        ok = requests.get(f"{base}/api/health", params={"token": "中文口令"}, timeout=5)
    assert ok.status_code == 200


def test_log_stream_cursor() -> None:
    """/api/logs 的游标语义：since=0 时 next_seq 停在已发出的最后一行。"""
    with _server() as (base, app):
        app.log_stream.append("INFO", "test", "测试日志行")
        resp = requests.get(f"{base}/api/logs?since=0", timeout=5)
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_seq"] >= 1
    assert isinstance(body["lines"], list)


# ---------------------------------------------------------------------------
# pydantic 行为契约（桌面真 pydantic 与安卓垫片都必须满足）
# ---------------------------------------------------------------------------
def test_pydantic_contract_inherited_field_keeps_default() -> None:
    """子类不重新声明时，继承字段必须保留父类默认值（webapi 的真实形态）。

    2026-10-02 真机踩坑：垫片曾把 get_type_hints 带进来的基类注解当作
    "子类重新声明"，EnterServerPayload 继承的 ``account: str | None = None``
    被重置成必填，选区后进区直接 400。

    .. note::
       与真 pydantic 的一个**有意差异**：若子类真的重新标注了字段但不赋值，
       真 pydantic 会把它变回必填，垫片则保留默认值（更宽松）。本项目代码
       风格不会写这种声明，故不纳入契约。
    """
    from pydantic import BaseModel

    class LoginPayload(BaseModel):
        account: str | None = None

    class EnterServerPayload(LoginPayload):
        server_id: int = 0

    payload = EnterServerPayload.model_validate({"server_id": 113})
    assert payload.account is None


def test_pydantic_contract_mutable_default_not_shared() -> None:
    """可变默认值（= [] / = {}）必须每个实例独享一份（真 pydantic 深拷贝语义）。"""
    from pydantic import BaseModel

    class M(BaseModel):
        rows: list[int] = []

    a = M.model_validate({})
    b = M.model_validate({})
    a.rows.append(1)
    assert b.rows == []


def test_pydantic_contract_lax_bool_from_int() -> None:
    """宽松模式下 bool 字段接受 0/1（前端 JS 常这么发，真 pydantic 也接受）。"""
    from pydantic import BaseModel

    class M(BaseModel):
        flag: bool = False

    assert M.model_validate({"flag": 1}).flag is True
    assert M.model_validate({"flag": 0}).flag is False


def test_hang_watchdog_dump_writes_to_log_stream() -> None:
    """卡死看门狗的转储必须落进日志流（网页"开发者日志"里能看到现场）。"""
    from webapi import android_app as mod

    with _server() as (base, app):
        # create_server 应已把日志流交给看门狗
        assert mod._WATCHDOG_STREAM is app.log_stream
        mod.dump_all_thread_stacks("测试：人为触发转储")
        lines, _truncated = app.log_stream.since(0)
        joined = "\n".join(line.message for line in lines)
    assert "测试：人为触发转储" in joined
    assert "hang-watchdog" in joined  # 看门狗线程自己也要出现在堆栈里
    assert "堆栈转储完毕" in joined
