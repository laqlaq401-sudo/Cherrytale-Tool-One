# 米娅小助手 · Android 端打包指南

本项目通过 **Android Studio + Chaquopy** 把 Python 后端与 Web 前端完整内嵌为独立的 Android 应用，手机端可脱离电脑独立运行。

> **当前版本：`1.0.0`**（`versionCode 13` / `versionName "1.0.0"`），与 PC 端四处版本号由 `tools/release.py` 统一维护。
> 归档产物：`dist/android/CherrytaleTool-1.0.0.apk`。
>
> 架构说明：**安卓端与桌面端现在共用同一份 HTTP 实现**（`webapi/app.py`），
> `webapi/android_app.py` 已退化为纯转发层（见下文「架构合流」）。

---

## 一、目录结构

```
android/
├── local.properties          # SDK 路径，由 Android Studio 生成，不入 git
├── settings.gradle           # 仓库配置（含 chaquo.com/maven）
├── build.gradle              # 顶层插件：AGP 8.4.2 / Kotlin 1.9.24 / Chaquopy 16.0.0
├── gradle.properties         # JVM 内存 + ★ org.gradle.java.home=JDK 21
├── gradlew / gradlew.bat     # Gradle 包装器（8.7）
└── app/
    ├── build.gradle          # 应用配置：minSdk 26 / targetSdk 34 / Chaquopy Python 3.11
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/cherrytale/tool/
        │   ├── MainActivity.kt    # WebView 渲染 + 启动服务 + 日志导出到「下载」
        │   └── ServerService.kt   # 前台服务：承载 Python HTTP 服务器
        ├── python/                # ★ 生成目录（.gitignore 忽略）—— 由同步脚本生成
        │   ├── pydantic/          #   纯 Python 垫片，来自 tools/android_shims/pydantic/
        │   └── ...                #   其余来自桌面工程的 client/models/services/...
        ├── assets/material/       # ★ 生成目录：运行时要用的游戏配置表（按需扫描打包）
        └── res/                   # 布局与暗色主题样式
```

---

## 二、一键同步桌面代码到安卓工程

桌面工程是**唯一源码**；安卓 `python/` 目录是**生成物**，改完桌面代码后同步一次即可：

```powershell
.\.venv\Scripts\python.exe tools/sync_android_assets.py
```

同步脚本会：

- **整目录替换** `client / crypto / models / services / tasks / webapi / web`；
- **单文件复制** `config.py` / `web_server.py` / `main.py` / `config_local.py`；
- 从 `tools/android_shims/pydantic/` 安装 pydantic 垫片；
- 扫描代码引用，把需要的游戏配置表复制进 `assets/material/`（见「技术要点 6」）。

> ⚠️ **改完 `web/` 或 `webapi/` 必须重跑同步 + 重新打包**，否则 APK 里还是旧代码。

---

## 三、打包

### 命令行（推荐，一条命令）

```powershell
cd android
.\gradlew.bat assembleDebug
```

产物：`android\app\build\outputs\apk\debug\app-debug.apk`

### 或走统一发布工具

```powershell
py -3.11 tools/release.py android          # 同步资产 + gradlew 构建 + 归档到 dist/android/
py -3.11 tools/release.py android --skip-sync   # 已同步过，跳过同步步骤
```

### 安装到手机

```powershell
# USB 调试
adb install -r android\app\build\outputs\apk\debug\app-debug.apk

# 或直接把 .apk 发到手机点击安装
```

### 使用 Android Studio

1. **打开工程**：File → Open → 选 `android` 文件夹。
2. **Gradle 同步**：自动读取 `local.properties`；`gradle.properties` 里的
   `org.gradle.java.home` 会强制使用 JDK 21（见技术要点 3）。
3. **打包**：Build → Build Bundle(s) / APK(s) → Build APK(s)。

---

## 四、技术要点

### 1. fastapi / uvicorn / pydantic 在安卓上装不上（硬阻断）

pydantic 2.x 强依赖 Rust 写的 `pydantic-core`，而 Chaquopy 的预编译包仓库
（`https://chaquo.com/pypi-13.1/`）里**没有** pydantic-core 的安卓构建。解决：

- **`app/src/main/python/pydantic/`**：纯 Python 垫片，只实现本项目用到的
  极窄 API 面（`BaseModel` / `Field` / `ConfigDict` / `ValidationError` +
  `model_validate / model_dump / model_copy / model_fields`）。已用桌面
  全部模型单元测试验证（`PYTHONPATH=android/app/src/main/python pytest tests/test_models_*.py`）。
- **HTTP 层改用标准库** `ThreadingHTTPServer`，不使用 fastapi / uvicorn。

> ⚠️ 垫片是**为安卓专门写的**，桌面端跑的是真 pydantic —— 所以桌面测试**测不到**垫片的
> 行为差异。回归靠 `tests/test_android_pydantic_shim.py`。

### 2. 架构合流：`android_app.py` 已并入 `webapi/app.py`

安卓端曾有一份独立的 `webapi/android_app.py`（自己实现一张路由表）。现已**完全合流**：

- 全部路由与业务逻辑都在 **`webapi/app.py`**（桌面与安卓**同一份代码**）；
- `webapi/android_app.py` 退化为**纯转发层**，只做三件事：
  1. 兼容旧 APK / 旧构建脚本 / 旧测试对 `webapi.android_app` 的导入；
  2. 让 `tools/release.py` 的版本号统一对齐仍能工作（保留 `APP_VERSION`）；
  3. 用 `__getattr__` 透明转发模块级属性访问。

**好处**：不再存在"桌面修了、安卓没修"的分叉。改一处，两端同时生效。

### 3. `cryptography` 钉死 42.0.8 的原因

这是 Chaquopy 官方索引上同时有 **cp311 + android（arm64/x86_64）** 预编译
wheel 的版本（桌面版用 50.0.1，但那个版本没有安卓构建）。

### 4. pip 索引改用清华镜像（2026-10-06 网络修复）

Chaquopy 默认主索引是 `pypi.org`，在本机访问会**直接挂死** —— 实测表现为
pip 进程 CPU=0s、内存 4.4MB 卡住不动，**不是"慢"而是阻塞在网络等待**。

```gradle
options "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple",
        "--extra-index-url", "https://chaquo.com/pypi-13.1"
```

> `cryptography` 的安卓 arm64 wheel **只有 chaquo 官方源提供**，所以必须保留为
> `--extra-index-url`，否则会退回到源码构建而失败。

### 5. Gradle 必须用 JDK 21（`gradle.properties` 已固定）

新版 Android Studio 自带 JBR 是 Java 25，Gradle 8.7 的 Groovy 编译器不认
（报 `Unsupported class file major version 69`）。本机
`C:\Program Files\Java\jdk-21.0.12.1` 已通过 `org.gradle.java.home` 固定。
**换机器时需要把这一行改成对应的 JDK 17~21 路径**。

### 6. buildPython 需要 Python 3.11

Chaquopy 要求**构建机的 Python** 与 **App 内置版本**主次号一致。本机用
`py install 3.11` 装了 3.11.9，`app/build.gradle` 里写 `buildPython "py", "-3.11"`。

> ⚠️ 本机默认 `python` 是 3.14，所以这里**必须显式指定**，不能靠默认值。

### 7. 安卓上的 TLS 信任库

`MainActivity` / `ServerService` 设置 `CHERRYTALE_USE_SYSTEM_TRUST_STORE=0`：
`truststore` 在安卓上没有对应证书目录，退回 `certifi`（随 requests 自动装入）。

### 8. 运行时配置表必须进包

日常宝箱 / 活动预览 / 主线状态 / 扫荡等任务运行时要读
`Cherrytale Asset/TextAsset/` 配置表。全量太大，`sync_android_assets.py`
**扫描代码引用**，把真正用到的表打进 APK `assets/material/`；
`ServerService.extractMaterialAssets()` 每次启动解压到 `filesDir/material/`
并设置 `CHERRYTALE_MATERIAL_ROOT`。

**代码里新引用了表 → 重跑 sync 即可自动带上**，无需手工维护清单。

### 9. 必须用前台服务承载服务器（"点击没反应"的根因）

**症状**：App 切后台 / 锁屏后再回来，点任何任务都没反应、日志不输出，
连 `/api/health` 都挂死，但进程活着、端口能握手。

**根因**：没有前台服务时，App 退后台就成了"缓存进程"，系统（vivo 更激进）
会把它**整体 `SIGSTOP` 冻结** —— 所有线程永不调度。模拟器上
`kill -STOP <pid>` 已 1:1 复现全部症状，`kill -CONT` 立即恢复。

**修复**：`ServerService.kt`（前台服务，`dataSync` 类型 + 常驻通知）承载
Python 服务器；`MainActivity` 只负责 WebView 与启动服务。

**排障武器**：`webapi/app.py` 内置**卡死看门狗** —— 请求 90 秒无进展，
自动把全部线程堆栈倒进"开发者日志"与 logcat（tag `python.stderr`）。
另注意 `force_utf8_output` 会把流改成块缓冲，安卓上已改**行缓冲**，
否则日志迟迟不进 logcat 全是假象。

### 10. 日志导出到公共「下载」目录（2026-10-06 新增）

**背景**：安卓端 `CHERRYTALE_DATA_ROOT` = `filesDir` = `/data/user/0/<包名>/files`，
这是**应用内部私有目录**，文件管理器 / USB 都看不到 —— 用户根本找不到导出的日志。

**为什么 Python 侧写不出去**：scoped storage 下 Python 直接写公共目录必然 `EACCES`，
**只能走 Kotlin 的 `MediaStore`**。

**实现**：`MainActivity.LogExporter` 通过 `@JavascriptInterface` 暴露给前端
（`window.MiyaAndroid`），导出到 **`下载/米娅小助手/`**：

- `IS_PENDING` 1 → 0（先占位再提交，避免出现半截文件）；
- 做 canonical 路径前缀校验；
- API < 29 直接返回"不支持"；
- ⚠️ 桥运行在 **JavaBridge 线程**，`startActivity` 必须包 `runOnUiThread`；
- 前端检测不到桥时**自动走 PC 分支**，不影响桌面端。

### 11. 多端挤号自动恢复

同一账号在另一设备登录会把旧设备会话顶掉（服务端行为，
`99004 code=-5 "token is Invalid"`）。runner 层捕获 `SessionKickedError` 后
自动用保存的账号免密重登并重试当前任务
（`services/runner.py::_recover_kicked_session`），被挤的一方**全程无感**。
桌面端同样生效。

### 12. 工会"未入会"的判定

工会主页 `21009` 对未入会账号实测回 `errorCode=-1` 且 `myAlliance` 全字段 `-1` 哨兵，
而客户端常量 `HaveNotBeen_ExistedMyAlliance=19` 在这条链路上**没出现**。
`models/alliance.py::is_not_in_alliance()` 把 `-1` 和 `19` 都按"未入会"处理，
捐献 / 签到 / 挖矿三个任务统一走它。

---

## 五、版本号管理

**版本号一共四处，只改一处会不一致** —— 由 `tools/release.py` 统一维护，**禁止手改**：

| 位置 | 字段 | 当前值 | 影响 |
| --- | --- | --- | --- |
| `android/app/build.gradle` | `versionCode` / `versionName` | `13` / `1.0.0` | 安卓安装包版本（**升级安装靠 `versionCode`**） |
| `webapi/app.py` | `APP_VERSION` | `1.0.0` | **桌面**页面页脚 |
| `webapi/android_app.py` | `APP_VERSION` | `1.0.0` | **安卓**页面页脚 |
| `file_version_info.txt` | `filevers` / `FileVersion` / `ProductVersion` | `1.0.0.0` | exe「属性 → 详细信息」 |

> 页脚版本由前端通过 `/api/...` **动态渲染**，没有硬编码；
> 最后一项由 `CherrytaleTool.spec` 的 `version='file_version_info.txt'` 传给 PyInstaller。

### 一键发版

```powershell
py -3.11 tools/release.py check           # 检查四处是否一致
py -3.11 tools/release.py bump 1.0.1      # 原子升级四处（versionCode 自增）
py -3.11 tools/release.py android         # 同步 + 构建 + 归档到 dist/android/
py -3.11 tools/release.py all             # 双端完整流水线
```

### 手动重封（归档备查）

```powershell
# ① 同步桌面代码进安卓工程（安卓 python/ 是生成物）
.\.venv\Scripts\python.exe tools/sync_android_assets.py

# ② 命令行封包
cd android
.\gradlew.bat assembleDebug

# ③ 归档产物
Copy-Item app\build\outputs\apk\debug\app-debug.apk ..\dist\android\CherrytaleTool-1.0.0.apk
```

> ⚠️ 若第 ① 步在 PowerShell / CMD 里报 `UnicodeEncodeError: 'gbk' codec can't encode
> character '\u2714'`，是控制台编码不是 UTF-8 导致脚本打印 `✔` 失败（Git Bash 下正常）。
> 临时绕过：先 `$env:PYTHONIOENCODING='utf-8'`。`tools/release.py` 已内置强制 UTF-8 兜底。

---

## 六、配置与环境变量

安卓端特有的行为：

| 变量 | 安卓取值 | 说明 |
| --- | --- | --- |
| `CHERRYTALE_USE_SYSTEM_TRUST_STORE` | `0` | 退回 certifi（安卓无系统证书目录） |
| `CHERRYTALE_DATA_ROOT` | `filesDir` | 应用**内部私有**目录，外部不可见 |
| `CHERRYTALE_MATERIAL_ROOT` | `filesDir/material/` | 首次启动解压的配置表 |
| `CHERRYTALE_V_CODE_KEY` | 由 `config_local.py` 注入 | **打包必须带上**，否则所有需 vCode 的任务报错 |

> ⚠️ `config_local.py` 随 APK 一起打包（内容是 `GAME_V_CODE_KEY`）。
> 它是**已 gitignore 的本机文件**，不会进仓库。
>
> ⚠️ APK 里的代码是 `.pyc`，打包进 `assets/chaquopy/app.imy`。因此
> `is_file()` 探测**必然失败** —— `_load_local_config()` 走的是
> "文件探测 + `import_module`" 两轮逻辑，别改成单轮。

---

## 七、常见问题

<details>
<summary><b>打包时报 <code>Cannot read pyvenv.cfg</code>（返回码 107）</b></summary>

**这不一定代表 env 损坏**，在受限/沙箱环境下是常见假象。
`tools/release.py::_env_shell_looks_intact()` 会帮你分辨真假。
若判定为假象，**在沙箱外重新构建**即可。

> ⚠️⚠️ **永远不要删 `android/app/build/python/env/`** —— 删了会触发全量重新下载，
> 直接暴露在 `pypi.org` 上卡死（本机访问不通）。
</details>

<details>
<summary><b>改了 gradlew / gradle 配置后构建行为诡异</b></summary>

先停掉常驻的 Gradle 守护进程再重试：

```powershell
cd android
.\gradlew.bat --stop
```
</details>

<details>
<summary><b>App 切后台回来点任务没反应</b></summary>

见「技术要点 9」—— 根因是进程被系统冻结，已由 `ServerService.kt` 前台服务修复。
若在部分 ROM 上仍复现，检查系统设置里是否允许本应用**后台运行 / 忽略电池优化**。
</details>

<details>
<summary><b>导出的日志文件在手机上找不到</b></summary>

导出目标是公共 **`下载/米娅小助手/`**（走 MediaStore）。
若找不到，确认系统版本 ≥ API 29（Android 10）—— 更低版本该功能直接返回"不支持"。
</details>

---

## 八、相关文档

- 主 README：[`../README.md`](../README.md)
- 同步脚本源码：`tools/sync_android_assets.py`（含"为什么同步这些、不同步那些"的完整说明）
- 发布工具：`tools/release.py`
