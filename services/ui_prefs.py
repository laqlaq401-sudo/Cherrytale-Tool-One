"""主动任务卡片"哪些是展开的"（界面偏好）的**服务端**持久化。

【为什么要这个模块（2026-10-16 用户需求）】
展开状态原来只存内存（``state.expanded``），刷新页面后一律折叠。当初的理由是
"免得上次点开的那张卡看起来像它还在执行/还在排队" —— 这条理由现在不成立了：
一张卡有没有在执行/排队，由按钮自己的「停止执行」红描边态**如实**表达，而且那个
态是从服务端派生的（见 ``webapi/state.py`` 的 ``status()`` 与前端 ``pollOnce``），
不再需要靠"卡片是不是展开的"去猜。于是它回归成一条普通的界面偏好：
跟 selection / 钻石总闸、宴席记账同一个病根（见 ``services/spend_state.py`` 的
模块文档）—— 凡是"必须跨重启记住"的界面状态，权威账本必须在服务端，
localStorage 只配当兜底（EXE 壳的 ``--user-data-dir`` 指向 ``tmp/``，一清理就全丢）。

【为什么**另开一份文件**，不并进 services/selection_state.py】
  ① 语义不同：selection 是**执行决策**（跑什么、怎么跑），这里只是**界面记忆**。
     工程里 daily / banquet / spend / selection / login_signin 各自独立一份，
     本条遵循同一范式（"一个关注点一个文件"）。
  ② 风险不同：``selection_state.load()`` 在版本号不符时会把**整份文件丢弃**。
     把新字段塞进那份文件、靠升版本来兼容，会让用户当前的**全部勾选与参数
     被清掉一次** —— 为一个界面记忆付这个代价不划算。

【白名单过滤】
读出与写入都只保留"``services/task_spec.py::TASK_SPECS`` 里确实存在、
``group == "manual"``（只有主动任务卡片能展开）、且未 ``hidden``"的任务名，
并按规格表的声明顺序去重。理由是防手改坏的文件，也防过期任务名长期堆积。
注意**不做版本升版**：``expanded`` 缺失时按空列表处理，所以老文件天然兼容。

【安全约束】文件里只有任务名与时间戳（不含任何凭据），已加入 ``.gitignore``。

【并发】与 spend_state / selection_state 相同：Web 服务单进程多线程，一把线程锁
保护，写是"整体重建 + 原子覆盖"式读改写。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Final, Iterable

import config
from services import task_spec

_LOGGER: Final[logging.Logger] = logging.getLogger("services.ui_prefs")

#: 文件格式版本。将来结构变了就升版，旧文件整体丢弃（用户重新展开一次即可恢复）。
_FILE_VERSION: Final[int] = 1

_lock: Final[threading.Lock] = threading.Lock()


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _path(path: Path | None = None) -> Path:
    """目标文件；``None`` 表示用 :data:`config.UI_PREFS_STATE_FILE`。"""
    return Path(path) if path is not None else config.UI_PREFS_STATE_FILE


def _default() -> dict[str, Any]:
    """一份"从未设置过"的状态：没有任何卡片展开。"""
    return {"version": _FILE_VERSION, "expanded": [], "updated_at": ""}


def _clean(raw: Any) -> list[str]:
    """把任意来源的 expanded 清洗成"只含已知的主动任务名"。

    【为什么读出时也要过一遍】
    与 selection_state 的 ``_clean_tasks`` 同理：用户拿编辑器手改一个
    不存在的任务名（或把已下线的任务留在里面）不该让它活过一次加载。
    返回顺序按规格表声明顺序，天然去重。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    wanted = {item for item in raw if isinstance(item, str) and item}
    if not wanted:
        return []
    kept = [
        name
        for name, spec in task_spec.TASK_SPECS.items()
        if name in wanted and spec.group == "manual" and not spec.hidden
    ]
    dropped = wanted - set(kept)
    if dropped:
        _LOGGER.debug("展开状态：丢弃无效任务名 %s", sorted(dropped))
    return kept


def load(path: Path | None = None) -> dict[str, Any]:
    """读出整份状态（文件不存在 / 损坏 → 返回"全部折叠"）。

    【为什么损坏时返回默认而不是抛异常】
    这份文件的价值只是"记住用户点开了哪几张卡"，丢了最坏结果是全部收起；
    而抛异常会让 ``GET /api/tasks`` 整个 500，页面直接打不开。两者不对等，
    所以静默降级，只留 WARNING（与 spend_state.load 同款取舍）。
    """
    target = _path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _default()
    except OSError as exc:
        _LOGGER.warning("读取展开状态失败（%s）：%s", target, exc)
        return _default()

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _LOGGER.warning("展开状态不是合法 JSON（%s）：%s", target, exc)
        return _default()

    if not isinstance(payload, dict) or payload.get("version") != _FILE_VERSION:
        return _default()
    return {
        "version": _FILE_VERSION,
        # ⚠️ 缺字段时按空列表处理（而不是当损坏）—— 这样将来加字段不必升版本，
        # 老文件也不会被整体丢弃。
        "expanded": _clean(payload.get("expanded", [])),
        "updated_at": str(payload.get("updated_at", "")),
    }


def save(payload: dict[str, Any], path: Path | None = None) -> None:
    """整份写回（由本模块内部在持锁状态下调用）。

    写失败同样只记日志：没有写权限时退化成"记不住展开状态"，
    每次启动全部收起 —— 总好过把保存接口 / 任务列表接口打挂。
    """
    target = _path(path)
    body = {
        "version": _FILE_VERSION,
        "expanded": _clean(payload.get("expanded", [])),
        "updated_at": str(payload.get("updated_at", "")),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _LOGGER.warning("写展开状态失败（%s）：%s", target, exc)


# ---------------------------------------------------------------------------
# 对外的两个动作
# ---------------------------------------------------------------------------
def expanded_tasks(path: Path | None = None) -> list[str]:
    """当前展开着的主动任务名（文件缺失 / 损坏一律空列表）。

    :param path: 状态文件（测试用；``None`` = 配置文件里的默认路径）。
    """
    with _lock:
        return list(load(path).get("expanded", []))


def describe(path: Path | None = None) -> dict[str, Any]:
    """给 ``GET /api/tasks`` 用的整份视图（与 ``selection_state.describe`` 同风格）。"""
    with _lock:
        return load(path)


def set_expanded(names: Iterable[str], path: Path | None = None) -> dict[str, Any]:
    """写入"展开着哪些卡片"并返回保存后的完整状态（给接口回显用）。

    :param names: 展开着的主动任务名（未知名 / 非主动任务会被白名单丢掉）。
    :param path: 状态文件（测试用；``None`` = 配置文件里的默认路径）。

    【为什么是整份覆盖而不是增量】
    前端提交的就是"当前的完整展开集合"（与 selection 的全量提交同款），
    整体重建天然规避"半新半旧"的中间态。
    """
    with _lock:
        payload = _default()
        payload["expanded"] = _clean(list(names))
        payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save(payload, path)
    _LOGGER.debug("展开状态已保存：%s", payload["expanded"])
    return payload
