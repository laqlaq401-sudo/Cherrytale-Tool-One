#!/usr/bin/env python3
"""通用战斗签名与令牌计算工具（向后兼容入口）。

算法实现已归入运行时模块 :mod:`crypto.battle_signer`。
本文件保留作为 CLI 命令与开发期离线计算的入口。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 保证从 tools/ 单独执行时能正确定位到项目根
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from crypto.battle_signer import (  # noqa: E402
    BATTLE_VERIFY_SALT,
    calc_arena_token,
    calc_fight_token,
    calc_general_fight_token,
    calc_yimo_token,
    make_empty_server_verify,
    main,
)

__all__ = [
    "BATTLE_VERIFY_SALT",
    "calc_arena_token",
    "calc_fight_token",
    "calc_general_fight_token",
    "calc_yimo_token",
    "make_empty_server_verify",
    "main",
]

if __name__ == "__main__":
    sys.exit(main())
