"""钻石使用总闸（用户偏好）的**服务端**持久化。

【为什么要这个模块（2026-10-05 二次踩坑）】
总闸"开一次、重启就回关"的 bug 修过一次 —— 当时记在浏览器的
``localStorage`` 里（``cherrytale.allowDiamond``）。源码 + 普通浏览器下
看起来好了，打包 EXE 后立刻翻车：界面窗口走的是 Edge ``--app`` 回退方案
（pywebview 6.2.1 的 WinForms 后端在本机导入期就炸），它的
``--user-data-dir`` 指向 ``PROJECT_ROOT/tmp/gui-browser-profile`` ——
``tmp/`` 属过程产物目录，一清理存储全丢；换端口 / 桌面壳与浏览器各自一份
存储的问题也依然在。

这与 autoDone 判重 2026-10-04 搬服务端是**同一个病根**（见
``services/daily_state.py`` 的模块文档）：凡是"必须跨重启记住"的界面状态，
权威账本必须在服务端，localStorage 只配当兜底。

【这条状态的语义（用户 2026-10-05 定）】
"开关情况不重置：关了就关了，开了就开了" —— 它表达的是
"**这台机器允许这个工具花钻石**"，属用户偏好而非账号数据，
所以**全局一份**，不按账号 / 区服分桶（与宴席记账刻意不同）。

【与 config.ALLOW_DIAMOND_SPEND 的关系】
两者是**或**关系（合并发生在 ``services/runner.py`` 的 ``prepare()``）：
``config_local.py`` 里的静态授权照旧有效；本模块记录的只是网页端那个
开关的最新位置。文件缺失 / 损坏一律按 **False（关闭）** 解释 ——
fail-closed：存不了状态时退回"默认禁止"，永远不会因为丢了状态反而放行。

【并发】
Web 服务是单进程多线程：前端勾选变化的写与 ``/api/tasks`` 的读、
``start_run`` 的读可能同时发生。整份文件用一把线程锁保护，
写是"整体重建 + 原子替换"式的读改写（本文件只有一个字段，
整体重建比增量合并更不容易留下半新半旧的状态）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Final

import config

_LOGGER: Final[logging.Logger] = logging.getLogger("services.spend_state")

#: 文件格式版本。将来结构变了就升版，旧文件整体丢弃（用户重拨一次开关即可恢复）。
_FILE_VERSION: Final[int] = 1

_lock: Final[threading.Lock] = threading.Lock()


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _path(path: Path | None = None) -> Path:
    """目标文件；``None`` 表示用 :data:`config.SPEND_STATE_FILE`。"""
    return Path(path) if path is not None else config.SPEND_STATE_FILE


def _default() -> dict[str, Any]:
    """一份"从未设置过"的状态：总闸关闭（fail-closed 的默认值）。"""
    return {"version": _FILE_VERSION, "allow_diamond": False, "updated_at": ""}


def load(path: Path | None = None) -> dict[str, Any]:
    """读出整份状态（文件不存在 / 损坏 → 返回默认关闭）。

    【为什么损坏时返回默认而不是抛异常】
    这份文件的价值是"记住用户上次的开关位置"，丢了最坏结果是
    总闸回到默认关闭 —— 保守方向的退化；而抛异常会让 ``/api/tasks``
    整个 500，页面直接打不开。两者不对等，所以静默降级，只留 WARNING。
    """
    target = _path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _default()
    except OSError as exc:
        _LOGGER.warning("读取钻石总闸状态失败（%s）：%s", target, exc)
        return _default()

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _LOGGER.warning("钻石总闸状态不是合法 JSON（%s）：%s", target, exc)
        return _default()

    if not isinstance(payload, dict) or payload.get("version") != _FILE_VERSION:
        return _default()
    allow = payload.get("allow_diamond")
    if not isinstance(allow, bool):
        # 字段缺失或类型不对（手改坏的文件）：当"从未设置"处理。
        return _default()
    return {
        "version": _FILE_VERSION,
        "allow_diamond": allow,
        "updated_at": str(payload.get("updated_at", "")),
    }


def save(payload: dict[str, Any], path: Path | None = None) -> None:
    """整份写回（由本模块内部在持锁状态下调用）。

    写失败同样只记日志：没有写权限时退化成"总闸记不住位置"，
    每次启动回到默认关闭 —— 总好过把保存接口 / 任务列表接口打挂。
    """
    target = _path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _LOGGER.warning("写钻石总闸状态失败（%s）：%s", target, exc)


# ---------------------------------------------------------------------------
# 对外的两个动作
# ---------------------------------------------------------------------------
def allow_diamond(path: Path | None = None) -> bool:
    """总闸当前值；文件缺失 / 损坏一律 ``False``（默认禁止）。

    :param path: 状态文件（测试用；``None`` = 配置文件里的默认路径）。
    """
    with _lock:
        return bool(load(path).get("allow_diamond"))


def set_allow_diamond(value: bool, path: Path | None = None) -> dict[str, Any]:
    """写入总闸并返回保存后的完整状态（给接口回显用）。

    :param value: 新的开关位置（True = 允许花钻石）。
    :param path: 状态文件（测试用；``None`` = 配置文件里的默认路径）。

    【为什么整体重建而不是增量改字段】
    本文件只有一个布尔字段，整体重建天然规避"半新半旧"的中间态；
    时间戳只是排障线索，不参与任何判定。
    """
    with _lock:
        payload = _default()
        payload["allow_diamond"] = bool(value)
        payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save(payload, path)
    _LOGGER.debug("钻石总闸已保存：%s", "开" if payload["allow_diamond"] else "关")
    return payload
