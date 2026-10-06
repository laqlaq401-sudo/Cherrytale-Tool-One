# 交接文档：完整登录模块（Plan 模式用）

> **怎么用这份文档**
> 新会话切到 **Plan 模式**后，第一句直接说：
> 「读 `notes/handoff_login_module.md`，按第三节为登录模块做方案规划，先别写代码」。
> 比把内容粘进对话更省 token，也保证以后只维护这一份。
>
> **收录标准**：只写**已经实测验证过**的结论。凡是「存在但未逐一验证」的，都在原地用
> ⚠️ 标出，**不要**把它当事实拿去推断。
>
> 生成时间：2026-09-21（即登录全流程打通、vCode 算法攻克当天）。

---

## 一、已 100% 验证的协议事实（不要重新验证）

### ① 传输层

| 项 | 值 |
|---|---|
| 地址 | `https://game-ct-labs.ecchi.xxx:1893/` |
| 方法 | `POST` |
| 请求头 | 固定，见 `client/game_client.py` 的 `UNITY_HEADERS`（`UnityPlayer/2020.3.49f1`、`Content-Type: application/octet-stream` 等） |
| 报文 | **裸 protobuf**（信封 + 子包），无长度前缀、无压缩、无加密 |

### ② 全流程已实测跑通（拿到 `playerId=10000001 @ <区服名>(#167)`）

```
平台身份(GET /api/v2/user → userId)
   → 握手(99015)
   → 1001 账号登录
   → 1002 区服列表
   → 1003 进入区服
   → 1004 进区结果（令牌 + playerId）
```

### ③ 1001 = `AccountClientInfoLoginPacket`

- 请求体 **330 字节**，组包时 `include_defaults=True`（零值字段也要发）。
- 关键取值：`socialAccount` = `socialOpenId` = 平台 `userId`；`customAccount="ecchigame"`；
  `socialAccountType=9`；`deviceID` / `osVersion` / `clientVersion` 等照 `client/` 里已写好的取。
- → **1002 = `AccountClientInfoLoginRes`**：`errorCode`、`accountID`(=10000001)、
  `allServerList`(167 个区)、`suggestedServer`、`lastLoginedServerList`、
  `backendDevelopMode`、`bbsid`、`cdnUrl`。
  ⚠️ 这些字段里只有一部分被解析使用过，**其余字段的取值语义未逐一验证**。

### ④ 1003 = `LoginPacket`

- 请求体 **178 字节**，8 个字段，`include_defaults=True`，**必须带信封 `activity`**。

| 编号 | 字段 | 取值 |
|---|---|---|
| 1 | `accountID` | 来自 1002 |
| 2 | `serverID` | 选中的区服（见下） |
| 3 | `auerWebLogin_sid` | `0` |
| 4 | `auerWebLogin_token` | `"-1"`（字符串，**不是**数字 -1） |
| 5 | `channelPlatformType` | `2` |
| 6 | `clientVersion` | `"2.2.0-el-h-win"` |
| 7 | `hamiSubNoList` | **`["平台 userId"]`** ← 必须带，否则服务端拒 |
| 8 | `languageCode` | `"zhcn"` |

- **区服选择**：必须进「本账号已有角色的区」（`choose_server` 传 `prefer_with_character=True`）。
- → **1004 = `LoginRes`**：`errorCode=0`；**会话令牌在子包 `playerInitClass.token`**（34 字符），
  同一子包里还有 `playerInitClass.playerID`。
  ⚠️ 令牌**不在信封的元信息字段里**，必须显式从子包取出来，再回填到后续请求。

### ⑤ 信封（`RootPacket`）字段编号

| 编号 | 字段 | 说明 |
|---|---|---|
| 1 | `packetID` | 业务消息号 |
| 2 | `token` | 会话令牌 |
| 3 | `settingMd5` | 默认 `"-1"` |
| 8 | `playerId` | |
| 10 | `timeStampToken` | 默认 `-1` |
| 12 | `timeStampClient` | 客户端时间戳（毫秒） |
| 14 | `vCode` | 见 ⑥ |
| 16 | `activity` | `SpecialActivityRefreshClass` |

- **子包槽位编号 = 消息号本身**（不是声明顺序）。
- 1001 / 1003 都要 `include_defaults=True`。

### ⑥ vCode 算法（逆向还原 + 两个独立样本验证通过）

落地在 `crypto/vcode.py`：

```python
source = f"{packetID}{token}{settingMd5}{timeStampClient}"   # 直接相连，无分隔符
vCode  = UPPER( MD5( Base64(source) + GAME_V_CODE_KEY ) )    # 32 位大写十六进制
```

| 样本 | 输入 | 期望 vCode |
|---|---|---|
| 1（1001） | `("", "-1", 1789905913885)` | `92AEDFDDD86362AD5A1FB38C817EAC80` |
| 2（1003） | `("", "-1", 1789908064585)` | `93ABA44B13800136EBED3CF097981137` |

- 两个样本取自**不同抓包**，双向命中 ⇒ 拼接顺序 / Base64 / KEY / MD5 / 大写全部确认。

### ⑦ 服务端行为（四条对照实验结论）

1. **不校验** `timeStampClient` 新鲜度 —— 用几小时前的旧值同样能通过。
2. **不记录**已用过的 vCode —— 可重复使用（不是防重放）。
3. 但 vCode 必须与实际发出的 `timeStampClient` **配对** —— 算错即被拒。
4. 拒绝时统一回 `ServerExceptionRes{code=-3, msg="ABNORMAL_PACKET"}`，**信息量为零**。
   → 别指望从错误信息推断原因，只能靠**对照实验**（每步只改一个变量）。

---

## 二、已经写好的代码（直接复用，不要重写）

### 分层骨架

| 文件 | 内容 |
|---|---|
| `config.py` | `GAME_V_CODE_KEY`（由 `config_local.py` / 环境变量注入，不入库）；`GAME_TIME_STAMP_CLIENT` / `GAME_V_CODE` 仅供重放 |
| `crypto/vcode.py` | `make_v_code(packet_id, token, setting_md5, time_stamp_client) -> str` |
| `crypto/hash.py` | `md5_hex()` 等基础哈希 |
| `models/protobuf_wire.py` | varint / string / message 编解码 |
| `models/game_packet.py` | `ProtoMessage` 基类（含 `field_tag_map` / `masked_fields`） |
| `models/envelope.py` | `GameEnvelope`；`next_client_timestamp()` 保证时间戳严格递增；**自动算 vCode** |
| `models/proto_fields.py` | 自动生成（45 类 / 1128 字段），来源 `tools/extract_proto_fields.py` 的 `FOCUS_CLASSES` |
| `models/server.py` | `AccountClientInfoLoginPacket` / `AccountClientInfoLoginRes` / `LoginPacket` / `PlayerClass` / `LoginRes` |
| `client/game_client.py` | `GameClient`：`handshake()` / `build()` / `send()` / `post_raw()` / `send_raw()` |
| `tasks/login.py` | `LoginTask`（**已跑通，本次要整理的对象**） |
| `tasks/base.py` | `register_task` / `TaskResult` |
| `tests/` | pytest 全绿；`tests/test_vcode.py` 用两个样本把算法钉死 |

> `models/proto_fields.py` 是**生成物**。要加新类，改
> `tools/extract_proto_fields.py` 的 `FOCUS_CLASSES` 后重跑
> `python tools/extract_proto_fields.py`；**不要手改**，
> 否则 `python main.py selftest` 的「生成表同步」会报 ✘。

### 逆向工具（本次新增，可复用）

| 文件 | 作用 |
|---|---|
| `tools/il2cpp_symtab.py` | 从 `dump.cs` 建「地址 → 方法名」索引（159 098 个方法） |
| `tools/disasm_il2cpp.py` | 反汇编 IL2CPP 方法，并把 `call` / `[rip+…]` 目标自动翻译成方法名 |
| `tools/verify_1003.py` | 离线逐字段核对 1003 报文 |
| `tools/probe_1003.py` | 真发探测 1003（含四种参数组合） |
| `tools/brute_vcode.py` | 未知常量反查（用已知样本遍历字面量池） |
| `notes/vcode_algorithm.md` | vCode 的完整逆向记录（含复现命令） |

---

## 三、当前状态与本次目标（Plan 模式聚焦这里）

> **✅ 本节 7 个议题已于 2026-09-21 落地**，结论与新增文件见
> `notes/recon_findings.md` 第八节「登录模块整理成会话基座」。
> 下方保留原始问题清单，便于回溯当初为什么这么设计。

| # | 议题 | 落地结论（一句话） |
|---|---|---|
| 1 | 模块边界与对外接口 | 新增 `models/session_state.py::GameSessionState` 作唯一载体；状态挂在 `GameSession.game_state` |
| 2 | token 生命周期 | 登录任务写回 `session.game_state` + 落盘 `.session.json`；`client/game_client.py` 的 `for_session()` 负责注入；失效靠错误码识别（**Q-010 未解**，故不自动重登） |
| 3 | 与后续任务衔接 | 同进程共享同一个 `GameSession`（`tasks/daily.py` 已有）；跨进程靠 `client/session_store.py`；业务任务统一走 `BaseTask.open_game_client()` |
| 4 | 失败分类与重试 | `AbnormalPacketError`（99004 / -3）= **不可重试**；业务错 = 不重试；网络错 = 仅幂等只读包可重试一次（POST 默认仍不重试） |
| 5 | 测试策略 | 离线：`tests/test_session_state.py` / `test_game_error.py` / `test_login.py`（假平台 + 按包号回放网络出口）；联网：`CHERRYTALE_RUN_NETWORK_TESTS` 与额外的 `CHERRYTALE_RUN_LOGIN_NETWORK_TESTS` 双层开关 |
| 6 | 模型补全 | 只补 `ServerExceptionRes`(99004)，并进生成表；1004 的其余初始化数据**继续不声明**（语义未验证） |
| 7 | 文件归属 | `models/session_state.py`、`models/game_error.py`、`client/session_store.py`、`client/game_client.py`、`tasks/base.py`、`tasks/login.py`、`config.py`、`tools/extract_proto_fields.py` |

**原状**：`.venv/Scripts/python.exe main.py run login` 已成功（约 6 秒，输出 `playerId`）。

**原目标**：把它整理成**完整、可复用的登录模块**，作为后续所有业务（6001 日常等）的会话基座。

Plan 模式请给出**结构化方案 + 分步 TODO**（先不要写代码）：

1. **模块边界与对外接口** —— 登录模块该对外暴露什么？会话状态（`token` / `playerId` / 区服）放在哪、怎么传下去？
2. **token 生命周期** —— 1004 取得后由谁负责带上？失效如何识别（错误码？重复回 -3？）与处理？
3. **与后续任务的衔接** —— daily 等任务如何复用这份会话（单例 / 依赖注入 / 上下文对象）？
4. **失败分类与重试** —— 区分「包结构·算法错」（不重试，要改代码）与「业务错」（可重试）；
   `ABNORMAL_PACKET` 属于前者。
5. **测试策略** —— 离线测试（用抓包样本）与可选联网测试如何分层（参考 `tests/test_platform.py`
   的 `CHERRYTALE_RUN_NETWORK_TESTS` 开关）。
6. **模型补全** —— 1002 / 1004 目前只声明了用到的字段，是否需要补全、补到什么程度？
7. **文件归属** —— 哪些改动进 `client/`、`models/`、`tasks/`、`crypto/`。

---

## 四、按需使用的验证命令（别重复跑）

```bash
cd '<项目根目录>'                                # 例如：cd /d %~dp0
.venv/Scripts/python.exe main.py run login     # 真实登录，约 6 秒
.venv/Scripts/python.exe main.py selftest      # 环境自检（应输出「环境就绪」）
.venv/Scripts/python.exe -m pytest -q          # 全量测试
.venv/Scripts/python.exe tools/verify_1003.py  # 离线看 1003 字节结构
```

---

## 五、已经排除的错误假设（别再走一遍）

- ✗ **vCode 不是随机值** —— 曾先试随机生成，服务端一律拒绝。
- ✗ **「1003 不带 activity」是错的** —— 当初那是把 HAR 里 85 字节的**握手包**误认成了 1003。
- ✗ 1003 的问题**不在**区服、字段编号、请求头、Cookie —— 这些都被逐项验证排除了。
- ✗ 真正的拦路虎只有两个：**vCode 算法** + **1003 的 `hamiSubNoList` 必须带平台 userId**。
- ✗ 反汇编时**不要**顺着 `mov rbp,[rip+…]` 去解 IL2CPP 元数据编码值 ——
  用已知样本**反查字面量池**更快（KEY 就是这么找到的，推导见本地修订记录）。
