"""米娅小助手 HTTP 服务：路由 + 访问令牌 + 静态目录挂载。

【全平台统一的标准库 HTTP 服务】
基于 Python 标准库 ``http.server.ThreadingHTTPServer`` 实现：
- 零第三方 HTTP/ASGI 依赖（不依赖 fastapi / uvicorn / starlette / anyio）；
- 双端（PC 桌面 / Android Chaquopy）统一使用同一份代码与同一套网络行为；
- 请求/响应的 JSON 形状（含 ``{"detail": ...}`` 错误格式）完全对齐，前端无感知；
- 内置卡死看门狗（90 秒无进展自动转储全线程堆栈，排障利器）；
- HTTP/1.1 长连接（Keep-Alive 复用）；
- **同源边界**：不提供跨域访问（2026-10-04 起）。详见 ``_check_local_boundary``。

【分层约定】
本模块只做「HTTP 翻译」，不做业务判断。所有业务逻辑由 ``services/`` 和 ``webapi/state.py`` 承担。
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, Sequence
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

import config
from models import daily_reset
from pydantic import BaseModel, Field, ValidationError
from services import (
    activity_cache_service,
    banquet_state,
    daily_state,
    runner,
    selection_state,
    spend_state,
    task_spec,
)
from webapi import logstream, state

_LOGGER: Final[logging.Logger] = logging.getLogger("webapi.app")

#: 应用版本（双端页面页脚显示）。
APP_VERSION: Final[str] = "1.0.0"

#: 默认端口（PC 端默认 8765；Android 端固定指向 8000）。
DEFAULT_PORT: Final[int] = 8000

#: 前端静态资源目录。优先取只读资源目录（如 PyInstaller 解压后的包内目录），
#: 回退到工程根目录下的 web/。
WEB_DIR: Final[Path] = (
    config.RESOURCE_ROOT / "web"
    if (config.RESOURCE_ROOT / "web").is_dir()
    else config.PROJECT_ROOT / "web"
)


# ---------------------------------------------------------------------------
# 卡死看门狗
# ---------------------------------------------------------------------------
_WATCHDOG_STREAM: logstream.LogStream | None = None
_ACTIVITY_LOCK = threading.Lock()
_ACTIVITY: dict[str, Any] = {
    "inflight": 0,
    "last_progress": time.time(),
    #: 最后一次心跳来自哪里（给看门狗转储用；不是数字，但同放一份避免多把锁）
    "last_note": "",
    #: worker 线程是否正在跑一批（★ 2026-10-06：旧看门狗只看 inflight，
    #: 漏掉了"HTTP 都返回了、只有 worker 卡住"这种最常见的卡死形态）
    "run_active": 0,
}

#: 一个请求进入后超过这么多秒仍无任何进展，判定"卡死"并转储堆栈。
HANG_DUMP_AFTER_SECONDS: Final[float] = 90.0

#: 转储时每个线程最多保留的栈帧数。
_DUMP_MAX_FRAMES: Final[int] = 14

#: 单个请求体的最大字节数（1 MiB，2026-10-04 安全审计 P1）。
#: 全部接口的正文都是小体积 JSON（最大的运行请求也就几 KB），
#: 留 1 MiB 余量足够；超出即视为异常客户端，由 ``_read_body`` 回 413。
MAX_REQUEST_BODY_BYTES: Final[int] = 1024 * 1024


def _note_request_entered() -> None:
    with _ACTIVITY_LOCK:
        _ACTIVITY["inflight"] += 1


def _note_request_finished() -> None:
    with _ACTIVITY_LOCK:
        _ACTIVITY["inflight"] -= 1
        _ACTIVITY["last_progress"] = time.time()


def note_work_progress(note: str = "") -> None:
    """记一笔"业务侧仍在推进"，供卡死看门狗判断有没有真的挂住。

    ★ 2026-10-06 新增（看门狗漏判的根因）。

    【旧判据为什么漏掉最该抓的那种卡死】
    旧看门狗只看 ``inflight``（HTTP 请求在飞的数量）：``inflight <= 0`` 就直接
    ``continue``。可是用户报的那种"点了执行、进度条一动不动"恰恰是**相反**的形态 ——

    - ``POST /api/run`` 早就返回了（它只是入队，立刻 202）；
    - 真正卡住的是 **worker 线程**（等闸门 / 等某个 30 秒超时的请求）；
    - 此时 ``inflight`` 可能是 0 或很小，看门狗于是**永远不转储**，
      日志里一行【看门狗】都没有 —— 排障时完全没有现场。

    【现在的两种触发方式】
    1. 任务级心跳（主路径）：``webapi/state.py`` 在"某一步开始 / 某一步结束"时
       调本函数。任务的天然节拍就是一两个请求一次，用它当心跳最贴合业务；
    2. 兜底：``state`` 在 worker 等待闸门期间也会周期性调一次
       （见 ``_acquire_gate_interruptible``），覆盖"整批卡在闸门外"的情况。

    心跳**同时**会刷新 ``last_progress``，所以只要 worker 还在按正常节拍干活，
    看门狗就不会误报 —— 阈值内没有心跳才判定为挂死。
    """
    with _ACTIVITY_LOCK:
        _ACTIVITY["last_progress"] = time.time()
        if note:
            _ACTIVITY["last_note"] = note


def _activity_progress_note() -> str:
    """给看门狗转储用的一行"最后进展在哪"。"""
    with _ACTIVITY_LOCK:
        note = str(_ACTIVITY.get("last_note", ""))
    return f"，最后进展：{note}" if note else ""


def dump_all_thread_stacks(note: str) -> None:
    """把所有线程的当前堆栈写进日志流（网页可见）与 stderr（logcat/终端可见）。"""
    frames = sys._current_frames()
    stream = _WATCHDOG_STREAM
    if stream is not None:
        stream.append("WARNING", "watchdog", f"【看门狗】{note}")
    _LOGGER.warning("【看门狗】%s —— 全线程堆栈如下", note)
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        if frame is None:
            continue
        text = (
            f"线程 {thread.name}（daemon={thread.daemon}）：\n"
            + "".join(traceback.format_stack(frame, limit=_DUMP_MAX_FRAMES))
        )
        if stream is not None:
            stream.append("WARNING", "watchdog", text)
        _LOGGER.warning("【看门狗】%s", text)
    if stream is not None:
        stream.append("WARNING", "watchdog", "【看门狗】堆栈转储完毕")
    _LOGGER.warning("【看门狗】堆栈转储完毕")


def _start_hang_watchdog() -> None:
    """每 15 秒巡检一次：有请求 / 有运行长时间无进展就转储现场。

    ★ 2026-10-06 修正判据（原来只看 ``inflight``，漏判最该抓的卡死）：

    - **旧**：``inflight <= 0`` 就跳过。可是 ``POST /api/run`` 入队后立刻返回，
      真正卡住的是 worker 线程 —— 那时 ``inflight`` 常是 0，
      于是"进度条一动不动"这种最典型的卡死**永远不转储**。
    - **新**：``inflight > 0`` **或** 有运行在跑（``run_active``）都算"有活在手上"，
      再结合 ``last_progress``（由 HTTP 请求进出 + 任务级心跳共同刷新）判断。
      只有"手上确实有活、且超过阈值没有任何心跳"才转储。

    这样既不会漏判 worker 卡死，也不会在空闲时刷屏。
    """
    def watch() -> None:
        while True:
            time.sleep(15)
            with _ACTIVITY_LOCK:
                inflight = _ACTIVITY["inflight"]
                run_active = _ACTIVITY.get("run_active", 0)
                stalled_for = time.time() - _ACTIVITY["last_progress"]
                busy = inflight > 0 or run_active > 0
                if not busy or stalled_for < HANG_DUMP_AFTER_SECONDS:
                    continue
                _ACTIVITY["last_progress"] = time.time()
            what = f"{inflight} 个请求 / {run_active} 个运行"
            dump_all_thread_stacks(
                f"有 {what} {stalled_for:.0f} 秒无任何进展，疑似挂死"
                + _activity_progress_note()
            )

    threading.Thread(target=watch, name="hang-watchdog", daemon=True).start()


# ---------------------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------------------
class TaskPayload(BaseModel):
    """前端提交的单个任务。"""

    name: str = Field(description="任务注册名，例如 daily")
    params: dict[str, Any] = Field(
        default_factory=dict, description="任务级参数（结构化 dict）"
    )


class CredentialsPayload(BaseModel):
    """平台账号密码。内存流转，不落盘。"""

    email: str = ""
    password: str = ""


class RunPayload(BaseModel):
    """一次运行的请求体。"""

    tasks: list[TaskPayload] = Field(default_factory=list)
    dry_run: bool = False
    allow_diamond: bool = False
    # ★ 2026-10-05：前端是否已把「服务端保存的总闸值」还原进勾选框。
    # True = allow_diamond 是用户此刻的**明确选择**（包括刚把闸关掉），后端照单全收；
    # False = 前端是旧 JS / 还原失败，allow_diamond=False 不足为信 ——
    # 后端改用「已保存值 或 本次请求值」合并，保住“开过就保持开”（见 start_run）。
    # 【为什么不做成简单 OR】用户刚关闸立刻点执行时，保存请求与运行请求存在竞态，
    # 简单 OR 会把刚关掉的闸又弹开 —— 花钱的事，错误方向必须朝“关”。
    allow_diamond_synced: bool = False
    account: str | None = None
    credentials: CredentialsPayload | None = None
    verbose: bool = False

    def to_request(self) -> runner.RunRequest:
        credentials: tuple[str, str] | None = None
        if (
            self.credentials is not None
            and self.credentials.email.strip()
            and self.credentials.password
        ):
            credentials = (self.credentials.email.strip(), self.credentials.password)

        return runner.RunRequest(
            tasks=tuple(
                runner.TaskRequest(name=task.name, params=dict(task.params))
                for task in self.tasks
            ),
            dry_run=self.dry_run,
            allow_diamond=self.allow_diamond,
            account=self.account,
            credentials=credentials,
            verbose=self.verbose,
        )


class LoginPayload(BaseModel):
    """登录 / 选区两步请求的公共部分。"""

    account: str | None = None
    credentials: CredentialsPayload | None = None

    def merged_credentials(self) -> tuple[str, str] | None:
        if self.credentials is None:
            return None
        email = self.credentials.email.strip()
        if not email or not self.credentials.password:
            return None
        return (email, self.credentials.password)


class EnterServerPayload(LoginPayload):
    """进入指定区服。"""

    server_id: int = Field(description="要进入的区服编号")
    direct: bool = Field(default=False, description="是否尝试直连已保存的区服凭据")


class SaveLogsPayload(BaseModel):
    """保存日志请求体。"""

    content: str | None = Field(default=None, description="可选的前端当前缓冲日志文本")


class SpendPayload(BaseModel):
    """钻石使用总闸的保存请求体（2026-10-05）。"""

    allow_diamond: bool = Field(description="True = 允许花钻石（总闸打开）")


class TaskSelectionPayload(BaseModel):
    """单个任务的界面选择（2026-10-05）。"""

    checked: bool = Field(default=False, description="勾选状态")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="任务参数（值不做类型归一，由前端 reconcile 按 ParamSpec.kind 处理）",
    )


class SelectionPayload(BaseModel):
    """任务勾选/参数的服务端保存请求体（2026-10-05，全量提交）。"""

    tasks: dict[str, TaskSelectionPayload] = Field(
        default_factory=dict,
        description="{任务名: {checked, params}}；未知任务名/参数键在写盘前被白名单过滤丢弃",
    )
    dry_run: bool = Field(
        default=False,
        description="演练开关（仅 ?dev=1 开发者模式的界面偏好；运行时是否演练由前端 isDevMode() 硬闸决定）",
    )


class ApiError(Exception):
    """统一业务异常：对应特定的 HTTP 状态码与 detail。"""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _mask_platform_id(value: str) -> str:
    """平台 userId 脱敏展示。"""
    text = str(value or "")
    if not text:
        return ""
    return f"{text[:10]}…({len(text)} 字符)"


def _masked_session() -> dict[str, Any]:
    """带有脱敏 platform_user_id 的会话摘要。"""
    info = runner.describe_session()
    info["platform_user_id"] = _mask_platform_id(str(info.get("platform_user_id", "")))
    return info


# ---------------------------------------------------------------------------
# 应用主体
# ---------------------------------------------------------------------------
class WebApp:
    """持有一套运行管理器与日志流，实现全部 13 个业务接口。"""

    def __init__(
        self,
        *,
        token: str | None = None,
        web_dir: Path | None = None,
        stream: logstream.LogStream | None = None,
        bridge: logstream.ThreadAwareStdout | None = None,
    ) -> None:
        if stream is not None:
            self.log_stream = stream
        elif bridge is not None:
            self.log_stream = bridge.stream
        else:
            self.log_stream = logstream.LogStream()
        self.manager = state.RunManager(self.log_stream, bridge=bridge)
        self.token = token
        self.web_dir = Path(web_dir) if web_dir is not None else WEB_DIR

    def require_token(
        self, request_headers: dict[str, str], query: dict[str, list[str]]
    ) -> None:
        if not self.token:
            return
        header_token = request_headers.get("X-Auth-Token") or request_headers.get(
            "x-auth-token"
        )
        provided = (header_token or (query.get("token", [""])[0]) or "").strip()
        if not provided or not secrets.compare_digest(
            provided.encode("utf-8"), self.token.encode("utf-8")
        ):
            raise ApiError(
                401,
                "访问令牌不正确：请在页面上填写，或用 /?token=… 打开",
            )

    def health(self) -> dict[str, Any]:
        """就绪探针（前端首屏路由靠它）。"""
        session_info = _masked_session()
        return {
            "ok": True,
            "app_version": APP_VERSION,
            "auth_required": bool(self.token),
            "session": session_info,
            "next_screen": "main" if session_info.get("has_session") else "login",
            "running": self.manager.is_running,
            "run": self.manager.status(),
            "next_seq": self.log_stream.next_seq,
        }

    def tasks(self) -> dict[str, Any]:
        """任务清单：分组 + 参数 schema + 真实约束 + 日常状态。"""
        return {
            "groups": task_spec.groups_view(),
            "tasks": task_spec.describe_tasks(),
            "run_level_params": [
                task_spec.param_view(param) for param in task_spec.RUN_LEVEL_PARAMS
            ],
            "suggested_dry_run": False,
            "game_day": daily_reset.game_day(),
            "next_daily_reset": daily_reset.format_reset(),
            "auto_done": list(daily_state.completed_names()),
            # ★ 2026-10-05：宴席"早/晚哪顿吃过了"的服务端记账（与 auto_done 无关）。
            # 前端据 ``meals`` 把对应按钮置灰。换日点 06:00，与游戏日 05:00 独立。
            "banquet": banquet_state.describe(),
            "spend": {
                "config_allow_diamond": config.ALLOW_DIAMOND_SPEND,
                "config_invite_friends": config.BBQ_INVITE_FRIENDS,
                "config_buy_energy": config.BUY_ENERGY,
                # ★ 2026-10-05：服务端保存的总闸位置 —— 前端启动时**以它为权威**还原
                # 勾选框（localStorage 在 EXE 的 Edge --app 壳里不可靠，只作兜底）。
                "saved_allow_diamond": spend_state.allow_diamond(),
                "summary": config.spending_summary(),
            },
            # ★ 2026-10-05：任务勾选/参数的服务端记账 —— 前端启动时**以它为权威**
            # 还原勾选框与参数框（localStorage 在 EXE 的 Edge --app 壳里不可靠）。
            # saved_updated_at 为空串 = 服务端从未存过（前端保留 localStorage 兜底）。
            "selection": selection_state.describe(),
        }

    def accounts(self) -> dict[str, Any]:
        """账号库已存凭据。"""
        from client.account_store import AccountStore
        from client.exceptions import CherrytaleError

        store = AccountStore()
        try:
            saved = store.list_accounts()
        except CherrytaleError as exc:
            return {
                "accounts": [],
                "zone_accounts": [],
                "platform_accounts": [],
                "error": str(exc),
            }

        zone_accounts: list[dict[str, Any]] = []
        platform_accounts: list[dict[str, Any]] = []

        for index, item in enumerate(saved, 1):
            acc_name = item.account or _mask_platform_id(item.user_id)
            # 1. 纯平台账号条目（用于免密选区/进新区）
            platform_accounts.append({
                "key": f"platform:{item.user_id}",
                "user_id": item.user_id,
                "account": item.account,
                "index": index,
                "expired": item.is_expired(),
                "can_refresh": item.can_refresh(),
                "display_text": f"✦ [平台账号] {acc_name}（免密选区 / 进新区）",
            })

            # 2. 该账号下的各区服角色条目（用于一键直进）
            sessions_map = dict(item.sessions)
            if not sessions_map and item.game.get("server_id"):
                sessions_map[str(item.game["server_id"])] = dict(item.game)

            for sid_str, sdata in sessions_map.items():
                sid = int(sdata.get("server_id") or sid_str)
                sname = str(sdata.get("server_name") or f"区服 #{sid}")
                pname = str(sdata.get("player_name") or f"角色 {sdata.get('player_id', '')}")
                lvl = int(sdata.get("character_level") or 0)
                lvl_str = f" Lv.{lvl}" if lvl > 0 else ""
                zone_accounts.append({
                    "key": f"zone:{item.user_id}:{sid}",
                    "user_id": item.user_id,
                    "account": item.account,
                    "server_id": sid,
                    "server_name": sname,
                    "player_id": int(sdata.get("player_id") or 0),
                    "player_name": pname,
                    "character_level": lvl,
                    "has_token": bool(sdata.get("token")),
                    #: 角色头像（data-url，登录 1004 编号解析所得；空串=无图，前端兜底）
                    "avatar_data_url": str(sdata.get("avatar_png") or ""),
                    "display_text": f"{acc_name} ── #{sid} {sname} ── {pname}{lvl_str}",
                })

        return {
            "zone_accounts": zone_accounts,
            "platform_accounts": platform_accounts,
            "accounts": [
                {
                    "index": index,
                    "account": item.account,
                    "user_id": item.user_id,
                    "expired": item.is_expired(),
                    "can_refresh": item.can_refresh(),
                    "summary": item.describe(),
                }
                for index, item in enumerate(saved, 1)
            ],
        }

    def login_servers(self, payload: LoginPayload) -> dict[str, Any]:
        """第一段：登录到拿到区服列表为止。"""
        request = runner.RunRequest(
            tasks=(
                runner.TaskRequest(
                    name="login",
                    params={"account": payload.account or "", "plan_only": True},
                ),
            ),
            account=payload.account,
            credentials=payload.merged_credentials(),
        )
        try:
            report = self.manager.run_inline(request)
        except state.InvalidRunError as exc:
            raise ApiError(400, {"problems": exc.problems}) from exc
        except state.RunBusyError as exc:
            raise ApiError(409, str(exc)) from exc

        step = report.steps[0]
        if not step.ok:
            raise ApiError(400, step.message)

        data = dict(step.data)
        current = _masked_session()
        return {
            "ok": True,
            "servers": data.get("servers", []),
            "suggested_server_id": data.get("suggested_server_id"),
            "account_id": data.get("account_id"),
            "user_id": _mask_platform_id(str(data.get("user_id", ""))),
            "current_server_id": current.get("server_id") or None,
            "current_player_id": current.get("player_id") or None,
        }

    def login_enter(self, payload: EnterServerPayload) -> dict[str, Any]:
        """第二段：进区写会话。"""
        if payload.direct and payload.account:
            from client.account_store import AccountStore
            from client.game_client import GameClient
            from client.session_store import save_session_state
            from models.session_state import GameSessionState
            from models.wallet import GetAllItemPacket, GetAllItemRes

            store = AccountStore()
            target_acc = store.find_account(payload.account)
            if target_acc:
                sid_str = str(payload.server_id)
                sess = target_acc.sessions.get(sid_str) or (
                    target_acc.game if str(target_acc.game.get("server_id")) == sid_str else None
                )
                if sess and sess.get("token"):
                    candidate_state = GameSessionState(
                        token=str(sess["token"]),
                        player_id=int(sess.get("player_id") or 0),
                        player_name=str(sess.get("player_name") or ""),
                        time_stamp_token=int(sess.get("time_stamp_token") or -1),
                        account_id=int(sess.get("account_id") or 0),
                        server_id=int(payload.server_id),
                        server_name=str(sess.get("server_name") or ""),
                        platform_user_id=target_acc.user_id,
                        obtained_at=float(sess.get("obtained_at") or 0.0),
                        # ★ 直连也要带头像：账号库里有图就透传，否则
                        #   save_session_state 会把会话文件里已有的头像抹成 null。
                        avatar_data=(
                            str(sess.get("avatar_png")) if sess.get("avatar_png") else None
                        ),
                    )
                    probe_alive = False
                    try:
                        with GameClient() as probe_game:
                            candidate_state.apply_to(probe_game.envelope)
                            probe_game.send(GetAllItemPacket(), expect=GetAllItemRes)
                            probe_alive = True
                            # ★ 直连补采（★ 2026-10-05）：日常主路径是直连，不经过
                            #   1004，头像编号永远没机会落库。令牌活着且该区条目
                            #   还没有头像时，用**同一条活连接**重发 1003→1004
                            #   （= 游戏客户端自己切区的原生序列）取玩家资料。
                            #   任何失败都静默回退 —— 头像是锦上添花，绝不动摇直连。
                            if not sess.get("avatar_png"):
                                self._capture_avatar_on_direct(
                                    probe_game,
                                    candidate_state,
                                    target_acc=target_acc,
                                    server_id=int(payload.server_id),
                                    sess=sess,
                                )
                    except Exception as probe_err:
                        _LOGGER.info("已保存区服令牌探测失效（将自动重新登录换取最新令牌）：%s", probe_err)
                        probe_alive = False

                    if probe_alive:
                        save_session_state(candidate_state)
                        _LOGGER.info("直连已保存区服凭据成功：%s", candidate_state.describe())
                        # 内置钩子：登录签到（每日+活动）—— 直连复用令牌**也算
                        # "进游戏"**：不在这里发的话，令牌活着跳过登录的日子会
                        # 漏发 33055。失败只记日志，绝不影响直连结果。
                        from tasks.login_signin import run_if_first_login_today

                        run_if_first_login_today(candidate_state)
                        return {
                            "ok": True,
                            "direct": True,
                            "message": f"直连成功：{candidate_state.player_name} @ {candidate_state.server_name}(#{candidate_state.server_id})",
                            "session": _masked_session(),
                            "player": {
                                "name": candidate_state.player_name,
                                "fighting_force": 0,
                                "login_days": 0,
                            },
                        }

        request = runner.RunRequest(
            tasks=(
                runner.TaskRequest(
                    name="login",
                    params={
                        "account": payload.account or "",
                        "server_id": payload.server_id,
                    },
                ),
            ),
            account=payload.account,
            credentials=payload.merged_credentials(),
        )
        try:
            report = self.manager.run_inline(request)
        except state.InvalidRunError as exc:
            raise ApiError(400, {"problems": exc.problems}) from exc
        except state.RunBusyError as exc:
            raise ApiError(409, str(exc)) from exc

        step = report.steps[0]
        if not step.ok:
            raise ApiError(400, step.message)

        data = dict(step.data)
        return {
            "ok": True,
            "message": step.message,
            "session": _masked_session(),
            "player": {
                "name": data.get("player_name", ""),
                "fighting_force": data.get("fighting_force", 0),
                "login_days": data.get("login_days", 0),
            },
        }

    def _capture_avatar_on_direct(
        self,
        game: Any,
        state: Any,
        *,
        target_acc: Any,
        server_id: int,
        sess: dict[str, Any],
    ) -> None:
        """直连补采头像：同一条活连接重发 1003→1004，取玩家资料里的头像编号。

        【为什么存在】日常主路径是直连（复用区服令牌），**不经过 1004**——
        头像编号只在 1004 里下发，直连用户的头像永远没机会落库。这里复用
        探活成功的连接按游戏原生"切区"序列重登一次，编号/新令牌一并落库。

        【安全边界】开关 ``CHERRYTALE_DIRECT_AVATAR_REFRESH=0`` 可关闭；
        **任何异常都静默回退**（只记日志），绝不让头像问题影响直连结果。
        1004 会下发新令牌：旧令牌被我们自己的重登作废，所以新令牌必须
        同步写回会话状态与账号库，否则下次直连探测会用死令牌。
        """
        import time

        from dataclasses import replace as _dc_replace

        from models.server import LoginPacket, LoginRes, ServerListClass
        from services.avatar_service import resolve_avatar_png

        if os.getenv("CHERRYTALE_DIRECT_AVATAR_REFRESH", "1").strip().lower() in (
            "0",
            "false",
            "no",
        ):
            return
        try:
            packet = LoginPacket.for_server(
                ServerListClass(serverID=int(server_id)),
                account_id=int(sess.get("account_id") or 0),
                platform_user_id=target_acc.user_id,
                channel_platform_type=config.CHANNEL_PLATFORM_TYPE_EL,
                client_version=config.GAME_CLIENT_VERSION,
                language_code=config.GAME_LANGUAGE,
            )
            view = game.send(packet, expect=LoginRes, include_defaults=True)
            login_res = view.decode_sub_packet(LoginRes)
            if login_res.errorCode != 0 or login_res.playerInitClass is None:
                _LOGGER.info(
                    "直连补采头像：1004 未通过（errorCode=%s），放弃本轮补采",
                    login_res.errorCode,
                )
                return
            player_init = login_res.playerInitClass
            icon_info = getattr(login_res, "myIconInfo", None)
            avatar_ids = {
                "icon": int(getattr(player_init, "icon", 0) or 0),
                "frame": int(getattr(player_init, "frameId", 0) or 0),
                "representative": int(getattr(player_init, "roleRepresentativeId", 0) or 0),
                "role_main": int(getattr(icon_info, "roleMainID", 0) or 0),
                "skin": int(getattr(icon_info, "skinId", 0) or 0),
                "bg": int(getattr(icon_info, "bgId", 0) or 0),
            }
            avatar_png = resolve_avatar_png(avatar_ids)

            # 1004 下发了新令牌/新时间戳：同步刷进会话状态（save_session_state 会落盘）
            state.token = str(player_init.token or state.token)
            state.player_id = int(player_init.playerID or state.player_id)
            state.time_stamp_token = int(getattr(game.envelope, "time_stamp_token", -1) or -1)
            state.obtained_at = time.time()
            state.avatar_data = avatar_png

            # 账号库条目：编号 + 图片 + 新令牌一并写回（下次直连直接命中）
            from client.account_store import AccountStore

            store = AccountStore()
            target = store.find_account(target_acc.user_id) or target_acc
            sessions = dict(target.sessions)
            entry = dict(sessions.get(str(server_id)) or sess)
            entry.update(
                {
                    "avatar": avatar_ids,
                    "avatar_png": avatar_png,
                    "token": state.token,
                    "time_stamp_token": state.time_stamp_token,
                    "obtained_at": state.obtained_at,
                }
            )
            sessions[str(server_id)] = entry
            store.upsert(_dc_replace(target, sessions=sessions, game=entry))
            _LOGGER.info(
                "直连补采头像完成（icon=%s，图片来源=%s）",
                avatar_ids["icon"],
                "已解析" if avatar_png else "暂无（静态目录未覆盖，前端回退首字）",
            )
        except Exception as exc:  # noqa: BLE001 —— 头像补采绝不影响直连
            _LOGGER.info("直连补采头像失败（不影响直连结果）：%s", exc)

    @staticmethod
    def _session_avatar_data_url() -> str:
        """当前会话的角色头像 data-url（无会话/无图/读盘失败都返回空串）。

        【为什么吞掉所有异常】头像是锦上添花：会话文件损坏、字段缺失时，
        正确行为是"任务页用默认立绘"，绝不是让整个玩家信息聚合查询失败。
        """
        from client.exceptions import CherrytaleError
        from client.session_store import load_session_state

        try:
            loaded = load_session_state()
        except (CherrytaleError, OSError):
            return ""
        if loaded is None or not loaded.avatar_data:
            return ""
        return str(loaded.avatar_data)

    def player_info(
        self, query: dict[str, list[str]] | None = None
    ) -> dict[str, Any]:
        """聚合查询玩家状态（钱包、竞技场、荣耀之巅等）。"""
        full_val = (query.get("full", ["0"])[0] if query else "0").strip().lower()
        full = full_val in ("1", "true", "yes", "on")

        read_tasks: list[runner.TaskRequest] = [
            runner.TaskRequest(name="wallet"),
            # ★ 2026-10-05：「角色与资源」面板要显示"我有哪些角色"。
            #   ``roles`` 是只读任务（只发 5011 空包），拿不到实时名册时会
            #   自动退回登录 1004 存下的快照，并在 ``source`` 里说明用的是哪一份
            #   —— 界面据此提示"这份数据是刚拉的还是登录时的"。
            runner.TaskRequest(name="roles"),
            runner.TaskRequest(name="arena_status"),
            runner.TaskRequest(name="wudou_status"),
            runner.TaskRequest(name="yimo_status"),
        ]
        if full:
            read_tasks.append(
                runner.TaskRequest(
                    name="top_pvp_battle", params={"battles": 0, "interval": 0}
                )
            )
        request = runner.RunRequest(tasks=tuple(read_tasks))
        try:
            report = self.manager.run_inline(request)
        except state.InvalidRunError as exc:
            raise ApiError(400, {"problems": exc.problems}) from exc
        except state.RunBusyError as exc:
            raise ApiError(409, str(exc)) from exc

        steps = {step.name: step for step in report.steps}

        def data_of(name: str) -> dict[str, Any]:
            return dict(steps[name].data) if name in steps else {}

        wallet = data_of("wallet")
        roles = data_of("roles")
        arena = data_of("arena_status")
        wudou = data_of("wudou_status")
        yimo = data_of("yimo_status")
        top_pvp = data_of("top_pvp_battle")

        session_info = _masked_session()
        return {
            "ok": True,
            "session": session_info,
            "player_name": session_info.get("player_name", ""),
            # ★ 2026-10-05：角色头像（登录 1004 编号 → services/avatar_service.py
            #   解析出的 data-url）。任务界面信息卡直接用它渲染 ``<img>``；
            #   空串 = 无图，前端维持默认立绘。单独读会话文件而不并进
            #   ``describe_session()`` —— 那是打码摘要、多处高频调用，
            #   不该被一张图片撑大。
            "avatar_data_url": self._session_avatar_data_url(),
            "game": {
                "coins": wallet.get("coins"),
                "diamonds": wallet.get("diamonds"),
                "stamina": wallet.get("stamina"),
                "item_count": wallet.get("item_count"),
            },
            "roles": {
                "count": roles.get("role_count", 0),
                #: "live"（实时名册 5012）/ "snapshot"（登录 1004 快照）
                "source": roles.get("source", ""),
                "fallback_reason": roles.get("fallback_reason", ""),
                #: ``[{role_sid, role_id, role_info_id, level, star, name, icon_code}, …]``
                #: ``icon_code`` 是静态头像目录的文件名（如 ``a001_03h``），前端拼
                #: ``assets/avatars/{icon_code}.png``；为空串时前端回退统一占位图。
                "items": list(roles.get("roles") or ()),
            },
            "arena": {
                "challenge_count": arena.get("challenge_count"),
                "buyed_count": arena.get("buyed_count"),
                "my_fighting": arena.get("my_fighting"),
                "alliance_name": arena.get("alliance_name"),
                "opponent_count": arena.get("opponent_count"),
            },
            "wudou": {
                "challenge_times": wudou.get("challenge_times"),
                "npc_times": wudou.get("npc_times"),
                "my_ranking": wudou.get("my_ranking"),
            },
            "yimo": {
                "remain_count": yimo.get("remain_count"),
                "now_score": yimo.get("now_score"),
                "now_rank": yimo.get("now_rank"),
            },
            "top_pvp": {
                "my_ranking": top_pvp.get("my_ranking"),
                "my_points": top_pvp.get("my_points"),
                "mode": top_pvp.get("mode"),
            },
            "errors": [
                f"{step.name}：{step.message}"
                for step in report.steps
                if not step.ok
            ],
        }

    def banquet_status(self) -> dict[str, Any]:
        """宴席两顿的实时状态（只读，发 32007→32008，**不发 15003**）。

        :return: ``{"ok":…, "banquet_day":…, "meals":[…], "done":[…] }``。

        【为什么单独开一个接口，而不是塞进 /api/tasks】
        ``/api/tasks`` 是静态规格 + 轻量状态，每次进页面都要读；而宴席两顿的窗口
        必须**联网问服务端**（"现在覆盖没覆盖"用的是服务端下发的毫秒时间戳，
        本地时钟不可信）。把它分开，页面的常规加载就不会被一次联网拖慢，
        只有宴席那张卡片展开 / 点「刷新」时才来问这个接口。

        【为什么跑的是 ``bbq_status`` 而不是 ``bbq_energy``（2026-10-05 修正）】
        ``eat`` 不在 ``bbq_energy`` 的规格参数里，走 ``normalize_params`` 会被当
        未知键丢弃，而 runner 对缺失的 ``eat`` 默认 **True**（主动任务点了就吃）——
        用 ``bbq_energy`` 做"只读"会**真的吃掉一顿**。``bbq_status`` 是结构上
        不可能有副作用的专用任务（见 ``tasks/bbq.py::BBQStatusTask``）。
        """
        request = runner.RunRequest(
            tasks=(runner.TaskRequest(name="bbq_status"),)
        )
        try:
            report = self.manager.run_inline(request)
        except state.InvalidRunError as exc:
            raise ApiError(400, {"problems": exc.problems}) from exc
        except state.RunBusyError as exc:
            raise ApiError(409, str(exc)) from exc

        step = report.steps[0] if report.steps else None
        data = dict(step.data) if step else {}
        return {
            "ok": step.ok if step else False,
            "message": step.message if step else "",
            "banquet": banquet_state.describe(),
            "meals": data.get("meals", []),
            "friend_count": data.get("friend_count", 0),
        }

    def main_stage_status(self) -> dict[str, Any]:
        """主线最高通关关卡与章节。"""
        request = runner.RunRequest(
            tasks=(runner.TaskRequest(name="main_stage_status"),)
        )
        try:
            report = self.manager.run_inline(request)
        except state.InvalidRunError as exc:
            raise ApiError(400, {"problems": exc.problems}) from exc
        except state.RunBusyError as exc:
            raise ApiError(409, str(exc)) from exc

        step = report.steps[0] if report.steps else None
        data = dict(step.data) if step else {}
        return {
            "ok": step.ok if step else False,
            "message": step.message if step else "",
            "highest_passed_id": data.get("highest_passed_id"),
            "highest_passed_desc": data.get("highest_passed_desc"),
            "highest_passed_area_id": data.get("highest_passed_area_id"),
            "highest_passed_area_name": data.get("highest_passed_area_name"),
            "next_section_id": data.get("next_section_id"),
            "next_section_desc": data.get("next_section_desc"),
            "is_all_cleared": data.get("is_all_cleared", False),
            "passed_count": data.get("passed_count", 0),
            "total_count": data.get("total_count", 0),
        }

    def _activity_view(
        self,
        result: activity_cache_service.ActivityCacheResult,
        *,
        area_id: int | None = None,
    ) -> dict[str, Any]:
        cache = result.cache
        area_entry: dict[str, Any] | None = None
        sections: dict[str, Any] = {}
        if area_id is not None:
            for item in cache.areas:
                if int(item.get("area_id") or 0) == area_id:
                    area_entry = dict(item)
                    break
            sections = {
                "sweepable": cache.sections_view(area_id),
                "blocked": cache.blocked_view(area_id),
            }
        return {
            "ok": result.ok,
            "scope": "current",
            "source": result.source,
            "area_selector": str(area_id) if area_id is not None else None,
            "areas": cache.areas_view(),
            "area": area_entry,
            "sections": sections,
            "cache": {
                "fetched_at": cache.fetched_text,
                "next_refresh_at": cache.next_refresh_text,
                "fetched_at_epoch": cache.fetched_at,
                "player_id": cache.player_id,
                "server_id": cache.server_id,
                "area_count": len(cache.areas),
            },
            "summary": result.note,
            "sent_packets": list(result.sent_packets),
            "errors": [result.error] if result.error else [],
        }

    def activity_preview(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """活动关卡预览。"""
        area = query.get("area", [None])[0]
        scope = query.get("scope", ["current"])[0]
        refresh = query.get("refresh", ["false"])[0].lower() in ("1", "true", "yes")
        if scope != "current":
            raise ApiError(
                400,
                {"problems": [f"暂不支持 scope={scope!r}（当前只实现 current）"]},
            )
        service = activity_cache_service.ActivityCacheService(
            self.manager.run_inline
        )
        area_text = (area or "").strip()
        if area_text and not area_text.isdigit():
            raise ApiError(
                400,
                {
                    "problems": [
                        f"区域只接受 areaID（数字），收到 {area_text!r}。"
                        "想按名称或清单编号选，请用命令行："
                        "python main.py run sweep_activity --area <名称关键字>"
                    ]
                },
            )
        try:
            result = (
                service.ensure_sections(int(area_text), force=bool(refresh))
                if area_text
                else service.ensure_areas(force=bool(refresh))
            )
        except state.InvalidRunError as exc:
            raise ApiError(400, {"problems": exc.problems}) from exc
        except state.RunBusyError as exc:
            raise ApiError(409, str(exc)) from exc
        return self._activity_view(
            result, area_id=int(area_text) if area_text else None
        )

    def material_tree(self, query: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """素材关卡与元素试炼的分类子目录树。"""
        from models.material_stage import get_material_areas

        category = query.get("category", ["all"])[0] if query else "all"
        areas = get_material_areas(category)
        return {
            "category": category,
            "areas": [a.as_view() for a in areas],
            "total_areas": len(areas),
            "total_sections": sum(len(a.sections) for a in areas),
            "categories": {
                "job": [a.as_view() for a in get_material_areas("job")],
                "element": [a.as_view() for a in get_material_areas("element")],
            },
        }

    def start_run(self, payload: RunPayload) -> tuple[int, dict[str, Any]]:
        """提交任务运行，返回 (202, payload)。"""
        # ★ 2026-10-05：总闸合并（在 to_request 之前改 payload，runner 零改动）。
        # - 前端已从服务端还原过勾选框（allow_diamond_synced=True）时：
        #   allow_diamond 就是用户此刻的明确选择（含刚关闸），照单全收；
        # - 否则（旧 JS / 还原失败）：allow_diamond=False 不足为信，
        #   用「已保存值 或 本次请求值」合并，保住“开过就保持开”。
        if not payload.allow_diamond_synced:
            payload.allow_diamond = spend_state.allow_diamond() or payload.allow_diamond
        try:
            run_id, position = self.manager.start(payload.to_request())
        except state.InvalidRunError as exc:
            raise ApiError(400, {"problems": exc.problems}) from exc
        return 202, {
            "run_id": run_id,
            "status": "queued" if position > 0 else "running",
            "queued_behind": position,
        }

    def set_spend(self, payload: SpendPayload) -> dict[str, Any]:
        """保存钻石使用总闸（2026-10-05：服务端权威，localStorage 只作兜底）。"""
        saved = spend_state.set_allow_diamond(payload.allow_diamond)
        _LOGGER.info(
            "钻石使用总闸已更新：%s（%s）",
            "开" if saved["allow_diamond"] else "关",
            saved["updated_at"],
        )
        return {
            "allow_diamond": saved["allow_diamond"],
            "updated_at": saved["updated_at"],
        }

    def save_selection(self, payload: SelectionPayload) -> dict[str, Any]:
        """保存任务勾选/参数（2026-10-05：服务端权威，localStorage 只作兜底）。"""
        tasks = {
            name: {"checked": entry.checked, "params": dict(entry.params)}
            for name, entry in payload.tasks.items()
        }
        saved = selection_state.save_selection(tasks, payload.dry_run)
        return {
            "saved_tasks": saved["tasks"],
            "saved_dry_run": saved["dry_run"],
            "updated_at": saved["updated_at"],
        }

    def run_status(self) -> dict[str, Any]:
        """运行状态。"""
        return self.manager.status()

    def run_logs(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """日志游标轮询。"""
        since = int(query.get("since", ["0"])[0] or 0)
        limit = int(query.get("limit", ["500"])[0] or 500)
        limit = max(1, min(limit, 2000))
        lines, truncated = self.log_stream.since(since)
        sent = lines[:limit]
        return {
            "next_seq": sent[-1].seq if sent else since,
            "stream_seq": self.log_stream.next_seq,
            "truncated": truncated,
            "lines": [line.to_dict() for line in sent],
        }

    def stop_run(self) -> dict[str, Any]:
        """停止运行。"""
        result = self.manager.stop()
        result["ok"] = result.get("stopped", False)
        return result

    def save_logs(self, payload: SaveLogsPayload | None = None) -> dict[str, Any]:
        """将当前会话日志持久化保存至本地磁盘 logs/ 目录。"""
        log_dir = (config.PROJECT_ROOT / "logs").resolve()
        log_dir.mkdir(parents=True, exist_ok=True)

        stamp = time.strftime("%Y-%m-%d-%H-%M-%S")
        filename = f"cherrytale-dev-log-{stamp}.txt"
        target_path = log_dir / filename

        text: str
        if payload is not None and payload.content and payload.content.strip():
            text = payload.content.strip()
        else:
            lines, _ = self.log_stream.since(0)
            if lines:
                text = "\n".join(
                    f"{line.ts} [{line.level}] {line.logger}: {line.message}"
                    for line in lines
                )
            else:
                text = "（缓冲为空：本次会话还没有任何日志）"

        target_path.write_text(text, encoding="utf-8")
        can_open = _can_open_folder()
        return {
            "ok": True,
            "filepath": str(target_path),
            "log_dir": str(log_dir),
            "filename": filename,
            "line_count": len(text.splitlines()),
            "can_open_folder": can_open,
        }

    def open_log_folder(self) -> dict[str, Any]:
        """在系统文件管理器中打开日志目录。"""
        log_dir = (config.PROJECT_ROOT / "logs").resolve()
        log_dir.mkdir(parents=True, exist_ok=True)

        if sys.platform == "win32":
            try:
                os.startfile(str(log_dir))  # type: ignore[attr-defined]
                return {"ok": True, "log_dir": str(log_dir)}
            except Exception as exc:  # noqa: BLE001
                raise ApiError(500, f"打开日志目录失败：{exc}") from exc
        elif sys.platform == "darwin":
            try:
                subprocess.Popen(["open", str(log_dir)])
                return {"ok": True, "log_dir": str(log_dir)}
            except Exception as exc:  # noqa: BLE001
                raise ApiError(500, f"打开日志目录失败：{exc}") from exc
        elif (
            shutil.which("xdg-open")
            and not ("ANDROID_ROOT" in os.environ or hasattr(sys, "getandroidapilevel"))
        ):
            try:
                subprocess.Popen(["xdg-open", str(log_dir)])
                return {"ok": True, "log_dir": str(log_dir)}
            except Exception as exc:  # noqa: BLE001
                raise ApiError(500, f"打开日志目录失败：{exc}") from exc
        else:
            return {
                "ok": False,
                "log_dir": str(log_dir),
                "message": "当前系统环境不支持直接调起文件管理器，请通过上方路径手动查阅",
            }

    def docs(self) -> tuple[int, str, str]:
        """精简 API 离线说明页（对齐 /api/docs 存在性验证）。"""
        html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Cherrytale Tool API - {APP_VERSION}</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #0d0f14; color: #e1e4ea; margin: 32px; line-height: 1.6; }}
h1 {{ color: #ff6b81; font-size: 20px; }}
code {{ background: #1a1e28; padding: 2px 6px; border-radius: 4px; color: #a4b0be; }}
ul {{ list-style-type: square; }}
li {{ margin: 8px 0; }}
</style>
</head>
<body>
<h1>Cherrytale Tool Web API ({APP_VERSION})</h1>
<p>内置标准库 HTTP 引擎已就绪。所有 API 接口均在 <code>/api/*</code> 下挂载：</p>
<ul>
  <li><code>GET  /api/health</code> - 服务就绪与首屏提示</li>
  <li><code>GET  /api/tasks</code> - 任务清单与日常状态</li>
  <li><code>GET  /api/accounts</code> - 已保存账号列表</li>
  <li><code>POST /api/login/servers</code> - 登录并获取区服列表</li>
  <li><code>POST /api/login/enter</code> - 进入区服并写入会话</li>
  <li><code>GET  /api/player</code> - 玩家数据与资产聚合查询</li>
  <li><code>GET  /api/main_stage/status</code> - 主线通关进度</li>
  <li><code>GET  /api/activity/preview</code> - 活动关卡与配置表预览</li>
  <li><code>GET  /api/material/tree</code> - 素材关卡树状关系</li>
  <li><code>POST /api/run</code> - 调度执行任务队列</li>
  <li><code>POST /api/spend</code> - 保存钻石使用总闸（服务端持久化）</li>
  <li><code>POST /api/selection</code> - 保存任务勾选/参数（服务端持久化）</li>
  <li><code>GET  /api/status</code> - 任务执行与队列状态</li>
  <li><code>GET  /api/logs</code> - 日志流游标增量轮询</li>
  <li><code>POST /api/logs/save</code> - 保存开发者日志至本地磁盘</li>
  <li><code>POST /api/logs/open_folder</code> - 打开日志存放目录</li>
  <li><code>POST /api/stop</code> - 协作式中断当前与排队任务</li>
</ul>
</body></html>"""
        return 200, html, "text/html; charset=utf-8"


def _can_open_folder() -> bool:
    """判断当前运行平台是否支持直接调起系统文件管理器。"""
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        return True
    if "ANDROID_ROOT" in os.environ or hasattr(sys, "getandroidapilevel"):
        return False
    if shutil.which("xdg-open") and (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return True
    return False


# 兼容历史别名
AndroidApp = WebApp


# ---------------------------------------------------------------------------
# 路由分发
# ---------------------------------------------------------------------------
def _route(
    app: WebApp, method: str, path: str, query: dict[str, list[str]], body: bytes
) -> tuple[int, Any]:
    payload_models = {
        "/api/login/servers": LoginPayload,
        "/api/login/enter": EnterServerPayload,
        "/api/run": RunPayload,
        "/api/logs/save": SaveLogsPayload,
        "/api/spend": SpendPayload,
        "/api/selection": SelectionPayload,
    }

    if method == "GET":
        if path == "/api/health":
            return 200, app.health()
        if path == "/api/tasks":
            return 200, app.tasks()
        if path == "/api/accounts":
            return 200, app.accounts()
        if path == "/api/player":
            return 200, app.player_info(query)
        if path == "/api/main_stage/status":
            return 200, app.main_stage_status()
        if path == "/api/banquet/status":
            return 200, app.banquet_status()
        if path == "/api/activity/preview":
            return 200, app.activity_preview(query)
        if path == "/api/material/tree":
            return 200, app.material_tree(query)
        if path == "/api/status":
            return 200, app.run_status()
        if path == "/api/logs":
            return 200, app.run_logs(query)
        if path == "/api/docs":
            return 200, app.docs()
    elif method == "POST":
        model_class = payload_models.get(path)
        if model_class is not None:
            try:
                parsed = json.loads(body.decode("utf-8")) if body.strip() else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiError(422, {"detail": f"请求体不是合法 JSON：{exc}"}) from exc
            try:
                payload = model_class.model_validate(parsed)  # type: ignore[attr-defined]
            except ValidationError as exc:
                raise ApiError(422, str(exc)) from exc
            if path == "/api/run":
                return app.start_run(payload)
            if path == "/api/spend":
                return 200, app.set_spend(payload)
            if path == "/api/selection":
                return 200, app.save_selection(payload)
            if path == "/api/login/servers":
                return 200, app.login_servers(payload)
            if path == "/api/login/enter":
                return 200, app.login_enter(payload)
            if path == "/api/logs/save":
                return 200, app.save_logs(payload)
        if path == "/api/stop":
            return 200, app.stop_run()
        if path == "/api/logs/open_folder":
            return 200, app.open_log_folder()

    raise ApiError(404, "Not Found")


_EXTRA_TYPES: Final[dict[str, str]] = {
    ".webmanifest": "application/manifest+json",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


#: 被允许的回环主机名（``Host`` 头比对用）。``[::1]`` 由 :func:`_normalize_host`
#: 归一成 ``::1``，所以这里不重复列带方括号的形式。
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})


def _normalize_host(value: str | None) -> str:
    """把 ``Host`` 头规范成小写主机名（去掉端口，兼容 IPv6 字面量）。

    :param value: 原始 ``Host`` 头；``None`` 或空串返回 ``""``。
    :return: 例如 ``"127.0.0.1:8000"`` → ``"127.0.0.1"``、``"[::1]:8000"`` → ``"::1"``。
    """
    text = (value or "").strip()
    if not text:
        return ""
    if text.startswith("["):  # IPv6 字面量：[::1] 或 [::1]:8000
        return text[1:].split("]", 1)[0].lower()
    return (text.rsplit(":", 1)[0] if ":" in text else text).lower()


def _origin_host(origin: str | None) -> str:
    """从 ``Origin`` 头取出小写主机名；``null`` 或非法值返回空串。"""
    text = (origin or "").strip()
    if not text or text.lower() == "null":
        return ""
    try:
        return (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""


class _RequestHandler(BaseHTTPRequestHandler):
    """HTTP 处理器：同源边界 + 鉴权 + 路由 + JSON / 静态文件。"""

    protocol_version = "HTTP/1.1"
    server_version = "CherrytaleTool/" + APP_VERSION
    app: WebApp

    #: 单次 socket 读/写的超时（秒，2026-10-04 安全审计 P1）。
    #:
    #: ``socketserver.StreamRequestHandler.setup()`` 会用它给连接设 ``settimeout``，
    #: 于是：① 半开连接 / 只发一半正文的客户端不会把处理线程永久占住；
    #: ② HTTP/1.1 keep-alive 的空闲连接会在超时后被 ``handle_one_request``
    #: 主动关闭（它专门捕获 ``socket.timeout`` 并置 ``close_connection``），
    #: 不再"连上了就挂着一个线程不放"。
    #: 超时按**单次读写**计，不影响慢任务（同步只读请求在执行期间并不读写 socket）。
    timeout: float = 30.0

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        _LOGGER.debug("%s %s", self.address_string(), fmt % args)

    def _is_local_only(self) -> bool:
        """服务器是否只绑定在本机回环地址上（``--host 0.0.0.0`` 时为 ``False``）。"""
        try:
            bound = str(self.server.server_address[0] or "")
        except (AttributeError, IndexError, TypeError):
            return False
        return bound in _LOOPBACK_HOSTS

    def _check_local_boundary(self) -> bool:
        """同源 + 本机边界校验；不通过时**直接回 403 并返回 False**。

        【为什么必须有它（2026-10-04 决策：不提供跨域访问）】
        原先每个响应都带 ``Access-Control-Allow-Origin: *``，等于告诉浏览器
        "任何站点都可以读这个接口"。而服务只监听回环地址**并不**构成防护：
        浏览器允许页面访问 ``http://127.0.0.1``，DNS 重绑定还能让外部域名
        解析到 127.0.0.1，从浏览器视角就成了"同源"。所以这里做两件事：

        1. **只绑回环时，``Host`` 必须是回环主机名** —— 掐掉 DNS 重绑定
           （攻击者域名会带 ``Host: evil.com``）；
        2. **带 ``Origin`` 时，其主机必须与 ``Host`` 一致** —— 掐掉跨站请求。
           同源页面本来就不需要 CORS 头，因此不再发送任何 ``Access-Control-*``。

        【为什么不会误伤既有入口】
        - 前端页面与 ``/api/*`` 由**同一个**服务同源提供（PC 8765 / 安卓 8000），
          请求不带 ``Origin`` 或与 ``Host`` 一致，照常放行；
        - 命令行、脚本、健康检查用的是非浏览器客户端，不发送 ``Origin``；
        - 真正想让局域网访问时用 ``--host 0.0.0.0``（此时 ``_is_local_only()``
          为假，只保留同源校验），并照既有约定强制 ``--token``。
        """
        host_name = _normalize_host(self.headers.get("Host"))
        if self._is_local_only() and host_name not in _LOOPBACK_HOSTS:
            _LOGGER.warning("已拒绝非回环 Host 的请求：Host=%r", self.headers.get("Host"))
            self._send_json(403, {"detail": "本机服务只接受回环地址访问"})
            return False

        origin = self.headers.get("Origin")
        if origin:
            origin_name = _origin_host(origin)
            if not origin_name or (host_name and origin_name != host_name):
                _LOGGER.warning("已拒绝跨域请求：Origin=%r Host=%r", origin, host_name)
                self._send_json(
                    403, {"detail": "已拒绝跨域请求（本服务不提供跨域访问）"}
                )
                return False
        return True

    def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        # 明确告知缓存/代理：响应随 Origin 是否被接受而不同。
        self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error_fastapi_style(self, exc: ApiError) -> None:
        self._send_json(exc.status_code, {"detail": exc.detail})

    def do_OPTIONS(self) -> None:  # noqa: N802
        """跨域预检：不提供跨域访问，因此只对同源请求回 204。"""
        if not self._check_local_boundary():
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        _note_request_entered()
        try:
            self._dispatch_inner(method)
        finally:
            _note_request_finished()

    def _dispatch_inner(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        # 同源 + 本机边界：不通过时已回 403，直接收工（见 _check_local_boundary）。
        if not self._check_local_boundary():
            return

        if path.startswith("/api/"):
            try:
                if path != "/api/docs":
                    self.app.require_token(dict(self.headers.items()), query)
                status, obj = _route(self.app, method, path, query, self._read_body())
                if isinstance(obj, tuple) and len(obj) == 3:
                    # 自定义二进制/HTML响应，如 docs
                    doc_status, doc_body, doc_type = obj
                    self._send_bytes(doc_status, doc_body.encode("utf-8"), doc_type)
                else:
                    self._send_json(status, obj)
            except ApiError as exc:
                self._send_error_fastapi_style(exc)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("接口处理异常：%s %s", method, path)
                self._send_json(
                    500,
                    {"detail": f"服务器内部错误：{type(exc).__name__}: {exc}"},
                )
            return

        if method != "GET":
            self._send_json(405, {"detail": "Method Not Allowed"})
            return
        self._serve_static(path)

    def _read_body(self) -> bytes:
        """读取请求体；**超过上限直接拒绝**（2026-10-04 安全审计 P1）。

        :raises ApiError: 413 —— 正文大于 :data:`MAX_REQUEST_BODY_BYTES` 时。

        【为什么要设上限】本服务是长驻的多线程 HTTP 服务，正文全靠
        ``rfile.read(size)`` 一次性读进内存。不设上限时，一个声称
        ``Content-Length: 2GB`` 的请求就能让进程按声明值去读、把内存与
        处理线程一起拖住 —— 而本应用全部接口的正文都是几百字节的 JSON。

        【为什么拒绝时要把连接关掉】没有读正文就返回，残留字节会被
        keep-alive 当成"下一个请求"的起始，导致协议错位。置
        ``close_connection`` 让本连接到此为止，残留数据随 socket 关闭丢弃。
        """
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            return b""
        if size <= 0:
            return b""
        if size > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            raise ApiError(
                413,
                f"请求体过大（声明 {size} 字节，上限 {MAX_REQUEST_BODY_BYTES} 字节）",
            )
        return self.rfile.read(size)

    def _serve_static(self, path: str) -> None:
        web_dir = self.app.web_dir
        if not web_dir.is_dir():
            self._send_json(404, {"detail": "前端目录不存在"})
            return

        relative = path.lstrip("/") or "index.html"
        target = (web_dir / relative).resolve()
        # 防目录穿越
        try:
            target.relative_to(web_dir.resolve())
        except ValueError:
            self._send_json(404, {"detail": "Not Found"})
            return

        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send_json(404, {"detail": "Not Found"})
            return

        suffix = target.suffix.lower()
        content_type = _EXTRA_TYPES.get(suffix) or mimetypes.guess_type(target.name)[0]
        self._send_bytes(
            200,
            target.read_bytes(),
            content_type or "application/octet-stream",
        )

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")


def create_app(
    *,
    token: str | None = None,
    web_dir: Path | None = None,
    stream: logstream.LogStream | None = None,
    bridge: logstream.ThreadAwareStdout | None = None,
) -> WebApp:
    """构造 WebApp 应用对象。"""
    return WebApp(token=token, web_dir=web_dir, stream=stream, bridge=bridge)


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    token: str | None = None,
    web_dir: Path | None = None,
    stream: logstream.LogStream | None = None,
    bridge: logstream.ThreadAwareStdout | None = None,
    application: WebApp | None = None,
) -> tuple[ThreadingHTTPServer, WebApp]:
    """构造 HTTP 服务器与配套应用实例。"""
    app_instance = (
        application
        if application is not None
        else create_app(token=token, web_dir=web_dir, stream=stream, bridge=bridge)
    )

    handler = type("BoundHandler", (_RequestHandler,), {"app": app_instance})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True

    global _WATCHDOG_STREAM
    _WATCHDOG_STREAM = app_instance.log_stream
    _start_hang_watchdog()
    return server, app_instance


def main(argv: Sequence[str] | None = None) -> int:
    """独立运行入口。"""
    parser = argparse.ArgumentParser(
        prog="webapi.app",
        description="米娅小助手（全平台标准库 HTTP 统一服务）",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    from main import force_utf8_output, setup_logging

    force_utf8_output()
    setup_logging(args.verbose)

    # 行缓冲优化
    for stream_obj in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream_obj, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(line_buffering=True)
            except (AttributeError, ValueError):
                pass

    # ★ 2026-10-06 修复（Android 端"任务内部日志一行都不显示"的根因）：
    # 这条入口是 **Android（Chaquopy）唯一**的启动路径（见 ServerService.kt 调
    # ``webapi.app.main``）。此前这里既没建 LogStream、也没挂 handler、也没装
    # stdout 代理 —— 于是 WebApp 落到 ``logstream.LogStream()`` 自建一个空流，
    # ``bridge=None`` 让 worker 的 print 不被捕获。
    #
    # 后果：``_log()`` 写出来的**状态机文案**（▶ 已受理 / ✔ 任务完成 / ✘ 失败）
    # 照常可见，但任务**内部**的 logger 日志（``tasks.mail`` 的"邮件共 N 封"、
    # ``packet_guard`` 的"→ 发送 …"、``client`` 的全部输出）**永远进不了
    # /api/logs** —— 用户看到的就是"任务在跑，但一行过程日志都没有"。
    #
    # 三个入口必须装配一致：web_server.py / main_gui.py 早就是这么做的，
    # 只有这里漏了。顺序与它们保持一致（先 attach 再 install）。
    stream = logstream.LogStream()
    stream_handler = logstream.attach_to_root(stream, level=logging.INFO)
    bridge = logstream.install_stdout_bridge(stream)

    try:
        server, _ = create_server(
            host=args.host,
            port=args.port,
            token=args.token,
            stream=stream,
            bridge=bridge,
        )
    except OSError:
        # 端口占用等启动失败：把刚装上的代理摘掉，避免留下半截状态
        logstream.uninstall_stdout_bridge(bridge)
        logstream.detach(stream_handler)
        raise
    _LOGGER.info("统一 HTTP 服务已在 http://%s:%d 启动", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        # 收尾与 web_server.py / main_gui.py 对齐：两条通道都要摘干净
        logstream.uninstall_stdout_bridge(bridge)
        logstream.detach(stream_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
