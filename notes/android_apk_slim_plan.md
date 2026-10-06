# 安卓包体瘦身方案（体检 + 计划）

> 体检对象：`dist/android/CherrytaleTool-0.9.8-beta.apk`（原始磁盘 **90.69 MB**，versionCode 9）
> 体检方式：直接解析 APK 的 zip 中央目录 + 逐条本地头，统计每个条目的真实占用。
> 状态：**A1 已落地**（新增 `tools/apk_slim.py`，已接进 `tools/release.py`）。
> 结果：**90.69 MB → 66.30 MB**。A2 之后的步骤仍待执行。

---

## 一、真实体积构成

APK 的 90.69 MB 分成三块：

| 区块 | 占用 | 占比 |
|---|---|---|
| 有效条目数据 | 66.23 MB | 73% |
| **死空隙（垃圾数据）** | **23.99 MB** | **26.5%** |
| 中央目录 + 头 + 尾部 | 0.47 MB | 0.5% |

### 有效条目明细（66.23 MB）

| 条目 | 占用 | 说明 |
|---|---|---|
| `assets/chaquopy/app.imy` | 23.99 MB | 应用自己的 Python 代码 + web 前端，**一个包** |
| ├ `web/assets/fonts/OPPO-Sans-4.0.ttf` | 15.60 MB | **单一字体文件**，占全包 17% |
| ├ `web/assets/avatars/*.png`（197 张） | ≈4.9 MB | 角色头像 |
| ├ `web/assets/icons/**/*.png`（47 张） | ≈2.0 MB | 界面图标 |
| └ Python 代码（`.pyc`） | ≈1.4 MB | models/tasks/services/webapi/… |
| `assets/chaquopy/stdlib-*` | 7.44 MB | Python 3.11 标准库（common + 两个 ABI 各一份） |
| `lib/arm64-v8a/*.so` | 10.55 MB | 真机 ABI（libpython 5.13 + libcrypto 3.54 + …） |
| `lib/x86_64/*.so` | 10.56 MB | **模拟器 ABI**，真机用不到 |
| `assets/chaquopy/requirements-*` | 3.30 MB | requests / cryptography / truststore |
| `assets/chaquopy/bootstrap-native/*` | 3.12 MB | Chaquopy 引导原生库（两个 ABI） |
| `classes*.dex` | 3.97 MB | Java/Kotlin（debug 构建，未开 R8） |
| `assets/material/*` | 0.93 MB | 游戏配置表（压缩后，原始 9.36 MB） |
| `resources.arsc` + `res/` | 1.59 MB | Android 资源 |

### 关键结论

1. **字体 = 15.6 MB，是最大的单点浪费**，比整个 Python 标准库还大 2 倍。
2. **x86_64 是纯模拟器 ABI，真机一律 arm64-v8a** —— 白送 10.56 MB。
3. **23.99 MB 是纯垃圾**（见下节）。
4. `assets/material` 看着 9.36 MB，压完只剩 0.93 MB，**不值得动**。

---

## 二、★ 最大发现：APK 里有 23.99 MB 死数据（已解决）

在 `assets/chaquopy/build.json` 之后、`assets/chaquopy/bootstrap-native/…` 之前，
存在一段 **23.99 MB 的连续空洞**：没有任何 zip 条目引用它，解压/安装都用不到，
但它在磁盘上实实在在占着 24 MB。

**空洞内容已确认为 99.98% 的纯零字节**（25155338 字节里只有 3830 个非零），
不是旧数据的残留，而是写入时被跳过的一段空白。

对比历史版本，确认这是**构建状态相关、时有时无**：

| 版本 | 磁盘大小 | 条目数据 | 死空隙 |
|---|---|---|---|
| 0.9.4 | 60.14 MB | 59.68 MB | **0.00 MB** |
| 0.9.5 | 77.25 MB | 60.20 MB | 16.55 MB |
| 0.9.6 | 60.92 MB | 60.46 MB | **0.00 MB** |
| 0.9.8 | 90.69 MB | 66.23 MB | **23.99 MB** |

### ✅ 解决方案：事后剥离（`tools/apk_slim.py`）

**不要用 `gradlew clean`。** 实测 clean 会把本机的安卓构建打瘸（详见 §七），
而且它只是"可能"消除空洞。改用**打包后剥离**：

```bash
py -3.14 tools/apk_slim.py dist/android/CherrytaleTool-<版本>.apk          # 就地剥离（自动备份 .bloated）
py -3.14 tools/apk_slim.py dist/android/CherrytaleTool-<版本>.apk --check  # 只体检
```

原理：重写 zip 容器（死区自然消失）→ `zipalign -p -f 4` → `apksigner` 用 debug
证书重签。已接进 `tools/release.py` 的 `android` 流程，**死空隙 ≥1 MB 时自动剥离**。

实测结果（0.9.8）：**90.69 MB → 66.30 MB，剥离 24.37 MB**。

安全性已逐项验证：
- 条目零丢失、零新增，共有条目**逐字节完全相同**；
- 每个条目的 `compress_type` 原样保留（`resources.arsc` 与
  `assets/chaquopy/app.imy` 仍是 STORED，共 297 个 STORED 条目不变）；
- `zipalign -c -p -v 4` 校验通过，`apksigner verify` 通过（v2+v3，debug 证书与原包同一把 key）。

---

## 三、分档优化方案

### A 档：零风险，立即可做

| # | 手段 | 预计节省 | 代价 |
|---|---|---|---|
| A1 | ✅ **已落地**：打包后剥离死空隙（`tools/apk_slim.py`，已接进 release.py） | **−24.4 MB** | 无。几秒钟，不依赖构建环境 |
| A2 | ❌ **已否决**：`abiFilters` 去掉 `x86_64` | ~~−10.6 MB~~ | **不做** —— 需要在模拟器上做 bug 测试，x86_64 必须保留 |
| A3 | 从 APK 排除 `web/assets/input.css`（Tailwind 源码，运行时只读 `styles.css`） | −0.08 MB | 无 |

A 档小计：**−24.5 MB → 66.30 MB**

### 关于 A2：保留 x86_64 的真实代价是 14.6 MB

不只是 `lib/x86_64/` 那 10.56 MB，Chaquopy 还会为每个 ABI 各出一份运行时包：

| 条目 | 占用 |
|---|---|
| `lib/x86_64/*.so` | 10.56 MB |
| `assets/chaquopy/bootstrap-native/x86_64/` | 1.54 MB |
| `assets/chaquopy/stdlib-x86_64.imy` | 1.44 MB |
| `assets/chaquopy/requirements-x86_64.imy` | 1.07 MB |
| **合计** | **14.61 MB** |

**如果以后想同时保留"模拟器可测"和"发布包更小"**，可以走 `productFlavors`：
`dev` flavor 保留两个 ABI（自测用），`release` flavor 只留 arm64-v8a（对外发布）。
两条产物、两个 APK，代价是构建配置复杂一点。**当前不做，仅作备选记录。**

### B 档：需要改资源，收益大

| # | 手段 | 预计节省 | 代价 / 风险 |
|---|---|---|---|
| B1 | **字体子集化**：扫描 `Cherrytale Asset/TextAsset` 全部文本表 + 前端静态字符串，提取实际用到的字形，产出子集 TTF/WOFF2 | **−12 ~ −14 MB** | 中。风险点是**运行时动态文本**（角色名、道具名、玩家昵称）可能含子集外的字，会掉回系统字体。可加常用汉字集（GB2312 一二级 6763 字）兜底，产物约 3–4 MB |
| B2 | **字体转 WOFF2**（不做子集，仅换格式） | −7 ~ −9 MB | 低。WebView 需 Chrome 36+（Android 5.0+，本项目 minSdk 26，完全没问题）。字形 100% 保留 |
| B3 | **直接删字体，走系统字体栈** | **−15.6 MB** | 低技术风险，但**视觉会变**。`styles.css` 的 `font-family` 已经写好完整兜底链：`OPPO Sans, OPPOSans, system-ui, …, PingFang SC, HarmonyOS Sans SC, MiSans, Noto Sans CJK SC, Microsoft YaHei` —— 安卓上会落到 HarmonyOS Sans / MiSans，不会出现方块字 |
| B4 | 头像 PNG → WebP（lossy q80） | −3.5 MB | 低。197 张，只需一次性转换脚本 + 改 `avatar_service.py` 的扩展名回退顺序 |
| B5 | 图标 PNG → WebP + 清理未引用图标 | −1.5 MB | 低。实测 47 张里只有 35 张能被字面量路径搜到，其余需确认是否动态拼接 |

B 档小计：**−15 ~ −25 MB**（取决于 B1/B3 取舍）

### C 档：需要改构建配置，收益中等

| # | 手段 | 预计节省 | 代价 / 风险 |
|---|---|---|---|
| C1 | 出 **release 变体** + R8（`minifyEnabled true`）+ `shrinkResources true` | −2 ~ −3 MB | 中。必须保留 `proguard-rules.pro` 里的 `@JavascriptInterface` keep 规则，否则 WebView 桥断掉。另外要配签名（目前 release.py 复制的是 debug 签名包） |
| C2 | ~~只留一份 stdlib~~ | — | **随 A2 一起否决**：要保留 x86_64，就得留 x86_64 那份 stdlib / requirements / bootstrap-native |
| C3 | `assets/material` 改首启下载 | −0.9 MB | 收益太低，**不建议** |

C 档小计：**−2 ~ −3 MB**

---

## 四、推荐执行顺序

```
第 1 步 ✅ 已完成（−24.4 MB）
  A1 打包后剥离死空隙
  结果：90.69 MB → 66.30 MB

第 2 步（−0.08 MB，顺手做）
  A3 排除 input.css
  预期：66.22 MB

第 3 步（−8 ~ −15 MB，需先拍板字体的视觉取舍）
  先做 B2（WOFF2，字形 100% 保留）验证效果；
  若接受系统字体，直接上 B3 一步到位 −15.6 MB；
  若必须保住品牌字体观感，走 B1 子集化。
  预期：约 58 MB（WOFF2）/ 约 50.7 MB（删字体）

第 4 步（可选，−4 ~ −6 MB）
  B4 + B5 图片转 WebP，C1 开 R8 + 签名
  预期：约 45 ~ 53 MB
```

**现实目标：50 MB 上下**（A2 已否决，x86_64 的 14.6 MB 保留给模拟器测试）。

**字体（15.6 MB）是剩下唯一的大头** —— 它一项就超过 B4+B5+C1 的总和，
所以第 3 步的取舍基本决定了最终能压到多少。

---

## 五、验证方法

体检（不改动文件）：

```bash
py -3.14 tools/apk_slim.py <apk> --check
```

判定标准：**死空隙必须 < 1 MB**（干净构建实测 0.14~0.38 MB，属 980 个条目做
4 字节/页对齐的正常累计开销）。若报"需要剥离"，直接跑不带 `--check` 的命令即可。

手动版（不依赖工具）见 §二 的说明；核心是逐条读**本地头**算数据区间，
注意必须用本地头里的 `name_len`/`extra_len`——中央目录里的 extra 长度
与实际写入的本地头可以不同，直接用会算错偏移。

---

## 六、需要注意的坑

- **x86_64 ABI 必须保留**（2026-10-05 定案）：需要在模拟器上做 bug 测试，
  所以 `abiFilters` 里的 `x86_64` 不能删。它连带 Chaquopy 的
  bootstrap-native / stdlib / requirements 各一份 x86_64 副本，共 **14.6 MB** ——
  这部分体积是"测试能力"的必要成本，不再计入可回收项。
  若将来想两头兼顾，见 §三 A2 下方的 `productFlavors` 备选方案。
- **C1 开 R8 前必须先加签名配置**，否则 `assembleRelease` 出的是未签名包。
  同时 `MainActivity` 的 `@JavascriptInterface` 方法必须 keep，否则前端 JS 桥全断。
- **B1/B3 改字体前，务必先跑一遍真机看关键页面**（角色与资源面板、任务列表、
  日志区），确认中文、数字、特殊符号（✔ ✘ ⚠ 之类）都不掉字形。
  日志和任务结果里用了不少符号字符，子集化时容易漏。
- **B4 改头像扩展名**会同时影响 `services/avatar_service.py` 的查找链和
  前端 `app.js` 的兜底图，改完记得跑 `tools/sync_android_assets.py`。
- 任何改动 `webapi/` 或 `web/` 之后，**必须**跑 `py -3.14 -u tools/sync_android_assets.py`，
  否则安卓侧还是旧副本。
- **`tools/apk_slim.py` 重签用的是 `~/.android/debug.keystore`**（alias
  `androiddebugkey` / 口令 `android`）。与 gradle 出包用的是同一把 key，
  所以设备上可以原地覆盖升级。若哪天改成 release 签名，这个工具也要同步改。

---

## 七、⚠️ 为什么不能用 `gradlew clean`（2026-10-05 实测踩坑）

`release.py android` 一度改成 `gradlew clean assembleDebug`，结果**直接把安卓构建打瘸**：

```
> Task :app:generateDebugPythonRequirements FAILED
  File "...\build\python\env\debug\Lib\site-packages\chaquopy\pip_install.py", line 21
    from pip._vendor.distlib.database import DistributionPath
ModuleNotFoundError: No module named 'pip._vendor.distlib.database'
```

**因果链**（已逐环实证）：

1. `clean` 会连 Chaquopy 预建的 Python 环境 `android/app/build/python/env/` 一起删掉
   （该目录自 2026-10-02 建好后一直没重建过，所以问题一直没暴露）；
2. 重建时 `py -3.11 -m venv` 能建出目录，但 **venv 的 `Scripts/python.exe` 读不到
   自己旁边的 `pyvenv.cfg`**，报 `Cannot read '...\pyvenv.cfg'`；
3. 读不到就**退化成 base 解释器**，于是 `pip` 变成 base 的 **pip 24.0**；
4. pip 24.0 里**没有** `_vendor/distlib`（只有 `_internal` 还在），
   而 Chaquopy 的 `pip_install.py` 第 21 行硬依赖它 → 直接崩。

**这个 venv 故障的边界已测清楚**（全部在同一台机器、同一个 `py -3.11`）：

| 位置 | 结果 |
|---|---|
| `C:\Users\Anqi Liu\Desktop\vtest`（工作区外，含空格） | ✅ 正常 |
| `C:\Users\Anqi Liu\Public\vtest`（工作区外，无空格） | ✅ 正常 |
| `…\Desktop\cherrytaletool needs\vt`（工作区外，含空格） | ✅ 正常 |
| `…\Desktop\Cherrytale tool One\` 下的**任意** venv | ❌ `Cannot read pyvenv.cfg` |

排查过并**排除**的原因：路径含空格、路径过长、8.3 短名缺失（`GetShortPathNameW`
正常返回 `CHERRY~3`）、`PYTHONPATH` 污染（`env -i` 清空后仍失败）、
关掉沙箱（`dangerouslyDisableSandbox` 后仍失败）、用 8.3 短名访问（仍失败）。
把工作区外**可用**的 venv 整个拷进工作区，**立刻变坏** —— 所以是位置效应。

**结论**：这是本机环境层面的缺陷，不是代码问题；但代价是
**一旦 `clean`，安卓构建就必须在 agent 之外重建一次环境**。恢复办法：

```bat
:: 在普通 Windows 终端（或 Android Studio → Build → Make Project）里执行
cd android
gradlew.bat assembleDebug
```

**所以 `release.py` 里刻意没有 `clean`，改由 `apk_slim` 事后兜底。**
死空隙是"可能产生"的，事后剥离是"必然消除"的 —— 后者更可靠，而且快得多。
