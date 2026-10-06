# -*- coding: utf-8 -*-
import sys

sys.stdout.reconfigure(encoding='utf-8')

print("=== SpecialAreaData ===")
with open('Cherrytale Asset/TextAsset/SpecialAreaData', 'r', encoding='utf-8') as f:
    headers = f.readline().strip().replace('┤', '')
    print('HEADERS:', headers)
    for idx, line in enumerate(f, 2):
        if any(w in line for w in ['素材', '試煉', '试炼', '16800', 'guard', 'battleelement']):
            print(f'{idx}: {line.strip().replace("┤", "")[:120]}')

print("\n=== SpecialSectionData Sample ===")
with open('Cherrytale Asset/TextAsset/SpecialSectionData', 'r', encoding='utf-8') as f:
    headers = f.readline().strip().replace('┤', '')
    print('HEADERS:', headers)
    for idx, line in enumerate(f, 2):
        if any(w in line for w in ['素材', '試煉', '试炼', '16800']):
            print(f'{idx}: {line.strip().replace("┤", "")[:120]}')
            if idx > 50:
                break
