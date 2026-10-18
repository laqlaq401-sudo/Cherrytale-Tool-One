"""「逐场 / 逐关进度」的专用日志级别（``PROGRESS``）。

【它解决什么问题（★ 2026-10-18 用户需求）】
用户的原话是"打完一场就输出一场，最后再总结"。网页的「运行结果」流水只吃两类
东西：每个任务**完成后**的一行步骤结果，以及 WARNING / ERROR 日志
（见 ``web/assets/app.js`` 的 ``appendLogLine`` / ``renderRunResults``）。
任务里那些逐场的 ``logger.info`` 因此只能躺在开发者日志里，界面上要等整批
跑完才蹦出一行汇总 —— 表现正是"全部执行完再输出"。

于是给"逐场进度"一个**独立级别**：

- 高于 ``INFO``（20）：根日志器被调到 INFO 时照样能出来；
- 低于 ``WARNING``（30）：进度不是告警，不能被当成 WARNING 染色，
  也不能落进"只取 WARNING/ERROR"那条分支；
- 名字固定为 ``PROGRESS``：网页只把这一条通道接进「运行结果」，
  不会把登录 / 查询之类的 INFO 噪声一起搬进去。

【为什么在这里 ``addLevelName``】
``logging`` 对未注册的数字级别会打印成 ``Level 25``；注册成 ``PROGRESS`` 之后
命令行与日志文件里都能一眼认出来，网页端也只需按名字判断
（``line.level === "PROGRESS"``），不必到处写魔法数字。

【谁负责显示】
- CLI / GUI：``config.LOG_FORMAT`` 里的 ``%(levelname)s`` 会打成 ``PROGRESS``；
- Web：``webapi/logstream.py`` 的 handler 原样收下级别名，
  ``app.js`` 把它渲染成「运行结果」里的一行 ⚔。
"""

from __future__ import annotations

import logging
from typing import Any, Final

#: 逐场进度的日志级别（INFO 与 WARNING 之间）。
PROGRESS_LEVEL: Final[int] = 25

#: 级别名（网页按这个名字路由，改名会同时影响前端）。
PROGRESS_LEVEL_NAME: Final[str] = "PROGRESS"

# 注册一次即可：重复调用是幂等的（只是覆盖同一映射）。
logging.addLevelName(PROGRESS_LEVEL, PROGRESS_LEVEL_NAME)


def report(logger: logging.Logger, message: str, *args: Any) -> None:
    """以 ``PROGRESS`` 级别记一条"这一场/这一关打完了"的行。

    :param logger: 任务自己的 logger（``self.logger``）。
    :param message: 支持 ``%`` 占位符的文案（与 ``logger.info`` 用法一致）。
    :param args: 占位符实参。

    【为什么不用 ``logger.info`` + 前缀标记】
    前缀标记要靠前端做字符串匹配，改一句文案就失效；级别是结构化信息，
    前端只需判断一个字段，任务侧也不必有"别写错前缀"的隐性约定。
    """
    logger.log(PROGRESS_LEVEL, message, *args)
