# -*- coding: utf-8 -*-
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('Cherrytale Asset/TextAsset/SpecialSectionData', 'r', encoding='utf-8') as f:
    headers = f.readline().strip().replace('┤', '').split('|')
    for idx, line in enumerate(f, 2):
        row = line.strip().replace('┤', '').split('|')
        sec_name = row[1]
        sec_id = row[0]
        if any(k in sec_name for k in ['元素', '火', '水', '風', '土', '光', '闇', '暗', '風暴', '火山', '荒漠', '極地', '深淵', '聖光']):
            if '遺跡' in sec_name or '試煉' in sec_name or '元素' in sec_name or '挑戰' in sec_name:
                print(f"ID:{sec_id:12s} Name:{sec_name:18s} areaID:{row[headers.index('areaID')]:12s} cheatCheck:{row[headers.index('cheatCheck')]} energyCost:{row[headers.index('energyCost')]} sweepOff:{row[headers.index('sweepOff')]}")
