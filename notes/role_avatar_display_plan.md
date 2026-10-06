# 「我的角色」加头像 — 可行性与落地方案（2026-10-06 侦察，只计划不写码）

> 结论先说：**能对上，但不是「文件名直接相等」，中间缺一道换算；而且仓库里的 196 张
> 静态头像只覆盖 83% 的可拥有角色形态，缺的 28 张（ct096~ct160）在游戏 CDN 里也还没有。**

---

## 一、现状：现在是什么样

- **静态头像目录** `web/assets/avatars/`：**196 个 PNG** + `manifest.json`（code→md5）。
  它们由 `tools/extract_avatar_icons.py` 从热更 CDN 全量预提取，`img_hero_{code}_icon`
  bundle 里贴图名 = `{code}_Icon_Texture`，与文件名严格对齐。
- **前端「我的角色」分组**（`web/assets/app.js`，renderInfo 第 ④ 组，约 455~520 行）：
  每张卡是 `div.role-chip`，内容只有两行文字 —— `span.role-chip-name`（角色名）+
  `span.role-chip-meta`（`Lv.N　★N`）。**没有任何 `<img>`**。
- **数据源** `/api/player` 的 `roles.items[]`，由 `tasks/roles.py::_describe()` 产出，
  字段只有：`role_sid` / `role_id`(RoleMainID) / `role_info_id` / `level` / `star` / `name`。
  **没有头像字段。**
- **唯一已有的头像链路**是「玩家自己的头像」：登录 1004 `PlayerClass.icon`
  （头像道具 itemID）→ `services/avatar_service.py` → data-url → `player.avatar_data_url`
  → 前端 `div.hero-avatar` 的 `<img src>`。这条链路**不适用于「我的角色」**，
  因为名册里每个角色只有 RoleMainID，没有「这个角色用哪张头像道具」。

---

## 二、核心发现：命名对不上，但规律是死的

仓库静态文件叫 **`a001_03h.png`**，而配置表给出的名字是：

| 来源 | 字段 | 值 | 与静态文件的关系 |
|---|---|---|---|
| `RoleMainData` | `roleModelID` | `a001_03` | **少一个 `h`** |
| `RoleInfoData` | `icon1_AssetId` | `a001_01_Icon` | 去掉 `_Icon` 后 **少一个 `h`** |
| `ItemData`（头像道具） | `iconAssetID` | `e001_01h_Icon` | **去掉 `_Icon` 后完全相等** ✅ |

也就是说：**`h` 后缀（= "hero 主头像"）只写在 ItemData 的 `iconAssetID` 里，
`RoleMainData.roleModelID` 和 `RoleInfoData.icon1_AssetId` 都是不带 `h` 的裸编号。**

> 这也解释了为什么 `services/avatar_service.py::hero_code_from_ids()` 这条
> 「roleModelID 兜底链」**从未命中过**：它拿 `a001_03` 去查文件 `a001_03.png`，
> 而文件叫 `a001_03h.png`。（该函数注释写"几乎全表 NULL"其实是错的 ——
> roleModelID **6653 行全部有值**，错的只是没补 `h`。）

### 实测覆盖率（本次全量跑表，非估计）

| 口径 | 命中 | 总数 | 覆盖率 |
|---|---|---|---|
| `RoleMainData` 全表（含小怪/占位/未开放） | 4448 | 6653 | 66.9% |
| **仅「可拥有形态」**（有真实名字的 RoleInfoID） | **1660** | **1999** | **83.0%** |
| ItemData 头像道具（玩家可能持有的头像） | 184 | 212 | 86.8% |

**可拥有的 196 张静态图全部被用到（196/196，零冗余）**，缺的是 339 个形态 /
69 个角色名，几乎全是**「XXX1階/2階/3階/4階」这类超限突破形态**（`ct116_01`~`ct121_04`）
和 **`mb0xx_1` / `da04_02` 这类怪物品阶**。

**关键结论：这 339 个不是"我们没提取"，是游戏 CDN 里就还没有。**
我把已提取的 196 张与 `tmp/index_save.txt`（37k 条热更清单）逐条比对：
热更清单里 `img_hero_*_icon` 共 **668 条**，其中**真正带 `h` 尾的现役主头像
恰好也是 196 个，提取率 196/196 = 100%，缺口为 0**。
`ct096/ct111/ct150` 等 28 个头像道具虽然已被 ItemData 引用，但热更清单里
**根本不存在对应 bundle**（游戏自己还没放包）。

---

## 三、要改哪些地方（四个文件，一条链路）

### ① 后端：新增「角色 → 头像文件名」解析（`models/game_config.py` + `services/avatar_service.py`）

数据全部已在现有加载器里，**不需要新配置表引用，不需要重新 sync assets**：

- `RoleMainData` → `load_role_id_map()` 已读 `qualityUpRoleSubID`（品质链）；
  **新增读 `roleModelID`**，产出 `role_main_id → icon code`。
- `RoleInfoData` → `load_role_id_map()` 已按 `initialRoleMainID` 建反查；
  **新增读 `icon1_AssetId`**，产出 `role_info_id → icon code`。
- 换算函数（放 `services/avatar_service.py`，复用现有 `_web_static_dir()`）：

```
resolve_role_icon_code(role_id, role_info_id) -> str
    candidates = [
        RoleMainData[role_id].roleModelID,          # a001_03   → +h
        RoleInfoData[role_info_id].icon1_AssetId,   # a001_01_Icon → 去_Icon → +h
    ]
    逐个候选，按序试 4 种文件名变体：
        {c}                # ct026       / a001_03（旧版冷存，未随包）
        {c}h               # ct026h ✅   / a001_03h ✅   ← 主力命中
        {pr}{num}_01       # ct026_01
        {pr}{num}_01h      # ct026_01h
    返回第一个真实存在的 code；都不存在 → ""（前端回退默认立绘）
```

要点：
- **必须查"文件是否真的存在"**，不能只按规则拼字符串 —— 规则有两套（`NN` 与 `NNh`
  并存、`_01` 系列只在超限形态出现），拼错了比不显示更糟。
- 结果**进程内缓存**（`{role_id: code}` LRU），名册一次上百条不能每条扫盘一次。
- 与现有 `resolve_code()`（玩家头像链）**分开放**，不要搅在一起 ——
  那条是 itemID 主链，这条是 RoleMainID 主链，混在一起以后没法单独排错。

### ② 后端：把头像带进 `/api/player` 的 `roles[]`（`tasks/roles.py` + `webapi/`）

- `_describe()` 每条角色多产出一个 `icon_code`（或直接 `icon_url`）。
- **按项目既有口径用「文件名 code」而不是 base64**：196 张图随包分发、
  页面与 API 同源（PC 8765 / 安卓 8000），前端直接
  `assets/avatars/{code}.png` 即可，**不必再穿一层 base64**（玩家头像用 base64
  是因为它要跨会话文件持久化，角色头像不需要）。
- 拿不到 code 时给 `""`，不要给假路径。

### ③ 前端：卡片加头像（`web/assets/app.js` + `web/assets/input.css`）

- `div.role-chip` 里插一个 `<img class="role-chip-avatar">`，放在名字左侧。
- **`loading="lazy"` 必加**：展开全部时可能一次铺上百条，不加会同时发上百个请求。
- 尺寸沿用现有网格（`.stat-grid--roles` 一行 6 列），头像按 32~40px 方形圆角，
  别把卡片撑高。
- `title` 属性保持现有的「名字（实例 N）」不变。

### ④ 缺图统一占位（★ 2026-10-06 用户定案）

**缺图的角色一律用「003.png」填充**，不再回退到 `assets/icons/stats/player.png`。

- **素材已就位**：`captures/avatar_cold_storage/003.png`，**128×128 RGBA**，
  与 `web/assets/avatars/*.png` **规格完全一致**（同为 128×128 / bitdepth 8 /
  colortype 6），所以**不需要任何缩放或重编码，直接复制过去即可**。

#### ★ 必须先解决的入库问题（否则换机器 / 重装就丢）

实测确认：`.gitignore:114` 把 **`web/assets/avatars/` 整目录忽略，`git ls-files` 返回 0 条**
（`manifest.json` 也不入库）；`captures/` 同样不入库；
`tools/sync_android_assets.py` 的规则里**没有头像目录**（它整目录同步 `web`，
所以安卓侧是跟着 `web/` 走的，`web/assets/avatars/` 里的文件会进 APK）。

⇒ **占位图不能只丢进 `web/assets/avatars/`**，否则它和那 196 张真图一样是"本地生成物"，
新克隆的仓库 / 另一台构建机上既没有真图也没有占位图。三条路，**建议选 B**：

| 方案 | 做法 | 评价 |
|---|---|---|
| A | 加一条 `.gitignore` 白名单 `!web/assets/avatars/000h.png` | ❌ 目录整体被忽略时，**要先 `!web/assets/avatars/` 再逐文件排除**，会顺带把 196 张真图也纳入追踪范围，引发大量误提交 |
| **B ✅** | **占位图入库到受版本控制的位置**（如 `assets/placeholder_avatar.png`），由 `tools/extract_avatar_icons.py` 在提取时**一并复制**到 `web/assets/avatars/000h.png` | 与现有"真图由脚本预提取"的规矩同构；换机器跑一次提取脚本就有；源代码受控、34 KB 极小 |
| C | 前端用内联 base64 常量兜底 | 34 KB base64 ≈ 46 KB 塞进 `app.js`，污染前端源码、且无法被真图自动取代（因为不走同一路径），**否决** |

选 B 的具体落点：`tools/extract_avatar_icons.py` 的 `cmd_all()` 结尾加一句
"冷存/受控占位图 → `OUT_DIR/000h.png`"。**注意**：该脚本依赖 UnityPy 环境
（`tmp/venv_extract`），不该为了复制一张图要求跑提取 —— 所以更稳的做法是
**在 `tools/sync_android_assets.py` 里**（它是发布链的必经步骤，且无重依赖）
加一条"保证 `web/assets/avatars/000h.png` 存在"的幂等复制。**这一点待你拍板。**

- **落位**：`web/assets/avatars/000h.png`。
  ⚠️ **不要叫 `placeholder.png`** —— 换算只产出 `[a-z]+\d+h` 形态的 code，
  混一个异形文件名进去会让 cache-busting 后缀拼接和目录扫描守卫出现分叉。
  用 `000h` 这个**同构名**，则"每个 code 必有同名文件"这条守卫可以继续无脑成立。
- **前端逻辑**：`icon_code` 为空时 `src = "assets/avatars/000h.png"`。
  **建议放前端兜底**，后端保持"如实报告数据源缺口"的语义，渲染层负责化妆。
- **APK 体积**：+34 KB（`003.png` 原始大小），可忽略。
- 注意 `000h` 在 `tmp/index_save.txt` 里**不存在**对应 bundle，
  所以**不可能与任何真实头像 code 冲突**，可安全长期占用。
- 若将来从 CDN 补齐 `ct096~ct160`，占位图**自动被真实图取代**
  （`resolve_role_icon_code()` 命中就返回真 code），**无需改任何代码** ——
  这正是选"文件名占位"而非"前端硬编码兜底"的价值。

### ⑤ 统计口径提示

「角色数量」这张卡**保持现状**（数形态数）。头像缺失是**数据源缺口**，
不要在运行视图里冒裸 ID 或错误码（项目「运行视图纪律」）。

---

## 四、验证方案

1. `tests/test_services_avatar_service.py`（若存在则扩充）：
   - 用**真实 ID 段**做夹具（`ROLE_MAIN_*` / `ROLE_INFO_*` 成对），
     断言 `resolve_role_icon_code()` 的 4 种变体优先级；
   - 断言「拼出来但文件不存在」时返回 `""` 而不是返回一个假 code。
2. `tests/test_tasks_roles.py`：断言 `roles.items[]` 每条都有 `icon_code` 键，
   且值要么是 `""` 要么能在 `web/assets/avatars/` 找到同名文件
   （这条守卫能防住整个链路静默失效）。
   **另断言占位图 `web/assets/avatars/000h.png` 存在**（否则缺图会变成破图）。
3. 覆盖率守卫（新增，可选但推荐）：从 `RoleMainData` 全量算一遍命中率，
   **断言 ≥ 当前基线 83.0%**，防止以后改换算逻辑把命中率悄悄改低。
4. 手工：`python main.py run roles` + 打开 PC 页面，肉眼核对前 6 个角色头像是否
   与角色名一致（尤其 `a001_03h`（葛麗特）vs `a001_01h`（黃金騎士）不能串）。

---

## 五、诚实的风险与取舍

| 风险 | 说明 | 处置建议 |
|---|---|---|
| **缺 28 张图** | `ct096~ct160` 已被 ItemData 引用，但游戏 CDN 无 bundle | **已定案：统一用 `000h.png`（即 003.png）填充**；等游戏更新后换新 index 重跑 `tools/extract_avatar_icons.py`，占位图**自动被真图取代**，零改动 |
| `_01` 系列变体是猜的 | `ct116_01`~`ct121_04` 的 4 种变体规则没有反汇编依据，纯靠命名观察 | 先落地「存在性校验」，命中就是对的、不命中就用占位图 —— **不会出错，只会少显示真图** |
| 超限形态同前缀不同后缀 | `a001_01` vs `a001_03` 是两个不同角色（黃金騎士 / 葛麗特），不能按前缀取第一张 | 必须用 `roleModelID` 的**完整编号**，禁止只取前缀 |
| 占位图可能被误认为真图 | 同一张图出现在很多角色上，用户可能以为"加载错了" | 占位图**不额外加角标**（用户明确要求"全部用这张图填充"）；但方案里记录：若日后觉得混淆，可在 img 上加 `data-placeholder="1"` 供 CSS 做灰度/降透明 |
| **占位图会随构建机丢失** | `web/assets/avatars/` 被 gitignore 且 **0 文件入库**（已实测）；`captures/` 同样不入库 | **必须**把占位图源文件入库到受控路径，由发布链幂等复制；见 §三 ④ 的 A/B/C 三案，建议 B |
| 「角色数量」与有头像数不一致 | 现已由占位图抹平，不再有"空格子" | 卡片副文案保持「来源：实时名册」 |
| 安卓 APK 体积 | 新增 1 张 34 KB 占位图 | 可忽略；无新增配置表引用，**不需要 sync 配置表**（但 web 目录本身要同步） |

---

## 六、要做的事（按顺序，工作量从小到大）

1. **落占位图 + 解决入库**（★ 这是最容易漏的一步）：
   把 `003.png` 落成 **受版本控制的** `assets/placeholder_avatar.png`，
   并让发布链（建议 `tools/sync_android_assets.py`，无重依赖、必经步骤）
   **幂等复制** → `web/assets/avatars/000h.png`。
   原因是 `web/assets/avatars/` **被 gitignore 且 0 文件入库**（已实测），
   只往那儿丢文件等于"只存在于我这台机器"。
2. **加换算**：`models/game_config.py` 补 `roleModelID` / `icon1_AssetId` 两个读取；
   `services/avatar_service.py` 加 `resolve_role_icon_code()`（含存在性校验 + 缓存）。
3. **补字段**：`tasks/roles.py::_describe()` 输出 `icon_code`。
4. **改前端**：`app.js` 的 role-chip 加 `<img loading="lazy">`，空 code 时用
   `assets/avatars/000h.png`；`input.css` 加 `.role-chip-avatar` 样式，跑一次
   `npx tailwindcss -i ./web/assets/input.css -o ./web/assets/styles.css --minify`。
5. **补测试**（§四的 4 条，含"占位图必须存在"），跑
   `C:\Users\Anqi Liu\.workbuddy-ai\binaries\python\envs\cherrytale\Scripts\python.exe -m pytest -q`。
6. `tools/sync_android_assets.py`（改了 `services/` `tasks/` `web/` 后的硬规矩）。
7. 手工 PC 核对一遍。

---

## 附：侦察证据留痕

- 静态目录：196 PNG（`a001_01h` … `wi04_01h`），`manifest.json` 196 条。
- `tmp/index_save.txt`：37134 条；`img_hero_*_icon` 668 条；其中 `h` 尾主头像 196 条
  → 与目录**完全相等**。
- ItemData 头像道具 212 条（`[HCG頭像]…`），命中 184；缺的 28 条正是 `ct096~ct160`
  且热更清单里**无对应 bundle**（`ct096`/`ct111`/`ct150` 探测均返回空）。
- 换算实测：加 `h` 变体后，`RoleInfoData` 侧命中 390/587；`RoleMainData` 可拥有形态
  命中 1660/1999 = 83.0%。
