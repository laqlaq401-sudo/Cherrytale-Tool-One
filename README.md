# 米娅小助手 · Cherrytale Tool One

> Cherrytale 日常任务自动化工具台：自动领取、一键签到、扫荡推图，把每天重复的点按交给它。
>
> **一套任务逻辑，四种入口（命令行 / 网页 / 桌面窗口 / 安卓 App），双端发布（Windows EXE + Android APK）。**
>
> 安全优先：默认只读、钻石消费默认关闭、支持演练模式 —— 不填参数就不会花掉任何资源。

![version](https://img.shields.io/badge/version-1.0.0-blue)

![python](https://img.shields.io/badge/python-3.11-3776AB?logo=python\&logoColor=white)

![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Android-0078D4)

![tests](https://img.shields.io/badge/tests-1807%20cases-brightgreen)

![tasks](https://img.shields.io/badge/tasks-27-blue)

![protocol](https://img.shields.io/badge/packets-868-purple)

![license](https://img.shields.io/badge/license-GPLv3-blue)

> **当前版本：`1.0.0`（正式版）** —— 首个稳定版本，四处版本号由 `tools/release.py` 统一维护。  
> 双端产物：`CherrytaleTool.exe`（Windows 免安装）· `CherrytaleTool-1.0.0.apk`（Android）。

---

## ☕ 支持作者

这个项目是俺业余时间一点点从零学习流程做出来的（虽然主力是 agent，但一次一次的试错确实很挠人），从功能设计到双端适配都只有俺一个人，也是市面上第一个该游戏日常化的工具。

它**完全免费、开源、不设任何付费功能**，也不会因为赞助而改变什么 —— 所有功能对所有人一视同仁。

如果它确实帮你省下了每天点日常的时间，觉得值，可以在爱发电请俺喝杯蜜雪柠檬水，那简直是万分感激不尽。在这里提前祝各位老板永远不死：

**👉 [afdian.com/a/cgbcacg](https://afdian.com/a/cgbcacg)**

不打赏也完全没关系，用着顺手就是最好的回报。遇到问题欢迎提 Issue。

---

## ⚠️ 免责声明（请先读这一段）

- 本项目是**个人自用的游戏日常自动化工具**，用于减少重复的手动点按。**不提供、不包含、不传播游戏本体、游戏素材或任何破解补丁。**
- 本项目分两部分：**源码仓库（本仓库）仅包含工具本身的源代码与脚本**，未收录任何游戏数据；**预编译发行包**（Release 中的 Windows / Android 产物）为便于开箱即用，内含运行所需的游戏配置表文件，仅作工具运行依赖随附，本项目不对其主张任何权利，其版权归游戏著作权人所有。若需自行准备，可将配置表放入程序目录下的同名路径。
- 自动化操作可能违反游戏的用户协议，存在**封号风险**。请仅在自己的账号上、以**只读或低风险**方式使用，并自行承担全部后果。
- 项目内置的"默认只读 + 消费闸门"设计是为了降低误操作代价，**不构成任何安全性承诺**。
- 本项目**仅供学习、研究和参考用途**，不构成任何形式的正式建议或承诺。
- 本软件按"原样"（AS IS）提供，不附带任何明示或暗示的担保。作者或版权持有人不对软件的使用或其他处理方式所产生的结果负责。完整声明见 [`NOTICE`](NOTICE)。
- 源代码依 **GNU General Public License v3.0** 发布（见 [`LICENSE`](LICENSE)）；仓库中的**游戏美术、角色头像、装备图标及其他第三方资源不在 GPL 授权范围内**，其版权归各自的原作者或权利人所有。
- 若本项目无意中侵犯了任何个人或实体的权益，请通过 Issue 联系，将在核实后第一时间处理。

---

## 目录

- [☕ 支持作者](#-支持作者)
- [一、这是什么](#一这是什么)
- [二、能做什么：任务清单](#二能做什么任务清单)
- [三、四种入口](#三四种入口)
- [四、安全设计（默认站在保守一侧）](#四安全设计默认站在保守一侧)
- [五、架构与分层](#五架构与分层)
- [六、目录结构](#六目录结构)
- [七、快速开始](#七快速开始)
- [八、配置项](#八配置项)
- [九、开发与测试](#九开发与测试)
- [十、打包与发布](#十打包与发布)
- [十一、排障 FAQ](#十一排障-faq)
- [十二、路线图](#十二路线图)
- [十三、致谢与许可](#十三致谢与许可)

---

## 一、这是什么

Cherrytale 的日常任务自动化工具台。把每天要重复点一遍的领取、签到、扫荡、挖矿交给它跑，你只需要看一眼结果。

它同时是一个**分层清晰的 Python 工程**：协议模型、通信层、业务任务、调度层各归各位，四种入口共用同一套执行逻辑：

```
models/（报文模型）  crypto/（签名与编码）
        │  ① 组装：先 build() 对齐字节
        ▼
client/（HTTP 通信层：Session / 头 / TLS / 异常重试）
        │  ② 发送：send() 联网
        ▼
tasks/（27 个可执行任务）→ services/（装配 · 调度 · 消费闸门）
        │  ③ 四种入口共用同一套执行逻辑
        ▼
main.py（CLI）  ·  web_server.py + web/ + webapi/（网页）  ·  main_gui.py（桌面窗口）
        │  ④ 双端封包
        ▼
CherrytaleTool.exe（PyInstaller）  ·  CherrytaleTool-<版本>.apk（Chaquopy）
```

### 通信层要点

工具与游戏服务端之间的通信有这些特征，直接决定了上层代码怎么写：

| 项       | 说明                                                      |
| ------- | ------------------------------------------------------- |
| 传输方式    | HTTP POST 一问一答，`Content-Type: application/octet-stream` |
| 报文格式    | 裸 protobuf，无私有分帧（长度由 HTTP `Content-Length` 给出）          |
| 加密 / 签名 | 都没有，载荷内含明文 JSON 与中文字符串                                  |
| `vCode` | 每次请求随机生成的 32 位 hex 字段                                   |
| 外层封包    | `RootPacket`，业务子包槽位编号 = 该子包自己的消息号                       |
| 消息号总量   | 868 个（`ePacketFormat_RPS` 枚举，类名与枚举成员名一致）                |
| 首包      | 88 字节 `VersionControlServerPacket`（99015），不需要登录态        |

> 客户端里另有一套 `CommonTcpClient` / `GetRootPacketVCode` 相关代码，属**备用路径**，实际通信并未走它 —— 实现时不要被它误导。

---

## 二、能做什么：任务清单

共 **27 个任务**，按**使用者的风险视角**分两组（而不是按开发者的功能视角）：能不能无脑全勾，才是使用者关心的事。

### 自动任务（纯领取 / 只读，不消耗任何资源，默认勾选）19 个

| 任务名                        | 名称          | 说明                   |
| -------------------------- | ----------- | -------------------- |
| `handshake`                | 网关握手        | 网关连通性握手（不需要登录态）      |
| `login`                    | 登录 / 换号     | 登录游戏并同步账号会话          |
| `mail`                     | 邮件一键领取      | 领取全部未读邮件             |
| `wallet`                   | 金币 / 钻石查询   | 查询金币、钻石与体力数据         |
| `roles`                    | 角色名册查询      | 拉取账号下的角色名册           |
| `free_gift`                | 特惠礼包免费领取    | 商城每日免费礼包（零消费）        |
| `arena_status`             | 竞技场状态       | 查询竞技场挑战状态            |
| `wudou_status`             | 天命对决状态      | 查询天命对决挑战状态           |
| `yimo_status`              | 失控炼成阵状态     | 查询失控炼成阵挑战状态          |
| `main_stage_status`        | 主线关卡进度状态    | 查询主线关卡推进进度           |
| `bbq_status`               | 宴席两顿状态      | 查询早/晚两顿宴席可吃、可邀请状态    |
| `alliance_sign_in`         | 工会签到        | 每日自动签到               |
| `alliance_mining_personal` | 工会个人挖矿      | 收获矿产并开启空闲矿           |
| `alliance_mining_team`     | 工会团体挖矿      | 参与团体矿位并开启            |
| `grand_line_supply`        | 辉煌航迹搜集物资    | 领取累计搜集奖励             |
| `daily_free_draw`          | 每日免费抽卡      | 自动抽取每日免费卡池（零钻石消耗）    |
| `top_pvp_box`              | 荣耀之巅宝箱      | 领取荣耀之巅每日宝箱           |
| `yimo_box`                 | 失控炼成阵宝箱     | 领取全服通关宝箱（3 份全满后自动停止） |
| `daily_box`                | 日常 / 周常活跃宝箱 | **仅**领取已达标的宝箱        |

### 主动任务（会消耗资源或需要参数，默认不勾选，运行前二次确认）8 个

| 任务名               | 名称          | 消耗                  |
| ----------------- | ----------- | ------------------- |
| `sweep_activity`  | 降临 / 活动关卡扫荡 | 体力 + 扫荡券            |
| `top_pvp_battle`  | 荣耀之巅        | 挑战次数                |
| `bbq_energy`      | 宴席          | 邀请好友花钻石（默认关闭）       |
| `buy_energy`      | 购买体力        | 钻石（每次 120 点）        |
| `sweep_material`  | 素材关卡与元素试炼   | 每日挑战次数（不耗体力）        |
| `market_buy`      | 一般市集购买      | 金币                  |
| `alliance_donate` | 工会金币捐献      | 25,000 金币 / 次（默认满档） |
| `push_main_stage` | 主线关卡通关      | 体力                  |

> 任务清单以 `services/task_spec.py` 为**唯一来源**，命令行与网页表单都从它派生；有机器化测试保证"参数面"不会两边抄歪。

---

## 三、四种入口

四个入口**共用同一份任务执行逻辑**（`services/runner.py`），不存在"命令行能跑、网页上跑不了"的分叉。  
前三种是同一个服务的不同启动方式（PC 端），第四种是把整套后端搬进安卓。

### ① 命令行（最轻，适合排障）

```bash
python main.py tasks                      # 列出全部已注册任务
python main.py run handshake              # 网关握手（不需要登录态）
python main.py run login mail wallet      # 可一次给多个任务，按顺序执行
python main.py run sweep_activity --times 5 --dry-run   # 演练：只组装字节，不联网
python main.py selftest                   # 离线自检：依赖 / 素材 / 加密向量 / 任务注册
python main.py accounts                   # 列出账号库里已保存的账号
```

### ② 网页版（PC 与手机都能访问）

```bash
python web_server.py                                  # 默认 http://127.0.0.1:8765/
python web_server.py --port 9000 --verbose             # 换端口 + DEBUG 日志
python web_server.py --host 0.0.0.0 --token 我的口令     # 让手机访问（必须给令牌）
```

- 默认**只绑定 `127.0.0.1`**；绑定 `0.0.0.0` 意味着同一 WiFi 下任何人都能操作你的账号，所以那一步必须由使用者显式执行，且脚本会**强制**要求同时给出令牌。
- 前端标签页：**信息展示 / 自动任务 / 主动任务 / 钻石使用 / 日志记录 / 设置**，底部为可拖拽高度的实时日志坞。

### ③ PC 独立窗口（同一个页面，换成原生窗口）

```bash
python main_gui.py                     # 无边框窗口（自绘最小化 / 关闭按钮）
python main_gui.py --no-frameless       # 用系统标题栏的普通窗口（更稳）
python main_gui.py --no-window          # 只起服务、不开窗口（等价于 web_server.py）
```

- 装了 `pywebview` 就是原生窗口；**不装会自动回退**到 Edge/Chrome 的 `--app` 独立窗口（无地址栏），功能完全一致。
- 打包产物 `CherrytaleTool.exe` 走的正是这条入口。

### ④ Android APK（脱离电脑独立运行）

通过 **Chaquopy** 把 Python 后端与 Web 前端整体内嵌进 APK，`MainActivity` 启动前台服务承载 Python 服务器，WebView 渲染同一个页面。

- 端口 `8000`，纯标准库 `ThreadingHTTPServer`（与 PC 端**同一份代码**）。
- 详见 [`android/README.md`](android/README.md)。

---

## 四、安全设计（默认站在保守一侧）

这类工具最贵的不是功能，而是**误操作代价的不对称**：领取类出错顶多少领一次，花钻石/重复领取却是不可逆损失。所以默认值一律站在保守一侧。

| 机制              | 做法                                                           | 为什么                         |
| --------------- | ------------------------------------------------------------ | --------------------------- |
| **默认只读**        | `times=0` / `battles=0` 即"只读"，不填 = 不发消耗类请求                   | fail-closed，不填就是最安全的行为      |
| **钻石总闸**        | `ALLOW_DIAMOND_SPEND` **默认关闭**，命令行 `--allow-diamond` 收敛到同一开关 | 花钻石不可逆；未配置 = 不允许            |
| **用途开关**        | 宴席"邀请好友"是独立用途开关，与总闸是**与**关系                                  | "能不能跑这个任务"与"能不能花钻石"必须拆开     |
| **演练模式**        | `--dry-run` 只组装并打印字节，**不联网、不发包**                             | 排查阶段永远先看字节，再决定是否落网          |
| **POST 不重试**    | HTTP 层只重试 GET                                                | 领奖类接口不幂等，超时重发可能重复领取甚至触发风控   |
| **密码不落盘**       | 密码只在内存里换一次令牌；令牌仅进浏览器会话存储                                     | 凭据最小暴露                      |
| **同源边界**        | 不发送任何 `Access-Control-*` 头；校验 `Host` 与 `Origin` 一致           | 掐 DNS 重绑定与跨站请求              |
| **单 worker 串行** | 同一时刻只允许一次运行，并发请求被拒                                           | 凭据走进程级环境变量、消费额度按运行累计，并行会互相踩 |
| **免密重登**        | 被挤号（`token is Invalid`）时自动用保存的账号重登并重试当前任务                    | 多端挤号全程无感                    |

---

## 五、架构与分层

依赖方向**单向、不会反向依赖**：

```
web_server.py / main_gui.py            ← 进程入口：端口 / 窗口 / 打印访问地址
        │
        ├──→ web/        纯静态前端（html / css / js / manifest，严禁放 .py）
        └──→ webapi/     HTTP 路由 + 状态 + 日志流（只做「HTTP 翻译」）
                 │
                 └──→ services/   装配与执行 —— **唯一**的调度层
                          │        ├─ task_spec.py     参数面唯一来源（CLI ↔ Web 共用）
                          │        ├─ task_registry.py 任务导入清单
                          │        ├─ runner.py        调度 / 消费闸门 / 被挤重登
                          │        └─ daily_state.py   「每游戏日一次」记账
                          ▼
                       tasks/  →  client/  →  网络
                          │
                          └──→  models/（报文） · crypto/（签名与编码） · config.py（配置）
```

### 各层职责

| 目录          | 职责                                                 |
| ----------- | -------------------------------------------------- |
| `models/`   | 报文数据结构（Pydantic 模型）+ 自动生成的协议号表 / 字段顺序表             |
| `crypto/`   | 签名拼装、哈希校验、AES / XOR 编解码、Token 封装                   |
| `client/`   | 底层通信：Session 管理、固定与动态 Headers、TLS 信任库、异常翻译与重试      |
| `tasks/`    | 高层业务逻辑（登录、领取、扫荡、推图……），每个任务一个模块、一个 `@register_task` |
| `services/` | 任务装配、参数规格、执行调度、消费闸门、日志通知                           |
| `web/`      | **纯静态**前端资源                                        |
| `webapi/`   | HTTP 服务：路由 / 鉴权 / 状态机 / 日志流（PC 与 Android 同一份）      |
| `tools/`    | 工程化脚本（生成协议表、打包发布、安全执行器……）                          |
| `tests/`    | 各模块最小可运行单元测试（77 个测试文件 / 1807 个用例）                  |
| `config.py` | 域名、固定参数与账号配置（**严禁硬编码敏感信息**）                        |

### 两条硬性工程纪律

1. **结果行只输出一次。** 通知走 `_notify_step_done(...)`，Web 与 CLI 二选一，重复 emit 会让 Web 步骤永远停在 `running`。
2. **单 worker 串行。** "队列为空"与"置空 worker"必须在同一把锁内完成；`thread.start()` 必须在持锁状态下调用 —— 破坏任一条会分别导致"提交后永不执行"或"两批任务并行跑"。

---

## 六、目录结构

```
Cherrytale tool One/
├── main.py                  # 命令行入口（selftest / tasks / run / accounts）
├── web_server.py            # 网页服务入口
├── main_gui.py              # PC 独立窗口入口
├── config.py                # 全局配置（环境变量 → config_local.py → 内置默认）
├── conftest.py / pytest.ini # 测试夹具与配置
├── requirements.txt         # 依赖清单（精确锁版本）
├── CherrytaleTool.spec      # PyInstaller 打包配置
├── 一键打包.bat              # Windows 一键发版助手（交互式菜单）
├── LICENSE                  # GNU General Public License v3.0（官方全文）
├── NOTICE                   # 版权声明与免责声明（含第三方资源授权范围说明）
├── client/                  # 通信层（session / headers / tls / 平台网关 / 账号库）
├── crypto/                  # 签名与编解码（aes / hash / sign / xor / vcode）
├── models/                  # 协议报文模型 + 自动生成的协议号表 / 字段表
├── services/                # 调度层（runner / task_spec / task_registry / daily_state）
├── tasks/                   # 27 个业务任务（每个模块一个 @register_task）
├── web/                     # 前端静态资源（index.html / login.html / assets/）
├── webapi/                  # HTTP 服务（app.py 桌面 · android_app.py 安卓 · state / logstream）
├── tools/                   # 工程化脚本（40 个）
│   └── android_shims/       # 安卓端 pydantic 纯 Python 垫片
├── tests/                   # 单元测试（77 个测试文件 / 1807 个用例）
├── notes/                   # 架构说明 / 设计取舍 / 待确认问题（**项目的知识档案**）
├── android/                 # Android Studio 工程（Chaquopy）
├── assets/                  # 图标资源
├── dist/                    # 发布产物（不入库）
├── Cherrytale IL2CPP/       # 运行所需游戏数据，约 227 MB，**不入库**（详见下方说明）
└── Cherrytale Asset/        # 运行所需游戏数据，约 345 MB，**不入库**（详见下方说明）
```

---

## 七、快速开始

### 1. 环境要求

| 项      | 要求                                                                                     |
| ------ | -------------------------------------------------------------------------------------- |
| Python | **3.11.x**（开发机实测 3.11.9 / win_amd64）—— 2026-10-06 起 PC 端由 3.14 降级对齐到安卓 Chaquopy 的 3.11 |
| 操作系统   | Windows 10/11（桌面端）；Android 7.0+（移动端）                                                   |
| 运行数据   | `Cherrytale IL2CPP/`、`Cherrytale Asset/`（**本仓库不提供**，需自行准备，见下节）                         |

> 依赖用 `==` **精确锁版本**而不是 `>=`：本项目对字节级结果敏感，版本一漂移，签名 / 加密结果就可能对不上。


### 2. 安装

```bash
git clone https://github.com/laqlaq401-sudo/Cherrytale-tool-One.git
cd "Cherrytale tool One"

py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

可选（PC 原生窗口，不装会自动回退到 Edge `--app` 窗口）：

```bash
.venv/Scripts/python.exe -m pip install pywebview==6.2.1
.venv/Scripts/python.exe tools/patch_pywebview_winforms.py   # 装完必须跑一次本地补丁
```

### 3. 准备运行数据

部分任务（日常宝箱、活动预览、主线进度等）需要读取游戏配置表。把 `Cherrytale IL2CPP/`、`Cherrytale Asset/` 两个目录按项目根目录放置，然后用自检确认：

```bash
python main.py selftest
```

> **本仓库不含上述数据**，也不提供获取方式 —— 请自行准备。缺少它们时，纯网络类任务（登录、领取、签到、扫荡等）仍可正常使用，仅涉及配置表读取的任务会跳过。

### 4. 跑起来

```bash
python main.py run handshake      # 先握手，验证连通性
python main.py run login          # 登录（会写入 .session.json）
python main.py run wallet         # 只读查询金币 / 钻石 / 体力
python main.py tasks              # 看看还有什么能跑

python web_server.py              # 或：打开网页版 http://127.0.0.1:8765/
python main_gui.py                # 或：开独立窗口
```

---

## 八、配置项

配置解析顺序：**环境变量 → `config_local.py` → 内置默认（采集自开发机）**。用 `config.device_identity_source()` 可查看当前来源。

### 常用环境变量

| 变量                                               | 默认     | 说明                   |
| ------------------------------------------------ | ------ | -------------------- |
| `CHERRYTALE_ALLOW_DIAMOND_SPEND`                 | `0`    | **钻石总闸**。未配置 = 禁止花钻石 |
| `CHERRYTALE_SERVER_ID`                           | 空      | 指定区服（不填则按响应自动挑）      |
| `CHERRYTALE_SERVER_GROUP_ID`                     | 空      | 指定服务器分组              |
| `CHERRYTALE_API_HOST` / `_PORT` / `_SCHEME`      | 内置     | 平台门户地址               |
| `CHERRYTALE_GAME_TIMEOUT`                        | `30`   | 单次请求超时（秒）            |
| `CHERRYTALE_GAME_LANGUAGE`                       | `zhcn` | 游戏语言                 |
| `CHERRYTALE_BUY_ENERGY` / `_TIMES` / `_MAX_COST` | 关闭     | 购买体力开关与额度            |
| `CHERRYTALE_BBQ_INVITE_FRIENDS`                  | `0`    | 宴席是否允许用钻石邀请好友        |
| `CHERRYTALE_SESSION_TOKEN`                       | 空      | 直接注入会话令牌（跳过登录）       |

### 敏感信息（全部已 `.gitignore`）

| 文件                        | 内容                         |
| ------------------------- | -------------------------- |
| `.auth_token`             | 平台鉴权令牌                     |
| `.session.json`           | 游戏会话状态（令牌 + playerId + 区服） |
| `.platform_accounts.json` | 多账号令牌库（最多 5 个）             |
| `config_local.py`         | 本机私有配置（覆盖内置默认）             |

> 密码**默认不落盘**，只在内存里换一次令牌。

---

## 九、开发与测试

```bash
# 全量测试
.venv/Scripts/python.exe -m pytest

# 单个模块
.venv/Scripts/python.exe -m pytest tests/test_crypto.py -v

# 离线自检（依赖 / 素材 / 加密向量 / 任务注册）
python main.py selftest
```

### 新增一个任务（三处登记，缺一不可）

1. `tasks/xxx.py` —— 写任务类并加 `@register_task`；
2. `services/task_registry.py` —— 加一行 `import tasks.xxx`（漏了它，网页上任务会变灰）；
3. `services/task_spec.py` —— 登记 UI 规格（中文标题 / 分组 / 参数面）。

有专属参数时还要在 `main.py` 的 `run` 子命令加旗标，并在 `services/runner.py::task_kwargs_for` 里点名传递。

> `tests/test_services_task_spec.py` 会做**机器化校验**：规格表里的每个参数名必须真实出现在 `main.build_parser()` 的字段集合中 —— 任一边加了参数忘了另一边，测试立刻变红。

### 测试里的两个隐藏前提（踩过的坑）

- **注册表"恰好是满的"**：pytest 收集阶段会 import 所有测试模块，`tests/test_tasks.py` 会注册假任务。凡涉及"注册表是否被加载"的断言，必须用 `subprocess` 起全新解释器。
- **长命令走 `tools/safe_run.py`**：直接 `cmd &` 会让子进程挂在同一条 PTY 上，终端被永久标记 busy。统一用 `python tools/safe_run.py --timeout 600 --label pytest -- <命令>`。

---

## 十、打包与发布

版本号共**四处**，由 `tools/release.py` 统一维护，**禁止手改**：

| 位置                         | 字段                                            | 影响                           |
| -------------------------- | --------------------------------------------- | ---------------------------- |
| `android/app/build.gradle` | `versionCode` / `versionName`                 | 安卓安装包版本（升级安装靠 `versionCode`） |
| `webapi/app.py`            | `APP_VERSION`                                 | **桌面**页面页脚                   |
| `webapi/android_app.py`    | `APP_VERSION`                                 | **安卓**页面页脚                   |
| `file_version_info.txt`    | `filevers` / `FileVersion` / `ProductVersion` | exe「属性 → 详细信息」               |

```bash
py -3.11 tools/release.py check              # 检查四处版本是否一致
py -3.11 tools/release.py bump 1.0.1         # 原子升级四处版本（versionCode 自增）
py -3.11 tools/release.py check-env          # 环境体检
py -3.11 tools/release.py test               # 核心回归单测
py -3.11 tools/release.py pc                 # 仅 PC 端（EXE + ZIP 归档）
py -3.11 tools/release.py android            # 仅 Android 端（同步资产 + gradlew 构建）
py -3.11 tools/release.py all                # 双端完整流水线
```

或者直接双击根目录的 **`一键打包.bat`**（交互式菜单，自动列出当前版本与候选版本）。

### 产物

```
dist/CherrytaleTool/CherrytaleTool.exe                        # PC 免安装目录
dist/CherrytaleTool-<版本>-windows-x64.zip                     # PC 发布包
dist/android/CherrytaleTool-<版本>.apk                         # 安卓安装包
```

### 分步打包（可选：`all` 失败时的手动流程）

正常情况下 `release.py all` 一条命令即可。若它在中途失败（多见于受限环境或构建缓存异常），  
按下面的顺序分步执行，效果与 `all` 相同：

```bash
# ① 清掉生成目录（残留的旧构建产物会让下一步读到过期文件）
py -3.11 -c "import shutil;shutil.rmtree(r'build/CherrytaleTool',ignore_errors=True);shutil.rmtree(r'dist/CherrytaleTool',ignore_errors=True)"
# ② 同步安卓资产
py -3.11 -u tools/sync_android_assets.py
# ③ Android 封包
py -3.11 tools/release.py android --skip-sync
# ④ PC 封包
py -3.11 -u tools/release.py pc
```

> `check` / `check-env` / `bump` / `test` 不涉及批量删除，任何时候都能直接用。

### 改完 `webapi/` 或 `web/` 之后

```bash
py -3.11 -u tools/sync_android_assets.py     # 必须同步，否则安卓端还是旧代码
```

---

## 十一、排障 FAQ

<details>

<summary><b>握手失败 / 证书报 <code>unable to get local issuer certificate</code></b></summary>

目标网关的证书链里有一张**交叉签名版根证书**，Python 自带的 OpenSSL 在这种链上无法建链，而 curl / 浏览器（走系统证书库）正常。

本项目已通过 `truststore` 改用操作系统信任库解决。确认已安装 `truststore==0.10.4`，并检查 `client/tls.py` 的模块文档。安卓端则设 `CHERRYTALE_USE_SYSTEM_TRUST_STORE=0` 退回 certifi。

</details>

<details>

<summary><b>安卓 App 切后台 / 锁屏后点任务没反应</b></summary>

根因：没有前台服务时，App 退后台会被系统（vivo 更激进）整体 `SIGSTOP` **冻结**，所有线程永不调度。

已由 `ServerService.kt`（前台服务 + 常驻通知）承载 Python 服务器修复。另内置**卡死看门狗**：请求 90 秒无进展自动把全线程堆栈倒进开发者日志与 logcat。

</details>

<details>

<summary><b>被挤号了怎么办</b></summary>

同一账号在另一台设备登录会把旧设备会话顶掉（服务端行为，`99004 code=-5 token is Invalid`）。

runner 层已自动捕获 `SessionKickedError`，用保存的账号免密重登并重试当前任务，被挤的一方全程无感。桌面端与安卓端同样生效。

</details>

<details>

<summary><b>网页上任务复选框是灰的</b></summary>

多半是新增任务时漏了 `services/task_registry.py` 里的 `import`。该文件是任务注册的**唯一导入清单**，`/api/tasks` 依赖它。

</details>

<details>

<summary><b>终端卡死 / <code>Command completion could not be observed</code></b></summary>

根因是某条命令的子进程一直挂在同一条 PTY 上，使 shell 永远发不出"命令完成"事件。

统一改用 `tools/safe_run.py`（子进程真正脱离 + `stdin=DEVNULL` + 硬超时 + 摘要落 `tmp/runs/last.json`）：

```bash
python tools/safe_run.py --timeout 600 --label pytest -- <完整命令>
python tools/safe_run.py --bg --label web -- python web_server.py --port 8767
python tools/safe_run.py --kill-all          # 收尾
```

</details>

<details>

<summary><b>跨域 / 跨端口访问本机 API 被拒（403）</b></summary>

这是**有意为之**：`webapi/app.py` 不再发送任何 `Access-Control-*` 头，且每个请求先过同源边界校验（`Host` 必须是回环主机名；带 `Origin` 时其主机必须与 `Host` 一致）。

正常入口是页面与 API **同源**（PC 8765 / 安卓 8000），不受影响。局域网访问走 `--host 0.0.0.0 --token <口令>`。

</details>

---

## 十二、路线图

- [ ] 更多活动关卡与限时任务的支持
- [ ] 任务执行结果的可视化统计（收益 / 消耗台账）
- [ ] 定时自动执行（每游戏日一次的自动化闭环）
- [ ] 文档补全：把 `notes/` 里的侦察档案整理成公开的协议说明

---

## 十三、致谢与许可

- 工程风格参考 lucima-tools（Ark Re:Code 系列工具）：模块化分层、可读优先、把"为什么这么做"写进注释。
- 感谢 protobuf、Pydantic、PyInstaller、Chaquopy 等开源项目 —— 没有它们这个工具台无从谈起。

### 许可证：GNU General Public License v3.0

本仓库源代码依据 **GNU General Public License v3.0** 发布，完整条款见 [`LICENSE`](LICENSE)（GPL-3.0 官方全文，未作任何修改）。

> 仓库中的**游戏美术、角色头像、装备图标及其他第三方资源不在 GPL 授权范围内**，  
> 其版权归各自的原作者或权利人所有。

GPL-3.0 是**强 copyleft 许可证**：你可以自由地使用、修改、分发本软件，也**允许商业用途**，但分发衍生作品时必须同样以 GPL-3.0 授权并提供完整源码（详见 LICENSE 第 5 节）。**源码仓库不包含**由游戏本体提取的素材（见 `.gitignore`）；**预编译发行包**中随附的游戏配置表仅为运行依赖，不计入本项目的授权范围，其权利归游戏著作权人所有。

### 免责声明

- 本项目仅供学习、研究和参考用途，不构成任何形式的正式建议或承诺。
- 本软件按"原样"（AS IS）提供，不附带任何明示或暗示的担保，包括但不限于适销性、特定用途适用性及不侵权的担保。作者或版权持有人不对软件的使用或其他处理方式所产生的结果负责。
- 在任何情况下，即使事先被告知可能发生损害，作者或贡献者均不对因使用本软件或无法使用本软件而引起的任何索赔、损害或其他责任负责，无论是合同诉讼、侵权行为还是其他原因。
- 如果本项目中包含指向第三方网站、资源或代码的链接，这些内容仅供参考，作者不对其准确性、合法性或安全性承担任何责任。
- 用户应自行确保其使用本项目的行为符合所在国家/地区的法律法规。因使用本项目而产生的任何法律风险由用户自行承担。
- 使用自动化工具操作游戏账号可能违反游戏的用户协议，并可能导致**账号被警告、封禁或永久停权**，或造成虚拟财产损失，请自行评估并承担该风险。
- 若本项目无意中侵犯了任何个人或实体的权益，请通过 Issue 或邮件联系我们，我们将在核实后第一时间处理（如删除相关内容）。

> 完整版权声明与免责声明见 [`NOTICE`](NOTICE)。

> 项目内所有注释、文档与提交信息均为中文，且刻意保留了大量"踩坑记录"与"为什么这么设计"的推理过程 —— 因为这个仓库的另一半价值，是**一份工程质量与设计取舍的实践记录**。

---

## ☕ 支持作者

如果这个项目帮到了你，可以到 [爱发电](https://afdian.com/a/cgbcacg) 请俺喝杯蜜雪柠檬水，祝各位老板永远不死。  
纯属自愿，不影响任何功能 —— 谢谢看到这里的每一个人。
