"""pytest 全局配置钩子（**只做一件事：让测试输出不再乱码**）。

【为什么需要这个文件】
Windows 下 Python 输出到管道时默认用 cp936（GBK）。pytest 的终端输出
（skip 原因、断言失败信息、`assert` 里的中文）走的正是这条路径，
于是你会看到：

    SKIPPED: Ĭ□ϲ□□□□□□□ʵ□□□□□□□�□□Ҫʱ□□ CHERRYTALE_RUN_NETWORK_TESTS=1

这不是测试坏了，而是**编码问题**：排查协议时断言消息往往含中文
（例如"区服列表（共 167 个）"），乱码会让你无法判断到底哪里不符。

`main.py` 里已经用同样的办法处理过命令行输出（见 ``force_utf8_output``），
这里把它补到 pytest 侧。``reconfigure`` 修改的是**同一个流对象**的编码，
因此 pytest 已经建好的终端写入器也会跟着变 —— 不需要改 pytest 的任何配置。
"""

from __future__ import annotations

import sys


def pytest_configure(config: object) -> None:
    """在 pytest 建立终端写入器之后、收集用例之前，把输出流改成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
