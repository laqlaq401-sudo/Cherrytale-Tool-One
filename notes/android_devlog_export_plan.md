# 安卓端「开发者日志」保存路径问题 —— 排查结论与改造计划

> 日期：2026-10-06
> 状态：**已实施**（代码改完并已同步进安卓工程）—— **APK 由用户手动打包**
> 现象：安卓端点「保存」后，在系统文件管理器里找不到导出的开发者日志

---

## 一、结论（TL;DR）

1. **保存本身是成功的**，不是写入失败 —— 文件确实写到了磁盘，只是写在一个**用户不可见**的位置。
2. 落点是 `PROJECT_ROOT/logs/`，而安卓端 `PROJECT_ROOT` = `filesDir` =
   **`/data/user/0/com.cherrytale.tool/files`**，即**应用内部私有存储**。
3. 用户的猜测方向正确，但比预想的位置**更深一层**：

   | 位置 | 路径 | 文件管理器可见性 |
   |---|---|---|
   | 应用内部私有存储（**实际落点**） | `/data/user/0/com.cherrytale.tool/files/logs/` | ❌ 无 root 完全不可见，USB/MTP 也看不到 |
   | 应用外部私有存储（Android/data） | `/storage/emulated/0/Android/data/com.cherrytale.tool/files/` | ⚠️ Android 11+ 多数 ROM 的文件管理器已屏蔽 |
   | 公共下载目录（建议落点） | `/storage/emulated/0/Download/米娅小助手/` | ✅ 任何文件管理器都能看到 |

4. 修复方向：**不改 Python 落盘逻辑**，在安卓侧新增一个极薄的导出桥，把 Python 已经写好的文件"复制"到公共「下载」目录。
   前端只在安卓环境走新分支，PC 端行为完全不变。

---

## 二、证据链（定位过程）

| # | 环节 | 位置 | 事实 |
|---|---|---|---|
| 1 | Kotlin 注入数据根目录 | `android/app/src/main/java/com/cherrytale/tool/ServerService.kt:89,96` | `dataDir = filesDir.absolutePath` → `CHERRYTALE_DATA_ROOT`。`filesDir` = `getFilesDir()` = 应用**内部**存储 `/data/user/0/<包名>/files` |
| 2 | Python 解析 PROJECT_ROOT | `config.py:38-42` | 非 frozen 分支：`PROJECT_ROOT = Path(os.getenv("CHERRYTALE_DATA_ROOT", ...))`。Chaquopy 不会设置 `sys.frozen`，所以必然走这里，值为上面的 dataDir |
| 3 | 保存实现 | `webapi/app.py:964-995` | `log_dir = (config.PROJECT_ROOT / "logs").resolve()`；文件名 `cherrytale-dev-log-<YYYY-MM-DD-HH-MM-SS>.txt` |
| 4 | 前端调用 | `web/assets/app.js:2842-2883` | `POST /api/logs/save`，成功后把服务端返回的 `filepath` 原样显示在「存储路径」里 |
| 5 | 「打开目录」在安卓被禁用 | `webapi/app.py:1069-1081`（`_can_open_folder`），`app.js:2868` | 安卓命中 `ANDROID_ROOT`/`getandroidapilevel` → 返回 `False` → 前端把「打开目录」按钮隐藏。**用户连"去哪儿找"的引导都没有** |
| 6 | 前后端均无安卓侧通道 | `grep window.MiyaAndroid / addJavascriptInterface` 零匹配；`MainActivity.kt` | 全工程没有任何 JS↔Kotlin 桥，也没有任何"导出到公共目录"的实现 |
| 7 | 清单无存储权限 | `android/app/src/main/AndroidManifest.xml:5-9` | 只有 INTERNET / 网络状态 / 前台服务 / 通知权限。`targetSdk 34, minSdk 26` |

### 1 分钟自证（不改代码，真机就能确认）

在安卓端再点一次「保存」，看卡片上「存储路径」那一行显示的完整路径。
若形如 `/data/user/0/com.cherrytale.tool/files/logs/cherrytale-dev-log-*.txt`，即与本文结论完全一致。

---

## 三、可选方案对比

| 方案 | 文件落点（用户视角） | 免权限 | 兼容性 | 改动面 | 评价 |
|---|---|---|---|---|---|
| **A. 原生 JS 桥 + MediaStore 写入「下载」** | `下载/米娅小助手/xxx.txt` | ✅ 是（API 29+） | Android 10+ 完美；API 26-28 无 `MediaStore.Downloads`，需降级分支 | Kotlin 新增约 60~80 行 + 前端小改 + 重打包 | ★ **推荐**，一键完成、无弹窗、位置固定好找 |
| **B. 原生 JS 桥 + SAF「另存为」** | 用户自选（Download / Documents / 任意目录） | ✅ 是（全版本） | API 26+ 全兼容，无版本分支 | 同上，但需处理 `onActivityResult` 回调 | 稳，但每次保存多一步系统弹窗 |
| C. 只把 `CHERRYTALE_DATA_ROOT` 指向 `getExternalFilesDir(null)` | `Android/data/<包名>/files/logs/` | ✅ 是 | ❌ Android 11+ 多数国产 ROM 屏蔽 Android/data | **1 行**（ServerService.kt） | 改动最小，但**大概率仍然找不到**，不建议单独用 |
| D. Python 侧用 Chaquopy 的 Java 桥直接调 MediaStore | 同 A | ✅ 是 | 需处理 Android API 版本 | 只改 `webapi/app.py`，前端不动 | 跨语言调用难调试、PC 侧完全测不到，不推荐 |
| E. 前端 Blob / data: URL 下载 | 依赖 WebView 下载处理 | — | ❌ | — | **不成立**：WebView 的 `DownloadListener` 不处理 `blob:` / `data:` URL，仍要绕回 JS 桥 |

---

## 四、推荐方案（A）详细设计

### 4.1 数据流

```
WebView 点「保存」
   └─ POST /api/logs/save                （Python 照旧，零改动）
        └─ 落盘 filesDir/logs/xxx.txt    （保留原始文件，作为完整备份）
             └─ 若 window.MiyaAndroid 存在：
                  MiyaAndroid.exportLog(srcPath, filename)
                     └─ Kotlin 读源文件 → MediaStore 写入 下载/米娅小助手/
                          └─ 返回"用户可见路径" → 前端展示 + Toast
```

**关键点：跨桥只传「源路径 + 文件名」两个短字符串，不传日志正文。**
日志缓冲可能有几 MB，跨 JS↔Java 边界传大字符串有截断/卡顿风险。

### 4.2 改动清单（文件级）

| # | 文件 | 改动 | 说明 |
|---|---|---|---|
| 1 | `android/app/src/main/java/com/cherrytale/tool/MainActivity.kt` | 新增 `LogExporter` 类 + `webView.addJavascriptInterface(..., "MiyaAndroid")`；方法：`exportLog(srcPath, fileName)`、`openDownloadsFolder()` | API 29+ 走 `MediaStore.Downloads` 插入（`RELATIVE_PATH="Download/米娅小助手"`、`IS_PENDING` 置 1→写字节→置 0）；API 26-28 走降级分支（见 §六 决策 2）。所有异常 catch 后返回错误串，不抛到 WebView |
| 2 | `android/app/src/main/AndroidManifest.xml` | 若统一走 MediaStore/SAF → **不需要任何新权限**；若要兼容 API 26-28 直写公共下载目录 → 需 `WRITE_EXTERNAL_STORAGE`(`maxSdkVersion=28`) + `requestLegacyExternalStorage="true"` | 权限越少越好，优先前者 |
| 3 | `web/assets/app.js::saveDevLog`（2842 行） | 保存成功后：若原生桥存在 → 调 `exportLog` 把文件复制到公共目录，展示**可见路径**（如 `下载/米娅小助手/xxx.txt`）；失败则回退现有展示 | `saveDevLog` 的现有 PC 路径一行不改 |
| 4 | `web/assets/app.js::openDevLogFolder`（2886 行） | 安卓下改调 `MiyaAndroid.openDownloadsFolder()`；PC 分支不变 | 让隐藏的「打开目录」按钮在安卓上重新可用 |
| 5 | `web/index.html`（必要时含另外 4 份 `_probe_*.html`） | 「存储路径」文案与按钮微调（可选） | 文案层面，非必需 |
| 6 | `tools/sync_android_assets.py` + `tools/release.py` | 脚本本身无需改，**但必须执行**：`web/` 是整目录同步，随后重打包 APK | 见 §七 |

**安卓/PC 的判定方式**：`typeof (window.MiyaAndroid && window.MiyaAndroid.exportLog) === "function"`。
PC 的 EXE / 浏览器里不存在这个对象 → 自动走原逻辑，双端行为不互相污染。

### 4.3 为什么不让 Python 直接写公共目录

`targetSdk 34` 下启用分区存储（scoped storage），Python 侧 `open("/storage/emulated/0/Download/...")`
会直接 `EACCES` —— 应用进程无权直写公共目录。合规路径只有两条：**MediaStore** 或 **SAF**，
两者都只能在 Android API 层面调用。若硬要从 Chaquopy 里调，需要 `jclass/autoclass` +
`ContentResolver` + `Uri` 解析，代码绕、易错，而且 **PC 测试完全覆盖不到**，会把安卓专属 bug 藏起来。

### 4.4 边界与细节

- **源文件保留**：`filesDir/logs/` 的原始文件建议保留（体积可忽略），导出只是复制，失败也不丢数据。
- **`IS_PENDING`**：MediaStore 插入后必须先把 `IS_PENDING=1`，字节写完后改 `0`，否则文件管理器里会看到半成品。
- **文件名**：现有 `cherrytale-dev-log-<时间戳>.txt` 自带时间戳，重名概率极低；`DISPLAY_NAME` 支持中文与连字符。
- **符号编码**：日志里有 `✔ ✘ ⚠`、中文与 emoji，统一按 UTF-8 字节写入，不经过任何有损转换。
- **失败必须可见**：任何一步失败都要把错误串回传前端并 Toast，禁止静默吞掉（本次问题的"静默"就是教训）。

---

## 五、验证计划

### PC 侧（回归）
Python 代码零改动 → 理论上不影响基线。改完仍跑精准四件套确认：
`tests/test_webapi_state_queue.py tests/test_webapi_logstream.py tests/test_webapi_app.py tests/test_tasks_yimo_box.py`，
并确认全量基线仍是 **1720 passed / 10 skipped**。

### 安卓真机（主验证）
1. 装新包 → 跑一次真实任务（保证有日志）。
2. 进「运行日志」→ 开发者日志 → 点「保存」。
3. 打开系统「文件管理 / 我的文件」→ 进「下载」→ 找「米娅小助手」目录 → 确认 `.txt` 在。
4. 打开文件确认内容完整（中文不乱码、`✔ ✘ ⚠` 正常、行数与 Toast 报的一致）。
5. 点「打开目录」确认能跳到下载目录。
6. 点「复制路径」确认复制的是**用户可见路径**，不再是 `/data/user/0/...`。

### 边界用例
空缓冲（没有日志时保存）、超大日志（几 MB）、连续保存多次（时间戳不同名不冲突）、
保存后立即杀进程再查看（确认已落盘）。

---

## 六、待你拍板的 3 个决策点

| # | 决策 | 选项 | 倾向 |
|---|---|---|---|
| 1 | 导出交互 | **A**：一键直存「下载/米娅小助手/」，无弹窗<br>**B**：弹系统「保存到…」让用户自选位置 | A（体验最顺）；若担心个别 ROM 的 MediaStore 行为，B 最稳 |
| 2 | Android 8/9（minSdk 26）策略 | ① 加 `WRITE_EXTERNAL_STORAGE(maxSdk 28)` 直写下载目录<br>② 低版本统一走 SAF<br>③ 低版本不导出，只保留原路径提示 | ③ 最省事（安卓 8/9 存量已很低）；② 功能完整度最高 |
| 3 | 打包时机 | 本次是否与之前挂起的 **pydantic 垫片修复**一起重打包 APK | 一起打包，省一次同步+构建 |

> **已拍板（2026-10-06）**：① 导出交互取 **A**（一键直存「下载/米娅小助手/」，无弹窗）；
> ② Android 8/9（API 26-28）取 **③ 不做导出**，只在卡片上保留应用内路径提示；
> ③ **APK 由用户手动打包**（本轮不代劳）。

---

## 七、影响面与风险

- **必须重打包 APK**：Kotlin 代码改动 + `web/` 需 `sync_android_assets.py` 同步（这是项目既有硬约定）。
- **PC 端零影响**：Python 与 PC 分支都不动，PC 基线测试与 PC 体验不变。
- **方案 C 的陷阱**：只改 `ServerService.kt` 一行成本最低，但落点仍是 `Android/data/<包名>/`，
  vivo / OPPO / 小米等 ROM 的文件管理器普遍已屏蔽该目录 —— **不能单独作为修复方案**。
- **JS 桥安全性**：页面仅从 `http://127.0.0.1:8000` 加载，且 `minSdk 26`（远超 API 17 的门槛），
  暴露面只有两个只读/导出方法；仍建议在 Kotlin 侧校验 `srcPath` 必须位于应用自己的 `filesDir` 之下，防止越权读文件。

---

## 八、实施记录（2026-10-06 已完成）

### 8.1 改动文件

| 文件 | 改动 |
|---|---|
| `android/app/src/main/java/com/cherrytale/tool/MainActivity.kt` | 新增 `inner class LogExporter`（`exportLog` / `openDownloadsFolder` / `sanitizeFileName` / `errorJson`），并在 `onCreate` 里 `addJavascriptInterface(LogExporter(), "MiyaAndroid")`；companion 新增 `EXPORT_SUBDIR = "米娅小助手"`。新增 import：`DownloadManager` / `ContentValues` / `Build` / `Environment` / `MediaStore` / `JavascriptInterface` / `File` / `JSONObject` |
| `web/assets/app.js` | 新增 `androidBridge()` 与 `exportLogOnAndroid()`；`saveDevLog()` 落盘后调用导出桥，并把展示路径/标签/状态文案切到可见路径；`openDevLogFolder()` 在有桥时走 `openDownloadsFolder()`；`复制路径` toast 文案去掉"完整"二字 |
| `web/index.html` | `devlog-saved-status-text` / `devlog-saved-label` 补 id 供 JS 改写；`app.js` 版本串 `?v=20261006_1 → 20261006_2`（破 WebView 缓存）；复制按钮 title 改为「复制日志路径」 |
| `android/app/src/main/python/**` | 由 `tools/sync_android_assets.py` 整目录同步（已执行 2 次，`web/` 与桌面逐字节一致，diff 已验） |
| `AndroidManifest.xml` | **未改动** —— 不需要任何存储权限（`< Q` 直接不导出，`>= Q` 走 MediaStore 免权限写入） |
| `webapi/app.py` | **未改动** —— Python 落盘逻辑保持原样，继续在 `filesDir/logs/` 保留一份原始文件 |

### 8.2 关键实现细节

- **版本门**：`Build.VERSION.SDK_INT < Build.VERSION_CODES.Q` → 直接返回
  `当前系统（Android x.y）不支持一键导出，日志已保存在应用内`，前端提示但不报错。
- **`IS_PENDING` 1→0**：插入 Downloads 时先标记 pending，字节写完后 `update` 清零；
  异常时 `delete` 掉半成品 URI，不留垃圾。
- **路径校验**：`File(srcPath).canonicalFile` 必须位于 `filesDir.canonicalPath + "/"` 之内，
  否则拒绝（防越权读任意文件）。
- **文件名清洗**：`[\\/:*?"<>|]` → `_`，空白名兜底 `cherrytale-dev-log.txt`。
- **跨线程**：`@JavascriptInterface` 方法跑在 WebView 的 JavaBridge 线程，
  `startActivity` 用 `runOnUiThread` 包一层；`contentResolver` / `filesDir` 在后台线程访问安全。
- **不传正文**：只有 `srcPath` + `fileName` 两个短字符串过桥。

### 8.3 已验证

- `node --check web/assets/app.js` 通过。
- `sync_android_assets.py` 执行成功；`diff -q` 确认 `web/assets/app.js`、`web/index.html`
  在桌面与安卓两侧逐字节一致。
- `pytest tests/test_webapi_app.py tests/test_webapi_logstream.py` → **64 passed**（Python 未改动，基线未破）。

### 8.4 待用户完成：手动打包

```bash
cd android && gradlew assembleDebug
# 或走封装脚本（会先跑 sync + 自检）：
python tools/release.py android
```

> 提醒（历史踩坑）：
> - **不要删** `android/app/build/python/env/`。
> - 若 `gradlew` 报 `Cannot read pyvenv.cfg`（rc=107），那是沙箱/权限隔离的假象 ——
>   在**沙箱外**重跑即可，env 本身是好的。
> - 改完 Kotlin 建议先 `gradlew --stop` 再构建，避免活着的 daemon 带旧环境。

### 8.5 打包后真机验收

1. 进「运行日志」→ 开发者日志 → 点「保存」。
2. 卡片应显示：状态「日志已保存并导出到「下载」目录」、标签「已导出到：」、
   路径 `下载/米娅小助手/cherrytale-dev-log-<时间戳>.txt`。
3. 打开系统「文件管理 / 我的文件」→ 下载 → 米娅小助手 → 确认 `.txt` 存在且内容完整（中文、`✔ ✘ ⚠` 不乱码）。
4. 点「打开目录」应跳系统「下载」界面；点「复制路径」复制到的是上面那条可见路径。
5. 在 Android 8/9 机器上（若有）应看到「不支持一键导出」的提示而不是崩溃。
