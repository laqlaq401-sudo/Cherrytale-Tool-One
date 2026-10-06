"""角色头像解析：编号 → PNG 字节（★ 2026-10-05）。

【编号 → 图片的链路（2026-10-06 定案 + 修订）】
登录 1004 的头像编号是**头像道具的 itemID**（ItemData 里有一类 "[HCG头像]…"
道具，如 ``200911039 → iconAssetID=a001_03h_Icon``）。编号**依次**从三个键取
（见 :data:`_PLAYER_ICON_KEYS`）：``icon`` → ``role_main`` → ``representative``
—— 因为实测 ``PlayerClass.icon`` 可以是 **0**（玩家从没设过自定义头像），
而真实编号在 ``IconClass.roleMainID`` / ``PlayerClass.roleRepresentativeId`` 里。

    1. ItemData[itemID].iconAssetID → 去掉 ``_Icon`` → 代码（如 ``a001_03h``）
       → ``web/assets/avatars/{code}.png``（主链）；
    2. 运行时缓存 ``avatars/icon_{n}.png``（直链下载路线的落盘位）；
    3. 都没有 → ``None``，前端回退**自制占位图** ``assets/avatars/000h.png``
       （★ 用户拍板：没选头像的人与读取失败的人**都看这张自制图**，
       不引入游戏官方默认剪影 ``a000_01``）。

【上面那条链只负责"玩家自己的头像"；名册角色走另一条链（★ 2026-10-06 勘误）】
曾被判为"死代码、已删除"的 RoleMainData/RoleInfoData 路线，结论**一半对一半错**：

* 对的一半：配置表给的名字**一律不带 h 尾**（``roleModelID=a001_03``、
  ``icon1_AssetId`` 去尾后 ``a001_01``），直接拿去找文件当然永远不命中
  —— 旧实现的错就在这里。
* 错的一半：由此得出"给无 h 代码补 h 也永远不命中"并**删掉整条链**。实测
  补 h 后 ``a001_03`` → ``a001_03h.png`` **命中 1394 处**。
  （此前"roleModelID 几乎全表 NULL"是 grep 方法错误：``[a-z0-9]+_Demo``
  匹配不到带下划线的完整值、``grep -c`` 数的是行数。实际 6653 行全有值。）

所以现在**有两条独立的链**，别混用（输入空间不同，合起来没法单独排错）：

* :func:`resolve_code` —— "玩家自己的头像"：1004 ``PlayerClass.icon`` 是
  **头像道具 itemID**，主链 ``ItemData[itemID].iconAssetID``（**自带 h**）。
* :func:`resolve_role_icon_code` —— "名册里的角色"：输入 RoleMainID，
  主链 ``RoleInfoData.icon1_AssetId``（**不带 h，要自己补**），且**只在
  "与角色名同名"的 RoleInfoID 里找** —— 因为一个 ``initialRoleMainID``
  可挂多个 RoleInfoID，用 ``roleModelID`` 路线会有 **28 处张冠李戴**
  （``130000100`` 名叫黃金騎士，``roleModelID=e001_01`` 补 h 后却是仙度瑞拉）。
  详见 ``models/game_config.py::RoleIdMap.role_icon_codes()``。

静态目录由 ``tools/extract_avatar_icons.py`` 全量预提取（热更 CDN → 解伪装 →
UnityPy；bundle 内贴图名 = ``{code}_Icon_Texture``，与文件名严格对齐），
**运行时纯文件名匹配，不需要网络、不需要 UnityPy**。

【缺图角色怎么办】
名册里约 17% 的角色（``ct096``~``ct160`` 段）在游戏 CDN 里就**没有** hero
图标（热更清单 ``img_hero_*_icon`` 里 h 尾恰好 196 条，与已提取的 196 张
完全相等，提取率 100%）——这不是本项目的问题，是游戏本身没出图。
这类角色统一回退 :data:`PLACEHOLDER_ROLE_AVATAR`（``000h.png``）。

【为什么存 base64 而不是路径】
头像要穿过两层边界到达前端：会话文件/账号库（跨进程持久化）→ JSON 接口。
base64 直存（小图几 KB~几十 KB）不需要为 ``<img>`` 单开免鉴权路由，
Android 端 filesDir 与桌面端工程根的路径差异也天然无感 ——
详见 ``models/session_state.py`` 的 ``avatar_data`` 字段说明。
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

import config

logger = logging.getLogger(__name__)

#: 运行时缓存目录（可写：桌面=工程根，Android=filesDir，见 config.PROJECT_ROOT）。
AVATAR_CACHE_DIR: Final[Path] = config.PROJECT_ROOT / "avatars"

#: 表里 iconAssetID 的固定后缀（如 "a001_03h_Icon"）；去掉它就是静态目录文件名。
_ASSET_SUFFIX: Final[str] = "_Icon"


def _web_static_dir() -> Path:
    """随包静态映射的查找根（与 webapi/app.py 的 WEB_DIR 同一套判定逻辑）。"""
    packaged = Path(config.RESOURCE_ROOT) / "web"
    if packaged.is_dir():
        return packaged
    return Path(config.PROJECT_ROOT) / "web"


# ---------------------------------------------------------------------------
# 代码换算（编号 → 静态目录文件名），懒加载 + 进程内缓存 + 失败静默
# ---------------------------------------------------------------------------
_ITEM_ICON_ROWS: dict[int, str] | None = None


def _item_icon_rows() -> dict[int, str]:
    """itemID → iconAssetID 去掉 ``_Icon``（ItemData 表，头像道具主链）。"""
    global _ITEM_ICON_ROWS
    if _ITEM_ICON_ROWS is not None:
        return _ITEM_ICON_ROWS
    rows: dict[int, str] = {}
    try:
        from models.config_table import read_table
        from models.game_config import table_path

        for row in read_table(table_path("ItemData")):
            try:
                item_id = int(row.get("itemID") or 0)
            except (TypeError, ValueError):
                continue
            asset = str(row.get("iconAssetID") or "").strip()
            if item_id and asset.endswith(_ASSET_SUFFIX):
                rows[item_id] = asset.removesuffix(_ASSET_SUFFIX)
    except Exception as exc:  # 表缺失/损坏都不该影响主流程
        logger.warning("ItemData 表不可用，头像主链解析停用：%s", exc)
    _ITEM_ICON_ROWS = rows
    return rows


#: 玩家头像编号的候选键，按优先级排序。
#:
#: 【★ 2026-10-06 排查实证：为什么不能只看 ``icon``】
#: 实测真实 1004（``captures/登录后自动签到（每日+活动）.saz`` → ``raw/04_s.txt``）：
#: ``PlayerClass.icon = 0``（该玩家从没设过自定义头像），而真实头像编号在
#: ``IconClass.roleMainID`` 与 ``PlayerClass.roleRepresentativeId`` 里
#: （都是 ``200913057`` = ``[頭像]預設`` 默认头像）。
#: 旧实现只读 ``icon`` ⇒ ``icon=0`` 时直接返回空 ⇒ 玩家头像永远出不来
#: —— 这就是"英雄头像能出、玩家头像出不来"的原因（两条链互不相干）。
#: 三个键按"语义强弱"排：``icon`` 是显式头像编号，后两个是实测的实际来源。
_PLAYER_ICON_KEYS: Final[tuple[str, ...]] = ("icon", "role_main", "representative")


def avatar_code_from_icon(icon: int) -> str:
    """头像道具 itemID（1004 icon）→ 静态目录文件名代码（唯一实证链）。"""
    if icon <= 0:
        return ""
    return _item_icon_rows().get(icon, "")


def _player_icon_ids(avatar_ids: Mapping[str, Any]) -> list[int]:
    """按 :data:`_PLAYER_ICON_KEYS` 的优先级取出快照里所有**正整数**编号。

    :return: 去重保序的编号列表；一个都没有时返回空列表（**不抛异常** ——
        坏值一律跳过，头像是锦上添花，不该让玩家信息查询失败）。
    """
    ids: list[int] = []
    for key in _PLAYER_ICON_KEYS:
        try:
            value = int(avatar_ids.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    return ids


def resolve_code(avatar_ids: Mapping[str, Any] | None) -> str:
    """把 1004 编号快照解析成静态目录文件名代码。

    依次尝试 :data:`_PLAYER_ICON_KEYS` 里的每个编号，**返回第一个能在
    ``ItemData`` 里换算成代码的**；全都换算不出时返回 ``""`` —— 调用方
    回退自制占位图（``web/assets/avatars/000h.png``，见 ``common.js`` 的
    ``AVATAR_PLACEHOLDER``）。

    .. note::
       ``role_main`` / ``representative`` 指向的常常是**默认头像**
       （``[頭像]預設`` → ``a000_01``），而那张图**刻意没有**进 runtime 静态
       目录 —— 2026-10-06 用户拍板：**没选头像的人与读取失败的人，一律看
       我们自制的占位图**，不引入游戏官方的默认剪影。
       所以此时返回空串是**预期行为，不是 bug**。
    """
    if not avatar_ids:
        return ""
    for icon in _player_icon_ids(avatar_ids):
        code = avatar_code_from_icon(icon)
        if code:
            return code
    return ""


def _candidate_paths(icons: Sequence[int], code: str) -> list[Path]:
    """一个头像的所有候选图片路径（按优先级）。

    :param icons: 该快照里所有候选头像编号（见 :func:`_player_icon_ids`）。
    :param code: :func:`resolve_code` 换算出的静态目录文件名（可为空串）。
    """
    paths: list[Path] = []
    if code:
        paths.append(_web_static_dir() / "assets" / "avatars" / f"{code}.png")
    for icon in icons:
        paths.append(AVATAR_CACHE_DIR / f"icon_{icon}.png")
    return paths


def resolve_avatar_png(avatar_ids: Mapping[str, Any] | None) -> str | None:
    """把 1004 头像编号解析成 PNG 的 base64 串。

    :param avatar_ids: ``tasks/login.py`` 采集的编号字典（含 ``icon`` /
        ``role_main`` / ``representative`` 等）。
    :return: ``data:image/png;base64,…`` 形式的 data-url；解析不到返回 ``None``
        —— 调用方（前端）回退自制占位图 ``assets/avatars/000h.png``。

    【为什么返回 data-url 而不是裸字节】调用方（login 任务）要把结果原样塞进
    账号库与会话文件，接口层再原样透出 —— 统一成"最终可直接给 ``<img>``"的
    形态，三层都不用再拼。
    """
    if not avatar_ids:
        return None
    code = resolve_code(avatar_ids)
    for path in _candidate_paths(_player_icon_ids(avatar_ids), code):
        try:
            if path.is_file():
                data = path.read_bytes()
                if data:
                    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        except OSError as exc:
            logger.warning("读取头像缓存失败 %s：%s", path, exc)
    return None


def cache_avatar_png(icon: int, data: bytes) -> Path | None:
    """把下载到的头像写进运行时缓存（直链路线的落盘位）。

    :return: 落盘路径；失败返回 ``None``（缓存失败只降级为"下次重新下载"，
        不值得让登录为此失败）。
    """
    if icon <= 0 or not data:
        return None
    try:
        AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        target = AVATAR_CACHE_DIR / f"icon_{icon}.png"
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)  # 原子替换：半截文件不会出现在正式路径上
        return target
    except OSError as exc:
        logger.warning("头像缓存写盘失败（icon=%d）：%s", icon, exc)
        return None


# ---------------------------------------------------------------------------
# 角色头像（★ 2026-10-06）：RoleMainID / RoleInfoID → 静态目录文件名 code
# ---------------------------------------------------------------------------
#: 缺图角色的统一占位图 code（文件 ``web/assets/avatars/000h.png``）。
#:
#: 命名刻意保持 ``[a-z]+\d+h`` 同构（而不是 ``placeholder``），这样
#: "code 要么为空、要么必有同名文件"这条守卫可以无脑成立；且 ``000h`` 在热更清单
#: 里没有对应 bundle，**不可能与任何真头像冲突** —— 将来从 CDN 补齐真图后，
#: 占位图自动被取代（本函数命中真 code 就返回真 code）。
PLACEHOLDER_ROLE_AVATAR: Final[str] = "000h"

#: ``roleModelID`` 里表示"默认剪影 / 演示用"的尾巴，必须去掉才可能命中文件。
_MODEL_DEMO_SUFFIX: Final[str] = "_Demo"

#: 角色头像 code 的进程内缓存：``role_main_id -> code``（"" = 没有）。
#:
#: 【为什么必须缓存】名册一次几十上百条，而每条要试多个变体 × 多个候选
#: = 多次 ``is_file()`` 系统调用；"展开全部"时是一次性几百条，不缓存会明显卡
#: 渲染。配置表是可换的素材，但**文件名集合在进程生命周期内不会变**（改了要重启），
#: 所以进程级缓存是安全的 —— 与 ``models/game_config.py`` 里"每次重新读表"的
#: 谨慎策略不同，那边的顾虑是表内容会变，这里只关心"文件在不在"。
_ROLE_ICON_CACHE: dict[int, str] = {}

#: 角色 ID 换算表的进程内缓存（**必须的，不是优化**）。
#:
#: 【为什么这里能缓存而别处不能】``load_role_id_map()`` 每次调用要读
#: ``RoleMainData`` 6653 行 + ``RoleInfoData`` 587 行，约 0.3~0.5 秒。
#: 而本函数是**逐角色**调用的（名册上百条）—— 不缓存的话一次渲染就是
#: 上百次读表，实测直接把一次全量校验跑成超时（SIGTERM）。
#: 换算期间表内容不会变（表是进程启动时就定下来的素材），所以这里缓存是安全的。
#: 需要"表变了立刻生效"的场景（测试）用 :func:`clear_role_icon_cache()` 清掉。
_ROLE_ID_MAP_CACHE: list[Any] = []  # 用 list 当可变容器，避免 global 声明


def clear_role_icon_cache() -> None:
    """清空角色头像换算缓存（供测试与"换了素材/表"的场景使用）。"""
    _ROLE_ICON_CACHE.clear()
    _ROLE_ID_MAP_CACHE.clear()


def _role_id_map() -> Any:
    """取（并缓存）角色 ID 换算表。失败返回 ``None``。"""
    if _ROLE_ID_MAP_CACHE:
        return _ROLE_ID_MAP_CACHE[0]
    from models.game_config import load_role_id_map

    table = load_role_id_map()
    _ROLE_ID_MAP_CACHE.append(table)
    return table


def _avatar_file_exists(code: str) -> bool:
    """``code`` 在静态头像目录里是否有对应 PNG。"""
    if not code:
        return False
    try:
        return (_web_static_dir() / "assets" / "avatars" / f"{code}.png").is_file()
    except OSError:  # 目录不可读时按"没有"处理，不让它拖垮渲染
        return False


def _code_variants(base: str) -> tuple[str, ...]:
    """把一个配置表裸名展开成**可能的静态文件名**（按优先级）。

    优先级依据（2026-10-06 实测，见 ``notes/role_avatar_display_plan.md``）：

    1. ``{base}h``  —— **现役主头像**，覆盖绝大多数。如 ``a001_03`` → ``a001_03h``；
       ``ct026`` → ``ct026h``。
    2. ``{base}``   —— 旧版 / 冷存编号（如 ``a000_01``），以及本来就带 h 的情况。
    3. ``{prefix}{num}_01h`` / ``{prefix}{num}_01`` —— **超限突破形态**的编号
       （如 ``ct116`` 的 1 阶是 ``ct116_01``）。这条规则**是从命名观察得出的，
       没有反汇编依据**，所以放在最后，且**必须靠存在性校验兜底** ——
       命中就用、不命中就继续，不会出错，只会少显示。
    """
    if not base:
        return ()
    variants: list[str] = [f"{base}h", base]
    match = re.fullmatch(r"([a-z]+)(\d+)", base)
    if match:
        stem = f"{match.group(1)}{match.group(2)}_01"
        variants += [f"{stem}h", stem]
    return tuple(dict.fromkeys(variants))


def resolve_role_icon_code(role_main_id: int) -> str:
    """角色（RoleMainID）→ 静态头像文件名 code。

    :param role_main_id: 名册里的 ``roleID``（= RoleMainID，130xxxxxx 段）。
    :return: 静态目录里**确实存在**的 code（如 ``a001_03h``）；找不到返回 ``""``
        —— 调用方应回退 :data:`PLACEHOLDER_ROLE_AVATAR`，**不要自己编名字**。

    【★ 为什么不带 ``role_info_id`` 参数（2026-10-06 定案）】
    初版实现让调用方传 ``role_info_id``（名册侧用 ``role_info_ids(...)[0]``），
    但全量实测发现这**必然造成 28 处张冠李戴**：一个 ``initialRoleMainID``
    可挂多个 RoleInfoID（早期合成角色），取"第一个"得到的 RoleInfoID 可能
    根本不是角色名对应的那条，于是 ``a001_01h``（黃金騎士）会串成
    ``e001_01h``（仙度瑞拉）。现在改由
    ``RoleIdMap.role_icon_codes()`` **内部**按"与角色名同名"排序候选，
    名字与图标同源，冲突归零。调用方只需给 RoleMainID。

    【与 :func:`resolve_code` 的区别（别混用）】
    ``resolve_code`` 走的是"玩家自己的头像"：1004 ``PlayerClass.icon`` 是
    **头像道具的 itemID**，主链是 ``ItemData[itemID].iconAssetID``（那条链**自带
    h 后缀**）。本函数走的是"名册里的角色"：输入是 RoleMainID，主链是
    ``RoleInfoData.icon1_AssetId``（**不带 h，要自己补**）。两者输入空间不同、
    补 h 的策略也不同，合在一起会让两边都没法单独排错。
    """
    key = int(role_main_id or 0)
    if key in _ROLE_ICON_CACHE:
        return _ROLE_ICON_CACHE[key]

    code = ""
    try:
        role_ids = _role_id_map()
        for base in role_ids.role_icon_codes(key):
            # a001_03_Demo → a001_03（演示用剪影，去掉尾巴才可能命中真文件）
            normalized = base[: -len(_MODEL_DEMO_SUFFIX)] if base.endswith(_MODEL_DEMO_SUFFIX) else base
            for variant in _code_variants(normalized):
                if _avatar_file_exists(variant):
                    code = variant
                    break
            if code:
                break
    except Exception as exc:  # 表缺失/损坏都不该让"信息展示"整页失败
        logger.warning("角色头像换算失败（roleMainID=%s）：%s", key, exc)

    _ROLE_ICON_CACHE[key] = code
    return code
