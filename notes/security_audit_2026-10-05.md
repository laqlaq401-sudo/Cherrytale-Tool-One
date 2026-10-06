# 协议资料敏感度审查报告

**审查对象**：`<项目根目录>` 已入库的 344 个文件
**审查时间**：2026-10-05
**当前基线提交**：`e23d14f`（已被强推至 GitHub）
**仓库可见性**：匿名访问返回 404，判定为 **私有仓库**（但结论见 P0-0）

---

## 摘要

共发现 **3 项 P0（必须处理）**、**2 项 P1（应当处理）**、**4 项 P2（建议处理）**。

最严重的一项：`tools/extract_saz_posts.py` 第 17 行藏着一段**明文账号 + 明文密码**，
是被误粘贴进 Python docstring 的 AI 提示词。这句话本身还写着"不要写到可上传部分"，
结果它自己就在可上传部分里。

第二严重：仓库**已经推送到 GitHub**，而提交者邮箱 `<account>@qq.com` 就写在提交元数据里。
首次提交的那份完整快照仍在 GitHub 的服务器上（强推只改变了分支指向，旧对象不会立即回收）。

---

## P0 —— 必须处理

### P0-1 明文账号密码躺在已入库文件里 🔴

**位置**：`tools/extract_saz_posts.py:17`

```python
【用法】
请跑一次荣耀之巅的战斗测试 用账号<REDACTED>@qq.com 密码<REDACTED>（不要写到可上传部分） 服务器选择S113 只要在战斗时有异常立即停止测试
    python tools/extract_saz_posts.py --list
```

**性质**：一段 AI 提示词被误粘进了模块 docstring 的「用法」段落。括号里那句
"（不要写到可上传部分）"正是当时的要求 —— 显然被违反了。

**泄露内容**：完整账号 + 完整明文密码，可直接用于登录 Erolabs 平台门户。

**扩散范围**：仅此一处（全仓库及未入库文件均已扫描确认）。

**处置**：删除该行 → **立即改密码** → 复查该账号的登录记录与绑定信息。

**状态**：✅ 已于 2026-10-05 00:41 处理完毕（详见文末「处置记录」）。

---

### P0-2 个人邮箱散布在多个已入库文件 🔴

| 文件 | 行 | 内容 |
|---|---|---|
| `tools/extract_saz_posts.py` | 17 | `<account>@qq.com`（与密码同行） |
| `notes/glory_summit_capture.md` | 110, 205 | `账号 <account>@qq.com / S113 伊莉莎白一世` |
| `notes/sweep_capture.md` | 197 | `账号 \`<account>@qq.com\` / #113 伊莉莎白一世 / role <playerId>` |
| `README.md` | 291 | `github.com/laqlaq401-sudo/...`（用户名非邮箱，低风险） |
| `NOTICE` | 18 | `Copyright (C) 2026 laqlaq401-sudo`（低风险） |

**额外**：git 提交元数据里是 `laqlaq123 <<account>@qq.com>` —— 这个邮箱会
**公开显示在每一次提交记录、GitHub 页面和 API 响应中**，无法通过改文件消除。

**处置**：
1. 文件里的邮箱替换为占位符（`<account>@<host>` 或 `redacted@example.com`）
2. 提交元数据要么接受（大多数人不在乎），要么重做提交并改 `user.email`
   —— 但**必须意识到**：只要推过一次，旧提交对象就在 GitHub 上，改不掉

**状态**：✅ 文件内邮箱已于 2026-10-05 处理完毕；提交元数据按用户决定**保留**。

---

### P0-4 账号令牌库 `.platform_accounts.json` 含明文 refresh_token 🔴

> 补充发现（2026-10-05 00:41 处置过程中发现，初版报告遗漏）

**位置**：`.platform_accounts.json`（5205 字节，最后修改 2026-10-03）

**该文件是否入库**：**否** —— `.gitignore` 第 66 行已正确排除。**未泄露到 GitHub** ✅

**但内容本身值得记录**：

| userId | 账号 | refresh_token | device_id | sessions |
|---|---|---|---|---|
| `ER00000000-…` | `<账号A>@9662.com` | 730 字符（有效 JWT） | 真实 deviceID | 1 个 |
| `ER00000000-…` | `<账号B>@qq.com` | 730 字符（有效 JWT） | 真实 deviceID | 2 个 |

**关键点**：这条记录推翻了初版报告的一个判断 —— 账号**并非"免密码"**。
文件里存着 730 字符的 refresh_token，说明这套体系是**令牌续期**而非无凭据。

**风险**：本地文件，风险可控。但两个 refresh_token 都是有效的，
若这份文件被同步到云盘、被打包进 `dist/`、或将来误加进 git，后果等同于交出了账号。

**处置建议**：
1. 确认这两个 refresh_token 对应的账号你自己还在用 → 若不用了，从账号库删掉
2. 确认 `dist/` 打包产物里**没有**夹带这个文件（`dist/` 已忽略，但物理文件仍在磁盘）
3. 无需改密码即可，但若你打算分享整个工程目录（压缩包发给别人），必须先删这个文件

---

### P0-3 真实登录凭据（userId）硬编码 🔴

`socialAccount` / `socialOpenId` 的真实值 **就是登录凭据本身**（代码注释明确写了：
"真正的凭据是 socialAccount + socialOpenId = 平台 userId"，密码反而是空的）。

| 真实值 | 所在文件 |
|---|---|
| `ER00000000-…` | `models/platform.py`、`models/server.py`、`protocol_samples/session_samples.py`、`tests/test_platform.py`、`tests/test_server.py`、`tests/test_webapi_app.py`、`tools/probe_1003_replay.py`、`tools/verify_1003.py` |
| `ER00000000-…` | `client/account_store.py`、`models/platform.py` |
| deviceID `0000000000000000000000000000000000000000` | `config.py:930`（`_DEVICE_ID_DEFAULT`）、`protocol_samples/session_samples.py`、`tests/test_platform.py` |

**风险判断**：
- `ER00000000-…` 是**旧账号**（`accountID=<accountID>`、`playerId=<playerId>`），
  若该账号已废弃/改绑，风险降低但仍不应公开
- `ER00000000-…` 是**新账号**（`playerId=<playerId>`）—— 若仍在用，这是**可直接登录的凭据**
- `deviceID` 是 40 位 hex，配合 userId 可完整复现登录报文

**处置**：全部替换为 `ER00000000-0000-0000-0000-000000000000` 这类零值占位
（仓库里已有这个惯例 —— `tasks/login.py:82`、`tests/test_login.py:83` 就在用）。
测试断言若依赖具体值，改为从常量导入。

---

## P1 —— 应当处理

### P1-1 真实玩家身份信息（playerId / 角色名 / accountID）

| 信息 | 值 | 位置 |
|---|---|---|
| playerId | `<playerId>`（旧/新/测试三组） | `notes/server_list.md`、`notes/handoff_login_module.md`、`notes/open_questions.md`、`tests/test_session_state.py`、`tests/test_webapi_app.py` 等 |
| accountID | `<accountID>`（旧/新两组） | `notes/server_list.md`、`notes/recon_findings.md`(已排除) |
| 角色名 | `宮本武藏` | `models/server.py`、`models/session_state.py`、多个 tests、notes |
| 区服 | `#113 伊莉莎白一世`、`#167` | 多个 notes 与 tests |

**风险**：这些是**可被其他玩家查到**的游戏内身份（排行/角色查询），
配合上面的 userId 能确认"哪个 B站/QQ 账号对应游戏里哪个角色"，
等于把**现实身份与游戏身份绑定**公开。

**处置**：notes 里的真机实测段落建议整体脱敏（"账号 `<redacted>` / #113"）；
`models/server.py` 的默认值改中性值。

---

### P1-2 真实服务器地址与端口（完整攻击面）

| 地址 | 类型 | 位置 |
|---|---|---|
| `game-ct-labs.ecchi.xxx:1893` | 游戏网关 | `config.py:135`、`notes/gateway_traffic.md`、`notes/handoff_login_module.md` |
| `login-cq.ecchi.xxx` | 网页登录 | `config.py:222`、`notes/metadata_strings.txt` |
| `sadpki-portal-v2.ebuajk.com` | 平台门户 | `config.py:885` |
| `api-ct-labs.ecchi.xxx` | API | `notes/metadata_strings.txt`、`notes/recon_findings.md`(已排除) |
| `dev-game-cq.ecchi.xxx`、`game-ct-dm.ecchi.xxx` | 开发/内测环境 | `notes/metadata_strings.txt` |
| `l.hyenadata.com`、`www.ero-labs.com`、`pfdd.88kongque.com` | CDN/官网 | `protocol_samples/gateway_samples.py`、`notes/metadata_strings.txt` |

**风险**：`dev-` 前缀的内测环境通常在鉴权上比生产宽松，且**这个域名池本身就是
逆向成果** —— 公开等于把"从哪个入口能打通"整条路径送出去。

**处置**：`config.py` 保留（工具要跑，且可经环境变量覆盖）；
但 `notes/` 里的**内测地址**（`dev-game-cq`、`game-ct-dm`）建议删掉或打码 —— 那些不是必需信息。

---

## P2 —— 建议处理

### P2-1 客户端逆向密钥（游戏本身的秘密）

| 项 | 值 | 位置 |
|---|---|---|
| vCode 密钥 | `**已移出源码** ✅ | 见下方处置 |
| 静态 MD5 指纹 | `2DB9B982...`、`77bf1876...` 等 6 个 | `crypto/vcode.py`、`tools/brute_vcode.py`、`tools/probe_*.py`、`notes/handoff_login_module.md`、`notes/glory_summit_capture.md` |
| 反汇编 RVA 地址 | `0xD8F0F0`、`0x101AB50` 等 | 已移出 ✅ 见下方处置 |

**说明**：这是**对游戏厂商的秘密**，不是对你个人的风险。
但我查了 `crypto/vcode.py` 的注释，它明确写了：

> KEY 是游戏客户端写死的常量（服务端要用同一个才能验证），不是账号/设备相关的值

所以它**没有反向泄露你的账号**。

**✅ 处置（2026-10-05 追加）**：用户决定「移出」——
- `config.GAME_V_CODE_KEY` 改为**必须由本机注入**（`config_local.py` 或环境变量
  `CHERRYTALE_V_CODE_KEY`），源码内不再有该常量；缺失时 `make_v_code()` 显式抛错。
- `notes/vcode_algorithm.md`（含完整推导 + RVA 证据）**移出跟踪**，文件留在本地。
- `crypto/vcode.py`、`models/main_stage.py`、`models/server.py`、`tools/brute_vcode.py`
  中的 RVA / VA 地址已删除（保留"由反汇编证实"的结论叙述）。
- `notes/handoff_login_module.md` 中的 KEY 字面量与 RVA 已脱敏为 `GAME_V_CODE_KEY`。
- `tests/test_vcode.py` 增加 `skipif`：未配置 KEY 时**跳过**（不再 fail），
  并新增 `TestKeyGuard` 验证"缺 KEY 必须报错"。

**`crypto/sign.py:32` 的 `SIGN_SALT = None`** 说明签名算法尚未逆向完成，
不存在泄露（这是个占位）。

### P2-2 抓包样本内嵌的真实报文

`protocol_samples/*.py` 共 222 KB，含**真实抓包的原始 hex 字节**。
我上一轮判断"这是协议资产不能丢"，从**工程角度**仍然成立。

但从**敏感度角度**需补充：`session_samples.py` 里内嵌的 330 字节是**一次真实登录请求的完整报文**，
里面含 userId 与 deviceID。若这些值被脱敏替换，fixture 的字节长度会变，
断言会失败 —— 所以**要么整体保留、要么整体重构**，不存在"改一半"。

建议：`gateway_samples.py`（响应侧，主要是服务端下发的配置）可保留；
`session_samples.py`（请求侧，含凭据）**若你能接受测试改动**，值得重构。

### P2-3 已入库的 Android IDE 配置

`android/.idea/` 共 12 个文件被跟踪，其中 `caches/deviceStreaming.xml`
含本机设备/环境缓存。结论：**无敏感内容**（已逐文件检查），但属于典型的
"不该入库"文件 —— 且我已经把 `.vscode/` 排除了，`.idea/` 却漏了，不一致。

建议：加 `android/.idea/` 到 `.gitignore` 并从跟踪中移除。

### P2-4 仓库可见性前提

匿名访问 `https://github.com/laqlaq401-sudo/Cherrytale-tool-One` 返回 404，
判定为**私有**。但注意：

- 私有仓库 ≠ 安全。GitHub 的员工、任何拿到你账号的人、以及
  **仓库被误设为公开的那一天**，都会看到全部历史
- 强推**不会**立即删除旧提交对象。旧快照（含 `dist/`、`captures/` 的早期版本）
  会作为**悬空对象**留在 GitHub 上，直到其垃圾回收（不可控）
- 若要彻底清除，需联系 GitHub Support 或删除重建仓库

---

## 已检查且无问题的项 ✅

- 无真实 `accessToken` / `refreshToken` / JWT 进入 git（只有 `eyJ0eXAiOiJK…(730 字符)` 这种**截断占位**）
- 无 `.auth_token`、`.session.json`、`.platform_credentials` 内容泄露
- 远程 URL 未内嵌凭据（`https://...` 形式，非 `https://user:pass@...`）
- 无 `.git-credentials` 文件
- `web/` 前端无第三方地址泄露（只有 `127.0.0.1` 本地回环）
- `android/` 源码无外部服务地址（只有 `schemas.android.com` 命名空间）
- `crypto/aes.py` 只有密钥长度校验，无硬编码密钥
- 所有 tests 里的 `token="test-token"` 之类均为**测试假值**
- `LICENSE` / `NOTICE` 无个人信息（除 GitHub 用户名）

---

## 建议的处置顺序

| 顺序 | 动作 | 状态 |
|---|---|---|
| 1 | **立即改平台密码** `<password>` | ⚠️ **待你执行**（改文件无法撤回已泄露的值） |
| 2 | 删除 `tools/extract_saz_posts.py:17` 整行 | ✅ 已完成 |
| 3 | 脱敏 notes 中的邮箱 | ✅ 已完成 |
| 4 | 替换 userId / deviceID 为占位符 | ⏸️ 你决定**暂缓** |
| 5 | 决定提交邮箱 | ⏸️ 你决定**保留** |
| 6 | 加 `android/.idea/` 到忽略 | ⏸️ 待你确认 |
| 7 | 决定 vCode KEY 的去留 | ⏸️ 待你确认 |
| 8 | 重做提交并强推 | ✅ 已完成（普通提交 `5f4a724` 并推送） |

**关于第 8 步的修正**：初版建议用 `--amend` 重做基线提交，实际执行时改为
**新增一个普通提交 `5f4a724`**（用户选择"先不做重构"，因此不需要重写历史）。
这样 `e23d14f` 中仍保留着原样文本，但已在 `5f4a724` 中移除 ——
**这意味着凭据仍留在 Git 历史里**，只是当前工作区与最新提交已干净。

> 若要让历史也干净，需 `git rebase -i e23d14f` 或重做整个仓库。
> 鉴于该账号即将废弃，用户未要求处理。

---

## 需要你决策的点

1. **vCode KEY 与逆向 RVA 保留吗？** → ✅ **已选择「移出」**（2026-10-05）
2. **提交邮箱 `<account>@qq.com` 改不改？** 你已选择保留
3. **`session_samples.py` 的整体重构做不做？** 你已选择暂缓
4. ~~是否要我直接执行脱敏~~ → ✅ 已执行完毕

---

## 处置记录（2026-10-05 00:41 ~ 00:55）

### 已完成

**改动文件 4 个**（备份在 `../cherrytale-redact-backup/`）：

| 文件 | 改动 |
|---|---|
| `tools/extract_saz_posts.py` | 删除第 17 行整行（含账号+密码的误粘提示词） |
| `notes/glory_summit_capture.md` | 第 110、205 行账号 → `<redacted>` |
| `notes/sweep_capture.md` | 第 197 行账号 → `<redacted>` |
| `notes/security_audit_2026-10-05.md` | 本报告内复述的凭据全部 → `<account>` / `<password>` |

**验证结果**：
- ✅ `git ls-files` 全文扫描：已入库文件中**无**那两条真实凭据（邮箱前缀与明文密码）
- ✅ `tools/extract_saz_posts.py` 通过 `ast.parse` 语法检查
- ✅ `tests/test_server.py` 57 项全通过
- ✅ `test_server/test_login/test_platform/test_webapi_app/test_session_state`
  全部通过（仅 8 项因缺 token / 未开网络测试而跳过，属预期）
- ✅ `dist/` 打包产物未夹带 `.platform_accounts.json` / `.session.json` / `.auth_token`

**提交与推送**：
- 提交 `5f4a724 security: 移除文档与工具中的明文账号密码`
- 已推送至 `origin/main`（`e23d14f..5f4a724`），远程已验证

### 仍然遗留

| 项 | 说明 |
|---|---|
| ⚠️ **密码未改** | 明文密码仍在 Git 历史 `e23d14f` 中（且已推送）。**改密码是唯一有效补救**（原始值见本地记录，勿写回本文件） |
| Git 历史残留 | `notes/recon_findings.md:898`（已忽略跟踪，但仍在磁盘）、`repomix-output.xml`（已忽略）、`tmp/edgeV2/V3` 浏览器缓存（已忽略） |
| 提交者邮箱 | 每条提交都显示 `<account>@qq.com`；你计划重建仓库重推时可改为 `<redacted>` |
| ~~`android/.idea/`~~ | ✅ 已解决（2026-10-05）：12 个文件已 `git rm --cached`，`.gitignore` 加 `.idea/` |
| ~~`session_samples.py`~~ | ✅ 已解决：改为等长占位符（见前文），真实 userId/deviceID 已清零 |
| vCode KEY | 按你决定暂缓 |
