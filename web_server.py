"""Web 服务入口：把米娅小助手跑成一个网页（PC 与手机都能访问）。

用法::

    python web_server.py                              # 只服务本机（127.0.0.1:8765）
    python web_server.py --port 9000                   # 换端口
    python web_server.py --verbose                     # 打开 DEBUG 日志
    python web_server.py --host 0.0.0.0 --token 我的口令   # 手机访问（必须给令牌）

【为什么默认只绑 127.0.0.1】
这个页面能在真实账号上做真实操作（领奖励、扫荡、甚至花钻石）。
绑定 ``0.0.0.0`` 意味着同一个网络里任何人打开这个地址都能操作你的账号，
所以那一步必须由使用者显式执行 —— 而且本脚本会**强制**要求同时给出令牌。

【为什么这个文件这么薄】
路由、鉴权、状态、日志全在 ``webapi/`` 里。这里只做三件事：
读命令行参数 → 组装日志（控制台 + 内存日志流）→ 启动 uvicorn 并打印访问地址。
``main_gui.py``（PC 独立窗口）会用**同一个** ``create_app()``，只是换成开窗口。
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path
from typing import Final, Sequence

from webapi import app as webapp
from webapi import logstream

_LOGGER: Final[logging.Logger] = logging.getLogger("main")

#: 默认端口。选 8765 的原因：不与常见开发端口（8000/8080/3000）撞车，
#: 也不在需要管理员权限的范围内，手机上直接输 4 位数字也好记。
DEFAULT_PORT: Final[int] = 8765

#: 视为"仅本机"的监听地址（这些地址不需要令牌）。
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="web_server.py",
        description=(
            "米娅小助手（网页版）：与命令行共用同一份任务执行逻辑。"
            "默认只服务本机；要让手机访问，加 --host 0.0.0.0 并同时给出 --token。"
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址（默认 127.0.0.1，只允许本机访问；0.0.0.0 表示允许局域网访问）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"监听端口（默认 {DEFAULT_PORT}）",
    )
    parser.add_argument(
        "--token",
        default=None,
        metavar="口令",
        help=(
            "访问令牌：所有 /api/* 接口都要求带上它（页面右上角可填，或用 ?token=… 打开）。"
            "用 --host 暴露到局域网时**必须**给出"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出 DEBUG 级别日志（会包含每个请求的细节，排查问题时用）",
    )
    return parser


def lan_ip() -> str:
    """尽力找出本机在局域网里的 IP；找不到时返回空串。

    【为什么不用 ``socket.gethostbyname(socket.gethostname())``】
    在多网卡 / 装了 VPN / 有虚拟网卡（WSL、Docker）的机器上，它经常返回
    ``127.0.1.1`` 这类**没有用**的地址，打印出来的手机地址自然打不开。
    这里用"UDP 连一个外网地址但不真的发包"的办法，让操作系统直接挑出
    **默认出口网卡**的地址 —— 这是同类小工具里最可靠的做法，且零流量。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:  # 完全没有网络时也会走到这里
        return ""
    finally:
        sock.close()


def describe_urls(host: str, port: int, token: str | None) -> list[str]:
    """生成"该用什么地址打开"的说明行（给使用者看，不涉及任何敏感信息）。"""
    lines = [f"  本机：http://127.0.0.1:{port}/"]
    if host in LOOPBACK_HOSTS:
        lines.append("  （当前只允许本机访问；要让手机访问请加 --host 0.0.0.0 并同时给出 --token）")
        return lines

    address = lan_ip() or "<本机局域网 IP>"
    suffix = f"?token={token}" if token else ""
    lines.append(f"  手机（需同一 WiFi）：http://{address}:{port}/{suffix}")
    if token:
        lines.append("  令牌：已启用 —— 页面里填一次即可（存在浏览器会话里，关掉标签页就没了）")
    else:
        lines.append("  ⚠ 未设置令牌，但已经暴露到局域网：任何人打开这个地址都能操作你的账号")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """程序主入口。

    :param argv: 命令行参数（``None`` 表示取 ``sys.argv[1:]``）。
    :return: 进程退出码（``0`` 正常结束 / ``2`` 参数有问题 / ``1`` 端口被占用等）。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # 复用命令行入口里的两个工具函数：
    #   ① force_utf8_output —— Windows 管道下的中文乱码问题（见 main.py 的说明）；
    #   ② setup_logging    —— 日志格式与级别只有一份定义，两个入口观感一致。
    from main import force_utf8_output, setup_logging

    force_utf8_output()
    setup_logging(args.verbose)

    if args.host not in LOOPBACK_HOSTS and not args.token:
        print(
            "✘ 拒绝启动：--host 已经暴露到局域网，但没有给 --token。\n"
            "  这个服务能在你的账号上做真实操作；没有令牌等于同网人人可用。\n"
            "  正确用法：python web_server.py --host 0.0.0.0 --token 自定义口令"
        )
        return 2

    # 日志分两路：控制台（basicConfig 已配好）+ 内存日志流（给网页看）
    stream = logstream.LogStream()
    stream_handler = logstream.attach_to_root(stream, level=logging.INFO)
    # print 只在 worker 线程里被捕获（见 webapi/logstream.py 的说明）
    bridge = logstream.install_stdout_bridge(stream)

    try:
        server, application = webapp.create_server(
            host=args.host,
            port=args.port,
            token=args.token,
            stream=stream,
            bridge=bridge,
        )
    except OSError as exc:
        print(f"✘ 启动失败：{exc}\n  换一个端口试试：python web_server.py --port {args.port + 1}")
        return 1

    print("米娅小助手已启动（与命令行共用同一套任务逻辑）：")
    for line in describe_urls(args.host, args.port, args.token):
        print(line)
    print("  停止：本终端按 Ctrl+C（VS Code 里也可以执行 Terminal: Kill All Terminals）")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:  # 手动 Ctrl+C 属于正常结束
        print("已停止。")
    finally:
        server.shutdown()
        server.server_close()
        logstream.uninstall_stdout_bridge(bridge)
        logstream.detach(stream_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

