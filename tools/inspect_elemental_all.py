# -*- coding: utf-8 -*-
import sys, os

sys.stdout.reconfigure(encoding='utf-8')

dirpath = 'Cherrytale Asset/TextAsset'
for fname in os.listdir(dirpath):
    if 'section' in fname.lower():
        fpath = os.path.join(dirpath, fname)
        with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
            for idx, line in enumerate(f, 1):
                if any(k in line for k in ['試煉', '試鍊', '元素', '遺跡', '火山', '風暴', '荒漠', '極地', '深淵', '聖光']):
                    clean_line = line.strip().replace('┤', '')
                    print(f"[{fname}:{idx}] {clean_line[:120]}")
                    if idx > 100:
                        break
