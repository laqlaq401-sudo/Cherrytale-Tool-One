"""命令行入口：环境体检、任务列表、任务执行。

【为什么要有这个文件，而不是让你到处敲 python -c】
早期排查问题时，人很容易随手敲一句 ``python -c "..."`` 去验证环境，
过两周后完全想不起来当时验证了什么、结论是什么。
把它固化成命令后，任何人（包括未来的你）都能用一条命令复现结论：

    python main.py selftest     # 离线自检：依赖 / 素材 / 加密向量 / 任务注册
    python main.py tasks        # 列出所有已注册任务
    python main.py run login    # 执行指定任务

``selftest`` 有一个重要特性：**它完全不联网**。
即使域名还没确认，它也能跑，用来快速判断「是我代码写错了，还是网络/协议的问题」。
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING, Sequence

import config

if TYPE_CHECKING:  # 仅用于类型检查；运行时不导入，避免多余依赖
    from tasks.base import BaseTask

_LOGGER = logging.getLogger("main")


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def force_utf8_output() -> None:
    """把标准输出/错误流强制改为 UTF-8 编码。

    【为什么需要这一步】
    Windows 的默认编码是 cp936（GBK）。当 Python 的输出被重定向到管道时，
    它会用 GBK 编码写出中文字节，而 Git Bash / Windows Terminal 按 UTF-8 解析，
    结果就是满屏乱码（例如「配置表目录」显示成 ``□□□ñ□Ŀ¼``）。

    本项目几乎全部输出都是中文，因此在入口处统一改掉这个默认行为：
    无论输出到终端、管道还是文件，都统一用 UTF-8。

    ``errors="replace"`` 是保险：万一某个字符真的编不出来，
    替换成占位符也比直接抛 UnicodeEncodeError 中断整个程序要好。

    注意：``reconfigure`` 是 Python 3.7+ 的流对象方法。某些被替换过的流
    （例如测试框架的捕获对象）可能没有它，因此这里做能力检测。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def setup_logging(verbose: bool) -> None:
    """配置根日志器。

    :param verbose: 是否输出 DEBUG 级别日志（等价于 ``--verbose``）。
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=config.LOG_FORMAT,
        datefmt=config.LOG_DATE_FORMAT,
    )


# ---------------------------------------------------------------------------
# selftest：离线自检
# ---------------------------------------------------------------------------
def check_python_version() -> tuple[bool, str]:
    """检查解释器版本。本项目按 Python 3.10+ 的语法编写（``X | Y`` 类型注解）。"""
    if sys.version_info >= (3, 10):
        return True, f"Python {sys.version.split()[0]}（{sys.executable}）"
    return False, f"Python {sys.version.split()[0]} 过低，需要 3.10 或更高"


def check_dependencies() -> tuple[bool, str]:
    """检查关键第三方依赖是否安装。"""
    required = {
        "requests": "HTTP 通信",
        "pydantic": "协议模型",
        "cryptography": "加密原语",
    }
    missing: list[str] = []
    versions: list[str] = []

    for module_name, purpose in required.items():
        try:
            module = __import__(module_name)
        except ImportError:
            missing.append(f"{module_name}（{purpose}）")
            continue
        version = getattr(module, "__version__", None) or getattr(module, "VERSION", "?")
        versions.append(f"{module_name} {version}")

    if missing:
        return False, "缺少依赖：" + "、".join(missing) + "；请运行 pip install -r requirements.txt"
    return True, "、".join(versions)


def check_crypto() -> tuple[bool, str]:
    """用**标准测试向量**验证加密原语可用。

    为什么自检要做这件事？因为加密算错一位照样能「跑通」，
    只有和权威向量比对才能真正说明实现是对的。
    """
    from crypto.aes import aes_ecb_decrypt, aes_ecb_encrypt
    from crypto.hash import md5_hex

    expected_md5 = "900150983cd24fb0d6963f7d28e17f72"
    actual_md5 = md5_hex("abc")
    if actual_md5 != expected_md5:
        return False, f"MD5 向量不符：期望 {expected_md5}，实际 {actual_md5}"

    key = bytes(range(16))
    plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
    expected_cipher = "69c4e0d86a7b0430d8cdb78070b4c55a"

    cipher = aes_ecb_encrypt(plaintext, key, use_padding=False)
    if cipher.hex() != expected_cipher:
        return False, f"AES 向量不符：期望 {expected_cipher}，实际 {cipher.hex()}"
    if aes_ecb_decrypt(cipher, key, use_padding=False) != plaintext:
        return False, "AES 解密未能还原明文"

    return True, "MD5 与 AES(ECB, 128bit) 标准向量全部匹配"


def check_materials() -> tuple[bool, str]:
    """检查 dump.cs 等逆向素材是否就位。"""
    status = config.material_status()
    missing = [name for name, exists in status.items() if not exists]

    if missing:
        return False, (
            f"未找到：{'、'.join(missing)}；当前素材根目录为 {config.MATERIAL_ROOT}\n"
            "     提示：可用环境变量 CHERRYTALE_MATERIAL_ROOT 指向素材所在目录"
        )

    dump_size_mb = config.DUMP_CS_PATH.stat().st_size / (1024 * 1024)
    return True, f"dump.cs（{dump_size_mb:.1f} MB）、DummyDll、TextAsset 均在位"


def check_game_gateway() -> tuple[bool, str]:
    """检查游戏网关配置是否就绪（**离线检查，不发请求**）。

    这里只确认「常量填了、格式看起来对」，不验证连通性 ——
    自检必须能离线跑。真正的连通性验证交给 ``python main.py run probe``。
    """
    url = config.GAME_GATEWAY_URL
    if not url:
        return False, "未配置 CHERRYTALE_GATEWAY_URL"
    if not url.startswith(("http://", "https://")):
        return False, f"地址格式异常：{url}（应以 http:// 或 https:// 开头）"
    return True, (
        f"{url}（Content-Type: {config.GAME_CONTENT_TYPE}，"
        f"客户端版本 {config.GAME_CLIENT_VERSION}）"
    )


def check_generated_tables() -> tuple[bool, str]:
    """检查「由 dump.cs 生成的表」是否与素材保持同步。

    【为什么自检要管这件事】
    ``models/packet_ids.py`` 与 ``models/const_ids.py`` 都是**生成**出来的，
    它们与 dump.cs 之间没有任何强制约束：游戏更新后重导了 dump.cs，
    但没人重新跑生成脚本，表就会**悄悄过期** —— 查到的消息号不再对应真实协议。

    这种错误的症状是「服务端收到一个它不认识的包」，既没有异常堆栈，
    也没有明确的错误码。把它固化成一次「重新生成 + 对比」就能提前发现。

    代价：本检查要读两遍 33 MB 的 dump.cs（约 2~4 秒），
    这是自检里最慢的一项，但换来的是「表永不过期」。
    """
    from models import const_ids, packet_ids
    from tools.extract_packet_ids import extract_consts, extract_packet_ids

    if not config.DUMP_CS_PATH.is_file():
        return False, f"找不到 {config.DUMP_CS_PATH}，无法校验生成表"

    entries = dict(extract_packet_ids(config.DUMP_CS_PATH))
    if entries != packet_ids.PACKET_IDS:
        return False, (
            "models/packet_ids.py 已过期（与 dump.cs 不一致）\n"
            "     修复：python tools/extract_packet_ids.py"
        )

    const_rows, _skipped = extract_consts(config.DUMP_CS_PATH)
    const_entries = dict(const_rows)
    if const_entries != const_ids.CONSTS:
        return False, (
            "models/const_ids.py 已过期（与 dump.cs 不一致）\n"
            "     修复：python tools/extract_packet_ids.py --target consts"
        )

    from models import proto_fields
    from tools.extract_proto_fields import FOCUS_CLASSES, collect_fields

    report = collect_fields(config.DUMP_CS_PATH, list(FOCUS_CLASSES))
    if report.fields != dict(proto_fields.PROTO_FIELDS):
        return False, (
            "models/proto_fields.py 已过期（与 dump.cs 不一致）\n"
            "     修复：python tools/extract_proto_fields.py"
        )
    if report.parse_failures:
        return False, (
            f"字段解析异常：{', '.join(report.parse_failures)} 有 ProtoMember 却没解析出字段\n"
            "     请检查 tools/extract_proto_fields.py 的解析规则是否还匹配 dump.cs 的写法"
        )

    return True, (
        f"消息号 {len(entries)} 条、常量 {len(const_entries)} 条、"
        f"报文字段 {report.field_count} 个（{len(report.fields)} 个类），均与 dump.cs 一致"
    )


def check_config() -> tuple[bool, str]:
    """检查配置完整性。

    平台网关（sadpki-portal-v2）早已确认；**游戏网关**由
    :func:`check_game_gateway` 单独检查，所以这里只报平台侧的状态。
    """
    if config.API_HOST:
        return True, f"服务端 {config.api_base_url()}"

    return True, f"平台网关 {config.PLATFORM_BASE_URL}（游戏网关见下一项）"


def check_tls_trust_store() -> tuple[bool, str]:
    """检查 TLS 是否已交给操作系统信任库管理。

    为什么自检要管这件事？因为「Python 报证书错误、浏览器却正常」这种情况
    极易被误判成网络故障。把结论直接摆在自检输出里，能省掉大量排查时间 ——
    本次实测就正是栽在这个坑上（详见 client/tls.py 的排查记录）。
    """
    from client.tls import is_using_system_trust_store, use_system_trust_store

    if not config.USE_SYSTEM_TRUST_STORE:
        return True, "已按配置关闭（退回 certifi 信任库）"

    if use_system_trust_store() and is_using_system_trust_store():
        return True, "已启用操作系统信任库（truststore）"

    return False, (
        "未启用：缺少 truststore 依赖。遇到证书链问题时会报 "
        "'unable to get local issuer certificate'；"
        "请执行 pip install -r requirements.txt"
    )


def check_tasks() -> tuple[bool, str]:
    """检查任务注册表是否可用。"""
    registry = load_builtin_tasks()
    if not registry:
        return False, "没有任何任务被注册，请检查 tasks/ 下的模块导入"
    return True, "已注册任务：" + "、".join(sorted(registry))


# ---------------------------------------------------------------------------
# 任务加载
# ---------------------------------------------------------------------------
def load_builtin_tasks() -> dict[str, type[BaseTask]]:
    """导入内置任务模块（触发注册）并返回注册表副本。

    **实现已搬到 ``services/runner.py``**：Web 入口需要同一份导入清单，
    而"注册是导入时的副作用"这件事必须只写一遍（见该模块里 ``load_builtin_tasks``
    的说明）。这里保留同名函数，是为了让现有调用点与测试一个字都不用改。
    """
    from services.runner import load_builtin_tasks as _load_builtin_tasks

    return _load_builtin_tasks()


# ---------------------------------------------------------------------------
# selftest 命令
# ---------------------------------------------------------------------------
def cmd_selftest(_args: argparse.Namespace) -> int:
    """执行全部离线检查并打印结果。

    :return: 全部通过返回 ``0``，否则返回 ``1``（便于在脚本里判断成败）。
    """
    checks = (
        ("解释器版本", check_python_version),
        ("第三方依赖", check_dependencies),
        ("配置完整性", check_config),
        ("游戏网关", check_game_gateway),
        ("TLS 信任库", check_tls_trust_store),
        ("逆向素材", check_materials),
        ("生成表同步", check_generated_tables),
        ("加密原语", check_crypto),
        ("任务注册表", check_tasks),
    )

    print("=" * 72)
    print("Cherrytale tool One —— 离线自检（不会产生任何网络请求）")
    print("=" * 72)

    all_ok = True
    for title, check in checks:
        try:
            ok, detail = check()
        except Exception as exc:  # noqa: BLE001
            # 这里刻意用宽泛的 except：自检工具本身绝不能因为某个检查崩掉
            # 就什么都不告诉你——那恰恰是最需要看到信息的时候。
            ok, detail = False, f"检查过程异常：{type(exc).__name__}: {exc}"
        all_ok = all_ok and ok
        print(f"[{'✔' if ok else '✘'}] {title}：{detail}")

    print("-" * 72)
    print("配置快照（不含任何机密值）：")
    for key, value in config.summary().items():
        print(f"    {key} = {value}")
    print("-" * 72)

    if all_ok:
        print(
            "结论：环境就绪。下一步：\n"
            "    1. 看抓包流量清单：notes/gateway_traffic.md（由 tools/extract_har.py 生成）\n"
            "    2. 查协议消息号：  python tools/extract_packet_ids.py --find Daily\n"
            "    3. 三缺口现状与证据：notes/recon_findings.md"
        )
        return 0

    print("结论：存在问题，请按上面标 ✘ 的条目逐条修复。")
    return 1


# ---------------------------------------------------------------------------
# tasks 命令
# ---------------------------------------------------------------------------
def cmd_tasks(_args: argparse.Namespace) -> int:
    """列出所有已注册任务。"""
    registry = load_builtin_tasks()

    print(f"已注册任务（共 {len(registry)} 个）：")
    for name in sorted(registry):
        task_class = registry[name]
        auth = "需登录" if task_class.requires_auth else "免登录"
        # 「可能花钻石」的任务单独标出来：这类任务在开关关闭时会被闸门拦下，
        # 提前标出来能避免"为什么任务报错"的困惑。
        money = "，可能花钻石" if task_class.spends_diamond else ""
        # 「需要主动开启」这类信息写不进类型系统，但用户必须在跑之前看到
        # （例如宴席任务只读跑完时"什么都没发生"，提前说明能避免困惑）。
        hint = getattr(task_class, "opt_in_hint", "")
        extra = f"，{hint}" if hint else ""
        doc = (task_class.__doc__ or "").strip().splitlines()
        summary = doc[0] if doc else ""
        print(f"  - {name:<14} [{auth}{money}{extra}] {summary}")
    return 0


# ---------------------------------------------------------------------------
# accounts 命令
# ---------------------------------------------------------------------------
def cmd_accounts(_args: argparse.Namespace) -> int:
    """列出账号库里已保存的账号（多账号登录的核心入口）。

    :return: 成功 ``0``；账号库读不了 ``1``。

    为什么单独做一条命令：多账号的关键不是"存"，而是**能看清存了哪些、还剩多久**——
    看不到列表就只能靠猜序号，而猜错的代价是"用错账号进游戏"。
    """
    from client.account_store import AccountStore
    from client.exceptions import CherrytaleError

    store = AccountStore()
    try:
        lines = store.describe_all()
    except CherrytaleError as exc:
        print(f"✘ {exc}")
        return 1

    print(f"已保存的账号（最多 {store.max_accounts} 个）：")
    for line in lines:
        print(f"  {line}")
    print()
    print("下一步：")
    print("  python main.py run login --account <序号|邮箱>   # 选号登录（免密码，自动续期）")
    print("  python main.py run login                         # 首次 / 换号：提供账号密码即可")
    return 0


# ---------------------------------------------------------------------------
# run 命令
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    """执行指定任务。

    :return: 任务成功返回 ``0``；任务失败返回 ``1``；任务名不存在返回 ``2``。

    【为什么这个函数变得这么短】
    装配逻辑（会话状态 / 消费闸门 / 运行参数 / 按任务过滤参数）整体搬到了
    ``services/runner.py``，因为 Web 入口（``webapi/``）必须用**同一份**逻辑 ——
    否则就会出现"命令行安全、网页上却真的把资源花掉了"这类分叉
    （详见该模块的模块文档）。

    这里只剩三件事：

    1. 把 ``argparse`` 的结果翻译成结构化的 :class:`services.runner.RunRequest`；
    2. 把说明文字打印到 stdout（``emit=print``）；
    3. 把结果翻译成退出码（``0`` 成功 / ``1`` 任务失败 / ``2`` 任务名不存在）。
    """
    from services import runner as run_runner
    from services.task_spec import run_level_from_cli, task_params_from_cli

    load_builtin_tasks()

    # 【为什么先变成 dict 再交给 runner】
    # "参数面"的唯一来源是 services/task_spec.py；这里只做搬运，不做任何判断 ——
    # 判断（能不能花钱 / 参数合不合法 / 要不要下发运行参数）全在 runner 里，只写一遍。
    run_level = run_level_from_cli(args)
    request = run_runner.RunRequest(
        tasks=(
            run_runner.TaskRequest(
                name=args.task, params=task_params_from_cli(args, args.task)
            ),
        ),
        dry_run=bool(run_level["dry_run"]),
        allow_diamond=bool(run_level["allow_diamond"]),
        account=getattr(args, "account", None),
        # ★ 2026-09-25：``--battles`` / ``--interval`` 不再走这里 —— 它们已经是
        # 荣耀之巅的任务级参数，由上面的 ``task_params_from_cli`` 取出。
        # 旗标仍留在 ``run`` 子命令上，所以命令行用法一字未变。
        verbose=bool(getattr(args, "verbose", False)),
    )

    context: run_runner.RunContext | None = None
    try:
        # 装配阶段会打印：会话状态 → 账号一致性 → 消费许可（与改造前逐字一致）
        context = run_runner.prepare(request, emit=print)
        report = run_runner.run_tasks(context, request, emit=print)
    except run_runner.UnknownTaskError as exc:
        # 任务名写错 → 退出码 2（与"任务失败"的 1 区分开，脚本里可据此判断）
        print(f"✘ {exc}")
        return 2
    finally:
        # 释放 HTTP 会话。命令行里关不关其实无所谓（进程马上退出），
        # 但两个入口保持同一套写法，将来把这段搬进长驻进程时不会漏掉
        # （见 services/runner.py 里 RunContext.close 的说明）。
        if context is not None:
            context.close()

    # 逐步打印结果行；单任务时与改造前的 `print(result)` 输出**逐字相同**
    for step in report.steps:
        print(step.line)
    return 0 if report.ok else 1




# ---------------------------------------------------------------------------
# 命令行解析
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Cherrytale 逆向工具链（协议确认前，涉及真实请求的任务会明确报错）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出 DEBUG 级别日志",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<命令>")

    selftest_parser = subparsers.add_parser(
        "selftest", help="离线自检：依赖 / 素材 / 加密向量 / 任务注册"
    )
    selftest_parser.set_defaults(func=cmd_selftest)

    tasks_parser = subparsers.add_parser("tasks", help="列出所有已注册任务")
    tasks_parser.set_defaults(func=cmd_tasks)

    run_parser = subparsers.add_parser("run", help="执行指定任务")
    run_parser.add_argument("task", help="任务名，例如 handshake / login / mail（可一次给多个）")
    run_parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="演练：只组装并打印将要发送的字节，**不联网、不发包**",
    )
    run_parser.add_argument(
        "--allow-diamond",
        action="store_true",
        help=(
            "允许本任务消耗钻石（**默认禁止**）。"
            "只影响存在花钱路径的任务（`python main.py tasks` 里会标注「可能花钻石」）；"
            "不加这个参数时，任何花钻石的请求都不会被组装出来"
        ),
    )
    run_parser.add_argument(
        "--invite-friends",
        action="store_true",
        help=(
            "宴席任务：授权「花钻石邀请好友」（钻石消费闸门里的**用途**开关）。"
            "仍须同时打开总闸 --allow-diamond；只走最高档，好友不足 5 人时按人数退档"
        ),
    )
    run_parser.add_argument(
        "--meal",
        default=None,
        choices=("morning", "evening"),
        metavar="时段",
        help=(
            "宴席任务：指定吃哪一顿（morning=早宴席 / evening=晚宴席）。"
            "不填 = 原有的「整场」行为；填了则只在该顿的开放时间窗内发包，"
            "窗外一个 15003 都不发 —— 网页端的早/晚两个按钮就是带这个参数在跑。"
        ),
    )
    run_parser.add_argument(
        "--invite-morning",
        action="store_true",
        help="宴席任务：授权**早宴席**花钻石邀请好友（网页双按钮用；仅早宴席窗口内有效）",
    )
    run_parser.add_argument(
        "--invite-evening",
        action="store_true",
        help="宴席任务：授权**晚宴席**花钻石邀请好友（网页双按钮用；仅晚宴席窗口内有效）",
    )
    run_parser.add_argument(
        "--account",
        default=None,
        metavar="序号|邮箱",
        help=(
            "选一个已保存的账号（见 `python main.py accounts`）。"
            "login 任务用它登录并自动续期；其它任务只用它提示"
            "「当前会话属于哪个账号」"
        ),
    )
    run_parser.add_argument(
        "--server-id",
        type=int,
        default=None,
        help=(
            "登录任务：**强制**进入这个区服编号（网页「选区」界面用的就是它）。"
            "先用 --plan-only 看清有哪些区、各自编号是多少"
        ),
    )
    run_parser.add_argument(
        "--plan-only",
        action="store_true",
        help=(
            "登录任务：只拉区服列表就退出 —— 不写会话、不写账号库、不发 1003"
            "（登录 / 选区两段式流程的第一段）"
        ),
    )
    run_parser.add_argument(
        "--battles",
        type=int,
        default=None,
        help=(
            "荣耀之巅：本次最多打几场（**默认 0 = 只读**，什么都不消耗）。"
            "竞技请求一发出就扣玩家真实次数，所以必须显式给出才会真打。"
            "网页上对应「主动任务 → 荣耀之巅」卡片里的「打几场」"
        ),
    )
    run_parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=(
            "荣耀之巅：每场之间的间隔秒数（默认取 config，15 秒）。"
            "网页上对应「主动任务 → 荣耀之巅」卡片里的「每场间隔」"
        ),
    )
    run_parser.add_argument(
        "--area",
        default=None,
        metavar="areaID|编号|关键字",
        help=(
            "扫荡任务：目标活动区域。三种写法——areaID（如 128110053）、"
            "清单编号（如 3）、名称关键字（如 降臨）。不给则只列出当期开放区域"
        ),
    )
    run_parser.add_argument(
        "--times",
        type=int,
        default=None,
        help=(
            "扫荡任务：**每个关卡**最多扫几次（不给 = 0 = 只读，什么都不扫）。"
            "可选 1 / 5，或用 **-1 = 扫荡到清空体力/次数耗尽**"
            "（活动关反复发「扫荡 5 次」直到体力清空；素材关直到次数耗尽；"
            "活动关用 -1 时**必须**同时给 --section）。"
            "扫荡会真实扣除体力与扫荡券，所以必须显式给出；"
            "实际次数还会被 config.SWEEP_MAX_TIMES 与剩余体力二次削减"
        ),
    )
    run_parser.add_argument(
        "--section",
        default=None,
        metavar="sectionID|编号",
        help="扫荡任务：只扫指定的一个关卡（ID 或候选清单编号）；不给则扫该区全部可扫关卡",
    )
    run_parser.add_argument(
        "--allow-unpassed",
        action="store_true",
        help=(
            "扫荡任务：允许扫**未通关**的关卡。默认禁止 —— 只扫已通关的，"
            "避免把体力花在还没打过的关卡上（判据是服务端返回的 sectionState）"
        ),
    )
    run_parser.add_argument(
        "--buy-blood-diamond",
        action="store_true",
        default=None,
        help=(
            "一般市集：购买血钻（200200000）。四种道具的开关默认全开；"
            "要**关掉**某一种请用 --no-buy-xxx（例如 --no-buy-amber）"
        ),
    )
    run_parser.add_argument(
        "--no-buy-blood-diamond",
        dest="buy_blood_diamond",
        action="store_false",
        help="一般市集：不购买血钻（其余三种照常买）",
    )
    run_parser.add_argument(
        "--buy-amber",
        action="store_true",
        default=None,
        help="一般市集：购买琥珀（200200003）",
    )
    run_parser.add_argument(
        "--no-buy-amber",
        dest="buy_amber",
        action="store_false",
        help="一般市集：不购买琥珀",
    )
    run_parser.add_argument(
        "--buy-chocolate",
        action="store_true",
        default=None,
        help="一般市集：购买爱心巧克力（200600001）",
    )
    run_parser.add_argument(
        "--no-buy-chocolate",
        dest="buy_chocolate",
        action="store_false",
        help="一般市集：不购买爱心巧克力",
    )
    run_parser.add_argument(
        "--buy-fragrance",
        action="store_true",
        default=None,
        help="一般市集：购买幸福香氛（200600002）",
    )
    run_parser.add_argument(
        "--no-buy-fragrance",
        dest="buy_fragrance",
        action="store_false",
        help="一般市集：不购买幸福香氛",
    )
    run_parser.add_argument(
        "--category",
        default="all",
        choices=["all", "job", "element"],
        help="素材关卡大类：all（全部）、job（角色素材）、element（元素试炼）",
    )
    run_parser.add_argument(
        "--max-stages",
        type=int,
        default=None,
        help="主线推图：本次最多推进关卡数（默认 0 = 不限，直到体力耗尽或遇到校验关卡）",
    )
    run_parser.add_argument(
        "--delay-min",
        type=float,
        default=None,
        help="主线推图：每关模拟战斗时间下限（秒，默认 15.0）",
    )
    run_parser.add_argument(
        "--delay-max",
        type=float,
        default=None,
        help="主线推图：每关模拟战斗时间上限（秒，默认 25.0）",
    )
    run_parser.set_defaults(func=cmd_run)

    accounts_parser = subparsers.add_parser(
        "accounts", help="列出账号库里已保存的账号（多账号登录用）"
    )
    accounts_parser.set_defaults(func=cmd_accounts)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """程序主入口。

    :param argv: 命令行参数（``None`` 表示取 ``sys.argv[1:]``）。
    :return: 进程退出码。
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # 先修编码，再干别的：否则任何早于它打印的中文都可能是乱码
    force_utf8_output()
    setup_logging(args.verbose)

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 0

    return int(func(args))


if __name__ == "__main__":
    raise SystemExit(main())
