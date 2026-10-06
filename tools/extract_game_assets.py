#!/usr/bin/env python3
"""从 Cherrytale Asset 中提取 UI 所需的图片资源到 web/assets/icons/ 目录。

保持对 Cherrytale Asset 只读访问，严格不修改源文件。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT_DIR / "Cherrytale Asset" / "Sprite"
DEST_BASE = ROOT_DIR / "web" / "assets" / "icons"

# (源文件名, 目标相对路径)
ASSET_MAPPINGS: list[tuple[str, str]] = [
    # 1. 品牌与头像
    # 【brand/logo.png 已移出，2026-10-05】界面 logo 现要求与 EXE / APK 图标
    # 共用同一张头像，改由 tools/build_icons.py 从 assets/icon_source.png 派生。
    # 此处务必不要再加回 Logo_01.png —— 两个生成器抢同一个文件，谁后跑谁赢，
    # 会导致"重跑提取脚本后 logo 悄悄退回中文艺术字横幅"。
    ("Btn_Home_Role2.png", "brand/default_avatar.png"),

    # 2. 侧边栏与分区导航
    ("Battle_btn034.png", "nav/tab_info.png"),
    ("Global_Icon_13.png", "nav/tab_auto.png"),
    ("Battle_026.png", "nav/tab_manual.png"),
    ("Item_002_Icon.png", "nav/tab_diamond.png"),
    ("Battle_btn046.png", "nav/tab_logs.png"),
    ("Level_Book.png", "nav/tab_about.png"),

    # 3. 角色与信息展示面板卡片
    ("Btn_Home_Role2.png", "stats/player.png"),
    ("Global_Btn_99.png", "stats/server.png"),
    ("Global_Item_02.png", "stats/coin.png"),
    ("Item_002_Icon.png", "stats/diamond.png"),
    ("Global_Item_01.png", "stats/stamina.png"),
    ("LevelTop_Entrance5.png", "stats/arena.png"),
    ("Btn_Ranking.png", "stats/power.png"),
    ("Level_other_14.png", "stats/alliance.png"),
    ("Btn_Home_EggA.png", "stats/wudou.png"),
    ("Battle_135.png", "stats/yimo.png"),
    ("Btn_Ranking.png", "stats/top_pvp_rank.png"),
    ("Btn_Ranking.png", "stats/top_pvp_points.png"),
    ("LevelTop_Entrance2.png", "stats/stage.png"),
    ("Level_Database_01.png", "stats/backpack.png"),

    # 4. 任务卡片图标
    ("ico_mail_black.png", "tasks/mail.png"),
    ("Btn_Home_VoyageGift.png", "tasks/free_gift.png"),
    ("CheckIn_7day_4.png", "tasks/alliance_sign_in.png"),
    ("Level_other_20.png", "tasks/alliance_mining_personal.png"),
    ("Level_other_20.png", "tasks/alliance_mining_team.png"),
    ("Global_Item_02.png", "tasks/alliance_donate.png"),
    ("LevelTop_Entrance1.png", "tasks/grand_line_supply.png"),
    ("BoxIcon_01_02.png", "tasks/daily_box.png"),
    ("LevelTop_Entrance3.png", "tasks/sweep_activity.png"),
    ("battleelement_1-s.png", "tasks/sweep_material.png"),
    ("Global_Item_02.png", "tasks/market_buy.png"),
    ("LevelTop_Entrance5.png", "tasks/top_pvp_battle.png"),
    ("Global_Item_01.png", "tasks/bbq_energy.png"),
    ("Item_002_Icon.png", "tasks/buy_energy.png"),
    ("LevelTop_Entrance2.png", "tasks/push_main_stage.png"),
    ("CheckIn_7day_4.png", "tasks/sign_in.png"),
    ("Btn_Home_EggA.png", "tasks/daily_free_draw.png"),
    ("Level_Database_016.png", "tasks/star_box.png"),

    # 5. UI 状态与小图标
    ("Level_star_01.png", "ui/star_gold.png"),
    ("Level_star_02.png", "ui/star_gray.png"),
    ("Level_Lock.png", "ui/lock.png"),
    ("Battle_btn022.png", "ui/exit.png"),
]


def extract_assets() -> int:
    if not SRC_DIR.exists():
        print(f"[ERROR] Source directory not found: {SRC_DIR}", file=sys.stderr)
        return 1

    total_bytes = 0
    copied_count = 0

    print(f"[*] Extracting assets from {SRC_DIR} to {DEST_BASE}...")
    for src_name, rel_dest in ASSET_MAPPINGS:
        src_path = SRC_DIR / src_name
        dest_path = DEST_BASE / rel_dest

        if not src_path.exists():
            print(f"[WARN] Missing source asset: {src_name}", file=sys.stderr)
            continue

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)

        size = dest_path.stat().st_size
        total_bytes += size
        copied_count += 1
        print(f"  + [{size:>7} B] {src_name} -> {rel_dest}")

    print(f"\n[OK] Successfully extracted {copied_count}/{len(ASSET_MAPPINGS)} icons.")
    print(f"[OK] Total size: {total_bytes / 1024:.1f} KB ({total_bytes} bytes).")
    return 0


if __name__ == "__main__":
    sys.exit(extract_assets())
