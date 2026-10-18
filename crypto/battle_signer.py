#!/usr/bin/env python3
"""通用战斗签名与令牌计算工具（Universal Battle Signer）。

【算法与逆向工程背景】
逆向反汇编 GameAssembly.dll 证实，游戏内核心战斗协议结算时的 Token 均由
底层 Utility 模块生成，并在网关处使用固定 Salt 进行 MD5 散列核对：

1. 通用战斗 / 竞技场对决 / 关卡试炼：
   - 对应汇编：Utility.GetFightVerifyData (VA: 0x181019780)
   - 汇编结构：Concat(iFightToken, iRandomData2, iWinOrLost.ToString(), "03120908")
                → GetMd5HashWithString(...)
   - 算法式：MD5(iFightToken + iRandomData2 + iWinOrLost.ToString() + "03120908")
   - 在竞技场对决中：iRandomData2 为 targetPlayerID，iWinOrLost 为 0（胜利）或 1（失败）
   - 在主线/常规战斗中：iRandomData2 为 timeCostSec，iWinOrLost 默认为 1（胜利）

2. 失控炼成阵（Yimo）：
   - 对应汇编：Utility.GetFightVerifyData_YImo (VA: 0x1810196E0)
   - 算法式：MD5(iFightToken + iNowScore.ToString() + "03120908")

【绑定表 —— iRandomData2 在每个玩法里到底是谁】
公式只有一行，容易错的从来是"哪个字段进了第二位"。下表每行都有抓包实证
（`tools/solve_signer_binding.py` 可从抓包独立反解，见各行"证据"）：

| 玩法 | 结算包 | iRandomData2 | winLose | 证据 |
|---|---|---|---|---|
| 主线 / 素材 PVE | 11017 | `timeCostSec`（战斗耗时秒） | `1` | raw/027 下发 fightToken，raw/005 的 17 秒 |
| 竞技场 | 26019 | `targetPlayerID`（对手玩家 ID） | `0` 胜利 / `1` 失败 | raw/020 的 26004 同时给出 29324 与 6175214 |
| 失控炼成阵 | 22109 | `nowScore`（本次得分） | 不参与（两段式） | raw/26 报文内含 `Score:53247` |

⚠ 新增玩法时**不要**照抄上表推：`dump.cs` 里 `GetFightVerifyData` 有 4 个同名实现
（三个私有实例版 + 一个静态版），客户端是复制粘贴的，每个玩法用哪份、传什么，
必须用 `tools/solve_signer_binding.py` 从抓包反解一次再落表。

【两个必须知道的坑】
1. **拼接歧义**：MD5 只对拼接后的字符串做散列，`"17"+"1"` 与 `"171"+""` 结果完全相同。
   所以"token 对得上"**不能**证明切分方式；必须回到报文里确认字段类型与取值来源。
2. **ILFix 热补丁**：`GetFightVerifyData` 开头就是 `IsPatched(0x2D2B)` 分支，
   命中时改走 `ILFixInterfaceBridge.GetPatch(0x2D2B)` 的动态包装
   （`__Gen_Wrap_1875`，签名同为 object/object/int）。
   也就是说这个签名函数**可以在不发版的情况下被服务端替换**。
   哪天公式突然失效，先查热补丁，再怀疑自己的实现。

【存证解耦特征】
签名计算入参 100% 解耦于战斗存证字符串（battleServerVerify），存证内容或 Proofs 
不参与任何 MD5 散列运算。

【红线守则】
严禁触碰天命系统（Destiny）与高难度属性试炼。
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import sys
from typing import Final

#: 战斗结算签名固定盐值（逆向自 GameAssembly.dll 静态字面量）
BATTLE_VERIFY_SALT: Final[str] = "03120908"

#: 官方权威玩法战斗类型（对齐 dump.cs eBattleType，TypeDefIndex 10058）
BATTLE_TYPE_PVE: Final[int] = 0          # 常规主线/素材 PVE
BATTLE_TYPE_ARENA: Final[int] = 1        # 天梯竞技场 ArenaPVP
BATTLE_TYPE_EXPERIMENT: Final[int] = 4   # 辉煌航迹·试炼 Experiment
BATTLE_TYPE_WUDOU: Final[int] = 6        # 天命对决 WuDouPVP
BATTLE_TYPE_YIMO: Final[int] = 7         # 失控炼成阵 YiMoBattle
BATTLE_TYPE_TOP_PVP: Final[int] = 9      # 荣耀之巅 TopPVP


def calc_fight_token(
    fight_token: int | str,
    random_data2: int | str,
    win_lose: int | str = 0,
    salt: str = BATTLE_VERIFY_SALT,
) -> str:
    """计算通用战斗/竞技场 Token。

    :param fight_token: 进战下发的会话令牌 fightToken（如 29324）
    :param random_data2: 随机或业务关联字段（竞技场填 targetPlayerID，通用战斗填 timeCost）
    :param win_lose: 胜负标识（竞技场胜利为 0，主线胜利通常为 1）
    :param salt: 签名加盐，默认为 "03120908"
    :return: 32 位小写十六进制 MD5 签名摘要
    """
    raw = f"{fight_token}{random_data2}{win_lose}{salt}"
    return hashlib.md5(raw.encode("ascii")).hexdigest()


def calc_yimo_token(
    fight_token: int | str,
    score: int | str,
    salt: str = BATTLE_VERIFY_SALT,
) -> str:
    """计算失控炼成阵（Yimo）战斗得分 Token。

    :param fight_token: 进战下发的会话令牌 fightToken（如 29744）
    :param score: 本次对锡樵夫造成的伤害得分 nowScore（如 53247）
    :param salt: 签名加盐，默认为 "03120908"
    :return: 32 位小写十六进制 MD5 签名摘要
    """
    raw = f"{fight_token}{score}{salt}"
    return hashlib.md5(raw.encode("ascii")).hexdigest()


def calc_arena_token(
    fight_token: int | str,
    target_player_id: int | str,
    win_lose: int | str = 0,
) -> str:
    """快捷计算竞技场结算 Token。

    :param fight_token: 进战下发的 fightToken
    :param target_player_id: 对手玩家 playerID
    :param win_lose: 胜负状态（0 胜利，1 失败，默认 0）
    :return: 32 位小写十六进制 MD5 签名摘要
    """
    return calc_fight_token(fight_token, target_player_id, win_lose)


def calc_general_fight_token(
    fight_token: int | str,
    time_cost_sec: int | str,
    win_lose: int | str = 1,
) -> str:
    """快捷计算主线及通用 PVE 战斗结算 Token。

    :param fight_token: 进战下发的 fightToken
    :param time_cost_sec: 战斗耗时秒数
    :param win_lose: 胜负状态（通常 1 为胜利，默认 1）
    :return: 32 位小写十六进制 MD5 签名摘要
    """
    return calc_fight_token(fight_token, time_cost_sec, win_lose)


def make_empty_server_verify(
    section_id: int | dict[str, Any] | str = 0,
    battle_type: int = 0,
) -> str:
    """生成极简/免存证（Proofs 为空）的 battleServerVerify 字符串。

    经逆向与抓包证实，失控炼成阵与竞技场等玩法在发奖时完全免存证复核，
    使用空 Proofs 载荷并按官方 GZIP+Base64 编码即可安全下发奖励。
    """
    sec_id: Any = section_id
    if isinstance(section_id, str):
        try:
            sec_id = int(section_id)
        except ValueError:
            sec_id = section_id
    payload = json.dumps(
        {"BattleType": int(battle_type), "SectionId": sec_id, "Proofs": []},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.b64encode(gzip.compress(payload)).decode("ascii")


def main() -> int:
    """命令行调试与计算入口。"""
    parser = argparse.ArgumentParser(
        description="Cherry Tale 通用战斗签名计算器（支持失控炼成阵、竞技场、通用战斗）"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--yimo",
        nargs=2,
        metavar=("FIGHT_TOKEN", "SCORE"),
        help="计算失控炼成阵 Token，如 --yimo 29744 53247",
    )
    group.add_argument(
        "--arena",
        nargs=2,
        metavar=("FIGHT_TOKEN", "PLAYER_ID"),
        help="计算竞技场对决 Token，如 --arena 29324 6175214",
    )
    group.add_argument(
        "--fight",
        nargs=3,
        metavar=("FIGHT_TOKEN", "RANDOM2", "WIN_LOSE"),
        help="计算通用战斗 Token，如 --fight 120483 17 1",
    )

    args = parser.parse_args()

    if args.yimo:
        token = calc_yimo_token(args.yimo[0], args.yimo[1])
        print(f"Yimo Token: {token}")
    elif args.arena:
        token = calc_arena_token(args.arena[0], args.arena[1], win_lose=0)
        print(f"Arena Token: {token}")
    elif args.fight:
        token = calc_fight_token(args.fight[0], args.fight[1], args.fight[2])
        print(f"Fight Token: {token}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
