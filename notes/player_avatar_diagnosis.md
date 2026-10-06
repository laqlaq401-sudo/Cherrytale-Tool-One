# 「玩家自己的头像」为什么拿不到图 —— 排查报告 + 实施记录（2026-10-06）

> 状态：**已排查 + 已实施**（采用方案 A + 前端统一占位图；**方案 B 被用户否决**）。
> 结论全部由真实抓包 + 真实配置表得出，可复现。

## 一、一句话结论

**代码只读 `PlayerClass.icon`，而这个玩家 `icon = 0`（从没设过自定义头像）；
真实头像信息在 `roleRepresentativeId` 里。更糟的是，就算读它，
默认头像的图 `a000_01.png` 压根没进运行时静态目录。**

英雄头像（名册角色）能成功，是因为它走的是**另一条链**（`RoleInfoData.icon1_AssetId`
→ 角色），完全不碰 `PlayerClass.icon`。两条链互不相干，所以一条通一条不通。

## 二、证据链（逐步实证）

### ① 真实 1004 长什么样

全仓扫描 21 个 `.saz` + 3 个 `.har`，**只有 1 条 1004**：
`captures/登录后自动签到（每日+活动）.saz` → `raw/04_s.txt`

```
PlayerClass.playerID              = 8406715
PlayerClass.name                  = 年粥
PlayerClass.icon                  = 0          ← ★ 就是这里
PlayerClass.roleRepresentativeId  = 200913057  ← ★ 真实头像在这里
IconClass.roleMainID              = 200913057
IconClass.accNickName             = 年粥
```

复现命令：`python tmp/diag_find_1004.py`（只读，不联网）

### ② `icon = 0` 之后代码直接放弃

`services/avatar_service.py::resolve_code()` **只读 `icon` 一个键**：

```python
icon = int(avatar_ids.get("icon") or 0)
return avatar_code_from_icon(icon)   # icon=0 → avatar_code_from_icon 直接 return ""
```

而 `tasks/login.py` 明明采了 6 个字段，另外 5 个（`frame` / `representative` /
`role_main` / `skin` / `bg`）**采集了却从未被使用**。⇒ `icon=0` 时必然返回空。

### ③ `200913057` 是什么：**默认头像**

```
ItemData[200913057].name             = [頭像]預設
ItemData[200913057].itemDescription  = 預設頭像。
ItemData[200913057].iconAssetID      = a000_01_Icon
```

`a000_01.png` 是一张**灰色人形剪影**（100×100，2.5 KB）—— 游戏给"没设头像的人"的兜底图。
它**只存在于** `captures/avatar_cold_storage/`，**不在** `web/assets/avatars/`。

### ④ 头像道具其实有三族，只提取了一族

| 道具区段 | 名称 | `iconAssetID` 形态 | 条数 | 命中 runtime |
|---|---|---|---|---|
| `200911xxx` | **`[HCG頭像]`** 大图 | `{code}h_Icon` | 212 | **184（87%）** |
| `200913xxx` | **`[紀念頭像]`** 小图 | `{code}h_s_Icon` | 222 | **0（0%）** |
| `200913057` | **`[頭像]預設`** 默认 | `a000_01_Icon`（**无 h 无 _s**） | 1 | **0** |

`tools/extract_avatar_icons.py` 只提取热更清单里的 `img_hero_{code}h_icon`，
所以只有 **`{code}h` 那一族**进了 runtime 目录（197 张）；
`_s` 小图族与 `a000_01` 默认图都只落在冷存目录。

### ⑤ 顺带：落盘状态也是旧的

`.session.json` 与 `.platform_accounts.json` 都是 **10-03 18:45** 的文件，
`version: 2`（当前 `SESSION_JSON_VERSION = 4`）—— **既没有 `roles` 也没有
`avatar_data`**。也就是说本机现存数据是"头像功能上线之前"写下的，
即使代码没问题也读不出图，需要重新登录一次才会写入。

## 三、三层原因（按重要性）

1. **主因（代码）**：`resolve_code()` 只认 `icon`，`icon=0` 时不做任何兜底。
   而 `roleRepresentativeId` / `IconClass.roleMainID` 里**有**可用的头像编号。
2. **次因（素材）**：默认头像 `a000_01` 与整个 `_s` 小图族**没进 runtime 静态目录**
   —— 即使修好主因，默认头像依然显示不出来。
3. **状态因（数据）**：现存会话/账号文件是旧版本，没有 `avatar_data` 字段。

## 四、修复方向（待拍板，本次未实施）

| 方案 | 做什么 | 代价 |
|---|---|---|
| **A. 兜底 `representative`** ✅ 已采用 | `resolve_code()` 在 `icon<=0` 时依次试 `role_main` → `representative` | 小；但默认头像仍无图（见 B） |
| ~~B. 补默认头像进 runtime~~ ❌ **用户否决** | ~~把 `a000_01.png` 复制进 runtime~~ | —— 用户明确要求：**没选头像的人看我们自制的占位图**，不引入游戏官方默认剪影 |
| C. 补 `_s` 小图族 | 提取 `{code}h_s` 进 runtime | 中等（222 张 100×100 小图，界面用的是大图，暂无消费方）—— **暂不做** |
| **D. 前端统一回退占位图** ✅ 已采用 | 玩家头像拿不到真图时，两处都回退 `000h.png` | 零成本 |

> **最终方案（用户拍板）**：A + D。**"没选头像的人"与"读取头像不成功的人"
> 一律看我们自制的占位图**（`web/assets/avatars/000h.png`，即用户提供的
> `003.png`）—— 刻意**不引入**游戏官方默认剪影 `a000_01`。

## 五、实施记录（2026-10-06）

| 文件 | 改动 |
|---|---|
| `services/avatar_service.py` | 新增 `_PLAYER_ICON_KEYS = ("icon", "role_main", "representative")`、`_player_icon_ids()`；`resolve_code()` 改为**依次尝试三个键**（原来只读 `icon`）；`_candidate_paths()` 签名由单 `icon` 改为 `icons` 序列，缓存路径对每个候选编号都试一遍 |
| `web/assets/common.js` | 新增共享常量 `AVATAR_PLACEHOLDER = "assets/avatars/000h.png"` 并导出（**前后端同一张图**） |
| `web/assets/app.js` | 任务页玩家名片头像：`|| "assets/icons/stats/player.png"` → `|| AVATAR_PLACEHOLDER`；角色头像改用共享常量（删掉本地重复的 `ROLE_AVATAR_PLACEHOLDER`） |
| `web/assets/login.js` | 登录页区服卡：**总是**渲染 `<img>`，无图时用 `AVATAR_PLACEHOLDER`（原来回退"昵称首字"）；平台账号卡仍是 🔑 |

**测试**（`tests/test_avatar_service.py`）新增 9 条：
- `TestItemDataChain`：兜底链生效 / `icon` 优先 / **默认头像必须落到占位图分支**（真跑 `resolve_avatar_png` 断言 `None`）/ RoleMainID 不是 ItemData 键 / 坏值跳过不抛异常
- `TestPlaceholderContract`（新类）：`common.js` 常量与后端 `PLACEHOLDER_ROLE_AVATAR` 必须指向同一文件 / 两页都引用共享常量 / 登录页不再有"昵称首字"兜底残留

**验证**：全量 `pytest` **1797 passed, 10 skipped**（较改动前 +7）；
Tailwind 重建与 HEAD **零差异**；`sync_android_assets.py` 已跑，安卓侧代码与 `000h.png` 均到位。

**踩到的一个小坑**：在 `app.js` 的注释里写了字面量 ``object-contain``，Tailwind 的
content 扫描把注释也当候选串，凭空生成了一条无人使用的 `.object-contain` 工具类。
改成中文描述后重建，CSS 与 HEAD 完全一致。**教训：注释里不要出现 Tailwind 类名的字面量。**

## 六、复现脚本

- `tmp/diag_find_1004.py` —— 扫全部 SAZ/HAR 找 1004，解出 `icon` 并跑完整链验证
- `tmp/diag_player_avatar.py` —— 早期版本（只扫 HAR，未命中 1004）

两个都是**只读**脚本，不发任何网络请求。
