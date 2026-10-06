"""全局配置：路径定位、服务端地址、Header 参数、账号与运行开关。

三条设计原则（都是踩过坑才总结出来的）：

1. **机密信息不进代码**
   账号 / Token / 抓包代理一律通过环境变量读取，代码里只保留字段名。
   原因：一旦把 Token 写进 ``.py``，它就会被 git 永久记录，事后删掉也抹不干净历史。

2. **大体积素材路径集中管理**
   ``dump.cs`` 有 34.8 MB，且旁边还有 474 MB 素材。这些路径统一在这里定义，
   配合 ``tools/search_dump.sh`` 做「只正则检索、绝不整文件读取」的保护。

3. **缺配置就报错，不要静默兜底**
   服务端域名尚未从 ``dump.cs`` 确认，所以默认值是空字符串；
   谁要用它，谁就会拿到一个写着「请先确认域名」的明确异常，而不是发出一个错误请求。
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# 0. 项目根目录与资源路径（适配源码运行与 PyInstaller 冻结打包）
# ---------------------------------------------------------------------------
# - RESOURCE_ROOT: 存放只读静态资源（如 web/ 目录）。打包时位于解压临时目录 sys._MEIPASS，开发时在源码根目录。
# - PROJECT_ROOT:  存放可写持久化数据（.session.json、.platform_accounts.json、缓存与日志）。
#                  打包时必须位于可执行文件所在目录（避免临时目录销毁导致数据丢失），开发时在源码根目录。
if getattr(sys, "frozen", False):
    RESOURCE_ROOT: Final[Path] = Path(getattr(sys, "_MEIPASS", sys.executable)).resolve()
    PROJECT_ROOT: Final[Path] = Path(
        os.getenv("CHERRYTALE_DATA_ROOT", str(Path(sys.executable).resolve().parent))
    ).resolve()
else:
    RESOURCE_ROOT: Final[Path] = Path(__file__).resolve().parent
    PROJECT_ROOT: Final[Path] = Path(
        os.getenv("CHERRYTALE_DATA_ROOT", str(RESOURCE_ROOT))
    ).resolve()


class ConfigError(RuntimeError):
    """配置缺失或非法时抛出。刻意与网络异常区分，便于一眼看出是「配置没填」而非「网线断了」。"""


# ---------------------------------------------------------------------------
# 1. 逆向素材路径（约 474 MB，已在 .gitignore 中忽略）
# ---------------------------------------------------------------------------
# 允许用环境变量整体搬家，换电脑/换目录时无需改代码。
MATERIAL_ROOT: Final[Path] = Path(
    os.getenv("CHERRYTALE_MATERIAL_ROOT", str(PROJECT_ROOT))
)

#: Il2CppDumper 产出的类型总览（34.8 MB / 20951 个类型）
DUMP_CS_PATH: Final[Path] = MATERIAL_ROOT / "Cherrytale IL2CPP" / "dump.cs"

#: 各程序集的原型 DLL，可用 dnSpy 打开做交叉验证
DUMMY_DLL_DIR: Final[Path] = MATERIAL_ROOT / "Cherrytale IL2CPP" / "DummyDll"

#: 静态配置表目录：405 个文件（397 个无扩展名 + 8 个 .txt）
TEXT_ASSET_DIR: Final[Path] = MATERIAL_ROOT / "Cherrytale Asset" / "TextAsset"


def material_status() -> dict[str, bool]:
    """检查素材是否就位，供 ``main.py selftest`` 打印体检结果。

    :return: 形如 ``{"dump.cs": True, "DummyDll": True, "TextAsset": True}`` 的字典。
    """
    return {
        "dump.cs": DUMP_CS_PATH.is_file(),
        "DummyDll": DUMMY_DLL_DIR.is_dir(),
        "TextAsset": TEXT_ASSET_DIR.is_dir(),
    }


# ---------------------------------------------------------------------------
# 2. 服务端地址
# ---------------------------------------------------------------------------
# TODO(protocol): 域名尚未确认。dump.cs 中的网络库是 BestHTTP，
#                 需进一步分析其调用链（以及 com.auer.game.protobuf 的 1098 个协议类）
#                 才能确定真实域名、路径与端口。请勿凭猜测填写。
API_SCHEME: Final[str] = os.getenv("CHERRYTALE_API_SCHEME", "https")
API_HOST: Final[str] = os.getenv("CHERRYTALE_API_HOST", "")
API_PORT: Final[int] = int(os.getenv("CHERRYTALE_API_PORT", "443"))

# TODO(protocol): dump.cs 中存在 Game.Http.DnsUdpClient，
#                 说明客户端可能绕开系统 DNS、用自建 UDP DNS 解析主机名。
#                 若直连失败，把解析结果填到这里（或设同名环境变量）。
CUSTOM_DNS_IP: Final[str] = os.getenv("CHERRYTALE_DNS_IP", "")


def api_base_url() -> str:
    """拼接 API 根地址，例如 ``https://api.example.com:443``。

    :raises ConfigError: 当 ``API_HOST``（或环境变量 ``CHERRYTALE_API_HOST``）为空时。
    """
    if not API_HOST:
        raise ConfigError(
            "尚未配置服务端域名（config.API_HOST 为空）。\n"
            "请先完成 dump.cs 的网络层分析，或临时设置环境变量后重试：\n"
            "    export CHERRYTALE_API_HOST=your.host.here"
        )
    return f"{API_SCHEME}://{API_HOST}:{API_PORT}"


# ---------------------------------------------------------------------------
# 2.5 游戏网关（**抓包实测确认**）
# ---------------------------------------------------------------------------
# 【结论：这是 HTTP，不是自定义 TCP —— 早期推测已被推翻】
#
# 早期根据 dump.cs 里的 ``CommonTcpClient`` / ``GetRootPacketVCode``，曾推测过
# 「TCP 长连接 + 私有分帧 + vCode 盐值签名」。真实抓包（captures/）推翻了它：
#
#     POST https://game-ct-labs.ecchi.xxx:1893/ HTTP/1.1
#     Content-Type: application/octet-stream
#     Content-Length: 88
#
#     body 首字节：08 C7 85 06  ← 标准 protobuf varint 标签
#                  （字段 1 = packetID = 99015 = VersionControlServerPacket）
#
# 由此确认四件事：
#   · 没有私有长度前缀 / 分帧 —— HTTP 自带 Content-Length；
#   · 没有应用层加密 —— 载荷里能直接看到明文 JSON 与中文字符串；
#   · 没有签名校验 —— ``vCode`` 字段是每次都不同的随机 32 位 hex（GUID 形态）；
#   · dump.cs 里的 Socket / DnsUdpClient 只是**未启用**的备用逻辑。
#
# 顺带推翻的另一条：曾以为 443 端口是游戏入口（实测只返回 503 HAProxy 错误页），
# 真正入口是 **1893**。

#: 网关根地址。实测抓包里 POST 的目标就是它，路径为 ``/``
GAME_GATEWAY_URL: Final[str] = os.getenv(
    "CHERRYTALE_GATEWAY_URL", "https://game-ct-labs.ecchi.xxx:1893/"
)

#: 请求体类型（**请求与响应都是它**，不要改成 application/json）
GAME_CONTENT_TYPE: Final[str] = "application/octet-stream"

#: 实测客户端声明的压缩支持（UnityWebRequest 的默认值，逐字符照抄）
GAME_ACCEPT_ENCODING: Final[str] = os.getenv(
    "CHERRYTALE_ACCEPT_ENCODING", "deflate, gzip"
)

#: 客户端版本号。实测值是 ``2.2.0-el-h-win``
#: （``el`` = Erolabs 渠道、``h`` = 成人版、``win`` = Windows 平台）
GAME_CLIENT_VERSION: Final[str] = os.getenv(
    "CHERRYTALE_CLIENT_VERSION", "2.2.0-el-h-win"
)

#: 语言代码（实测 ``zhcn``；抓包里作为 VersionControlServerPacket 的字段 3 发出）
GAME_LANGUAGE: Final[str] = os.getenv("CHERRYTALE_GAME_LANGUAGE", "zhcn")

#: 渠道 / 平台类型（协议字段 ``channelPlatformType``）。
#:
#: 实测值 **2** —— 出现在抓包的 99015 握手包里（字段 1），与
#: ``GAME_CLIENT_VERSION`` 里的 ``el``（= Erolabs 渠道）互相对应。
#: 登录包（1001）与进区登录包（1003）沿用同一取值；
#: ⚠️ 但这两条的请求体在 HAR 里是有损的，**尚未逐字节核对**，
#: 所以第一次真发登录请求时要特别留意这个字段是不是 2。
CHANNEL_PLATFORM_TYPE_EL: Final[int] = int(os.getenv("CHERRYTALE_CHANNEL_TYPE", "2"))

#: 礼包商店（37001 ``GetGiftListPacket``）的实测筛选参数（三项一组）。
#:
#: 【依据】10.3 安卓抓包 ``captures/每日免费钻石和扫荡券领取.saz``：
#: 真实客户端发 ``{platform=4, cashType=7, channelType=2, giftType=0}``，
#: 返回 216 个礼包（约 116 KB）。旧值 ``{2, 0, 0}`` 只会拿到**空列表** ——
#: 这就是"每日免费钻石/扫荡券领不到"的直接原因。
#: ⚠️ ``platform=4`` 与 :data:`CHANNEL_PLATFORM_TYPE_EL`（= 2）**不是一回事**：
#: 前者是礼包商店的平台筛选（安卓客户端 = 4），后者是握手/登录的渠道类型，
#: 两者各管各的，不要互相套用。
GIFT_STORE_PLATFORM: Final[int] = int(os.getenv("CHERRYTALE_GIFT_PLATFORM", "4"))
GIFT_STORE_CASH_TYPE: Final[int] = int(os.getenv("CHERRYTALE_GIFT_CASH_TYPE", "7"))
GIFT_STORE_CHANNEL_TYPE: Final[int] = int(os.getenv("CHERRYTALE_GIFT_CHANNEL_TYPE", "2"))

#: 「月卡礼包」的识别关键字（2026-10-04 用户需求）。
#:
#: 【为什么只能按名字认，不能信 ``isFree``】
#: 服务端把月卡类礼包也标成了 ``isFree != 0``，可它**实际需要先付费买月卡**
#: 才能领 —— 直接领只会拿到一个错误码。这类礼包服务端给了 3 条，
#: 按 :data:`GIFT_MONTHLY_KEYWORDS` 认出来之后**仍然照原 giftID 逐个尝试**
#: （不删、不跳过），只是运行结果里合并成一行，见 ``tasks/gift_package.py``。
#:
#: 【为什么做成可配置元组】
#: 礼包名会随版本/语种改动；写成常量后改一行即可，不必动任务逻辑。
GIFT_MONTHLY_KEYWORDS: Final[tuple[str, ...]] = ("月卡",)

#: 月卡类礼包在运行结果里的**统一显示名**（3 条合并成一条时用这个名字）。
GIFT_MONTHLY_LABEL: Final[str] = os.getenv("CHERRYTALE_GIFT_MONTHLY_LABEL", "月卡礼包")

#: 月卡类礼包全部领取失败时的提示（反映"还没买月卡"这一事实，不带错误码）。
GIFT_MONTHLY_NOT_PURCHASED_NOTE: Final[str] = "未购买月卡"

#: 单次请求超时。比平台网关宽松：游戏服可能要现算大存档（实测有 344 KB 的响应）
GAME_SEND_TIMEOUT: Final[float] = float(os.getenv("CHERRYTALE_GAME_TIMEOUT", "30"))

#: 网关主机与端口（**我们实际发请求用的**；抓包里的 URL 就是这个组合）
GAME_SERVER_HOST: Final[str] = os.getenv(
    "CHERRYTALE_GAME_HOST", "game-ct-labs.ecchi.xxx"
)
GAME_SERVER_PORT: Final[int] = int(os.getenv("CHERRYTALE_GAME_PORT", "1893"))

#: 服务端在握手响应（``VersionControlServerRes``）里下发的 ``serverPort``，实测 **1888**。
#:
#: ⚠️ 它**不等于**我们请求用的 1893 —— 两者不是同一个端口。
#: 合理推测：1893 是 HTTP 网关入口，1888 是下发给客户端用于长连接/重连的业务端口。
#: 在抓到 1888 的实际流量之前，**不要**把请求改到它。
GAME_DOWNLINK_PORT_HINT: Final[int] = 1888


# ---- 以下为次要线索（元数据提取，暂未用到，保留备查）----
#: 游戏服的 API 网关（承载下面两个 .jsp 接口）。实测两者均返回
#: 「HTTP 200 + 0 字节」，说明还需要额外参数或已停用 —— 既然 1893 网关能用，
#: 这两个接口暂时不必深挖。
GAME_API_HOST: Final[str] = os.getenv(
    "CHERRYTALE_GAME_API_HOST", "api-ct-labs.ecchi.xxx"
)

#: 平台登录页地址（= dump.cs 里的 ``const string WebLoginHost``）
WEB_LOGIN_HOST: Final[str] = os.getenv(
    "CHERRYTALE_WEB_LOGIN_HOST", "https://login-cq.ecchi.xxx/"
)

#: 元数据里唯一出现的真实公网 IP，疑似历史遗留的硬编码服务器
LEGACY_IP_HINT: Final[str] = "119.28.41.111"

GAME_SERVER_STATE_PATH: Final[str] = "/api/getServerState.jsp?version="
GAME_MD5_BY_NODE_PATH: Final[str] = "/api/getMd5ByNode_V2.jsp?node="


# ---------------------------------------------------------------------------
# 2.6 区服选择（登录后必经的一步）
# ---------------------------------------------------------------------------
# 【这一步在链路上的位置（来自 dump.cs + 抓包实测）】
#
#     1001 AccountClientInfoLoginPacket   → 发起账号登录
#     1002 AccountClientInfoLoginRes      → 返回 allServerList / suggestedServer /
#                                            lastLoginedServerList（每项都带 serverID）
#     1003 LoginPacket（字段 2 = serverID）→ 把「选中的区服编号」回传给服务端
#     1004 LoginRes                       → 才真正进游戏
#
# 关键结论：**区服编号不是我们编的，只能从 1002 响应里取**。
# 客户端 UI（dump.cs 里的 ``Login_Main_serverlist_UIView``）做的也正是这件事：
# 把列表摆出来 → 等玩家点一个 → 把它的 serverID 填进 LoginPacket。
# 所以自动化的做法是：拿 1002 的列表 → 按下面的优先级挑一个 → 填进 1003。

#: 目标区服 ID（协议字段 ``serverID``）的环境变量名。
#:
#: 【为什么默认留空，而不是写死 1】
#: 区服 ID 是**服务端下发的**（1002 响应里才有）。写死一个默认值会让人误以为
#: 「已经选好了」，实际上可能选到一个不存在的区服；留空则明确表示
#: 「还没选，等拿到列表再按规则挑」。
SERVER_ID_ENV_NAME: Final[str] = "CHERRYTALE_SERVER_ID"

#: 目标区服分组 ID（协议字段 ``serverGroupID``）的环境变量名。
#: 仅当想按「组」筛选区服时才需要；单区服选择用不到，留空表示不按组过滤。
SERVER_GROUP_ID_ENV_NAME: Final[str] = "CHERRYTALE_SERVER_GROUP_ID"


def _optional_int_env(name: str) -> int | None:
    """读一个「可缺省」的整数环境变量。

    :return: 解析成功返回整数；未设置或不是整数时返回 ``None``。

    【为什么非法值不直接抛异常】
    本函数在模块导入时执行。若因为环境里有个手误的 ``CHERRYTALE_SERVER_ID=abc``
    就让整个程序起不来，你会在完全无关的地方看到 ImportError。
    这里改为「当作没配置」；真正的报错交给选服逻辑
    （``models.server.AccountClientInfoLoginRes.choose_server``）——
    那时才有上下文说清「你要选服，但配置是错的」。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


#: 显式指定的区服 ID（未配置时为 ``None``）
SERVER_ID: Final[int | None] = _optional_int_env(SERVER_ID_ENV_NAME)

#: 显式指定的区服分组 ID（未配置时为 ``None``）
SERVER_GROUP_ID: Final[int | None] = _optional_int_env(SERVER_GROUP_ID_ENV_NAME)


def server_selection_summary() -> str:
    """区服选择配置的一行摘要（供体检 / 日志使用，不含任何机密）。"""
    parts: list[str] = []
    parts.append(f"{SERVER_ID_ENV_NAME}={SERVER_ID}" if SERVER_ID else "区服=按响应自动挑")
    if SERVER_GROUP_ID is not None:
        parts.append(f"{SERVER_GROUP_ID_ENV_NAME}={SERVER_GROUP_ID}")
    return "，".join(parts)


# ---------------------------------------------------------------------------
# 2.5 消费许可（钻石开关）—— 与"领奖励"完全相反的一类操作
# ---------------------------------------------------------------------------
#: 是否允许任务花掉钻石的环境变量名。
#:
#: 【为什么默认关闭，而且必须是"默认关闭"】
#: 领取类操作只进不出，出错顶多少领一次；花钻石是不可逆的损失。
#: 两种方向的代价不对称时，默认值必须站在保守一侧 ——
#: 所以这里"未配置"就等于"不允许"。想开放时显式打开即可
#: （命令行 ``--allow-diamond`` 也收敛到同一个开关）。
ALLOW_DIAMOND_SPEND_ENV_NAME: Final[str] = "CHERRYTALE_ALLOW_DIAMOND_SPEND"


def _bool_env(name: str) -> bool | None:
    """读一个布尔环境变量。

    :return: 取值属于 ``1/true/yes/on``（不区分大小写）→ ``True``；
        属于 ``0/false/no/off`` → ``False``；未设置或写了别的值 → ``None``。

    【为什么返回 ``None`` 而不是默认值】
    "没配置"和"配置成关闭"必须能区分开，否则无法实现
    "环境变量 > config_local.py > 默认值"这套优先级。
    至于看不懂的值（例如手误打了 ``maybe``），一并当成 ``None`` ——
    与 :func:`_optional_int_env` 同样的容错哲学：一个手误不该让程序起不来，
    而"当作没配置"正好落在安全的一侧。
    """
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


# ---------------------------------------------------------------------------
# 0.5 本机配置文件 ``config_local.py`` 的加载
# ---------------------------------------------------------------------------
# 这个文件已被 ``.gitignore`` 忽略，存放"只在这台机器上生效"的设置
# （含逆向常量 GAME_V_CODE_KEY）。
#
# 【三级查找，为什么不是一句 import 就完事】
# 三种运行形态对「config_local.py 在哪」的答案是**互不相同**的：
#
#   ① 源码运行（开发机）      —— 就是工程根里那个 .py 文件
#   ② PyInstaller 打包的 EXE  —— datas 把它塞进 _MEIPASS，但**用户改不了**；
#                                所以优先找「可执行文件同级」的就地副本
#   ③ Chaquopy 打包的 APK     —— 代码被编成 .pyc 收进 assets/chaquopy/app.imy
#                               （一个 zip 归档），**磁盘上根本没有这个文件**，
#                               任何 Path(...).is_file() 探测都必然失败。
#
# ③ 是 2026-10-05 的翻车点：先前只按文件路径找，APK 里 config_local.pyc
# 明明在包里、KEY 也在里面，运行时却读到空串 → 所有要算 vCode 的任务
# (wallet / arena / wudou / yimo / top_pvp) 全部 RuntimeError。
#
# 因此顺序是**先问 Python 导入系统**（它能命中 zip 归档里的模块，覆盖 ③；
# 在 ①② 下若同名模块恰好在 sys.path 上也无害），**再退回文件系统探测**
# （覆盖 ①② 里"就地编辑"的诉求 —— 这是 import 做不到的）。
_LOCAL_CONFIG_CACHE: dict[str, Any] | None = None


def _read_local_config_from_importer() -> dict[str, Any] | None:
    """尝试用 Python 导入系统加载 ``config_local``（Chaquopy/APK 场景的唯一出路）。

    成功返回变量字典；模块不存在或执行出错时返回 ``None``（交给文件探测兜底）。

    ⚠️ 刻意用 ``importlib`` 而不是 ``import config_local`` 语句：后者在模块
    不存在时会抛 ``ImportError``，而我们需要"静默地换下一条路"。
    """
    import importlib

    try:
        module = importlib.import_module("config_local")
    except Exception:  # noqa: BLE001 —— 不存在 / 语法错 / 版本不符都走兜底
        return None
    return {k: v for k, v in vars(module).items() if not k.startswith("__")}


def _load_local_config() -> dict[str, Any]:
    """加载本机配置，返回其中定义的变量字典（找不到则空字典，且不缓存失败）。"""
    global _LOCAL_CONFIG_CACHE
    if _LOCAL_CONFIG_CACHE is not None:
        return _LOCAL_CONFIG_CACHE

    import importlib.util

    search_dirs = [PROJECT_ROOT, RESOURCE_ROOT]
    if getattr(sys, "frozen", False):
        search_dirs.append(Path(getattr(sys, "_MEIPASS", "")))

    # 第 1 轮：文件系统探测（PC 场景优先 —— 让用户能就地编辑 EXE 旁那份）。
    for directory in search_dirs:
        candidate = Path(directory) / "config_local.py"
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location("config_local", candidate)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 —— 本机配置写错不该炸掉整个程序
            continue
        _LOCAL_CONFIG_CACHE = {
            k: v for k, v in vars(module).items() if not k.startswith("__")
        }
        return _LOCAL_CONFIG_CACHE

    # 第 2 轮：导入系统（★ 2026-10-05 APK 修复）。
    # 磁盘上没有 config_local.py 并不代表没有这个模块 —— Chaquopy 把整个
    # 应用代码收进了 app.imy，`import config_local` 能直接命中里面的 .pyc。
    from_importer = _read_local_config_from_importer()
    if from_importer is not None:
        _LOCAL_CONFIG_CACHE = from_importer
        return _LOCAL_CONFIG_CACHE

    return {}


def _read_bool_from_local_config(name: str, *, default: bool) -> bool:
    """从可选的 ``config_local.py`` 读取一个布尔开关。

    :return: 读到的布尔值；文件不存在、没有该变量、或值不是布尔时返回 :paramref:`default`。
    """
    value = _load_local_config().get(name)
    if isinstance(value, bool):
        return value
    return default


def _read_int_from_local_config(name: str, *, default: int) -> int:
    """从可选的 ``config_local.py`` 读取一个整型配置。"""
    value = _load_local_config().get(name)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def _read_str_from_local_config(name: str) -> str:
    """从可选的 ``config_local.py`` 读取一个字符串配置；没有则返回空串。"""
    value = _load_local_config().get(name)
    if isinstance(value, str):
        return value.strip()
    return ""


def _resolve_allow_diamond_spend() -> bool:
    """按固定优先级解析"是否允许花钻石"。

    优先级（高 → 低）：

    1. 环境变量 :data:`ALLOW_DIAMOND_SPEND_ENV_NAME`
       （``CHERRYTALE_ALLOW_DIAMOND_SPEND=1/0``，最灵活，适合临时放开一次）；
    2. ``config_local.py`` 里的 ``ALLOW_DIAMOND_SPEND``（适合设置界面持久化）；
    3. **默认 ``False``** —— 谁都没说话时，就别花钱。
    """
    from_env = _bool_env(ALLOW_DIAMOND_SPEND_ENV_NAME)
    if from_env is not None:
        return from_env
    return _read_bool_from_local_config("ALLOW_DIAMOND_SPEND", default=False)


#: 是否允许任务消耗钻石（默认 **False** = 禁止）。
ALLOW_DIAMOND_SPEND: Final[bool] = _resolve_allow_diamond_spend()


def spending_summary() -> str:
    """消费许可的一行摘要（供体检 / 启动日志使用）。"""
    if ALLOW_DIAMOND_SPEND:
        return f"钻石使用=允许（用户已显式开启：{ALLOW_DIAMOND_SPEND_ENV_NAME}）"
    return "钻石使用=禁止（默认；加 --allow-diamond 或设 CHERRYTALE_ALLOW_DIAMOND_SPEND=1 可开启）"


# ---------------------------------------------------------------------------
# 2.6 宴席（BBQ）：两个独立开关 + 档位策略
# ---------------------------------------------------------------------------
# 【为什么这块在 2026-09-21 被重写】
# 宴席原本被当成"日常清理的一环"，但它其实分两步，而且**只有第一步会花钱**：
#
#     ① 邀请好友（花钻石；最高档 31 钻）   ← 必须由用户主动决定
#     ② 确认吃宴席（免费）                  ← 与消费闸门无关
#
# 所以"能不能跑这个任务"与"能不能花钻石"必须拆开：钻石那一步做成一个**用途开关**
# （见 ``client/spending.py`` 的 ``SpendPurpose``），与总闸 :data:`ALLOW_DIAMOND_SPEND`
# 是**与**关系。两个都没开时，宴席任务仍能只读跑完（列档位、好友与状态），不报错。

#: 「允许用钻石邀请好友吃宴席」的环境变量名。
BBQ_INVITE_FRIENDS_ENV_NAME: Final[str] = "CHERRYTALE_BBQ_INVITE_FRIENDS"


def _resolve_bbq_invite_friends() -> bool:
    """按固定优先级解析"是否允许用钻石邀请好友"。

    优先级（高 → 低）：

    1. 环境变量 :data:`BBQ_INVITE_FRIENDS_ENV_NAME`（适合临时放开一次）；
    2. ``config_local.py`` 里的 ``BBQ_INVITE_FRIENDS``（适合设置界面持久化）；
    3. **默认 ``False``** —— 谁都没说话时就不邀请，即不花钱。

    .. note::
       本开关只是**用途开关**，不等于总闸：总闸（:data:`ALLOW_DIAMOND_SPEND`）
       关着时，即使这里为 ``True`` 也一律拒绝 —— 两道门是"与"关系。
    """
    from_env = _bool_env(BBQ_INVITE_FRIENDS_ENV_NAME)
    if from_env is not None:
        return from_env
    return _read_bool_from_local_config("BBQ_INVITE_FRIENDS", default=False)


#: 是否允许在宴席里花钻石邀请好友（默认 **False**）
BBQ_INVITE_FRIENDS: Final[bool] = _resolve_bbq_invite_friends()

#: 一次最多邀请几位好友 —— 直接对应配置表 ``ExtraEnergyData`` 的**最大档**。
#:
#: 【★ 2026-10-04 实测修订：最多 4 位好友；``number`` 是含自己的总人数】
#: ``ExtraEnergyData`` 的 5 行（``number`` 1~5，钻石 0/7/15/24/31）里，
#: ``number`` 是**参与人数（含自己）**，不是好友数：
#: 免费档 ``number=1``（0 钻）= 不带好友 —— 与"免费吃只发 groupID"实测吻合；
#: 10.3 抓包里真实客户端的付费邀请带**恰好 4 位好友**（number=5 档，31 钻）。
#: 2026-10-04 真机发 5 位好友 → 服务端拒 ``errorCode=-6``。
#: 档位选取 = ``number == 好友数 + 1``（见 ``tasks.bbq._pick_tier``）。
BBQ_MAX_INVITE_FRIENDS: Final[int] = 4

#: （**逃生门，正常路径不使用**）显式指定宴席档位 ID（``ExtraEnergyData.menuID``）
#: 的环境变量名。
#:
#: 改造前，档位是"服务端给什么就用什么"；现在档位由 :data:`BBQ_MAX_INVITE_FRIENDS`
#: 与好友数共同决定，所以本变量只留作**人工兜底**：显式指定后档位不再按人数挑，
#: 而是用你指定的这一档（金额仍从配置表读，读不到就拒绝，绝不按未知金额发包）。
BBQ_MENU_ID_ENV_NAME: Final[str] = "CHERRYTALE_BBQ_MENU_ID"

#: 显式指定的宴席档位 ID（未配置时为 ``None``）
BBQ_MENU_ID: Final[int | None] = _optional_int_env(BBQ_MENU_ID_ENV_NAME)


# ---------------------------------------------------------------------------
# 2.6.1 钻石购买体力（BuyEnergy）
# ---------------------------------------------------------------------------
#: 「允许用钻石购买体力」的环境变量名。
BUY_ENERGY_ENV_NAME: Final[str] = "CHERRYTALE_BUY_ENERGY"


def _resolve_buy_energy() -> bool:
    """按固定优先级解析"是否允许用钻石购买体力"（默认 False）。"""
    from_env = _bool_env(BUY_ENERGY_ENV_NAME)
    if from_env is not None:
        return from_env
    return _read_bool_from_local_config("BUY_ENERGY", default=False)


#: 是否允许用钻石购买体力（默认 **False**）
BUY_ENERGY: Final[bool] = _resolve_buy_energy()

#: 购买体力次数的环境变量名
BUY_ENERGY_TIMES_ENV_NAME: Final[str] = "CHERRYTALE_BUY_ENERGY_TIMES"


def _resolve_buy_energy_times() -> int:
    val = _optional_int_env(BUY_ENERGY_TIMES_ENV_NAME)
    if val is not None:
        return max(0, val)
    return max(0, _read_int_from_local_config("BUY_ENERGY_TIMES", default=0))


#: 默认购买体力次数（默认 0 = 只读查询）
BUY_ENERGY_TIMES: Final[int] = _resolve_buy_energy_times()

#: 购买体力钻石上限的环境变量名
BUY_ENERGY_MAX_COST_ENV_NAME: Final[str] = "CHERRYTALE_BUY_ENERGY_MAX_COST"
BUY_ENERGY_MAX_COST: Final[int | None] = _optional_int_env(BUY_ENERGY_MAX_COST_ENV_NAME)


# ---------------------------------------------------------------------------
# 2.7 游戏会话令牌（跨进程复用登录结果）
# ---------------------------------------------------------------------------
#: 游戏会话令牌的环境变量名。
#:
#: 【这是什么令牌】
#: 登录任务的最后一步（1004 ``LoginRes``）会在 ``playerInitClass.token`` 里下发
#: **游戏会话令牌**，之后每个业务请求（6001 拉任务、33057 拉通行证、37001 拉礼包…）
#: 都要把它放进 ``RootPacket``。没有它就一律被拒 —— 表现为含糊的"包异常"。
#:
#: 【为什么需要环境变量这一层】
#: ``python main.py run <任务>`` 每次都是**独立进程**，内存里的会话（``GameSession``）
#: 不会保留。所以"先跑 login、再跑日常"必须把令牌**带过去**：
#:
#:     # ① 跑登录，从日志里拿到 playerID 与令牌
#:     python main.py run login
#:     # ② 用环境变量把令牌交给下一个进程（不要写进源码！）
#:     export CHERRYTALE_SESSION_TOKEN='<令牌>'
#:     python main.py run daily_box
#:
#: 它**不是**平台门户令牌（那个是 ``AUTH_TOKEN``，走 HTTP 头）——
#: 两者用途不同，不要混用。
SESSION_TOKEN_ENV_NAME: Final[str] = "CHERRYTALE_SESSION_TOKEN"

#: 游戏会话令牌（未配置时为空字符串）
SESSION_TOKEN: Final[str] = os.getenv(SESSION_TOKEN_ENV_NAME, "").strip()


def has_session_token() -> bool:
    """是否已提供游戏会话令牌（只看环境变量这一层）。"""
    return bool(SESSION_TOKEN)


#: 会话文件路径的环境变量名（想把文件放到别处时用它覆盖）。
SESSION_FILE_ENV_NAME: Final[str] = "CHERRYTALE_SESSION_FILE"

#: **会话文件**：登录成功后由 ``tasks/login.py`` 把 1004 拿到的令牌写在这里。
#:
#: 【它解决什么】``python main.py run <任务>`` 每次都是独立进程，内存里的会话
#: （``GameSession.game_state``）不会保留。有这个文件之后就不必再手工复制令牌：
#:
#:     python main.py run login       # 自动写入 .session.json
#:     python main.py run daily       # 自动读回来，直接可用
#:
#: 【安全约束】
#: - 文件里**含令牌明文**，因此已在 ``.gitignore`` 里排除（切勿提交）；
#: - 文件名带前导点，且不放在 ``captures/`` 之类会同步出去的目录；
#: - 环境变量 ``CHERRYTALE_SESSION_TOKEN`` 的优先级**永远高于**这个文件，
#:   临时切换账号时不需要去改文件。
SESSION_FILE: Final[Path] = PROJECT_ROOT / ".session.json"

#: 是否允许把登录结果写进会话文件（环境变量 ``CHERRYTALE_SAVE_SESSION=0`` 可关闭）。
#:
#: 默认**开启**：命令行每次都是新进程，不落盘就得靠人手复制令牌，而手工环节
#: 恰恰最容易出错（复制漏字符 → 服务端只回含糊的"包异常"）。
SESSION_SAVE_ENABLED: Final[bool] = (
    os.getenv("CHERRYTALE_SAVE_SESSION", "1").strip() != "0"
)

#: 单独用环境变量给出游戏 playerId（一般不需要 —— 会话文件里已经带了）。
GAME_PLAYER_ID_ENV_NAME: Final[str] = "CHERRYTALE_GAME_PLAYER_ID"

# ---- 多账号令牌库（`.platform_accounts.json`）----
# 目的：**每个账号各存一份平台令牌**，下次开终端只要"选账号"就不必再输密码。
# 详见 ``client/account_store.py`` 的结构说明。
#
# ⚠️ 文件里含**明文** accessToken / refreshToken，因此已进两份忽略清单；
#    不进日志、不打印（打印一律走 ``SavedAccount.describe()`` 打码）。
PLATFORM_ACCOUNTS_FILE: Final[Path] = PROJECT_ROOT / ".platform_accounts.json"

#: 账号库最多保留几个账号。**超出时新增会被拒绝**（更新已有账号不受限）。
#:
#: 为什么要设上限：令牌是**长期凭据**，一个失控增长的凭据文件比"限制 5 个"危险得多；
#: 而真实使用场景（自己的几个号）很少超过 5 个。要更多就显式改环境变量。
PLATFORM_ACCOUNTS_MAX: Final[int] = int(os.getenv("CHERRYTALE_ACCOUNTS_MAX", "5"))

#: 选择用哪个已保存账号的环境变量名（值为**序号**或**邮箱**或 **userId**）。
#: 等价于命令行 ``--account``；两者取其一即可。
PLATFORM_ACCOUNT_ENV_NAME: Final[str] = "CHERRYTALE_ACCOUNT"

#: 账号密码所在的环境变量名（用于"首次登录/换号"时换取令牌，不落盘）。
PLATFORM_LOGIN_ACCOUNT_ENV_NAME: Final[str] = "CHERRYTALE_LOGIN_ACCOUNT"
PLATFORM_LOGIN_PASSWORD_ENV_NAME: Final[str] = "CHERRYTALE_LOGIN_PASSWORD"


# ---------------------------------------------------------------------------
# 2.8 竞技板块（本阶段只做荣耀之巅）运行开关
# ---------------------------------------------------------------------------
#: 「这场要打几场」的环境变量名。
#:
#: 【默认 0 = 只读，这是刻意的、也是最重要的一条】
#: 竞技类请求（39007 挑战 …）**一发出就扣次数**，而次数是玩家的真实资源。
#: 所以默认值必须是"一次都不打"：不加参数运行任务时只拉取并展示状态；
#: 想真打，必须用户**显式**用 ``--battles N``（或本环境变量）说出来。
TOP_PVP_BATTLES_ENV_NAME: Final[str] = "CHERRYTALE_TOP_PVP_BATTLES"

#: 默认场数（0 = 只读）
TOP_PVP_BATTLES: Final[int] = int(os.getenv(TOP_PVP_BATTLES_ENV_NAME, "0") or "0")

#: 单次运行的**硬上限**（即使命令行写 ``--battles 999`` 也不会超过它）。
#:
#: 【为什么在"用户给的场数"之外还要一道上限】
#: 命令行参数表达的是"人的意图"，而人会手误（多打一个 0 → 20 场变 200 场）。
#: 两道防线管的是两件不同的事：这一道防手误，用户那道防程序自己乱跑。
TOP_PVP_MAX_BATTLES_ENV_NAME: Final[str] = "CHERRYTALE_TOP_PVP_MAX_BATTLES"

#: 硬上限的默认值（按每日免费次数（``eOtherSettings.TopPvp_FreeChallengeTimes = 395``）
#: 留出余量，正常玩家一天用不到 10 场）
TOP_PVP_MAX_BATTLES: Final[int] = int(os.getenv(TOP_PVP_MAX_BATTLES_ENV_NAME, "10") or "10")

#: 两场战斗之间的间隔秒数。不是技术必需，而是**礼貌与自保**：
#: 连续猛打不像真人，也更容易被风控注意到。
TOP_PVP_INTERVAL_ENV_NAME: Final[str] = "CHERRYTALE_TOP_PVP_INTERVAL"

#: 默认间隔（秒）。
#:
#: 【为什么是 15 秒（2026-09-25 用户要求）】
#: 原来是 2 秒 —— 连打十场只要 18 秒，节奏比真人快得多。15 秒只是"不着急"，
#: 不改变任何协议行为；嫌慢就用环境变量 ``CHERRYTALE_TOP_PVP_INTERVAL``
#: 或在荣耀之巅卡片里把「每场间隔」填小（卡片里填的值优先于这里）。
TOP_PVP_INTERVAL_SECONDS: Final[float] = float(
    os.getenv(TOP_PVP_INTERVAL_ENV_NAME, "15") or "15"
)

#: 「跳过战斗演出」：工具是否会**主动把服务端开关置为开启**（需求：默认要）。
#:
#: 它是**服务端**保存的开关（写包 39019，读回 39002 字段 16），与安全无关：
#: 不跳过就只是"每次都要看回放动画"。
#:
#: 【2026-09-25 起它只是"逃生舱"，不再是界面开关】
#: 用户要求删掉「保留战斗演出」这个开关、行为固定为跳过，于是：
#: * 界面上（荣耀之巅卡片）与命令行（``--keep-battle-animation``）都**没有**它了；
#: * 本常量保留为最后的逃生舱：``CHERRYTALE_TOP_PVP_SKIP_BATTLE=0`` 时，
#:   工具**不再去改**服务端设置（万一 39019 的行为有变，还有一条不改账号设置的路）。
#: 注意只读模式（``battles=0``）本来就不会写这个开关 —— 与它无关。
TOP_PVP_SKIP_BATTLE_ENV_NAME: Final[str] = "CHERRYTALE_TOP_PVP_SKIP_BATTLE"

#: 默认值（``False`` = 按逃生舱处理，即"不去动服务端那个开关"）
TOP_PVP_SKIP_BATTLE_DEFAULT: Final[bool] = (
    os.getenv(TOP_PVP_SKIP_BATTLE_ENV_NAME, "1").strip() != "0"
)

#: 荣耀之巅的「赛区/关卡段 ID」环境变量名（``39011`` 请求的第一个字段）。
#:
#: 【为什么它是"照抄实测的魔法值"而不是从配置表推出来的】
#: 真实客户端在挑战前会先发 ``39011 TopTeamEditInfoPacket{sectionID, actionType}``
#: （抓包实测 ``726010017`` / ``0``），而这个编号在本地 ``TopPvp*`` 配置表里**查不到**。
#: 按本项目"宁可照抄实测，也不猜一个看起来合理的编号"的原则：
#: 默认值用实测值，同时允许用环境变量覆盖 —— 与 ``BBQ_MENU_ID`` 同一套处理方式。
TOP_PVP_SECTION_ID_ENV_NAME: Final[str] = "CHERRYTALE_TOP_PVP_SECTION_ID"

#: 赛区/关卡段 ID（实测值；服务端行为变化时改环境变量即可，不必改代码）
TOP_PVP_SECTION_ID: Final[int] = int(
    os.getenv(TOP_PVP_SECTION_ID_ENV_NAME, "726010017") or "726010017"
)


# ---------------------------------------------------------------------------
# 2.9 降临/活动关卡「体力扫荡」运行开关
# ---------------------------------------------------------------------------
#: 单次运行**最多扫荡几次**（硬上限，防手误多打一个 0）。
#:
#: 【为什么在"服务端有体力兜底"之外还要一道上限】
#: 每扫一次都真的扣体力与扫荡券。实测客户端自己也会把用户点的次数削到
#: "体力可支撑的最大值"（点 5 次、体力只够 4 次 → 包里 ``times=4``）；
#: 我方再加一道硬上限，是为了让"程序算错"也不会变成一次大额消耗。
SWEEP_MAX_TIMES_ENV_NAME: Final[str] = "CHERRYTALE_SWEEP_MAX_TIMES"

#: 默认硬上限 = **客户端扫荡档位的最大值**（``StageModule.eSweepType`` 的 ``Ten = 10``）。
#: 客户端只有 1 / 5 / 10 三档（见 ``models.activity_stage.SWEEP_TIME_CHOICES``），
#: 所以 10 既是"档位顶"、也是"硬上限"：两个概念在这里重合。
SWEEP_MAX_TIMES: Final[int] = int(os.getenv(SWEEP_MAX_TIMES_ENV_NAME, "10") or "10")

#: 两次扫荡之间的间隔（与 ``TOP_PVP_INTERVAL_SECONDS`` 同理：礼貌与自保）。
#:
#: ★ 2026-10-04：从"固定值"改成**在 [MIN, MAX] 之间随机摇动**。原因是
#: "扫荡到清空体力 / 次数耗尽"会连发几十轮，固定间隔会形成非常规整的请求节律
#: （一眼像脚本）；随机 1~2 秒既保持礼貌，也更接近真人手点的节奏。
SWEEP_INTERVAL_ENV_NAME: Final[str] = "CHERRYTALE_SWEEP_INTERVAL"

#: 随机间隔**下限**（秒）。旧环境变量 ``CHERRYTALE_SWEEP_INTERVAL`` 仍被读作下限，
#: 以兼容既有配置（原先它是"固定值"，现在等于下限）。
SWEEP_INTERVAL_MIN_SECONDS: Final[float] = float(
    os.getenv(SWEEP_INTERVAL_ENV_NAME, "1.0") or "1.0"
)

#: 随机间隔**上限**（秒）的环境变量名与默认值。
SWEEP_INTERVAL_MAX_ENV_NAME: Final[str] = "CHERRYTALE_SWEEP_INTERVAL_MAX"
SWEEP_INTERVAL_MAX_SECONDS: Final[float] = float(
    os.getenv(SWEEP_INTERVAL_MAX_ENV_NAME, "2.0") or "2.0"
)

#: 兼容旧名：原先的"固定间隔"现在等价于随机**下限**。
SWEEP_INTERVAL_SECONDS: Final[float] = SWEEP_INTERVAL_MIN_SECONDS


def sweep_interval() -> float:
    """本次扫荡之间该等多久（秒）：在 ``[MIN, MAX]`` 之间随机摇动。

    :return: 随机浮点数；上限小于下限时退回下限（不做无意义摇动）。

    .. note::
       其它"同类请求连发"的场景请用 :func:`polite_interval`（同一个含义）。
    """
    low = max(SWEEP_INTERVAL_MIN_SECONDS, 0.0)
    high = max(SWEEP_INTERVAL_MAX_SECONDS, low)
    return random.uniform(low, high)


def polite_interval() -> float:
    """两次**同类业务请求**之间的拟人化随机间隔（秒）。

    :return: 与 :func:`sweep_interval` 同源的随机值（默认 1~2 秒）。

    【为什么复用扫荡那组上下限（2026-10-04 安全审计 P2）】
    它表达的本来就是同一件事 —— "别让同类请求以完全固定的节律连发"。
    再开一套配置只会让使用者多一个要调的地方，而默认值（1~2 秒）
    对这类低频连发场景同样合适（例如工会的固定 5 次连捐）。

    【调用方必须用可中断的等待】
    用 ``services.cancellation.sleep_interruptible(config.polite_interval())``，
    不要用 ``time.sleep`` —— 否则用户点「停止执行」后还要等满这一轮。
    """
    return sweep_interval()

#: 扫荡请求里 ``useSweepTicket`` 的取值（**实测真实客户端固定发 1**）。
#:
#: 【⚠️ 适用范围（2026-10-03 修订）】这个 1 只对**降临/活动关卡**扫荡成立
#: （依据：``captures/降临扫荡抓包1.saz``）。10.3 的素材抓包
#: （``captures/素材关卡扫荡.saz``）证明**素材/元素关**的扫荡固定发 0、
#: 不查券也不消耗券（三次 11009 的 costList 均为空）——两类关卡是两套规则，
#: 各自钉死在下面的两个常量里，不要混用。
SWEEP_USE_TICKET_VALUE: Final[int] = 1

#: 素材/元素关扫荡 ``useSweepTicket`` 的取值（**10.3 素材抓包实测固定发 0**）。
#:
#: 与 :data:`SWEEP_USE_TICKET_VALUE`（活动关 = 1）分开定义的原因见上；
#: 取 0 意味着素材扫荡**不需要扫荡券**，因此素材任务里"0 张券拒扫"的
#: 闸门一并移除（真实客户端不查券直接扫）。
MATERIAL_SWEEP_USE_TICKET_VALUE: Final[int] = 0

#: 「扫荡到清空体力 / 次数耗尽」的**单次运行安全上限**（最多推进多少次扫荡）。
#:
#: 【为什么需要它】
#: 这类循环的停止条件是"服务端拒绝"（体力不足 / 券不足 / 次数已满），正常不会跑飞；
#: 但万一服务端异常（例如永远回成功），没有上限就会一直发包。按用户给的经验值取：
#: **体力很难超过 2000 点，而活动关最小的单次消耗是 10 点 → 最多 200 次。**
#: 素材关的单日次数远小于此，同样被这一道上限兜住。
SWEEP_UNTIL_EMPTY_MAX_TIMES_ENV_NAME: Final[str] = "CHERRYTALE_SWEEP_UNTIL_EMPTY_MAX_TIMES"

#: 默认安全上限（次）
SWEEP_UNTIL_EMPTY_MAX_TIMES: Final[int] = int(
    os.getenv(SWEEP_UNTIL_EMPTY_MAX_TIMES_ENV_NAME, "200") or "200"
)


# ---------------------------------------------------------------------------
#: 活动区域 / 关卡候选的**本地缓存**文件（``11025`` / ``11003`` 的只读快照）。
#:
#: 【为什么需要它（2026-09-22）】
#: 降临关卡的区域与关卡**一周（实测两周）才轮换一次**，而网页上的
#: 「活动区域」「指定关卡」下拉每次都需要那份清单。若每次打开页面都重新拉，
#: 就等于"每开一次页面多发两个只读包"——既没必要，也白白增加风控暴露面。
#: 所以把清单落盘，之后直接用；只在**过期**或用户点「更新区域」时才重拉。
#:
#: 【安全约束】
#: 文件里含 ``player_id``（不含令牌），已加入 ``.gitignore``；
#: 且它**只服务界面** —— 真正扫荡时 ``tasks/sweep.py`` 仍现场发 11025/11003，
#: 绝不拿缓存里的"是否已通关"做判断。
ACTIVITY_CACHE_FILE_ENV_NAME: Final[str] = "CHERRYTALE_ACTIVITY_CACHE"

#: 缓存文件默认路径（与 ``.session.json`` 同级：都在工程根、带前导点）。
ACTIVITY_CACHE_FILE: Final[Path] = Path(
    os.getenv(ACTIVITY_CACHE_FILE_ENV_NAME, "") or (PROJECT_ROOT / ".activity_cache.json")
)

#: 自动任务「**每游戏日只跑一次**」的记账文件（2026-10-04 用户需求）。
#:
#: 【为什么不继续记在浏览器的 localStorage 里】
#: localStorage 是**按 origin 隔离**的：换一个端口（``--port 8766``）、
#: 或桌面壳与 Edge 回落窗口各自使用独立的存储，记录就丢了 ——
#: 表现正是"今天第一次登录之后又登录一次，这三个任务又跑了一遍"。
#: 记账挪到服务端之后，无论从哪个窗口 / 哪个端口进来，同一角色同一游戏日
#: 都只会自动执行一次（见 ``services/daily_state.py``）。
#:
#: 【安全约束】文件里只有区服号与角色号（不含令牌），已加入 ``.gitignore``。
AUTO_DAILY_STATE_FILE_ENV_NAME: Final[str] = "CHERRYTALE_AUTO_DAILY_STATE"

#: 记账文件的默认路径（工程根、带前导点，与活动缓存同级同风格）。
AUTO_DAILY_STATE_FILE: Final[Path] = Path(
    os.getenv(AUTO_DAILY_STATE_FILE_ENV_NAME, "")
    or (PROJECT_ROOT / ".daily_auto_state.json")
)

#: 宴席「哪一顿吃过了」的服务端记账文件（**与自动任务记账刻意分开**）。
#:
#: 【为什么另开一份，不复用 AUTO_DAILY_STATE_FILE】
#: 1. **换日口径不同**：自动任务按游戏日（05:00），宴席按 :func:`models.daily_reset.banquet_day`
#:    （06:00）—— 同一份文件里放两套不同"今天"，只会在跨 05:00–06:00 时互相污染；
#: 2. **语义不同**：``auto_done`` 是"任务名清单"，宴席是"早/晚两顿各自的完成位"。
#:    混在一起后，将来任何一方改结构都会牵动另一方（隐性耦合）。
#: 3. 宴席是**手动任务**，不参与 ``is_once_per_day`` 那套判重 —— 它的"吃过"
#:    只用来给界面置灰（并顺带阻止重复发包），不参与"自动执行跳过"。
#:
#: 【安全约束】文件里只有区服号与角色号（不含令牌），已加入 ``.gitignore``。
BANQUET_STATE_FILE_ENV_NAME: Final[str] = "CHERRYTALE_BANQUET_STATE"

#: 宴席记账文件的默认路径（工程根、带前导点，与 ``.daily_auto_state.json`` 同级同风格）。
BANQUET_STATE_FILE: Final[Path] = Path(
    os.getenv(BANQUET_STATE_FILE_ENV_NAME, "")
    or (PROJECT_ROOT / ".banquet_state.json")
)

#: 登录后自动签到的记账文件（2026-10-06 用户需求：内置钩子、无开关、每天只发一次）。
#:
#: 【为什么另开一份，不复用 AUTO_DAILY_STATE_FILE】换日口径不同：
#: 自动任务按游戏日（05:00，见 ``models/daily_reset.game_day``），
#: 登入奖励按**自然日 0 点**结算（2026-10-06 抓包实证：03:09 登入即入账当日
#: 奖励）。同一份文件里放两套不同"今天"，只会在 00:00–05:00 之间互相污染
#: （与宴席 06:00 另开一份的理据同构）。链路本体见 ``tasks/login_signin.py``。
#:
#: 【安全约束】文件里只有区服号与角色号（不含令牌），已加入 ``.gitignore``。
LOGIN_SIGNIN_STATE_FILE_ENV_NAME: Final[str] = "CHERRYTALE_LOGIN_SIGNIN_STATE"

#: 登录签到记账文件的默认路径（工程根、带前导点，与 ``.banquet_state.json`` 同级同风格）。
LOGIN_SIGNIN_STATE_FILE: Final[Path] = Path(
    os.getenv(LOGIN_SIGNIN_STATE_FILE_ENV_NAME, "")
    or (PROJECT_ROOT / ".login_signin_state.json")
)

#: 钻石使用总闸（用户偏好）的服务端持久化文件。
#:
#: 【为什么不能记在浏览器的 localStorage 里（2026-10-05 二次踩坑）】
#: 上一版把总闸记在 localStorage，结果打包 EXE 后"开一次、重启就回关"：
#: EXE 的界面窗口是 Edge ``--app`` 回退方案，其 ``--user-data-dir`` 指向
#: ``PROJECT_ROOT/tmp/gui-browser-profile`` —— ``tmp/`` 属过程产物目录，
#: 一清理存储全丢；桌面壳与浏览器、不同端口之间 localStorage 也互不相通
#: （与 autoDone 判重 2026-10-04 搬服务端是**同一个病根**，见
#: ``services/daily_state.py`` 的模块文档）。
#: 总闸表达"这台机器允许这个工具花钻石"，属**用户偏好**，权威状态必须在服务端，
#: localStorage 只做服务端不可用时的兜底（见 ``services/spend_state.py``）。
#:
#: 【安全约束】文件里只有一个布尔开关与时间戳（不含任何凭据），已加入 ``.gitignore``。
SPEND_STATE_FILE_ENV_NAME: Final[str] = "CHERRYTALE_SPEND_STATE"

#: 总闸持久化文件的默认路径（工程根、带前导点，与 ``.daily_auto_state.json`` 同级同风格）。
SPEND_STATE_FILE: Final[Path] = Path(
    os.getenv(SPEND_STATE_FILE_ENV_NAME, "")
    or (PROJECT_ROOT / ".spend_state.json")
)

#: 任务勾选/参数（selection）服务端持久化文件的**环境变量名**（2026-10-05）。
#:
#: 【安全约束】文件里只有界面勾选/参数与时间戳（不含任何凭据），已加入 ``.gitignore``。
SELECTION_STATE_FILE_ENV_NAME: Final[str] = "CHERRYTALE_SELECTION_STATE"

#: 任务勾选/参数持久化文件的默认路径（工程根、带前导点，与 ``.spend_state.json`` 同级同风格）。
SELECTION_STATE_FILE: Final[Path] = Path(
    os.getenv(SELECTION_STATE_FILE_ENV_NAME, "")
    or (PROJECT_ROOT / ".selection_state.json")
)

#: 自动刷新的**星期**（``datetime.weekday()`` 口径：周一 = 0 → 周三 = 2）。
#:
#: 【为什么是"周三"而不是读服务端的 endTime】
#: 服务端给的 ``endTime`` 只能说明"旧区域什么时候结束"，**说明不了
#: "新区域什么时候出现"** —— 而"更新"必须在新区域出现之后才有效。
#: 用户实测：降临关卡每周三更新（20:00 之后）。所以这里按**固定星期 + 时刻**判定，
#: 界面上同时显示服务端的真实起止时间，规则不符时一眼能看出来。
ACTIVITY_REFRESH_WEEKDAY: Final[int] = int(
    os.getenv("CHERRYTALE_ACTIVITY_REFRESH_WEEKDAY", "2") or "2"
)

#: 自动刷新的**时刻**（24 小时制，按 ``models.activity_stage.DISPLAY_TZ`` = UTC+8 理解）。
ACTIVITY_REFRESH_HOUR: Final[int] = int(
    os.getenv("CHERRYTALE_ACTIVITY_REFRESH_HOUR", "20") or "20"
)

#: **游戏日的换日时刻**（24 小时制，按 ``models.activity_stage.DISPLAY_TZ`` = UTC+8 理解）。
#:
#: 【为什么与 ACTIVITY_REFRESH_HOUR 分开】
#: 活动区域表是"每周三 20:00 换一期"，日常任务是"每天 05:00 换一天" ——
#: 周期与时刻都不同，共用一个常量迟早会把其中一个改错。
#:
#: 【05:00 从哪来】
#: 用户实测：游戏服务器**不是** 00:00 换日，而是**凌晨 05:00** 刷新日常。
#: 网页靠这个时刻判断"今天有没有跑过日常"（见 ``models/daily_reset.py``）。
#: 算错的两种后果都不小：算早了半夜会自动多跑一遍；算晚了早上 05:00 之后
#: 打开页面，界面说"今天已完成"、实际一件都没做。
DAILY_RESET_HOUR: Final[int] = int(
    os.getenv("CHERRYTALE_DAILY_RESET_HOUR", "5") or "5"
)

#: 缓存**兜底有效期**（天）。超过它一律视为过期，即使"周三规则"没触发。
#:
#: 【为什么要兜底】
#: 万一游戏改了更新周期（或我们的"周三"猜错了），"按周三判定"会变成
#: **永远不刷新**，那是最难发现的一类故障（数据看着正常，只是越来越旧）。
#: 兜底上限保证"最多旧 7 天"。
ACTIVITY_CACHE_MAX_AGE_DAYS: Final[int] = int(
    os.getenv("CHERRYTALE_ACTIVITY_MAX_AGE_DAYS", "7") or "7"
)




# ---------------------------------------------------------------------------
# 3. HTTP 行为参数
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT: Final[float] = float(os.getenv("CHERRYTALE_TIMEOUT", "10"))
MAX_RETRIES: Final[int] = int(os.getenv("CHERRYTALE_MAX_RETRIES", "3"))
RETRY_BACKOFF: Final[float] = float(os.getenv("CHERRYTALE_RETRY_BACKOFF", "0.8"))

#: 是否校验 TLS 证书。本地用 Charles/Fiddler 抓包时需要临时设为 0。
VERIFY_TLS: Final[bool] = os.getenv("CHERRYTALE_VERIFY_TLS", "1") == "1"

#: 是否改用**操作系统证书信任库**（Windows = CryptoAPI/schannel）来校验 HTTPS 证书。
#:
#: 【为什么默认开启】实测目标网关下发的证书链中，根证书那一环是
#: 「交叉签名版 GTS Root R4」（由 GlobalSign Root CA 签发），
#: Python 自带的 OpenSSL 3.0.18 无法为它建链，直接报
#: ``unable to get local issuer certificate``；而 curl / 浏览器用系统库就正常。
#: 开启本开关后，Python 与浏览器行为一致（详见 client/tls.py 的排查记录）。
#:
#: 关闭方式：``export CHERRYTALE_USE_SYSTEM_TRUST_STORE=0``
USE_SYSTEM_TRUST_STORE: Final[bool] = (
    os.getenv("CHERRYTALE_USE_SYSTEM_TRUST_STORE", "1") == "1"
)

#: 抓包代理，例如 ``http://127.0.0.1:8888``；留空表示直连。
HTTP_PROXY: Final[str] = os.getenv("CHERRYTALE_HTTP_PROXY", "")


# ---------------------------------------------------------------------------
# 4. 账号信息（严禁硬编码，只从环境变量读取）
# ---------------------------------------------------------------------------
ACCOUNT_UID: Final[str] = os.getenv("CHERRYTALE_UID", "")
ACCOUNT_TOKEN: Final[str] = os.getenv("CHERRYTALE_TOKEN", "")


def has_credentials() -> bool:
    """是否已提供账号凭据。用于在真正发包前做友好拦截。"""
    return bool(ACCOUNT_UID and ACCOUNT_TOKEN)


# ---------------------------------------------------------------------------
# 5. 运行开关与日志
# ---------------------------------------------------------------------------
DEBUG: Final[bool] = os.getenv("CHERRYTALE_DEBUG", "0") == "1"
LOG_LEVEL: Final[str] = os.getenv("CHERRYTALE_LOG_LEVEL", "INFO").upper()
LOG_FORMAT: Final[str] = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT: Final[str] = "%H:%M:%S"


# ---------------------------------------------------------------------------
# 6. 平台认证网关（sadpki-portal-v2）
# ---------------------------------------------------------------------------
# 这组配置与上面的「游戏服务端」是**两套不同的服务**：
#   · 平台网关：账号体系、登录态、游戏绑定关系（本项目的入口）；
#   · 游戏服务端：进游戏之后的业务接口（protobuf over BestHTTP）。
# 二者域名与鉴权方式都不一样，所以分开配置，避免调试时互相干扰。

PLATFORM_BASE_URL: Final[str] = os.getenv(
    "CHERRYTALE_PLATFORM_BASE_URL", "https://sadpki-portal-v2.ebuajk.com"
)

#: 查询当前令牌对应的用户信息
USER_INFO_PATH: Final[str] = "/api/v2/user"

#: 查询「本账号是否已绑定某游戏」的状态；``{game_id}`` 为占位符
GAME_STATUS_PATH: Final[str] = "/api/v2/user/games/status/{game_id}"

#: 默认查询的游戏 ID（27 = 本项目对应的 Cherrytale）
DEFAULT_GAME_ID: Final[int] = int(os.getenv("CHERRYTALE_GAME_ID", "27"))

# ---- 客户端身份 Header ----
# 【重要事实，来自 HAR 实测 + 你确认】
# 平台网关的认证请求来自**内嵌 Edge WebView2 浏览器**，不是 Unity 的原生网络栈。
# 证据：UA 是 Edg/153、带 sec-ch-ua: "Microsoft Edge WebView2"，并有
#       Origin / Referer / Sec-Fetch-* 等网页专有头；
#       而 UnityWebRequest 的 User-Agent 与 X-Unity-Version 在这些请求里完全不存在。
# 结论：平台网关必须用「浏览器身份」发包；Unity 头保留给将来的游戏 API 侧。

#: Unity 原生网络栈身份（**不用于平台网关**，仅保留备用）
UNITY_USER_AGENT: Final[str] = os.getenv(
    "CHERRYTALE_UNITY_USER_AGENT",
    "UnityPlayer/2020.3.49f1 (UnityWebRequest/1.0, libcurl/7.84.0-DEV)",
)
UNITY_VERSION: Final[str] = os.getenv("CHERRYTALE_UNITY_VERSION", "2020.3.49f1")

#: WebView 浏览器身份（平台网关使用，逐字符对齐 HAR）
WEBVIEW_USER_AGENT: Final[str] = os.getenv(
    "CHERRYTALE_WEBVIEW_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0",
)
WEBVIEW_SEC_CH_UA: Final[str] = os.getenv(
    "CHERRYTALE_WEBVIEW_SEC_CH_UA",
    '"Microsoft Edge WebView2";v="153", "Not_A Brand";v="8", '
    '"Chromium";v="153", "Microsoft Edge";v="153"',
)

#: 设备身份的**内置默认值**。
#:
#: 【为什么单独提出来】把默认值抽成具名常量之后，
#: :func:`device_identity_source` 才能判断"当前到底是在用默认值、还是被覆盖过"，
#: 从而在自检输出里如实标出"这一项还是默认值"。
#:
#: 【为什么 DEVICE_ID 是占位符（2026-10-05 安全审计）】
#: 它原先填的是开发机的真实设备标识（40 位 hex）。设备标识与平台 userId 一起
#: 足以复现一次完整登录报文，属凭据类信息，因此改为**等长占位符**。
#: 长度保持 40 字符，是为了让依赖「逐字节等长」的协议断言继续成立
#: （见 ``protocol_samples/session_samples.py``）。
#:
#: 真机使用时应经环境变量 ``CHERRYTALE_DEVICE_ID`` 或 ``config_local.py`` 覆盖，
#: 不要把这个占位符当成可用的设备号。型号/系统版本不构成凭据，保留真实取值。
_DEVICE_ID_DEFAULT: Final[str] = "0" * 40
_DEVICE_MODEL_DEFAULT: Final[str] = "Dell G15 5520 (Dell Inc.)"
_OS_VERSION_DEFAULT: Final[str] = "Windows 11  (10.0.26200) 64bit"


def _read_identity_from_local_config(name: str) -> str:
    """从可选的 ``config_local.py`` 读取一个设备身份项。

    :param name: ``"DEVICE_ID"`` / ``"DEVICE_MODEL"`` / ``"OS_VERSION"``。
    :return: 取到的字符串；文件不存在或不含该项时返回空串（属正常情况，不报错）。

    【为什么设备身份也要支持 config_local.py（2026-10-04 安全审计 P2）】
    这三项与 ``AUTH_TOKEN`` 同属"只在你这台机器 / 这个端上成立"的配置：
    内置默认值是**采集自开发机**的取值，换一台机器、尤其是换到**安卓端**
    就不该继续上报它。环境变量当然也能覆盖，但手机上没有一个顺手的
    "设置环境变量"入口，``config_local.py``（已在 .gitignore 里）是那边
    唯一省事的落点。

    .. note::
       刻意**不**用 ``"内置默认"`` 之外的兜底：值是否合理由使用者决定，
       这里只负责"能配得上"。
    """
    try:
        import config_local  # type: ignore[import-not-found]
    except ImportError:
        return ""
    return str(getattr(config_local, name, "") or "")


def _identity_value(env_name: str, local_name: str, default: str) -> str:
    """按「环境变量 → config_local.py → 内置默认」解析一个设备身份项。"""
    from_env = os.getenv(env_name, "").strip()
    if from_env:
        return from_env
    from_local = _read_identity_from_local_config(local_name).strip()
    if from_local:
        return from_local
    return default


#: 设备标识（对应请求头 ``deviceId``）。
#: 【新手最爱踩的坑】它与 JWT 载荷里的 ``device_id`` **不是同一个值**：
#:   本值 40 位十六进制（SHA-1 摘要），JWT 里的 device_id 是 64 位十六进制（SHA-256）。
#: 两者都真实存在、用途不同 —— 因此**绝不要**在代码里校验二者相等，那会一直误报。
#:
#: .. note::
#:    **游戏登录（1001）用的也是这个值** —— 实测抓包里 1001 的 ``deviceID`` 字段
#:    与本常量一字不差。所以它既是平台网关的请求头，也是游戏登录的设备标识。
#:
#: 【覆盖方式（2026-10-04 安全审计 P2）】按「环境变量 → ``config_local.py``
#: → 内置默认」解析。内置默认值采集自**开发机**：换一台机器、尤其是
#: **安卓端**继续上报它就与实际设备不符（型号/系统版本同理），
#: 所以换机器时应当覆盖。想知道当前用的是哪一路，看
#: :func:`device_identity_source`（自检输出里会带上）。
DEVICE_ID: Final[str] = _identity_value(
    "CHERRYTALE_DEVICE_ID", "DEVICE_ID", _DEVICE_ID_DEFAULT
)


# ---- 游戏登录（1001）的取值（**实测自 330 字节登录包**）----
# 抓包来源：Fiddler 抓到的 1001 请求，完整 body 330 字节。
# 关键锚点常量存在 protocol_samples/gateway_samples.py 里，可据此复核。
# 下面这些值不是猜的，而是逐字段从真实字节里解出来的。

#: 社交账号类型（实测 ``9`` = Erolabs 渠道）。
#: 它告诉服务端「用哪个平台的账号登录」，服务端据此去对应平台校验
#: ``socialAccount`` / ``socialOpenId``。
SOCIAL_ACCOUNT_TYPE_EL: Final[int] = int(
    os.getenv("CHERRYTALE_SOCIAL_ACCOUNT_TYPE", "9")
)

#: 自定义账号名（实测 ``"ecchigame"``）—— 渠道标识，不是玩家账号。
GAME_CUSTOM_ACCOUNT: Final[str] = os.getenv(
    "CHERRYTALE_CUSTOM_ACCOUNT", "ecchigame"
)

#: 设备型号（实测 ``"Dell G15 5520 (Dell Inc.)"``）。
#: 服务端大概率只把它用于风控画像、不参与校验，但仍建议与真实机器保持一致。
#: ⚠️ 安卓端继续上报这个"桌面机型"是与实际设备不符的，应当覆盖
#: （见 ``DEVICE_ID`` 的覆盖方式说明）。
DEVICE_MODEL: Final[str] = _identity_value(
    "CHERRYTALE_DEVICE_MODEL", "DEVICE_MODEL", _DEVICE_MODEL_DEFAULT
)

#: 系统版本（实测 ``"Windows 11  (10.0.26200) 64bit"``）。
#: ⚠️ 注意 ``Windows 11`` 与括号之间是**两个空格** —— 照抄实测值，别"顺手规范化"。
OS_VERSION: Final[str] = _identity_value(
    "CHERRYTALE_OS_VERSION", "OS_VERSION", _OS_VERSION_DEFAULT
)


def device_identity_source() -> dict[str, str]:
    """设备身份三项**各自**的取值来源（供自检核对，不涉及任何凭据）。

    :return: ``{"DEVICE_ID": 来源, "DEVICE_MODEL": 来源, "OS_VERSION": 来源}``，
        来源取值是 ``"环境变量"`` / ``"config_local.py"`` /
        ``"内置默认（采集自开发机，建议覆盖）"``。

    【为什么逐项返回而不是给一个总的"来自哪里"】
    三项完全可以来自不同地方（例如 ``DEVICE_ID`` 用环境变量、型号用
    ``config_local.py``）。只给一个总来源会把这个事实盖掉，而使用者恰恰
    需要知道"到底还有哪几项没覆盖"。
    """
    pairs = (
        ("DEVICE_ID", "CHERRYTALE_DEVICE_ID"),
        ("DEVICE_MODEL", "CHERRYTALE_DEVICE_MODEL"),
        ("OS_VERSION", "CHERRYTALE_OS_VERSION"),
    )
    result: dict[str, str] = {}
    for name, env_name in pairs:
        if os.getenv(env_name, "").strip():
            result[name] = "环境变量"
        elif _read_identity_from_local_config(name).strip():
            result[name] = "config_local.py"
        else:
            result[name] = "内置默认（采集自开发机，建议覆盖）"
    return result


# ---- ``vCode`` 的算法密钥（**逆向结论**）----
#
# 信封字段 14 的 ``vCode`` 不是随机值，而是客户端算出来的：
#
#     source = f"{packetID}{token}{settingMd5}{timeStampClient}"   # 直接相连
#     vCode  = UPPER( MD5( Base64(source) + GAME_V_CODE_KEY ) )
#
# 推导过程：反汇编 ``GameAssembly.dll`` 得到 ``NetWorkModule.GetRootPacketVCode``
# 的完整调用链（``GetGlobalVerifyData`` → ``GetGlobalVCode`` → ``GetMd5Hash``），
# 再从 ``stringliteral.json`` 的 27500+ 个字面量里搜出这个 KEY，
# 最后用两个真实抓包样本双向验证通过。
#
# 【为什么不再把 KEY 写进源码】
# 它是游戏客户端里的常量、本身不含个人凭据；但**公开仓库里不该出现它** ——
# 那等于替官方把这道校验的钥匙摆出来。所以改为**必须由本机提供**：
#
#   方式一（推荐）：``config_local.py`` 里写 ``GAME_V_CODE_KEY = "…"``
#   方式二：环境变量 ``CHERRYTALE_V_CODE_KEY``
#
# 两项都没有时，取值为空串 → ``crypto.vcode.make_v_code()`` 会显式抛错，
# 不会静默算出一个错误的 vCode 让服务端回 ``ABNORMAL_PACKET``。
#
# 完整推导过程（含 RVA、反汇编证据）留在**本地** notes/vcode_algorithm.md，
# 该文件已排除在版本库之外。
GAME_V_CODE_KEY: Final[str] = (
    os.getenv("CHERRYTALE_V_CODE_KEY", "").strip()
    or _read_str_from_local_config("GAME_V_CODE_KEY")
)


# ---- 下面两个值：**仅供重放/复现某一次抓包时使用** ----
#
# 它们原本是为绕过"算法未知"而写死的（``tools/probe_1003_replay.py`` 会用到它俩）。
# 现在算法已还原（见上面的 ``GAME_V_CODE_KEY``），**正常流程不再需要** ——
# 信封会用当前时间戳现算 vCode。保留它们的唯一用途是：
# 把某一次抓包的字节**逐字节复现**出来做对比。

#: 某次抓包里的 ``timeStampClient``（毫秒）
GAME_TIME_STAMP_CLIENT: Final[int] = int(
    os.getenv("CHERRYTALE_TIME_STAMP_CLIENT", "1789905913885")
)

#: 与上面时间戳配对的 ``vCode``（32 位十六进制大写）
GAME_V_CODE: Final[str] = os.getenv(
    "CHERRYTALE_V_CODE", "92AEDFDDD86362AD5A1FB38C817EAC80"
)

# ---- 平台网关的「网页上下文」Header（同样来自 HAR 实测）----
#: 请求来源页。部分 WAF/网关会校验同源，缺失可能被直接拒绝。
PLATFORM_LOGIN_REFERER: Final[str] = os.getenv(
    "CHERRYTALE_REFERER", f"{PLATFORM_BASE_URL}/login/loginPage"
)
#: 页面语言标记。实测存在；服务端可能据此决定返回文案的语言。
PLATFORM_LANG: Final[str] = os.getenv("CHERRYTALE_LANG", "cn")
#: 声明可接受的响应类型。
PLATFORM_ACCEPT: Final[str] = "application/json, text/plain, */*"
#: 浏览器语言偏好。
PLATFORM_ACCEPT_LANGUAGE: Final[str] = os.getenv(
    "CHERRYTALE_ACCEPT_LANGUAGE", "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6"
)
#: GET 请求其实不需要 Content-Type，但实测客户端带上了，为保真照抄。
PLATFORM_CONTENT_TYPE: Final[str] = "application/json"
#: 关于 ``Accept-Encoding``：这里刻意**不设置**，交给 requests 的默认值
#: （``gzip, deflate, zstd``——三者在 Python 3.14 上都能解码，zstd 由标准库
#: ``compression.zstd`` 原生支持）。HAR 里的 WebView 还会声明 ``br``，
#: 但那需要额外的 brotli 依赖；**声明了却解不开会收到二进制乱码**，
#: 比少声明一项难查得多，所以不声明。

# ---- 平台账号密码登录（EROLABS V2 门户）----
# 端点与请求形状**全部照抄上游实测**（lucima-tools 的 backend/portal.py + config.py），
# 并在本机对 Cherrytale 的账号真跑通过（2026-09-21）：
#
#     ① POST /api/v2/captcha/verify  {"captcha": [0, 0, 0, 1, 0]}   → data.hashCode
#     ② POST /api/v2/account/login   {account, password(base64), gameId, hashCode(int)}
#                                    → data.accessToken / refreshToken / userId
#     ③ POST /api/v2/token/access    （空 body，Authorization: Bearer <refreshToken>）
#
# 【关于①的那个固定数组 —— 别被"验证码"三个字吓到】
# 门户的 captcha 对**前端交互**是硬要求（hashCode 为空时登录按钮被禁用），
# 但服务端对这个预检请求很宽松：给它一个固定的整数数组，它照样签发 hashCode。
# 上游（其注释称"流程与 Android SDK 的 V2 分支一致"）就是靠这一点做无头登录的，
# 因此本项目**不需要**解滑块、不需要任何图像依赖（Pillow/numpy 都不装）。
#
# ⚠️ 这是一条**依赖服务端宽松策略**的路径（不是我们破解了什么）。若将来收紧，
#    现象是②回 `4139 Captcha 驗證失敗`，届时要么改用别的入口，要么真做滑块求解。
PLATFORM_LOGIN_CAPTCHA_PATH: Final[str] = os.getenv(
    "CHERRYTALE_LOGIN_CAPTCHA_PATH", "/api/v2/captcha/verify"
)
PLATFORM_LOGIN_PATH: Final[str] = os.getenv(
    "CHERRYTALE_LOGIN_PATH", "/api/v2/account/login"
)
PLATFORM_LOGIN_REFRESH_PATH: Final[str] = os.getenv(
    "CHERRYTALE_LOGIN_REFRESH_PATH", "/api/v2/token/access"
)


def _json_object_env(name: str, default: dict[str, object]) -> dict[str, object]:
    """读一个 **JSON 对象型**环境变量；缺失或写坏了都退回默认值。

    :param name: 环境变量名。
    :param default: 默认值（返回的是**副本**，避免调用方就地修改把默认值也改了）。
    :return: 解析出来的 dict，或默认值的副本。

    【为什么用 JSON 而不是拆成一堆小变量】
    验证码预检的请求体（``{"captcha": [0, 0, 0, 1, 0]}``）整体是一个结构。
    拆成 ``A=0 B=0 ...`` 既难读、又会在结构变化时改一堆代码；一个 JSON 字符串
    就能原样覆盖它 —— 上游换数组形状时我们只改环境变量，不动代码。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return dict(default)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return dict(default)
    return value if isinstance(value, dict) else dict(default)


#: ① 验证码预检的请求体。固定数组是**上游实测值**（见本节开头的说明）。
#: 想覆盖：``export CHERRYTALE_LOGIN_CAPTCHA_BODY='{"captcha":[...]}'``
PLATFORM_LOGIN_CAPTCHA_BODY: Final[dict[str, object]] = _json_object_env(
    "CHERRYTALE_LOGIN_CAPTCHA_BODY", {"captcha": [0, 0, 0, 1, 0]}
)

#: ② 登录请求体的字段名（与上游、与门户前端一致）。
#: 做成常量是为了"协议改名时改配置、不改代码"——与本文件既有的
#: ``AUTH_HEADER_NAME`` / ``AUTH_VALUE_TEMPLATE`` 同一个思路。
PLATFORM_LOGIN_ACCOUNT_FIELD: Final[str] = "account"
PLATFORM_LOGIN_PASSWORD_FIELD: Final[str] = "password"
PLATFORM_LOGIN_HASH_CODE_FIELD: Final[str] = "hashCode"
PLATFORM_LOGIN_GAME_ID_FIELD: Final[str] = "gameId"

#: 密码的编码方式。实测门户前端是 ``btoa(明文)``，即 **base64**（**不是** md5）。
#: 保留这个开关是为了万一上游改成其它编码时能先用配置顶住。
PLATFORM_LOGIN_PASSWORD_ENCODING: Final[str] = os.getenv(
    "CHERRYTALE_LOGIN_PWD_ENCODING", "base64"
)

#: 登录时带的游戏 ID。**实测 ``27`` = Cherrytale 可用**（2026-09-21 真跑通过）；
#: 上游的 Ark Re:Code 用的是 32 —— 同一个门户、不同游戏，所以这个值必须按游戏区分。
PLATFORM_LOGIN_GAME_ID: Final[int] = int(
    os.getenv("CHERRYTALE_LOGIN_GAME_ID", str(DEFAULT_GAME_ID))
)

#: 三步各自的超时（秒）。预检是纯计算所以短；登录要建会话所以长。
PLATFORM_LOGIN_CAPTCHA_TIMEOUT: Final[float] = float(
    os.getenv("CHERRYTALE_LOGIN_CAPTCHA_TIMEOUT", "45")
)
PLATFORM_LOGIN_TIMEOUT: Final[float] = float(
    os.getenv("CHERRYTALE_LOGIN_TIMEOUT", "60")
)
PLATFORM_LOGIN_REFRESH_TIMEOUT: Final[float] = float(
    os.getenv("CHERRYTALE_LOGIN_REFRESH_TIMEOUT", "30")
)

#: 账号密码文件（**可选**）：仅当用户显式选择"把凭据落到文件"时才存在。
#: 内容两行（第 1 行账号、第 2 行密码），已在 ``.gitignore`` / ``.clineignore`` 里排除。
#: 默认流程**不写它** —— 交互式脚本用 ``getpass``，凭据只活在内存里。
PLATFORM_CREDENTIAL_FILE: Final[Path] = PROJECT_ROOT / ".platform_credentials"

# ---- 鉴权 ----
#: 鉴权 Header 的字段名。
#: TODO(protocol): 当前按最常见的 ``Authorization`` 处理。若抓包显示服务端用的是
#:     ``token`` / ``X-Token`` / ``Access-Token``，只需改这里（或设同名环境变量），
#:     不必改动 platform_client.py 一行代码。
AUTH_HEADER_NAME: Final[str] = os.getenv("CHERRYTALE_AUTH_HEADER", "Authorization")

#: 鉴权值的模板。默认 ``Bearer {token}``；若服务端要裸 token，把它设成 ``{token}``。
AUTH_VALUE_TEMPLATE: Final[str] = os.getenv("CHERRYTALE_AUTH_TEMPLATE", "Bearer {token}")

#: Token 本地文件（放在项目根目录，已在 .gitignore 中忽略）
AUTH_TOKEN_FILE: Final[Path] = PROJECT_ROOT / ".auth_token"


def _read_token_file(path: Path) -> str:
    """从本地文件读取 Token（取第一个非空、非注释行）。

    推荐用法::

        echo 'your-token-here' > .auth_token

    该文件已被 .gitignore 忽略，不会进入 git 历史。
    之所以支持「跳过 # 开头」的行，是为了让你能在里面写备注。
    """
    if not path.is_file():
        return ""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped
    except OSError:
        # 读文件失败（权限/占用）不应让整个程序起不来，退化为「没有 Token」
        return ""
    return ""


def _read_token_from_local_config() -> str:
    """尝试从可选的 ``config_local.py`` 读取 ``AUTH_TOKEN``。

    该文件同样被 .gitignore 忽略，适合存放「只在你这台机器上生效」的配置。
    它不存在或不含该变量时返回空字符串——属于正常情况，不该报错。
    """
    try:
        import config_local  # type: ignore[import-not-found]
    except ImportError:
        return ""
    return str(getattr(config_local, "AUTH_TOKEN", "") or "")


def resolve_auth_token() -> str:
    """按固定优先级解析 Token，返回第一个非空来源提供的值。

    优先级（高 → 低）：

    1. 环境变量 ``CHERRYTALE_AUTH_TOKEN`` —— 最推荐，不落到任何文件里；
    2. ``config_local.py`` 里的 ``AUTH_TOKEN`` —— 需要同时管理多个值时用；
    3. 项目根目录的 ``.auth_token`` 文件 —— 长期使用最省事。

    .. warning::
       **绝不要把真实 Token 写成本函数的默认值**。源码一旦带上凭据就等于永久泄露，
       git 历史即使后续删除也抹不干净。
    """
    from_env = os.getenv("CHERRYTALE_AUTH_TOKEN", "").strip()
    if from_env:
        return from_env

    from_local = _read_token_from_local_config().strip()
    if from_local:
        return from_local

    return _read_token_file(AUTH_TOKEN_FILE)


#: 当前生效的平台 Token（空字符串表示尚未配置）
AUTH_TOKEN: Final[str] = resolve_auth_token()


def has_auth_token() -> bool:
    """是否已提供平台 Token。"""
    return bool(AUTH_TOKEN)


def format_auth_value(token: str) -> str:
    """按模板把 Token 包装成最终的 Header 值，例如 ``Bearer eyJhb...``。"""
    return AUTH_VALUE_TEMPLATE.format(token=token)


def auth_headers(token: str | None = None) -> dict[str, str]:
    """生成鉴权 Header；没有 Token 时返回**空字典**。

    为什么不返回 ``{"Authorization": ""}``？因为空值 Header 在不少网关会被判为
    非法请求，于是你收到 400 而不是 401，反而干扰对真实原因的判断。
    """
    value = AUTH_TOKEN if token is None else token
    if not value:
        return {}
    return {AUTH_HEADER_NAME: format_auth_value(value)}


def platform_headers(include_auth: bool = True) -> dict[str, str]:
    """生成平台网关所需的请求头（**浏览器 / WebView 身份**，逐项来自 HAR 实测）。

    :param include_auth: 是否附加鉴权头；没有 Token 时不会附加（见 :func:`auth_headers`）。

    为什么不是 Unity 头？认证请求实际由内嵌 WebView 发出（见本文件开头的说明）。
    缺少 ``Origin`` / ``Referer`` / ``lang`` 这类网页上下文头时，
    部分网关会以风控理由直接拒绝请求 —— 而且报错往往含糊，极难定位。

    .. note::
       ``Sec-Fetch-*`` 这组头原本由浏览器自动生成，我们手工模拟是为了让请求
       在网关看来与真实 WebView 一致；``Content-Type`` 在 GET 上其实无意义，
       但实测客户端带上了，为保真照抄。
    """
    headers = {
        "User-Agent": WEBVIEW_USER_AGENT,
        "sec-ch-ua": WEBVIEW_SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "Accept": PLATFORM_ACCEPT,
        "Accept-Language": PLATFORM_ACCEPT_LANGUAGE,
        "Content-Type": PLATFORM_CONTENT_TYPE,
        "lang": PLATFORM_LANG,
        "Origin": PLATFORM_BASE_URL,
        "Referer": PLATFORM_LOGIN_REFERER,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "deviceId": DEVICE_ID,
    }
    if include_auth:
        headers.update(auth_headers())
    return headers


def game_status_path(game_id: int | None = None) -> str:
    """拼出「查询游戏绑定状态」的相对路径。

    :param game_id: 目标游戏 ID，默认取 :data:`DEFAULT_GAME_ID`。
    """
    resolved_id = DEFAULT_GAME_ID if game_id is None else game_id
    return GAME_STATUS_PATH.format(game_id=resolved_id)


def summary() -> dict[str, object]:
    """输出一份**不含机密**的配置快照，供体检命令打印。

    注意：这里刻意只输出 ``ACCOUNT_UID`` 是否已配置（布尔值），
    绝不回显 UID / Token 本身，防止截图或日志泄露。
    """
    return {
        "project_root": str(PROJECT_ROOT),
        "material_root": str(MATERIAL_ROOT),
        "materials": material_status(),
        "api_host": API_HOST or "<未配置>",
        "api_base_url": f"{API_SCHEME}://...:{API_PORT}" if not API_HOST else api_base_url(),
        "custom_dns_ip": CUSTOM_DNS_IP or "<未配置>",
        "timeout": REQUEST_TIMEOUT,
        "max_retries": MAX_RETRIES,
        "verify_tls": VERIFY_TLS,
        "http_proxy": HTTP_PROXY or "<直连>",
        "credentials_configured": has_credentials(),
        "debug": DEBUG,
        "log_level": LOG_LEVEL,
        "platform_base_url": PLATFORM_BASE_URL,
        # 平台账号密码登录：只输出路径与 gameId（都非机密），便于体检时核对配置
        "platform_login_path": PLATFORM_LOGIN_PATH,
        "platform_login_captcha_path": PLATFORM_LOGIN_CAPTCHA_PATH,
        "platform_login_game_id": PLATFORM_LOGIN_GAME_ID,
        "user_info_path": USER_INFO_PATH,
        "game_status_path": game_status_path(),
        "auth_header": AUTH_HEADER_NAME,
        "auth_token_configured": has_auth_token(),
        # 游戏会话（1004 令牌）相关：**同样只输出布尔值/路径**，不回显令牌
        "session_token_configured": has_session_token(),
        "session_file": str(SESSION_FILE),
        "session_save_enabled": SESSION_SAVE_ENABLED,
        # 已删除：RELOGIN_ON_AUTH_FAIL（曾是"看着有、其实没人消费"的死开关，
        # 2026-09-21 按用户要求移除；等 Q-016 有实测结论再谈是否实现自动重登）
        # 消费许可：**只输出布尔值**，便于截图分享时不自曝账号信息
        "allow_diamond_spend": ALLOW_DIAMOND_SPEND,
        # 用途级开关（同样是布尔值）：宴席「花钻石邀请好友」是否被授权
        "bbq_invite_friends": BBQ_INVITE_FRIENDS,
        # 用途级开关：购买体力是否被授权与默认次数
        "buy_energy": BUY_ENERGY,
        "buy_energy_times": BUY_ENERGY_TIMES,
        "device_id": DEVICE_ID,
        # 设备身份三项各自的来源（"内置默认"= 还在用采集自开发机的值）。
        # 只输出来源名，不回显任何凭据 —— 可以安全截图。
        "device_identity_source": device_identity_source(),
        "unity_version": UNITY_VERSION,
        "platform_referer": PLATFORM_LOGIN_REFERER,
        "platform_lang": PLATFORM_LANG,
        "platform_client_identity": "WebView (Edge)",
        "use_system_trust_store": USE_SYSTEM_TRUST_STORE,
        "game_server_host": GAME_SERVER_HOST,
        "game_api_host": GAME_API_HOST,
        "web_login_host": WEB_LOGIN_HOST,
        "server_selection": server_selection_summary(),
    }
