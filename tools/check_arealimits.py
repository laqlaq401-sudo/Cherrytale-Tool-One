# -*- coding: utf-8 -*-
import sys

sys.stdout.reconfigure(encoding='utf-8')

material_areas = [
    128000020, 128000021, 128000022, 128000023, 128000024, 128000025, 128000026,
    128010001, 128010002, 128010003, 128010004, 128010005, 128010006
]

with open('Cherrytale Asset/TextAsset/SpecialAreaData', 'r', encoding='utf-8') as f:
    headers = f.readline().strip().replace('┤', '').split('|')
    aid_idx = headers.index('areaID')
    name_idx = headers.index('areaName')
    limit_idx = headers.index('arealimit')
    for line in f:
        row = line.strip().replace('┤', '').split('|')
        aid = int(row[aid_idx])
        if aid in material_areas:
            print(f"Area {aid:10d} | Name: {row[name_idx]:20s} | arealimit: {row[limit_idx]}")
