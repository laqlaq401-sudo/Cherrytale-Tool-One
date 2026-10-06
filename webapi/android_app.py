"""安卓端 HTTP 服务兼容层：已完全合流并入 ``webapi/app.py``。

【为什么保留此文件】
1. 兼容历史编译产物及外部引用：旧版 Android APK、未更新的构建脚本或测试套件可能
   显式导入 ``webapi.android_app``；
2. 保持版本管理工具的稳定工作（如 ``tools/release.py`` 的统一对齐）；
3. 纯 Python 转发，不含任何重复逻辑，所有实现均直接来源于 ``webapi.app``。
"""

from __future__ import annotations

import sys
from typing import Any, Final, Sequence

import webapi.app as _app
#: 应用版本（与 webapi/app.py 保持同步，供 tools/release.py 统一管理）
APP_VERSION: Final[str] = "1.0.0"

from webapi.app import (
    DEFAULT_PORT,
    WEB_DIR,
    ApiError,
    CredentialsPayload,
    EnterServerPayload,
    LoginPayload,
    RunPayload,
    TaskPayload,
    WebApp,
    _mask_platform_id,
    _masked_session,
    _route,
    create_app,
    create_server,
    dump_all_thread_stacks,
    main as _app_main,
)

#: 历史兼容别名
AndroidApp = WebApp


def main(argv: Sequence[str] | None = None) -> int:
    """转发至统一的 webapi.app 入口。"""
    return _app_main(argv)


def __getattr__(name: str) -> Any:
    """透明转发模块级属性访问（如 runner, state, _WATCHDOG_STREAM 等）。"""
    return getattr(_app, name)


if __name__ == "__main__":
    sys.exit(main())
