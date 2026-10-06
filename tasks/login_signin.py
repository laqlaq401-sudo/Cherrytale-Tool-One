"""登录后自动签到钩子（每日循环登入奖励 + 活动登入奖励；**内置、无开关**）。

【它不是任务 —— 这是刻意的设计（2026-10-06 用户需求）】
签到是"登入即结算"的服务端行为，没有参数、没有花费、没有失败重试的价值判断，
把它做成任务只会带来"要不要勾、忘了勾怎么办"这类多余的选择。所以本模块：

* **不**用 ``@register_task`` 注册 —— 不进 ``/api/tasks``，前端没有卡片、
  没有复选框、没有开关；
* 由登录成功后的固定钩子调用（``tasks/login.py`` 与
  ``webapi/app.py::login_enter`` 的直连快速路径），**登录即执行**；
* 结果只写一条 ✔/✘ 摘要日志（``tasks.login_signin`` logger，经 root handler
  进开发者日志流），**不进运行视图**（结果流水仍只显示用户勾选的任务）。

【"每天只执行一次"的口径 —— 自然日 0 点，与日常任务的 05:00 游戏日不同】
登入奖励的服务端结算换日是**自然日 0 点**（2026-10-06 抓包：03:09 登入即
入账当日第 11 天奖励；若是 05:00 换日口径则此刻不应有新入账）。记账键是
``s={server}|p={player}`` → 本地日期字符串，跨进程持久在
:obj:`config.LOGIN_SIGNIN_STATE_FILE`。

【为什么记账失败/漏记也无害】
33055 是**幂等查询**：游戏客户端自己每次登录都发；服务端每自然日只结算一次，
重复查询只会得到空的 addList（不会重复发奖）。所以：

* 记账只是"少发一次冗余查询"的优化，不是安全依赖；
* 本机时区与服务器 0 点有偏差时，最坏多发一次幂等查询；
* 发送失败**不记账** —— 下次登录自动重试，直到成功为止。

【协议依据】33055 空包 → 33056（三路入账），见 ``models/login_rewards.py``
与 ``notes/recon_findings.md`` 的「登录签到」节。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import config
from client.game_client import GameClient
from models.login_rewards import (
    SIGNIN_ALLOWED_PACKET_IDS,
    SIGNIN_FORBIDDEN_PACKET_IDS,
    GetActivityLoginRewards,
    GetActivityLoginRewardsRes,
)
from models.reward import QuantityScope, reward_phrase
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.login_signin")

#: 日志行的固定前缀（用户在开发者日志里一眼认出这条链路）。
TITLE: Final[str] = "登录签到（每日+活动）"

#: 文件格式版本：结构变了就升版，旧文件整体丢弃（它没有长期价值）。
_FILE_VERSION: Final[int] = 1

_lock: Final[threading.Lock] = threading.Lock()


# ---------------------------------------------------------------------------
# 记账：s=|p= → 本地自然日（0 点换日）
# ---------------------------------------------------------------------------
def _identity_key(session_state: Any) -> str:
    """``s={server_id}|p={player_id}``；区服/角色缺一个就不算数（返回空串）。

    【为什么同时要区服与角色】同一个平台账号在不同区服是不同角色，
    只看其一会把"换区登录"误判成"今天已经签到过"（与
    ``services/daily_state.identity_key`` 同一套教训）。
    """
    server_id = int(getattr(session_state, "server_id", 0) or 0)
    player_id = int(getattr(session_state, "player_id", 0) or 0)
    if server_id <= 0 or player_id <= 0:
        return ""
    return f"s={server_id}|p={player_id}"


def _today(now: datetime | None = None) -> str:
    """本地自然日字符串（0 点换日的判据；``now`` 供测试注入）。"""
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _load(path: Path | None = None) -> dict[str, Any]:
    """读记账文件；不存在 / 损坏一律静默降级成空结构。

    【为什么不抛异常】这份记录的价值是"少发一次幂等查询"，丢了最坏
    多发一次；抛异常反而会把登录主流程拖下水 —— 得不偿失。
    """
    target = Path(path) if path is not None else config.LOGIN_SIGNIN_STATE_FILE
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": _FILE_VERSION, "targets": {}}
    except OSError as exc:
        _LOGGER.warning("读登录签到记账失败（%s）：%s", target, exc)
        return {"version": _FILE_VERSION, "targets": {}}
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _LOGGER.warning("登录签到记账不是合法 JSON（%s）：%s", target, exc)
        return {"version": _FILE_VERSION, "targets": {}}
    if not isinstance(payload, dict) or payload.get("version") != _FILE_VERSION:
        return {"version": _FILE_VERSION, "targets": {}}
    targets = payload.get("targets")
    return payload if isinstance(targets, dict) else {"version": _FILE_VERSION, "targets": {}}


def _save(payload: dict[str, Any], path: Path | None = None) -> None:
    target = Path(path) if path is not None else config.LOGIN_SIGNIN_STATE_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _LOGGER.warning("写登录签到记账失败（%s）：%s", target, exc)


def _already_done(
    key: str, *, now: datetime | None = None, path: Path | None = None
) -> bool:
    """这个角色今天的签到查询是否已经发过（跨日自动作废，无需清理）。"""
    with _lock:
        entry = _load(path).get("targets", {}).get(key)
    return isinstance(entry, dict) and str(entry.get("date", "")) == _today(now)


def _mark_done(
    key: str, *, now: datetime | None = None, path: Path | None = None
) -> None:
    """记下"今天已发过"。与 :func:`_already_done` 同用一把锁保护整份读写。"""
    with _lock:
        payload = _load(path)
        targets = payload.setdefault("targets", {})
        targets[key] = {
            "date": _today(now),
            # 仅排障用的时间戳，不参与判定（唯一判据是上面的 date）。
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        payload["targets"] = targets
        _save(payload, path)


# ---------------------------------------------------------------------------
# 结果文案
# ---------------------------------------------------------------------------
def _phrase(groups: Any, *, scope: QuantityScope) -> str:
    """按口径出文案；道具不在名称表时降级成"获得 N 件道具"。

    ``reward_phrase`` 的 GAINED/HELD 口径只念得出**认识名字**的道具
    （见 ``models/reward.py``）；登入奖励的道具大多不在 ``_ITEM_NAMES`` 里，
    直接用会把"确实入了账"说成一句话都没有 —— 所以这里显式降级，
    与 UNKNOWN 口径的"获得 N 件道具"同款（如实、不猜数量含义）。
    """
    return reward_phrase(groups, scope=scope) or reward_phrase(
        groups, scope=QuantityScope.UNKNOWN
    )


def describe_credits(res: GetActivityLoginRewardsRes) -> str:
    """把 33056 三路入账汇成一行 ✔ 摘要；三路全空 → "今日已结算"。"""
    parts: list[str] = []
    daily = _phrase(res.dailyAddList, scope=QuantityScope.GAINED)
    if daily:
        parts.append(f"每日签到 {daily}")
    newbie = _phrase(res.newPlayerAddList, scope=QuantityScope.UNKNOWN)
    if newbie:
        parts.append(f"新手签到 {newbie}")
    for activity in res.activityData:
        text = _phrase(activity.addList, scope=QuantityScope.UNKNOWN)
        if text:
            parts.append(f"「{activity.title or activity.sid}」{text}")
    if not parts:
        return f"{TITLE} ✔ 今日已结算，无新入账（可能已在游戏客户端领取）"
    return f"{TITLE} ✔ " + "；".join(parts)


# ---------------------------------------------------------------------------
# 对外的唯一入口
# ---------------------------------------------------------------------------
def run_if_first_login_today(
    session_state: Any, *, now: datetime | None = None, path: Path | None = None
) -> None:
    """每个角色每自然日第一次进游戏后，发一次 33055 并汇报入账。

    :param session_state: 刚就绪的 ``GameSessionState``（登录任务或
        ``login_enter`` 直连路径产出的都行）。
    :param now: "今天"的基准（测试注入）。
    :param path: 记账文件（测试注入；默认 :obj:`config.LOGIN_SIGNIN_STATE_FILE`）。

    .. warning::
       **绝不向调用方抛异常** —— 它跑在登录主流程的正后方，签到查询失败
       只值一条 ✘ 日志，不值得让"登录成功"变成失败。
    """
    try:
        key = _identity_key(session_state)
        if not key:
            _LOGGER.debug("%s：会话缺区服/角色信息，跳过", TITLE)
            return
        if _already_done(key, now=now, path=path):
            _LOGGER.debug("%s：今天已查询过，跳过（%s）", TITLE, key)
            return

        with GameClient() as game:
            session_state.apply_to(game.envelope)
            view = guarded_send(
                game,
                GetActivityLoginRewards(),
                # 33056 在"无活动且无新入账"时可能是**纯元信息应答**（子包为空），
                # 带 expect 会在 send 内部被强行解析而误报协议错误 ——
                # 所以这里拿原始视图后按需解码（见下一行）。
                None,
                allowed=SIGNIN_ALLOWED_PACKET_IDS,
                forbidden=SIGNIN_FORBIDDEN_PACKET_IDS,
                logger=_LOGGER,
            )
        res = (
            GetActivityLoginRewardsRes()
            if not view.sub_packet_bytes
            else view.decode_sub_packet(GetActivityLoginRewardsRes)
        )

        # 查询成功就记账（无论有无新入账）：33055 幂等，记账只是"少打一遍
        # 冗余查询"的优化，失败路径不走到这里 —— 下次登录会自动重试。
        _mark_done(key, now=now, path=path)
        _LOGGER.info("%s", describe_credits(res))
    except Exception as exc:  # noqa: BLE001  见函数 docstring 的 warning
        _LOGGER.warning(
            "%s ✘ 失败：%s（不影响登录；下次登录自动重试）", TITLE, exc
        )
