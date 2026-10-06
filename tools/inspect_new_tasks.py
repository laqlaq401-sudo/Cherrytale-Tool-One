# -*- coding: utf-8 -*-
"""Inspect packet IDs and notes for Voyage/Brilliant and Material/Trial stages."""
import re
import os

def search_in_file(filepath, patterns):
    if not os.path.exists(filepath):
        return
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for idx, line in enumerate(f, 1):
            for pat in patterns:
                if re.search(pat, line, re.IGNORECASE):
                    print(f"[{os.path.basename(filepath)}:{idx}] {line.strip()[:120]}")
                    break

def main():
    print("=== SEARCHING PACKET IDS ===")
    patterns_track = [r"航迹", r"辉煌", r"Voyage", r"Brilliant", r"Track", r"Collect", r"Material", r"Trial", r"Element", r"Hero", r"试炼", r"素材"]
    search_in_file("models/packet_ids.py", patterns_track)
    search_in_file("models/const_ids.py", patterns_track)
    search_in_file("notes/recon_findings.md", patterns_track)
    search_in_file("notes/metadata_strings.txt", [r"辉煌", r"航迹", r"试炼", r"素材", r"搜集"])

if __name__ == '__main__':
    main()
