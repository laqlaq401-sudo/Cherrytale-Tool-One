#!/usr/bin/env python3
"""从 dump.cs 提取各报文的 **protobuf 字段顺序**，生成 models/proto_fields.py。

【为什么需要这张表】
protobuf 的每个字段都要带**编号**（wire format 里的 tag），
而编号必须和客户端发的完全一致 —— 差一位，服务端就会把值读到别的字段上，
现象是「参数看起来是对的，但服务端行为很奇怪」，极难排查。

dump.cs 里的字段长这样::

    [ProtoMemberAttribute] // RVA: 0x113A40 ...
    public int mistypeID { get; set; }

注意 ``[ProtoMemberAttribute]`` **没有显示编号参数**（Il2CppDumper 不输出它），
所以我们只能按**声明顺序**推断：protobuf-net 在未显式指定编号时，
就是按成员声明顺序从 1 开始编号。字段的内存偏移严格递增（0x10, 0x14, 0x18…）
也从侧面印证了「声明顺序」这一事实。

【⚠️ 一个必须由抓包验证的假设】
``TAGS_VERIFIED = False`` —— 编号 = 声明顺序这件事**尚未经真实报文验证**。
验证方法很简单：抓一个 ``DMReward``（消息号 6005）请求，确认报文开头是
``08 ... 10 ...``（= 字段 1 是 varint、字段 2 是 varint）。

万一实际用的是 protobuf-net 的 ``ImplicitFields.AllPublic`` 模式（按字母序编号），
只需要改本文件的推断规则并重新生成，**业务代码一行都不用动** ——
这正是把所有编号集中到一张生成表里的意义。

【用法】
    python tools/extract_proto_fields.py                 # 生成 models/proto_fields.py（聚焦类）
    python tools/extract_proto_fields.py --all           # 全部 868 个协议类（文件会很大）
    python tools/extract_proto_fields.py --check         # 校验现有文件是否过期
    python tools/extract_proto_fields.py --show DMReward # 打印某个类的字段顺序
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Sequence

# tools/ 不是包，无法直接 ``import config``，只能手动把项目根加进 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402  (必须在 sys.path 调整之后导入)

#: 参与 protobuf 序列化的属性一定带这个特性
PROTO_MEMBER_ATTR: Final[str] = "[ProtoMemberAttribute]"

#: 属性声明行，例如 ``\tpublic int mistypeID { get; set; }``
#: 用贪婪的 ``.+`` 是为了正确吃下泛型：``public List<DailyMissionListClass> dmInitList { get; }``
_PROPERTY_RE: Final[re.Pattern[str]] = re.compile(r"^\s*public\s+.+\s+(\w+)\s*\{\s*get;")

#: 任意 ``public class`` 的声明行，捕获类名（用于「单遍扫描时判断当前在哪个类里」）
_ANY_CLASS_RE: Final[re.Pattern[str]] = re.compile(
    r"^public\s+(?:sealed\s+|abstract\s+|static\s+)*class\s+(\w+)"
)


def extract_fields_for_classes(
    path: Path, class_names: Sequence[str]
) -> tuple[dict[str, tuple[str, ...]], dict[str, int]]:
    """**一次扫描** dump.cs，取出多个协议类的字段顺序。

    :return: ``(字段表, ProtoMember 特性计数表)``。

    【为什么不能"每个类扫一遍文件"】
        dump.cs 有 33 MB。若为 28 个类各扫一遍，读写量会接近 1 GB；
        改成「先识别当前处于哪个类、再决定要不要记录」的单遍扫描后，
        速度快了一个数量级 —— 而且目标类越多，优势越明显。

    【第二个返回值（特性计数）用来干什么】
        它区分两种**在输出里长得一模一样、含义却完全不同**的情况：

        - 类里**一条 ProtoMember 都没有** → 这是**空报文**
          （例如 ``MailGetListPacket`` 只有一个 ``extensionObject``），完全正常；
        - 类里**有 ProtoMember 却没解析出字段名** → 说明 dump.cs 的书写形式变了，
          解析规则需要更新，必须显式报警。

        没有这个计数，你会把「解析坏了」误判成「这个包本来就是空的」。

    【为什么只认「ProtoMember 之后紧跟的属性」】
        这样能自动排除三类干扰：``extensionObject`` 这类内部状态字段（无该特性）、
        ``_dmInitList`` 这类 backing field（是私有字段而非属性）、以及各种方法。
        另外，特性与属性行之间偶尔会插进别的特性（如 ``[DefaultValueAttribute]``），
        所以遇到非 ProtoMember 的特性**不能**清掉 pending 标志。
    """
    targets = set(class_names)
    fields: dict[str, list[str]] = {name: [] for name in class_names}
    member_counts: dict[str, int] = {name: 0 for name in class_names}
    current: str | None = None
    pending = False

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if current is not None and line.startswith("}"):
                # 顶格的 } = 当前类结束（嵌套类的 } 会带缩进）
                current = None
                pending = False
                continue

            declaration = _ANY_CLASS_RE.match(line)
            if declaration:
                candidate = declaration.group(1)
                current = candidate if candidate in targets else None
                pending = False
                continue

            if current is None:
                continue

            stripped = line.strip()
            if stripped.startswith(PROTO_MEMBER_ATTR):
                pending = True
                member_counts[current] += 1
                continue

            if stripped.startswith("["):
                # 其它特性：不动 pending（ProtoMember 可能在它前面，也可能在后面）
                continue

            match = _PROPERTY_RE.match(line)
            if match:
                if pending:
                    fields[current].append(match.group(1))
                pending = False
                continue

            if stripped and not stripped.startswith("//"):
                # 方法、私有字段等：都不是目标，重置标志
                pending = False

    return {name: tuple(names) for name, names in fields.items()}, member_counts


def extract_proto_fields(path: Path, class_name: str) -> tuple[str, ...]:
    """取**单个**协议类的字段顺序（:func:`extract_fields_for_classes` 的便捷包装）。

    即使只取一个类，内部也走同一套单遍扫描实现 —— 不另写一份逻辑，
    就不会出现「单个查询与批量导出结果不一致」这种最难查的问题。
    """
    fields, _counts = extract_fields_for_classes(path, [class_name])
    return fields.get(class_name, ())

    fields, _counts = extract_fields_for_classes(path, [class_name])
    return fields.get(class_name, ())




def discover_proto_classes(path: Path) -> list[str]:
    """列出全部协议类名。

    直接复用消息号表的键空间：枚举成员名与协议类名**完全一致**，
    而消息号表已经由 ``tools/extract_packet_ids.py`` 验证过，
    所以这里不必再扫一遍 1098 个 ``// Namespace: com.auer.game.protobuf``。
    """
    from models import packet_ids

    return list(packet_ids.PACKET_IDS)


# ---------------------------------------------------------------------------
# 生成 Python 模块
# ---------------------------------------------------------------------------
#: 本轮真正要用的协议类。
#:
#: 【为什么不做「全部 868 个类」】
#: 全量导出会让生成文件膨胀到几千行，而其中 99% 的报文你近期都不会碰。
#: 需要更多时：往这个列表里加名字，或直接用 ``--all``。
FOCUS_CLASSES: Final[tuple[str, ...]] = (
    # 外层封包：所有报文的信封
    "RootPacket",
    # 服务端异常包（99004）—— 包被拒时服务端回的就是它。
    # ⚠️ 它的 ``errorCode=-3`` 表示"包结构/算法不对"，**重试没有意义**，
    #    所以单独建模并分类，见 models/game_error.py。
    "ServerExceptionRes",
    # 登录
    "LoginPacket",
    "LoginRes",
    # 玩家角色信息（``LoginRes.playerInitClass`` 的类型）。
    # ⚠️ 会话令牌就在它的字段 1 ``token`` —— 1004 的**信封里没有令牌**，
    #    必须来这里取，否则后续每个业务请求都会因"没有令牌"被拒。
    "PlayerClass",
    # 玩家头像与昵称信息（``LoginRes.myIconInfo`` 与 ``PlayerDetailRes.playerIcon`` 的类型）
    "IconClass",
    # LoginRes 的另外三个初始化清单（★ 2026-10-05：进游戏时的全量快照，
    # 与 ``itemInitClass`` 同一个包 —— 客户端登录后**不会**再单独拉这几样）。
    #   equipInitClass → 装备；roleInitClass → 角色名册（英雄）；teamInitClass → 队伍编成
    # ``RoleClass`` / ``ItemClass`` 已在下面各自的小节里声明，这里只补装备与队伍。
    "EquipClass",
    "TeamClass",
    # 日常任务
    "DailyMission",
    "DailyMissionRes",
    "DailyMissionListClass",
    "DMReward",
    "DMRewardRes",
    "DMActReward",
    "DMActRewardRes",
    "UnLockActiveMission",
    "UnLockActiveMissionRes",
    # 奖励明细
    "GetObjClass",
    "ItemClass",
    # 邮件
    "MailGetListPacket",
    "MailGetListRes",
    "MailOpenParcelPacket",
    "MailOpenParcelRes",
    # 商店（L2 待办清单要用）
    "GetStoreListPacket",
    "GetStoreListRes",
    "BuyProductPacket",
    "BuyProductRes",
    "ProductClass",
    # 抽卡（L2 待办清单要用）
    "DrawNewPacket",
    "DrawNewRes",
    "GetDrawMachineDataNewPacket",
    "GetDrawMachineDataNewRes",
    "DrawMachineNewDataClass",
    # ---- 抓包实测覆盖的报文（captures/ 里的 20 条流量，清单见 notes/gateway_traffic.md）----
    # 握手：整条链路的第一个包（88 字节请求 / 650 字节响应）
    "VersionControlServerPacket",
    "VersionControlServerRes",
    # 服务端下发的配置容器（RootPacket 字段 16，内含两段 32 字符 MD5）
    "SpecialActivityRefreshClass",
    # 功能开关列表（跟握手响应一起下发，同一次响应里含 3 个包）
    "CheckFunctionStateRes",
    # 登录：**实测真正的登录包是 1001**，不是 LoginPacket（1003）
    "AccountClientInfoLoginPacket",
    "AccountClientInfoLoginRes",
    # 区服列表的**元素类型**：1002 响应里的 allServerList / suggestedServer /
    # lastLoginedServerList 都是它。没有它就没法从响应里解出 serverID
    # （选服就无从谈起），所以必须有。
    "ServerListClass",
    # 登录之后紧随其后的补充数据
    "LoginOtherDataPacketRes",
    # 玩家基础数据（RootPacket 字段 11，多数响应都会带）
    "PlayerBasicDataClass",
    # 周期性同步
    "SyncDataRes",
    # 本次抓包覆盖的其它响应（加上它们，夹具里的每条真实字节都能解出字段名）
    "TeachingRecordRes",
    "GetActivityLoginRewardsRes",
    # 33056 的元素类型（★ 2026-10-06 登录签到：每日=dailyAddList、活动=activityData[].addList，
    # 证据链见 notes/recon_findings.md 的「登录签到」节）
    "ActivityLoginRewardsData",
    # 33055 请求（⚠️ 类名没有 Packet 后缀，dump.cs 里就是这个名字；本身 0 字段）
    "GetActivityLoginRewards",
    "GetHomeBannerPopUpSettingsRes",
    "GetAllActivityStageRes",
    "CashItemListRes",
    "GetLastSectionRes",
    "GetStoreTypeListRes",
    # ---- 宴席 / BBQ（吃好友宴席领体力；**含"花钻石邀请好友"的付费路径**）----
    # 32007/32008 拉数据（groupID 与好友列表），15003/15004 才是真正"吃"的动作。
    "GetBBQDataPacket",
    "GetBBQDataRes",
    "ActivityIntervalDataClass",
    "FriendClass",
    "BBQFriendsPacket",
    "BBQFriendsRes",
    # ---- 特惠礼包（GiftPackage：37001 拉列表 / 37019 领免费礼包）----
    "GetGiftListPacket",
    "GetGiftListRes",
    "GiftInfoClass",
    "GetFreeGiftPacket",
    "GetFreeGiftPacketRes",
    # ---- 补齐「有抓包依据」的请求类 ----
    # 上面那批只收了响应（Res），而抓包已经证明这些**请求**也真实存在
    # （每条响应都能在 captures/ 里找到对应的请求，清单见 notes/gateway_traffic.md）。
    # 补齐之后，每个抓到的接口都能**双向**解出字段名 —— 这是「已验证基线」的延伸，不是猜。
    #
    # ⚠️ 收录前必须先查消息号是否存在（本次就查出两个名字根本不在 ePacketFormat_RPS 里：
    #    GetAllActivityStagePacket、GetActivityLoginRewardsPacket）——
    #    不存在的类会被 extract 误判成「空报文」，比不收录更具误导性。
    "GetStoreTypeListPacket",  # 14011（对应响应 14012 已收录）
    "CashItemListPacket",  # 27003
    "SyncDataPacket",  # 99023
    "TeachingRecordPacket",  # 31003
    "GetHomeBannerPopUpSettingsPacket",  # 32005
    "GetLastSectionPacket",  # 11011
    "LoginOtherDataPacket",  # 1009
    # ---- 荣耀之巅（TopPvp）----
    # 四个竞技板块里**唯一由服务器结算**的一个（实测见 notes/glory_summit_capture.md）：
    # 39007 挑战包的请求体是 0 字节，服务器算完整场战斗后把战报放在 39008 的 replayResult 里。
    # 所以本板块**不需要**"本地战斗引擎"，可以直接用协议自动化。
    #
    # 39001 拉主页：字段 16 isSkipBattle 就是「跳过战斗演出」开关的**服务端当前状态**
    "GetTopPvpHomePacket",
    "GetTopPvpHomeRes",
    # 39002 两个段位榜的元素类型（用来读"我现在第几名/多少分"）
    "TopPvpRankClass",
    # 39008 里 versusInfo_Me / versusinfo_Enemy 的类型（本场积分变化在这里）
    "TopPvpVersusInfoClass",
    # 39019 写入「跳过战斗演出」开关（本项目默认置 True，见 tasks/top_pvp.py）
    "TopPvpSetSkipBattleTogglePacket",
    "TopPvpSetSkipBattleToggleRes",
    # 39007/39008 挑战与结算（请求空包，响应带回放战报）
    "TopPvpChallengePacket",
    "TopPvpChallengeRes",
    # 39011/39012 队伍编辑信息（**实测：真实客户端在挑战前必发这一条**）
    # 请求 {sectionID, actionType}，响应给出 roleRankList 与 defTeamClass（防守队伍）。
    # 不建里面两个嵌套类型（PopularRoleRankingsClass / TeamClass）—— 按 bytes 保留原样即可。
    "TopTeamEditInfoPacket",
    "TopTeamEditInfoRes",
    # 39003/39004 荣耀之巅宝箱领取
    "ReceiveTopPvpRewardPacket",
    "ReceiveTopPvpRewardRes",
    # ---- 竞技板块的其余三个模式（**只读**：读次数/排名，绝不进战）----
    # 实测依据与字段语义见 notes/recon_findings.md 第七节、notes/glory_summit_capture.md。
    #
    # ① 竞技场（PvPNetWorkModule）：26001 空包 → 26002 带 challengeCount(可挑战次数)
    "PVPHomePacket",
    "PVPHomeRes",
    # ② 天命对决（WuDouPvPNetWorkModule）：26031{groupID=-1} → 26032、26033(空) → 26034
    "WuDouHome",
    "WuDouHomeRes",
    "WuDouChallengeHome",
    "WuDouChallengeHomeRes",
    # ③ 失控炼成阵（YimoBattleModule）：22101 空包 → 22102 带 remainCount(剩余次数)
    "GetYimoState",
    "GetYimoStateRes",
    "OpenYimoRewardPacket",
    "OpenYimoRewardRes",
    "YimoBossDailyRewardClass",
    # ---- 只读「信息面板」：金币 / 钻石 / 体力（网页顶部的资源展示用）----
    # 2007 是**空包**（只请求一份背包快照）；2008 只有一个字段 ``itemList``，
    # 元素结构与 ``GetObjClass.itemList`` 同型 → Python 侧**复用**
    # ``models.daily.ItemClass``，不再为它单独建一遍模型（见 models/wallet.py）。
    # 只读、零消耗：会改动背包的包（2001 UseItem / 2005 MixItems / 2011 HarvestDreamStone）
    # 写在 models/wallet.py 的禁发清单里。
    "GetAllItemPacket",
    "GetAllItemRes",
    # ---- 降临/活动关卡扫荡（体力扫荡板块）----
    # 实测抓包：``captures/降临扫荡抓包1.saz``（整份包只有 4 个网关 POST），
    # 字节级解析与结论见 ``notes/sweep_capture.md``。链路：
    #
    #   11025（**空包**）→ 11026 区域状态总表（areaStateList / memoryAreaStateList /
    #                       permanentAreaStateList）——「当期开放哪些活动区」全靠它，
    #                       所以关卡轮换**不需要重新抓包**，每次跑一遍即可。
    #   11003{areaID}    → 11004 该区的关卡状态列表（sectionStateList）
    #   11009{sectionID, times, useSweepTicket} → 11010{errorCode, rewardList, costList}
    #                       ⚠️ rewardList / costList 里的数量是「**操作后的持有量**」，
    #                       不是"本次获得/消耗了多少"（推导见 notes/sweep_capture.md 第 4 节）。
    "GetAllActivityStage",
    # 11026 三个列表的元素类型（区域状态）：areaID / 起止时间 / 进度 / 可扫荡进度串…
    "ActivityAreaStateClass",
    # 11004 的元素类型（关卡状态）：sectionID / sectionState / weekDay …
    "SectionStateClass",
    "GetSectionByAreaPacket",
    "GetSectionByAreaRes",
    # 扫荡本体（**会消耗体力与扫荡券**，是本项目里唯一"主动花资源打关卡"的包）
    "SweepSectionPacket",
    "SweepSectionRes",
    # ---- 钻石购买体力（VIP 模块：24005 / 24006，24011 / 24012）----
    "VIPDetailPacket",
    "VIPDetailRes",
    "BuyEnergyPacket",
    "BuyEnergyRes",
    # ---- 主线推图与通关（Main Stage：11015 / 11016，11017 / 11018，11007 / 11008 等）----
    "SetUseTeamIDPacket",
    "SetUseTeamIDRes",
    "FightingRequestPacket",
    "FightingResponseRes",
    "FightingResultPacket",
    "FightingResultRes",
    "FightDataClass",
    "BattleResultClass",
    "BattleAchiComboClass",
    "BattleAchiInterupClass",
    "BattleAchiAwakeClass",
    "WuDouResultRecord",
    "ReceiveStarRewardPacket",
    "ReceiveStarRewardRes",
    "AreaStateClass",
    "GetAreaByDifficultyPacket",
    "GetAreaByDifficultyRes",
    "GetAllMainStageStatePacket",
    "GetAllMainStageStateRes",
    # ---- 工会 / Alliance（签到、个人与团体挖矿、金币捐献）----
    # 21009/21010 工会主页：判"是否入会"（myAlliance）与捐献状态（donateCount）的入口。
    # 21017/21018 捐献：请求只有 actionType —— 档位序号（配置表 AllianceDonateData：
    # 693000001 初級=5000 金币、693000002 中級=50 钻、693000003 高級=200 钻），
    # 本项目只实现金币档，中/高級档不存在任何可构造路径（见 models/alliance.py）。
    # 21055-21058 签到（Bingo 面板）：先 21055 取 bingoData 决定 pos，再 21057 签。
    # 21039-21050 新版挖矿（个人 + 团体同源）：21040 同时带 personalList/teamList；
    # 一键开矿 21047 的两个字段都只装 AlliancePersonalPitClass —— 团体开矿
    # 走 21043/21045（SetRole + Start{pitSid, roleSidList}）。
    "AllianceHomePacket",
    "AllianceHomeRes",
    "AllianceClass",  # AllianceHomeRes.myAlliance 的类型
    "DonatePacket",
    "DonateRes",
    "AllianceBingoHomePacket",
    "AllianceBingoHomeRes",
    "AllianceBingoClass",  # bingoHome / 签到响应里 bingoData 的类型
    "GeneralRewardInfoClass",  # AllianceBingoSignInRes.bingoRewardData 的类型
    "AllianceBingoSignInPacket",
    "AllianceBingoSignInRes",
    # 连线奖励领取（21061/21062）—— 2026-10-03 接入（10.3 抓包实证可当场领）
    "AllianceBingoRewardPacket",
    "AllianceBingoRewardRes",
    # 辉煌航迹搜集物资奖励目录（34019/34020 的元素类型）—— 2026-10-03 流程修订
    "GrandLineCountRewardPacket",
    "GrandLineCountRewardRes",
    "GrandLineCountRewardClass",
    "AllianceNewPitPacket",
    "AllianceNewPitRes",
    "AlliancePersonalPitClass",  # personalList / oneKeyPersonalList / updateList 的元素类型
    "AllianceTeamPitClass",  # teamList 的元素类型
    "AllianceTeamPitDetailClass",  # AllianceTeamPitClass.detaliClassList 的元素类型（团体矿槽位）
    "AllianceNewPitSetRolePacket",
    "AllianceNewPitSetRoleRes",
    "AllianceNewPitStartPacket",
    "AllianceNewPitStartRes",
    "AllianceNewPitOneKeyStartPacket",
    "AllianceNewPitOneKeyStartRes",
    "AllianceNewPitRewardPacket",
    "AllianceNewPitRewardRes",
    # ---- 角色名册（挖矿开矿要按条件挑角色：等级/星级来自名册，元素/职业来自配置表）----
    # 5011 空包 → 5012 {roleList: List<RoleClass>}。只读，不含任何消耗路径。
    "GetAllRolePacket",
    "GetAllRoleRes",
    "RoleClass",
    # ---- 辉煌航迹（GrandLine，SystemID 77；34015/34021 出站白名单见 models/grand_line.py）----
    # 34015 空包 → 34016 {grandLineTowerData, syncItem, nextResetTime, rewardCount}；
    # 34021 {type=1} → 34022 领取远航搜集物资结算。复活/排行等只列响应不进白名单。
    "GrandLineHomePacket",
    "GrandLineHomeRes",
    "GrandLineTowerClass",  # GrandLineHomeRes.grandLineTowerData 的元素类型
    "GrandLineGetCountRewardPacket",
    "GrandLineGetCountRewardRes",
    "GrandLineCountRewardRes",
)

_SOURCE_LABEL: Final[str] = "Cherrytale IL2CPP/dump.cs"

_MODULE_DOC = '''"""各报文的 protobuf 字段顺序（**由 tools/extract_proto_fields.py 自动生成，请勿手工编辑**）。

【这份表解决什么问题】
protobuf 的每个字段都要带**编号**，编号必须与客户端发的一模一样：
差一位，服务端就会把值读到别的字段上，现象是「参数看着没错，但行为很奇怪」，
而报错信息通常帮不上任何忙。

dump.cs 里能看到字段**清单与顺序**，却看不见 ``[ProtoMember(n)]`` 的 n
（Il2CppDumper 不输出特性参数）。所以本表记录「字段声明顺序」，
由 :func:`field_tags` 按 ``1, 2, 3…`` 派生出编号。

【✅ 已验证的部分（2026-09 真实抓包）】
普通协议类「**编号 = 声明顺序**」这件事，已经被线上字节证实：
``VersionControlServerPacket``（消息号 99015）在抓包里的载荷是
``08 02 | 12 0E "2.2.0-el-h-win" | 1A 04 "zhcn"``，
而本表里它的字段顺序正是 ``channelPlatformType / clientVersion / languageCode`` ——
三个字段的编号与线上逐一对上。

【⚠️ 但有一处例外，务必注意：``RootPacket``】
``RootPacket`` 的编号**不是**声明顺序：

- 前 16 个元信息字段确实是 1~16（实测一致：``packetID``=1、``token``=2、``settingMd5``=3…）；
- 但**每个业务子包槽位的编号 = 该子包自己的消息号**。实测两条：

  ==================  ==============  =======================================
  线上字节            解出的字段编号   对应的协议类（消息号）
  ==================  ==============  =======================================
  ``BA AC 30``        99015           ``VersionControlServerPacket``（99015）
  ``82 01 44``        16              ``SpecialActivityRefreshClass``（16）
  ==================  ==============  =======================================

所以给 ``RootPacket`` 组包时，子包槽位必须用
``models.packet_ids.packet_id(类名)``，**不要**用本表按顺序算出来的编号。
（具体实现见 ``models/envelope.py``。）

【怎么用】
    from models.proto_fields import field_tags, tag_of

    tag_of("DMReward", "misID")     # 1
    field_tags("DMReward")          # {'misID': 1, 'mistypeID': 2}
    field_name_of("RootPacket", 99015)   # 'VersionControlServerPacket'
                                          #（注意：这个编号是消息号，不是声明顺序）
"""'''


def render_module(fields_by_class: dict[str, tuple[str, ...]]) -> str:
    """渲染 ``models/proto_fields.py`` 的完整内容。"""
    total_fields = sum(len(names) for names in fields_by_class.values())
    lines: list[str] = [
        _MODULE_DOC,
        "",
        "from __future__ import annotations",
        "",
        "from typing import Final",
        "",
        f'SOURCE_DUMP: Final[str] = "{_SOURCE_LABEL}"',
        "",
        "#: 普通报文类的「编号 = 声明顺序」是否已被真实报文验证。",
        "#: ✅ 已由 ``VersionControlServerPacket``（99015）的三个字段逐一证实（见模块文档）。",
        "#: ⚠️ ``RootPacket`` 的子包槽位是例外：编号 = 消息号。",
        "TAGS_VERIFIED: Final[bool] = True",
        "",
        "#: 收录的协议类数量",
        f"CLASS_COUNT: Final[int] = {len(fields_by_class)}",
        "",
        "#: 收录的字段总数",
        f"FIELD_COUNT: Final[int] = {total_fields}",
        "",
        "#: 协议类名 → 字段名（**按声明顺序排列**，顺序即编号顺序，从 1 开始）",
        "PROTO_FIELDS: Final[dict[str, tuple[str, ...]]] = {",
    ]
    for class_name, names in fields_by_class.items():
        lines.append(f"    {class_name!r}: (")
        for name in names:
            lines.append(f"        {name!r},")
        lines.append("    ),")
    lines.append("}")
    lines.append(_HELPERS.strip("\n"))
    return "\n".join(lines) + "\n"



_HELPERS = '''

# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------
def field_tags(class_name: str) -> dict[str, int]:
    """取某个报文的 ``{字段名: 编号}``。

    :raises KeyError: 类名不在表中时，错误信息会给出相近候选。

    每次调用都重新构造字典（不缓存）：组包/解包时一次几十个字段的构造成本可忽略，
    而缓存会带来「重新生成后内存里还是旧表」这种更难排查的问题。
    """
    import difflib

    try:
        names = PROTO_FIELDS[class_name]
    except KeyError as exc:
        candidates = difflib.get_close_matches(
            class_name, PROTO_FIELDS.keys(), n=5, cutoff=0.5
        )
        hint = f"；最相近的类名：{'、'.join(candidates)}" if candidates else ""
        raise KeyError(f"未知的协议类 {class_name!r}{hint}") from exc
    return {name: index for index, name in enumerate(names, start=1)}


def tag_of(class_name: str, field_name: str) -> int:
    """取某个字段的编号。

    :raises KeyError: 类名或字段名不存在时（两种情况给出不同的提示）。
    """
    tags = field_tags(class_name)  # 类名写错时，这里会给出候选类名
    try:
        return tags[field_name]
    except KeyError as exc:
        known = "、".join(PROTO_FIELDS.get(class_name, ())) or "<该类没有任何 ProtoMember 字段>"
        raise KeyError(f"{class_name} 中没有字段 {field_name!r}；它只有：{known}") from exc


def field_name_of(class_name: str, tag: int) -> str | None:
    """按编号反查字段名。

    :return: 字段名；编号超出范围时返回 ``None``。

    返回 ``None`` 而不是抛异常，是为了让日志代码写成
    ``field_name_of(...) or f"<未知字段 {tag}>"`` 这种一行表达式 ——
    解析响应时经常遇到「表里没有的编号」，那通常意味着字段表落后于游戏版本。
    """
    names = PROTO_FIELDS.get(class_name, ())
    if 1 <= tag <= len(names):
        return names[tag - 1]
    return None


def known_classes() -> tuple[str, ...]:
    """返回本表收录的全部协议类名。"""
    return tuple(PROTO_FIELDS)


def describe_message(cls_name: str, data: bytes) -> str:
    """把一段报文渲染成可读文本 —— **对照"自己组的包"与"抓到的包"时最有用**。

    用法::

        from models.proto_fields import describe_message
        print(describe_message("DMReward", payload))

    输出形如::

        DMReward (12 字节)
          #1 misID = 249000007
          #2 mistypeID = 1

    遇到本表没有的编号会标注 ``<未知字段>`` 并照样打印原始值 ——
    这往往说明「字段表落后于游戏版本」或「字段对齐错了」，两种都值得立刻处理。
    """
    from models.protobuf_wire import WIRE_LENGTH_DELIMITED, WIRE_VARINT, iter_fields

    lines = [f"{cls_name} ({len(data)} 字节)"]
    for field in iter_fields(data):
        name = field_name_of(cls_name, field.number) or "<未知字段>"
        if field.wire_type == WIRE_VARINT:
            shown: object = field.as_int32()
        elif field.wire_type == WIRE_LENGTH_DELIMITED:
            raw = field.as_bytes()
            try:
                shown = raw.decode("utf-8")
            except UnicodeDecodeError:
                shown = f"<{len(raw)} 字节二进制> {raw[:32]!r}"
        else:
            shown = field.value
        lines.append(f"  #{field.number} {name} = {shown!r}")
    return "\\n".join(lines)
'''



# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def force_utf8_output() -> None:
    """把标准输出/错误流强制改为 UTF-8（原因见 main.py 里的同名函数）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def resolve_dump_path(override: str | None) -> Path:
    """确定 dump.cs 路径：命令行参数 > config（含环境变量）> 默认相对路径。"""
    if override:
        return Path(override).expanduser()
    return config.DUMP_CS_PATH


def write_module(path: Path, content: str) -> None:
    """写出生成文件（``newline="\\n"`` 保证跨平台一致，``--check`` 才不会误报）。

    **写盘之前先做语法校验**：生成器最容易犯的错误之一是「拼出来的文本少一个引号」，
    结果是一个看起来正常、但一 import 就报 SyntaxError 的文件 ——
    而且报错行号通常指向**错误发生位置的后面很远**，极难定位。

    实测就踩到过：``_MODULE_DOC`` 结尾漏写 ``\"\"\"``，导致模块 docstring 未闭合，
    错误被报在几百行之后的某个函数 docstring 上。所以这里强制校验，
    宁可生成失败，也不留下一个坏文件。
    """
    import ast

    try:
        ast.parse(content, filename=str(path))
    except SyntaxError as exc:
        raise RuntimeError(
            f"生成的内容有语法错误（第 {exc.lineno} 行：{exc.msg}）—— "
            "请检查模板字符串里的引号是否成对，本次**没有写盘**"
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def check_module(path: Path, expected: str) -> bool:
    """比较磁盘上的文件与「现在重新生成会得到的内容」。"""
    if not path.is_file():
        print(f"✘ 文件不存在：{path}")
        print("  修复：运行 python tools/extract_proto_fields.py")
        return False

    actual = path.read_text(encoding="utf-8")
    if actual == expected:
        print(f"✔ 一致     : {path.name}（{len(expected.splitlines())} 行）")
        return True

    diff = list(
        difflib.unified_diff(
            actual.splitlines(),
            expected.splitlines(),
            fromfile=path.name,
            tofile="<重新生成>",
            lineterm="",
            n=1,
        )
    )
    print(f"✘ 已过期   : {path.name} 有 {len(diff)} 行差异")
    for line in diff[:20]:
        print(f"    {line}")
    print("  修复：运行 python tools/extract_proto_fields.py 覆盖它")
    return False


@dataclass(frozen=True)
class ExtractionReport:
    """一次提取的结果 + 诊断信息。

    :param fields: 类名 → 字段名（按声明顺序；顺序即编号顺序）
    :param member_counts: 类名 → 该类里 ``[ProtoMemberAttribute]`` 的条数
    """

    fields: dict[str, tuple[str, ...]]
    member_counts: dict[str, int]

    @property
    def field_count(self) -> int:
        """解析出的字段总数。"""
        return sum(len(names) for names in self.fields.values())

    @property
    def empty_packets(self) -> tuple[str, ...]:
        """**本来就是空报文**的类（一条 ProtoMember 都没有）——属于正常。

        例如客户端要「拉取邮件列表」时，请求包不需要任何参数，
        所以 ``MailGetListPacket`` 里只有 ``extensionObject``。
        """
        return tuple(name for name, count in self.member_counts.items() if count == 0)

    @property
    def parse_failures(self) -> tuple[str, ...]:
        """**有 ProtoMember 却没解析出字段**的类 —— 解析规则需要更新。

        这类必须显式报警：它和「空报文」在输出里长得一模一样，
        但含义完全相反（一个是正常，一个是解析坏了）。
        """
        return tuple(
            name
            for name, count in self.member_counts.items()
            if count > 0 and not self.fields.get(name)
        )


def collect_fields(dump_path: Path, class_names: Sequence[str]) -> ExtractionReport:
    """取一批协议类的字段顺序（单遍扫描）。"""
    fields, member_counts = extract_fields_for_classes(dump_path, class_names)
    return ExtractionReport(fields=fields, member_counts=member_counts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_proto_fields.py",
        description="从 dump.cs 提取各报文的 protobuf 字段顺序（编号依据）",
    )
    parser.add_argument("--dump", metavar="路径", help="dump.cs 路径（默认取 config.DUMP_CS_PATH）")
    parser.add_argument("--all", action="store_true", help="导出全部协议类（文件会很大）")
    parser.add_argument("--show", metavar="类名", help="只打印某个类的字段与编号，不写盘")
    parser.add_argument("--check", action="store_true", help="只校验现有文件是否过期（不写盘）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    force_utf8_output()
    args = build_parser().parse_args(argv)

    dump_path = resolve_dump_path(args.dump)
    if not dump_path.is_file():
        print(f"✘ 找不到 dump.cs：{dump_path}")
        print("  可用 --dump 指定路径，或设置环境变量 CHERRYTALE_MATERIAL_ROOT")
        return 1

    # ---- 单类查询：不写盘 ----
    if args.show:
        names = extract_proto_fields(dump_path, args.show)
        if not names:
            print(f"✘ {args.show} 没有解析到任何 ProtoMember 字段")
            print("  可能原因：类名拼写不同、或它不是 protobuf 报文类")
            return 1
        print(f"{args.show}（{len(names)} 个字段，按声明顺序 = 编号顺序）")
        print("-" * 60)
        for index, name in enumerate(names, start=1):
            print(f"  #{index:<3} {name}")
        return 0

    class_names = list(discover_proto_classes(dump_path)) if args.all else list(FOCUS_CLASSES)

    size_mb = dump_path.stat().st_size / 1024 / 1024
    print(f"dump.cs    : {dump_path}（{size_mb:.1f} MB）")
    print(f"目标类     : {len(class_names)} 个")

    report = collect_fields(dump_path, class_names)

    with_fields = len(report.fields) - len(report.empty_packets)
    print(f"解析结果   : {with_fields} 个类共 {report.field_count} 个字段")
    if report.empty_packets:
        print(
            f"· 空报文   : {len(report.empty_packets)} 个类本身没有字段（正常）—— "
            f"{', '.join(report.empty_packets[:6])}"
        )
    if report.parse_failures:
        print(
            f"✘ 解析异常 : {len(report.parse_failures)} 个类有 ProtoMember 却没解析出字段 —— "
            f"{', '.join(report.parse_failures)}"
        )
        print("             这说明解析规则需要更新，请核对 dump.cs 的书写形式")

    content = render_module(report.fields)
    output_path = config.PROJECT_ROOT / "models" / "proto_fields.py"

    if args.check:
        return 0 if check_module(output_path, content) else 1

    write_module(output_path, content)
    print(f"✔ 已生成   : {output_path}（{len(content.splitlines())} 行）")
    print(
        "  提示：普通报文类「编号 = 声明顺序」**已由抓包验证**；"
        "RootPacket 的子包槽位是例外（编号 = 消息号）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

