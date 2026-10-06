"""自动任务「**每游戏日只跑一次**」的服务端记账。

【为什么要这个模块（2026-10-04 用户需求）】
之前"今天跑过没有"记在浏览器的 ``localStorage`` 里（``cherrytale.autoRun``）。
它有两个致命弱点：

1. localStorage 是**按 origin 隔离**的 —— ``127.0.0.1:8765`` 与 ``:8766``
   是两份互不相干的存储。工具启动遇到"端口被占用"换个端口，
   前一次的记录就等于从来没存在过；
2. 桌面窗口壳（pywebview / Edge ``--app``）与手机浏览器各有各的存储，
   同一台机器上从两个入口进来会各跑一遍。

用户的诉求是"**每游戏日第一次登录上去跑一次，之后不再重复**" ——
这条判据属于**账号**，不该属于浏览器。所以记账挪到服务端：

    /api/tasks  ──返回→  auto_done: [今天已完成的任务名]
    RunManager  ──写完→  .daily_auto_state.json（按 角色 + 区服 + 游戏日 分桶）

前端拿到 ``auto_done`` 后与本地记录**取并集**（任一命中即跳过），
localStorage 因此退化成"服务端不可用时的兜底"，不再是唯一来源。

【记账时机（用户 2026-10-04 选定）】
任务**跑完就记账，失败也算**。理由是用户的目标是"每天别重复打扰"：
网络抖一下导致失败，明知道今天已经领过，下一次登录还要再撞一次墙，
还不如老实记下来。真要手动补跑，用任务卡片上的「执行」小按钮 ——
那条路径（``origin="manual"``）刻意不受本记账约束。

【并发】
Web 服务是单进程多线程：``/api/tasks`` 的读与 worker 线程的写可能同时发生。
所以整份文件用一把线程锁保护；写是"读改写"整体原子完成的，
不会出现"两个线程各写一半"把别人的记录覆盖掉的情况。
（要求是稳、不是快：记账一天最多几次，锁的代价可以忽略。）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Iterable, Mapping

import config
from models import daily_reset

_LOGGER: Final[logging.Logger] = logging.getLogger("services.daily_state")

#: 文件格式版本。将来结构变了就升版，旧文件整体丢弃（它没有长期价值）。
_FILE_VERSION: Final[int] = 1

_lock: Final[threading.Lock] = threading.Lock()


# ---------------------------------------------------------------------------
# 身份键：这个账号 + 这个区服
# ---------------------------------------------------------------------------
def identity_key(session: Mapping[str, Any] | None = None) -> str:
    """算出记账用的身份键（``s=12|p=345678``）。

    :param session: ``runner.describe_session()`` 的返回值；``None`` = 现查一次。

    【为什么键要同时带区服与角色】
    同一个平台账号在不同区服有不同的角色；只看 player_id 会让"换区之后再登录"
    被判成"今天已经做过了"，新角色一整天都不跑日常 —— 这比多跑一遍严重得多。

    【为什么没有会话时退回 "<no-session>" 而不是 None】
    未登录时不该记账（也没有东西可记），但调用方不必因此写一条分支：
    统一退到一个不可能与真实身份冲突的键上，写进去的记录在真正登录后
    也不会被读到。
    """
    info: Mapping[str, Any]
    if session is None:
        from services import runner

        try:
            info = runner.describe_session()
        except Exception as exc:  # noqa: BLE001
            # 【为什么不把异常放出去】这个函数会被 ``/api/status`` 的轮询反复调用
            # （1.2 秒一次）。会话文件被手工改坏之类的小问题不该让整个状态接口 500
            # —— 那会让界面直接失联，比"今天判重不准"严重得多。
            _LOGGER.warning("读取会话状态失败，本次按“未登录”处理记账：%s", exc)
            return "<no-session>"
    else:
        info = session

    server_id = int(info.get("server_id") or 0)
    player_id = int(info.get("player_id") or 0)
    if server_id <= 0 and player_id <= 0:
        return "<no-session>"
    return f"s={server_id}|p={player_id}"


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _path(path: Path | None = None) -> Path:
    """目标文件；``None`` 表示用 :data:`config.AUTO_DAILY_STATE_FILE`。"""
    return Path(path) if path is not None else config.AUTO_DAILY_STATE_FILE


def load(path: Path | None = None) -> dict[str, Any]:
    """读出整份记账（文件不存在 / 损坏 → 返回空结构）。

    【为什么损坏时返回空而不是抛异常】
    这份文件的价值是"少跑一遍日常"，丢了最坏结果是多跑一遍；
    而抛异常会让 ``/api/tasks`` 整个 500 —— 页面直接打不开。
    两者不对等，所以这里静默降级，只留一条 WARNING。
    """
    target = _path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": _FILE_VERSION, "targets": {}}
    except OSError as exc:
        _LOGGER.warning("读取自动任务记账失败（%s）：%s", target, exc)
        return {"version": _FILE_VERSION, "targets": {}}

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _LOGGER.warning("自动任务记账不是合法 JSON（%s）：%s", target, exc)
        return {"version": _FILE_VERSION, "targets": {}}

    if not isinstance(payload, dict) or payload.get("version") != _FILE_VERSION:
        return {"version": _FILE_VERSION, "targets": {}}
    targets = payload.get("targets")
    if not isinstance(targets, dict):
        return {"version": _FILE_VERSION, "targets": {}}
    return {"version": _FILE_VERSION, "targets": targets}


def save(payload: dict[str, Any], path: Path | None = None) -> None:
    """整份写回（由本模块内部在持锁状态下调用）。

    写失败同样只记日志：这台机器上没有写权限时，退化成"每次都跑"，
    总好过把整个 HTTP 请求打挂。
    """
    target = _path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _LOGGER.warning("写自动任务记账失败（%s）：%s", target, exc)


def clear(path: Path | None = None) -> None:
    """清空全部记账（测试与"想重跑一遍日常"时用）。"""
    with _lock:
        save({"version": _FILE_VERSION, "targets": {}}, path)


# ---------------------------------------------------------------------------
# 对外的两个动作
# ---------------------------------------------------------------------------
def completed_names(
    session: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> tuple[str, ...]:
    """本游戏日（本角色本区服）已经跑过的任务名。

    :param session: ``describe_session()`` 的结果；``None`` = 现查。
    :param now: 计算"今天"的基准（测试用；``None`` = 现在）。
    :param path: 记账文件（测试用；``None`` = 配置文件里的默认路径）。
    :return: 任务名元组；没有记录时为空元组。

    【为什么"游戏日"要后端算】
    游戏服务器是**凌晨 05:00** 换日，不是 00:00，也不是浏览器时区。
    口径统一由 :func:`models.daily_reset.game_day` 给出，
    与界面上"下次重置"用的是同一个函数 —— 不会出现两处各算一套、
    互相矛盾的情况。

    【跨日自动作废】
    记录里存着它自己的 ``game_day``；与本局的 game_day 不一致就当作没有 ——
    不需要定时任务去清理旧记录。
    """
    key = identity_key(session)
    game_day = daily_reset.game_day(now)
    with _lock:
        payload = load(path)
    entry = payload.get("targets", {}).get(key)
    if not isinstance(entry, dict):
        return ()
    if str(entry.get("game_day", "")) != game_day:
        return ()
    tasks = entry.get("tasks")
    if not isinstance(tasks, (list, tuple)):
        return ()
    return tuple(str(name) for name in tasks if str(name))


def mark_completed(
    names: Iterable[str],
    session: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> tuple[str, ...]:
    """把若干任务记成"本游戏日已跑过"。

    :return: 记账生效后，该角色本日的完整已跑清单（便于日志直接打印）。

    【为什么只增不减】
    同一个任务一天只会是"跑过"或"没跑过"两种状态，追加是幂等的；
    ``dict`` 去重后写回，重复调用不会把记录越撑越大。

    【为什么顺手做了"换日即覆盖"】
    本局 game_day 与记录里的不一致 ⇒ 这是**新的一天**，旧清单整体作废，
    只从这批名字重新开始 —— 旧日期的记录因此自然消失，
    不需要额外的清理逻辑（隐性 bug 的常见温床）。
    """
    key = identity_key(session)
    game_day = daily_reset.game_day(now)
    if key == "<no-session>":
        # 没有会话就不该记账：连"是谁"都不知道，写进去只会在真正登录后
        # 影响不到任何东西，却可能让排障的人误以为"已经跑过"。
        _LOGGER.debug("尚无游戏会话，跳过自动任务记账：%s", key)
        return ()
    incoming = tuple(str(name) for name in names if str(name))
    if not incoming:
        # 没给名字就纯读取，不写文件（写一次只为了更新时间戳毫无意义）
        return completed_names(session, now=now, path=path)
    with _lock:
        payload = load(path)
        targets = payload.setdefault("targets", {})
        entry = targets.get(key)
        previous: tuple[str, ...] = ()
        if isinstance(entry, dict) and str(entry.get("game_day", "")) == game_day:
            raw = entry.get("tasks")
            previous = tuple(str(name) for name in raw) if isinstance(raw, (list, tuple)) else ()
        merged = tuple(dict.fromkeys([*previous, *incoming]))
        targets[key] = {
            "game_day": game_day,
            "tasks": list(merged),
            # 只是给排障看的时间戳：它**不参与**任何判定（唯一判据是上面的
            # game_day），所以只写人话格式的本地时间。
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        payload["targets"] = targets
        save(payload, path)
    _LOGGER.debug(
        "自动任务记账[%s]：本游戏日已完成 %s", key, "、".join(merged) or "（无）"
    )
    return merged
