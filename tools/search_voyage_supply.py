# -*- coding: utf-8 -*-
import sys, os

sys.stdout.reconfigure(encoding='utf-8')

dirpath = 'Cherrytale Asset/TextAsset'
for fname in os.listdir(dirpath):
    fpath = os.path.join(dirpath, fname)
    try:
        with open(fpath, 'r', encoding='utf-8', errors='ignore') as f:
            for idx, line in enumerate(f, 1):
                if any(k in line for k in ['輝煌航跡', '辉煌航迹', '航跡', '搜集物資', '搜集物资', '收集物資', '收集物资', '物資']):
                    clean_line = line.strip().replace('┤', '')
                    print(f"[{fname}:{idx}] {clean_line[:120]}")
    except Exception:
        pass
