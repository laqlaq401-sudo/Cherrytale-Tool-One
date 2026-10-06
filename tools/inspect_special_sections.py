# -*- coding: utf-8 -*-
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('Cherrytale Asset/TextAsset/SpecialSectionData', 'r', encoding='utf-8') as f:
    headers = f.readline().strip().replace('┤', '').split('|')
    print("Headers:", [(i, h) for i, h in enumerate(headers)])
    for idx, line in enumerate(f, 2):
        row = line.strip().replace('┤', '').split('|')
        sec_name = row[1]
        sec_id = row[0]
        if any(k in sec_name for k in ['挑戰', '試煉', '試鍊', '元素', '鎮守', '衝鋒', '精準', '轟炸', '應援', '好感', '經驗', 'Exp', 'Favor']):
            print(f"ID:{sec_id:12s} Name:{sec_name:12s} cheatCheck:{row[headers.index('cheatCheck')]} energyCost:{row[headers.index('energyCost')]} sweepOff:{row[headers.index('sweepOff')]}")
