# -*- coding: utf-8 -*-
import re

with open("models/const_ids.py", "r", encoding="utf-8") as f:
    for idx, line in enumerate(f, 1):
        if "SpecialDaily" in line or "DailyRegion" in line or "Region" in line or "Stage" in line or "Section" in line or "Altar" in line:
            print(f"const_ids.py:{idx}: {line.strip()}")

print("\n--- PACKET IDS ---")
with open("models/packet_ids.py", "r", encoding="utf-8") as f:
    for idx, line in enumerate(f, 1):
        if any(w in line for w in ["Stage", "Section", "Daily", "Sweep", "Material", "Fight", "Pass", "Supply", "Ship", "Reward"]):
            print(f"packet_ids.py:{idx}: {line.strip()}")
