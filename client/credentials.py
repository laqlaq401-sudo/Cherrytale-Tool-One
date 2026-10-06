"""平台**账号密码**的来源解析（不落盘、不进日志、不进异常）。

【为什么单独成一层】
"账号密码从哪来"原本散落在测试脚本里，而生产代码（``tasks/login.py``）需要它时
**不能去 import tests** —— 那是反过来的依赖。收到这里之后两边共用同一套规则：

    ① 环境变量 ``CHERRYTALE_LOGIN_ACCOUNT`` / ``CHERRYTALE_LOGIN_PASSWORD``（自动化）
    ② 凭据文件 ``.platform_credentials``（若存在；两行：账号、密码）

环境变量优先，与 ``client/session_store.py`` 的"环境变量 > 文件"风格一致。

.. warning::
   本模块**只负责取**，从不打印、从不写盘。密码一旦落到日志或异常信息里，
   就等于泄露（见 ``tests/test_auth_token.py`` 里"凭据不入日志"的回归用例）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final

import config

_LOGGER: Final[logging.Logger] = logging.getLogger("client.credentials")


def read_credentials_file(path: Path | None = None) -> tuple[str, str]:
    """从凭据文件读账号密码（第 1 行账号、第 2 行密码；``#`` 开头的行忽略）。

    :param path: 文件路径；``None`` 表示 ``config.PLATFORM_CREDENTIAL_FILE``。
    :return: ``(账号, 密码)``。
    :raises ValueError: 文件里凑不出两行有效内容时（错误信息里**不含**密码）。
    :raises OSError: 文件读不了时。

    支持 ``#`` 注释行，是为了让人能在文件里写"这是哪个号、什么时候加的"。
    """
    target = Path(path) if path is not None else config.PLATFORM_CREDENTIAL_FILE
    account = password = ""
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not account:
            account = stripped
        elif not password:
            password = stripped
            break
    if not account or not password:
        raise ValueError(f"凭据文件格式不对（需要两行：账号、密码）：{target}")
    return account, password


def credentials_from_env() -> tuple[str, str] | None:
    """从环境变量读账号密码；没配齐（缺任一项）时返回 ``None``。"""
    account = os.getenv(config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME, "").strip()
    password = os.getenv(config.PLATFORM_LOGIN_PASSWORD_ENV_NAME, "")
    return (account, password) if account and password else None


def resolve_credentials() -> tuple[str, str] | None:
    """按优先级取账号密码。

    :return: ``(账号, 密码)``；两处都没有时返回 ``None`` —— 由调用方决定报什么错
        （不同场景该说的话不一样：手动登录该提示输入，自动任务该提示选账号）。
    """
    from_env = credentials_from_env()
    if from_env is not None:
        return from_env
    if config.PLATFORM_CREDENTIAL_FILE.is_file():
        try:
            return read_credentials_file()
        except (OSError, ValueError) as exc:
            # 文件存在但读不了/格式不对：明确报出来，而不是"当作没有凭据"
            _LOGGER.warning("凭据文件不可用：%s", exc)
            return None
    return None


def describe_source() -> str:
    """当前凭据来自哪里（**只描述来源，不含任何凭据内容**），用于打印。"""
    if credentials_from_env() is not None:
        return f"环境变量 {config.PLATFORM_LOGIN_ACCOUNT_ENV_NAME}"
    if config.PLATFORM_CREDENTIAL_FILE.is_file():
        return f"凭据文件 {config.PLATFORM_CREDENTIAL_FILE}"
    return "（无）"
