# -*- coding: utf-8 -*-
import sys

sys.stdout.reconfigure(encoding='utf-8')

material_areas = [
    (128000020, "角色素材", "鎮守挑戰 (守卫)"),
    (128000021, "角色素材", "衝鋒挑戰 (战士)"),
    (128000022, "角色素材", "精準挑戰 (射手)"),
    (128000023, "角色素材", "應援挑戰 (辅助)"),
    (128000024, "角色素材", "轟炸挑戰 (法师)"),
    (128000025, "角色素材", "禮物挑戰 (好感度)"),
    (128000026, "角色素材", "經驗書挑戰 (经验书)"),
    (128010001, "元素試煉", "火之元素試煉塔"),
    (128010002, "元素試煉", "水之元素試煉塔"),
    (128010003, "元素試煉", "風之元素試煉塔"),
    (128010004, "元素試煉", "土之元素試煉塔"),
    (128010005, "元素試煉", "光之元素試煉塔"),
    (128010006, "元素試煉", "闇之元素試煉塔"),
]

area_ids = {a[0]: a for a in material_areas}

with open('Cherrytale Asset/TextAsset/SpecialSectionData', 'r', encoding='utf-8') as f:
    headers = f.readline().strip().replace('┤', '').split('|')
    area_idx = headers.index('areaID')
    name_idx = headers.index('sectionName')
    cheat_idx = headers.index('cheatCheck')
    energy_idx = headers.index('energyCost')
    sweep_idx = headers.index('sweepOff')
    
    sections_by_area = {aid: [] for aid in area_ids}
    
    for line in f:
        row = line.strip().replace('┤', '').split('|')
        try:
            aid = int(row[area_idx]) if row[area_idx] != 'NULL' else None
        except ValueError:
            continue
        if aid in area_ids:
            sections_by_area[aid].append({
                'id': int(row[0]),
                'name': row[name_idx],
                'cheatCheck': int(row[cheat_idx]),
                'energyCost': int(row[energy_idx]),
                'sweepOff': int(row[sweep_idx]),
            })

for aid, cat, desc in material_areas:
    secs = sections_by_area[aid]
    print(f"\n=== [{cat}] {desc} (AreaID: {aid}) - 共 {len(secs)} 关 ===")
    for s in secs:
        print(f"  Section {s['id']}: {s['name']:15s} | cheatCheck={s['cheatCheck']} | energyCost={s['energyCost']} | sweepOff={s['sweepOff']}")
