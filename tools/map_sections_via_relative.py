# -*- coding: utf-8 -*-
import sys

sys.stdout.reconfigure(encoding='utf-8')

# Read SpecialSectionData sectionIDs
sections = []
with open('Cherrytale Asset/TextAsset/SpecialSectionData', 'r', encoding='utf-8') as f:
    headers = f.readline().strip().replace('┤', '').split('|')
    for line in f:
        row = line.strip().replace('┤', '').split('|')
        sections.append((int(row[0]), row[1], int(row[headers.index('cheatCheck')]), int(row[headers.index('energyCost')]), int(row[headers.index('sweepOff')])))

# Read line 746 from DataRelative
area_ids_from_rel = []
with open('Cherrytale Asset/TextAsset/DataRelative', 'r', encoding='utf-8') as f:
    for idx, line in enumerate(f, 1):
        if idx == 746:
            parts = line.strip().replace('┤', '').split('|')
            area_ids_from_rel = [int(p) if p != '-1' and p != 'NULL' else None for p in parts]
            break

print(f"Total sections: {len(sections)}, Total relative areaIDs: {len(area_ids_from_rel)}")

target_areas = {
    128000020: "鎮守挑戰 (守卫)",
    128000021: "衝鋒挑戰 (战士)",
    128000022: "精準挑戰 (射手)",
    128000023: "應援挑戰 (辅助)",
    128000024: "轟炸挑戰 (法师)",
    128000025: "禮物挑戰 (好感度)",
    128000026: "經驗書挑戰 (经验书)",
    128010001: "火之元素試煉塔",
    128010002: "水之元素試煉塔",
    128010003: "風之元素試煉塔",
    128010004: "土之元素試煉塔",
    128010005: "光之元素試煉塔",
    128010006: "闇之元素試煉塔",
}

found = {aid: [] for aid in target_areas}
for (sec_id, name, cheat, energy, sweep), aid in zip(sections, area_ids_from_rel):
    if aid in target_areas:
        found[aid].append((sec_id, name, cheat, energy, sweep))

for aid, desc in target_areas.items():
    secs = found[aid]
    print(f"\nArea {aid} ({desc}): 共 {len(secs)} 关")
    for s in secs:
        print(f"    ID:{s[0]} Name:{s[1]:15s} cheatCheck={s[2]} energyCost={s[3]} sweepOff={s[4]}")
