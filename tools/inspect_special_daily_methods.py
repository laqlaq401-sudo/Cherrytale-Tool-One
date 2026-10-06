# -*- coding: utf-8 -*-
import sys

sys.stdout.reconfigure(encoding='utf-8')

# Search dump.cs around SpecialDailyArea_UIView for how it retrieves sections and areas
with open('Cherrytale IL2CPP/dump.cs', 'r', encoding='utf-8', errors='ignore') as f:
    for idx, line in enumerate(f, 1):
        if 'SpecialDailyArea' in line and ('Set' in line or 'Get' in line or 'Area' in line or 'Section' in line):
            if 'public' in line or 'private' in line or 'internal' in line:
                print(f"{idx}: {line.strip()[:100]}")
