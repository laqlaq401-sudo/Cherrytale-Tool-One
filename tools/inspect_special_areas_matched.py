# -*- coding: utf-8 -*-
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('Cherrytale Asset/TextAsset/SpecialAreaData', 'r', encoding='utf-8') as f:
    headers = f.readline().strip().replace('┤', '').split('|')
    print("Headers:", [(i, h) for i, h in enumerate(headers)])
    for idx, line in enumerate(f, 2):
        row = line.strip().replace('┤', '').split('|')
        area_id = row[0]
        area_name = row[headers.index('areaName')]
        # check if area_id or row has any match
        if any(k in line for k in ['guard', 'fighters', 'archer', 'priest', 'master', 'Favor', 'Exp', 'battleelement', '元素', '試煉', '試鍊', '挑戰', '遺跡']):
            print(f"ID:{area_id:12s} Name:{area_name:25s} type:{row[1]} progress:{row[5]} weekly:{row[7]}")
