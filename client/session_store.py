"""会话状态的跨进程存取：会话文件读写 + 三条来源的优先级。

【为什么需要这一层】
``python main.py run <任务>`` **每次都是独立进程**：登录拿到的令牌活在内存里，
进程一退就没了。所以"先登录、再跑日常"必须有个地方把令牌递过去。
本项目支持三条来源，优先级与 ``config.resolve_auth_token()`` 保持一致：

======================================  ==========================================
优先级                                   来源
======================================  ==========================================
1（最高）                               环境变量 ``CHERRYTALE_SESSION_TOKEN``
                                        —— 临时切账号、或只想跑一次时用
2                                       ``config_local.py`` 里的 ``GAME_SESSION_TOKEN``
                                        —— 想长期固定、又不愿磁盘上有明文文件时用
3                                       **会话文件** ``.session.json``
                                        —— ``run login`` 自动写入，日常最省事
======================================  ==========================================

【为什么会话文件用 JSON 而不是纯文本】
它要存的不止令牌：``playerId`` / 区服 / 取得时间都要一起带过去
（少了 playerId，业务请求会被当成"没选定角色"而拒绝，而错误信息只会说"包异常"）。
JSON 还带 ``version`` 字段，将来结构变了可以判断"旧文件要不要重登"，不必靠猜。

.. warning::
   文件里**含令牌明文**，已在 ``.gitignore`` 里排除。凭据不进版本库、不进日志：
   本模块只把令牌写进文件，日志里一律用 ``GameSessionState.describe()``（已打码）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Final

import config
from client.exceptions import ProtocolError
from models.session_state import SESSION_JSON_VERSION, GameSessionState

_LOGGER: Final[logging.Logger] = logging.getLogger("client.session_store")


def session_file_path() -> Path:
    """会话文件路径：环境变量 ``CHERRYTALE_SESSION_FILE`` > ``config.SESSION_FILE``。

    :return: 会话文件的绝对/相对路径（不保证文件存在）。

    支持覆盖是为了让"多账号"或"临时换目录"不必改代码 —— 与抓包目录
    （``CHERRYTALE_CAPTURE_DIR``）的处理方式保持一致。
    """
    override = os.getenv(config.SESSION_FILE_ENV_NAME, "").strip()
    if override:
        return Path(override).expanduser()
    return config.SESSION_FILE


def save_session_state(
    state: GameSessionState, *, path: Path | str | None = None
) -> Path:
    """把会话状态写进会话文件（**含令牌明文**）。

    :param state: 登录成功后构造的状态。
    :param path: 目标路径；``None`` 表示用 :func:`session_file_path`。
    :return: 实际写入的路径。

    .. note::
       这里**不做** ``chmod 600`` 之类的权限处理：Windows 上语义不同，
       而且让人误以为"设了权限就安全"反而更松。真正该做的是：别提交它
       （已进 ``.gitignore``）、别把它的内容贴到任何地方。
    """
    target = Path(path) if path is not None else session_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
    target.write_text(payload + "\n", encoding="utf-8")
    _LOGGER.debug("会话状态已写入 %s（%s）", target, state.describe())
    return target


def load_session_state(*, path: Path | str | None = None) -> GameSessionState | None:
    """从会话文件读回会话状态。

    :param path: 源文件路径；``None`` 表示用 :func:`session_file_path`。
    :return: 可用的状态；**文件不存在、或文件里没有可用令牌**时返回 ``None``。
    :raises ProtocolError: 文件存在但读不了、或不是合法 JSON 时。

    【为什么"文件坏了"要报错，而"文件里没令牌"只是返回 None】
    前者是**异常情况**（磁盘问题、手工编辑坏了），必须让人知道；
    后者是**正常状态**（比如上次登录失败只写了半截，或用户新建了空文件），
    完全可以走"没登录过"的常规路径。
    """
    target = Path(path) if path is not None else session_file_path()
    if not target.is_file():
        return None

    try:
        data: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(
            f"会话文件读取失败：{target}\n"
            f"  原因：{exc}\n"
            f"  处理：直接删掉这个文件，再跑一次 `python main.py run login` "
            f"（它是缓存，不要手工修）"
        ) from exc

    if not isinstance(data, dict):
        raise ProtocolError(
            f"会话文件内容不是 JSON 对象：{target}（顶层是 {type(data).__name__}）\n"
            f"  处理：删掉它重新登录"
        )

    version = data.get("version")
    if version is not None and version != SESSION_JSON_VERSION:
        _LOGGER.debug(
            "会话文件结构版本为 %s（当前 %s），按宽容策略继续解析",
            version,
            SESSION_JSON_VERSION,
        )

    state = GameSessionState.from_dict(data)
    if state.time_stamp_token in (0, -1):
        # 旧版会话文件（或服务端未下发）时给一条**可执行**的提示：
        # 实测战斗类请求不带 timeStampToken 会被前置层拦成
        # `{"response_code":"OK"}`（text/html），现象是"读得到状态、打不了架"。
        _LOGGER.warning(
            "会话文件里没有 timeStampToken（旧版结构或服务端未下发）："
            "读类请求可用，但**战斗类请求会被拒绝**。"
            "重新登录一次即可写入：python main.py run login"
        )

    if not state.is_valid():
        _LOGGER.warning(
            "会话文件里没有可用令牌（%s）：%s；如需重新登录请跑 `python main.py run login`",
            target,
            state.describe(),
        )
        return None
    return state


def clear_session_state(*, path: Path | str | None = None) -> bool:
    """删除会话文件（退出登录 / 换账号时用）。

    :param path: 目标路径；``None`` 表示用 :func:`session_file_path`。
    :return: 真的删掉了返回 ``True``；文件本来就不存在返回 ``False``。
    """
    target = Path(path) if path is not None else session_file_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    _LOGGER.info("已删除会话文件：%s", target)
    return True


# ---------------------------------------------------------------------------
# 三条来源的优先级（环境变量 > config_local.py > 会话文件）
# ---------------------------------------------------------------------------
def resolve_session_state() -> tuple[GameSessionState | None, str]:
    """按优先级取当前可用的会话状态。

    :return: ``(状态, 来源描述)``；找不到时是 ``(None, "（无来源）")``。
        来源描述是给**人看的**（会打印到终端），所以用中文短语而不是枚举。

    .. note::
       这里**只读不写**：它不负责登录，也不负责修文件。找不到就返回 ``None``，
       由调用方决定提示什么 —— 保持"读取"这一件事足够简单。
    """
    state = _from_environment()
    if state is not None:
        return state, f"环境变量 {config.SESSION_TOKEN_ENV_NAME}"

    state = _from_config_local()
    if state is not None:
        return state, "config_local.py 的 GAME_SESSION_TOKEN"

    state = load_session_state()
    if state is not None:
        return state, f"会话文件 {session_file_path()}"

    return None, "（无来源）"


def _from_environment() -> GameSessionState | None:
    """从环境变量读会话（只给得出令牌与可选的 playerId）。"""
    token = config.SESSION_TOKEN
    if not token:
        return None
    return GameSessionState(
        token=token,
        player_id=_env_int(config.GAME_PLAYER_ID_ENV_NAME),
    )


def _from_config_local() -> GameSessionState | None:
    """从可选的 ``config_local.py`` 读会话。

    依次认 ``GAME_SESSION_TOKEN`` 与 ``SESSION_TOKEN`` 两个名字：
    后者是历史写法（早期文档里就是这么叫人配的），保留以免旧配置失效。
    读不到就返回 ``None``（该文件本来就可以不存在）。
    """
    try:
        import config_local  # type: ignore[import-not-found]
    except ImportError:
        return None

    token = str(
        getattr(config_local, "GAME_SESSION_TOKEN", "")
        or getattr(config_local, "SESSION_TOKEN", "")
        or ""
    ).strip()
    if not token:
        return None
    return GameSessionState(
        token=token,
        player_id=int(getattr(config_local, "GAME_SESSION_PLAYER_ID", 0) or 0),
    )


def _env_int(name: str) -> int:
    """读一个整数型环境变量；没有或写坏了都返回 0。

    :param name: 环境变量名。

    这里刻意**不报错**：playerId 缺失只是少了点便利（信封里不带字段 8），
    而为此让整个进程启动失败，代价过大。
    """
    raw = os.getenv(name, "").strip()
    try:
        return int(raw)
    except ValueError:
        _LOGGER.debug("环境变量 %s 不是整数（%r），按 0 处理", name, raw)
        return 0
