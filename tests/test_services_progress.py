"""``services/progress.py``（逐场进度通道）与网页日志流的对接测试。

【为什么值得单独钉住（★ 2026-10-18 用户需求）】
"打完一场就输出一场"的整条链路是：

    tasks/*.py  →  BaseTask.progress()  →  logging（级别 25 = PROGRESS）
                →  webapi.logstream.LogStreamHandler  →  /api/logs
                →  app.js appendLogLine（level === "PROGRESS" → 「运行结果」）

这条链上有三个容易悄悄断掉的接点：级别名没注册（变成 ``Level 25``）、
handler 的级别门槛把它挡掉、前缀过滤器拒绝 ``tasks.*``。前两个在这里断言，
第三个由 ``CAPTURED_LOGGER_PREFIXES`` 的既有测试覆盖。
"""

from __future__ import annotations

import logging

import pytest

from services import progress
from webapi import logstream


def test_level_is_registered_and_named() -> None:
    """级别号与名字必须稳定 —— 网页是按**名字**路由的。"""
    assert progress.PROGRESS_LEVEL == 25
    assert logging.getLevelName(progress.PROGRESS_LEVEL) == "PROGRESS"


def test_progress_passes_info_level_handler_threshold() -> None:
    """它必须高于 INFO（20），否则根日志器调到 INFO 时整条通道就没了。"""
    assert progress.PROGRESS_LEVEL > logging.INFO
    # 低于 WARNING：它不是告警，不能落进"只取 WARNING/ERROR"的分支
    assert progress.PROGRESS_LEVEL < logging.WARNING


def test_handler_captures_progress_with_level_name() -> None:
    """``LogStreamHandler`` 要把级别名原样交给前端（app.js 据此判断）。"""
    stream = logstream.LogStream(capacity=10)
    handler = logstream.LogStreamHandler(stream)
    logger = logging.getLogger("tasks.test_progress")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        progress.report(logger, "第 %d 场：胜", 1)
    finally:
        logger.removeHandler(handler)

    lines, _ = stream.since(0)
    assert len(lines) == 1
    assert lines[0].level == "PROGRESS"
    assert lines[0].message == "第 1 场：胜"


def test_base_task_progress_uses_same_channel() -> None:
    """``BaseTask.progress`` 是任务侧唯一入口，级别必须与常量一致。"""
    from tasks.base import BaseTask

    stream = logstream.LogStream(capacity=10)
    handler = logstream.LogStreamHandler(stream)

    class _Task(BaseTask):  # type: ignore[misc]
        name = "probe_progress"
        requires_auth = False

        def execute(self):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    task = _Task.__new__(_Task)
    logger = logging.getLogger("tasks.test_progress_base")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        task.logger = logger  # type: ignore[assignment]
        task.progress("第 %d 关：通关", 2)
    finally:
        logger.removeHandler(handler)

    lines, _ = stream.since(0)
    assert [line.level for line in lines] == ["PROGRESS"]
    assert lines[0].message == "第 2 关：通关"


def test_progress_is_ignored_by_low_level_handler() -> None:
    """handler 被设成 WARNING(30) 时进度不该出现（INFO+ 的通性，钉住别写错级别）。"""
    stream = logstream.LogStream(capacity=10)
    handler = logstream.LogStreamHandler(stream, level=logging.WARNING)
    logger = logging.getLogger("tasks.test_progress_low")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        progress.report(logger, "不该出现")
    finally:
        logger.removeHandler(handler)

    lines, _ = stream.since(0)
    assert lines == []
