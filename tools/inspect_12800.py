# -*- coding: utf-8 -*-
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('Cherrytale Asset/TextAsset/SpecialAreaData', 'r', encoding='utf-8') as f:
    headers = f.readline().strip().replace('┤', '').split('|')
    for idx, line in enumerate(f, 2):
        row = line.strip().replace('┤', '').split('|')
        aid = row[0]
        if aid.startswith('12801') or aid.startswith('12800'):
            print(f"ID:{aid:12s} Name:{row[headers.index('areaName')]:25s} type:{row[1]} progress:{row[5]} weekly:{row[7]}")
