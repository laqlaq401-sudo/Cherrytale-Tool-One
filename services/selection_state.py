"""任务勾选与参数（selection）的**服务端**持久化。

【为什么要这个模块（2026-10-05 用户需求）】
任务清单里的所有界面状态——自动任务的勾选、主动任务的参数
（扫荡次数 ``times``、推关的 ``max_stages``、市集开关、宴席邀请……）——
原来只存浏览器的 ``localStorage``（``cherrytale.selection.v2``）。打包 EXE 后
窗口走 Edge ``--app`` 回退方案，``--user-data-dir`` 指向 ``tmp/`` 下的
过程产物目录，一清理用户就发现"勾选和参数全回到默认了"。

这与 autoDone 判重（2026-10-04）、钻石总闸（2026-10-05）是**同一个病根**
（见 ``services/daily_state.py`` / ``services/spend_state.py`` 的模块文档）：
凡是"必须跨重启记住"的界面状态，权威账本必须在服务端，
localStorage 只配当兜底。

【作用域：全局一份（用户 2026-10-05 拍板）】
勾选与参数表达的是"这台机器上用户的使用习惯"，不是账号数据 ——
与钻石总闸一致取全局一份，不按账号/区服分桶。角色差异（例如关卡下拉
的候选范围）由前端既有的 reconcile + 候选对齐逻辑兜底，不在存储层区分。

【为什么没有 sync 标志（对比钻石总闸的 allow_diamond_synced）】
总闸参与**运行决策**（放不放行花钱），前后端值不一致有安全方向问题；
selection 只影响"下次打开页面时勾选框长什么样"，运行请求本身自带完整的
tasks/params —— 存坏/存旧顶多回到默认值，没有需要合并语义守护的方向。

【白名单过滤（写入口清洗）】
POST /api/selection 进来的任务名/参数键**只认规格表**
（``services/task_spec.py::TASK_SPECS``）：未知任务名整条丢弃、未知参数键
丢弃，各留一条 DEBUG。前端与规格漂移时不会把脏数据攒进状态文件；
值本身不做类型归一 —— 那是前端 ``reconcileSelection()`` 的既有职责
（按 ``ParamSpec.kind`` parse），服务端重复造一遍迟早两边不一致。

【并发】
与 spend_state 相同：Web 服务单进程多线程，一把线程锁保护，
写是"整体重建 + 原子覆盖"式读改写。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Final, Mapping

import config
from services import task_spec

_LOGGER: Final[logging.Logger] = logging.getLogger("services.selection_state")

#: 文件格式版本。将来结构变了就升版，旧文件整体丢弃（重新勾选一遍即可恢复）。
_FILE_VERSION: Final[int] = 1

_lock: Final[threading.Lock] = threading.Lock()


# ---------------------------------------------------------------------------
# 白名单清洗：任务名与参数键只认规格表
# ---------------------------------------------------------------------------
def _clean_tasks(raw: Any) -> dict[str, dict[str, Any]]:
    """把任意来源的 tasks 段清洗成"只含已知任务与已知参数键"的形状。

    【为什么读写两条路径都过这一遍】
    写入时过滤挡住前端漂移；读取时再过滤一次是防手改 ——
    用户拿编辑器把 ``times`` 拼成 ``tims`` 不该让它活过一次加载。
    """
    if not isinstance(raw, Mapping):
        return {}
    cleaned: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        spec = task_spec.TASK_SPECS.get(str(name))
        if spec is None:
            _LOGGER.debug("selection 保存：丢弃未知任务名 %r", name)
            continue
        if not isinstance(entry, Mapping):
            continue
        known = {param.name for param in spec.params}
        params: dict[str, Any] = {}
        raw_params = entry.get("params")
        if isinstance(raw_params, Mapping):
            for key, value in raw_params.items():
                if key in known:
                    params[str(key)] = value
                else:
                    _LOGGER.debug("selection 保存：丢弃未知参数键 %s.%r", name, key)
        cleaned[str(name)] = {"checked": bool(entry.get("checked")), "params": params}
    return cleaned


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _path(path: Path | None = None) -> Path:
    """目标文件；``None`` 表示用 :data:`config.SELECTION_STATE_FILE`。"""
    return Path(path) if path is not None else config.SELECTION_STATE_FILE


def _default() -> dict[str, Any]:
    """一份"从未设置过"的状态：全默认勾选、dry_run 未记录。"""
    return {
        "version": _FILE_VERSION,
        "updated_at": "",
        "dry_run": None,
        "tasks": {},
    }


def load(path: Path | None = None) -> dict[str, Any]:
    """读出整份状态（文件不存在 / 损坏 → 返回默认值）。

    【为什么损坏时返回默认而不是抛异常】
    这份文件的价值是"记住用户上次的勾选与参数"，丢了最坏结果是
    界面回到默认勾选 —— 无害方向的退化；而抛异常会让 ``/api/tasks``
    整个 500，页面直接打不开。两者不对等，所以静默降级，只留 WARNING。
    """
    target = _path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _default()
    except OSError as exc:
        _LOGGER.warning("读取任务勾选状态失败（%s）：%s", target, exc)
        return _default()

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _LOGGER.warning("任务勾选状态不是合法 JSON（%s）：%s", target, exc)
        return _default()

    if not isinstance(payload, dict) or payload.get("version") != _FILE_VERSION:
        return _default()

    dry_run = payload.get("dry_run")
    if not isinstance(dry_run, bool):
        dry_run = None  # 字段缺失或类型不对（含手改坏）：当"从未记录"处理
    return {
        "version": _FILE_VERSION,
        "updated_at": str(payload.get("updated_at", "")),
        "dry_run": dry_run,
        "tasks": _clean_tasks(payload.get("tasks")),
    }


def save(payload: dict[str, Any], path: Path | None = None) -> None:
    """整份写回（由本模块内部在持锁状态下调用）。

    写失败同样只记日志：没有写权限时退化成"界面记不住选择"，
    每次启动回到默认 —— 总好过把保存接口 / 任务列表接口打挂。
    """
    target = _path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _LOGGER.warning("写任务勾选状态失败（%s）：%s", target, exc)


# ---------------------------------------------------------------------------
# 对外的三个动作
# ---------------------------------------------------------------------------
def saved_tasks(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """上次保存的任务勾选/参数；从未保存或损坏一律 ``{}``。

    :param path: 状态文件（测试用；``None`` = 配置文件里的默认路径）。
    """
    with _lock:
        return load(path).get("tasks", {})


def saved_dry_run(path: Path | None = None) -> bool | None:
    """上次保存的演练开关；从未记录（或损坏）一律 ``None``。

    ``None`` 会被 ``/api/tasks`` 序列化成 ``null``，前端以此区分
    "服务端明确存了 false" 与 "服务端没记录"（后者走 localStorage 兜底）。
    """
    with _lock:
        return load(path).get("dry_run")


def describe(path: Path | None = None) -> dict[str, Any]:
    """一次读出供 ``/api/tasks`` 下发的全部字段。

    【为什么要带 ``saved_updated_at``】前端需要区分"服务端存过但为空"
    （用户上次确实什么都没勾，应当清空本地）与"服务端从未存过"
    （升级后首次启动，必须保留 localStorage 里既有的选择）。
    时间戳为空串 = 从未存过。
    """
    with _lock:
        payload = load(path)
    return {
        "saved_tasks": payload["tasks"],
        "saved_dry_run": payload["dry_run"],
        "saved_updated_at": payload["updated_at"],
    }


def save_selection(
    tasks: Mapping[str, Any],
    dry_run: bool,
    path: Path | None = None,
) -> dict[str, Any]:
    """清洗后整体写回，并返回保存后的完整状态（给接口回显用）。

    :param tasks: ``{任务名: {checked, params}}``（先经 :func:`_clean_tasks`）。
    :param dry_run: 演练开关（仅 ``?dev=1`` 开发者模式的界面偏好；
        运行时是否演练由前端 ``isDevMode()`` 硬闸决定，与本记录无关）。
    :param path: 状态文件（测试用；``None`` = 配置文件里的默认路径）。

    【为什么整体重建而不是增量改字段】
    前端每次提交的都是**全量** selection（saveSelection 序列化整个 Map），
    整体重建天然规避"半新半旧"的中间态；时间戳只是排障线索。
    """
    payload: dict[str, Any] = {
        "version": _FILE_VERSION,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": bool(dry_run),
        "tasks": _clean_tasks(tasks),
    }
    with _lock:
        save(payload, path)
    _LOGGER.info(
        "任务勾选状态已保存：%d 个任务，演练=%s",
        len(payload["tasks"]),
        "开" if payload["dry_run"] else "关",
    )
    return payload
