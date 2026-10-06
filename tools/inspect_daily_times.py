# -*- coding: utf-8 -*-
import sys

sys.stdout.reconfigure(encoding='utf-8')

# Search dump.cs for GetDailyFinishTimes implementation
with open('Cherrytale IL2CPP/dump.cs', 'r', encoding='utf-8', errors='ignore') as f:
    for idx, line in enumerate(f, 1):
        if 'GetDailyFinishTimes' in line:
            print(f'{idx}: {line.strip()[:100]}')

# Search for daily challenge limit in SpecialAreaData
with open('Cherrytale Asset/TextAsset/SpecialAreaData', 'r', encoding='utf-8') as f:
    headers = f.readline().strip().replace('┤', '').split('|')
    print("SpecialAreaData headers:", headers)
    for line in f:
        row = line.strip().replace('┤', '').split('|')
        if row[0] in ['128000020', '128010001']:
            print(row[0], [(h, row[i]) for i, h in enumerate(headers) if row[i] not in ['NULL', '-1', '0']])
