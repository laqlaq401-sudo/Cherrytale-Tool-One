# 正式版发布前 —— 删仓库 / 重建操作清单

> 生成时间：2026-10-07 00:1x
> 适用仓库：`https://github.com/laqlaq401-sudo/Cherrytale-tool-One`
> 本清单只给步骤，**不代为执行**。等你整理完毕、明确说"可以发"再照做。

---

## 一、为什么要删掉重建

旧仓库里那批敏感信息**不在工作区、也不在最新提交里**（这两处 2026-10-07 已清干净），
但它们**永久留在 Git 历史对象中**：

| 残留内容 | 所在历史提交 |
|---|---|
| 明文密码 + 邮箱 | `e23d14f`（`tools/extract_saz_posts.py:17`） |
| 真实平台 userId（可用作登录凭据） | `e23d14f`、`5f4a724`、`80a60b7` |
| 明文邮箱 `trbpx1859@9662.com` | `0e43d61` |
| 提交元数据里的邮箱 | **全部 10 个提交** |

**关键认知**：改文件、甚至 `git rebase` 都清不干净 —— 只要推过一次，
旧对象就留在 GitHub 服务器上直到其垃圾回收（不可控）。
**删除重建是唯一彻底的解法**，而这正好和"发正式版重新开始"的目标重合。

> `git filter-repo` / `filter-branch` 也能做，但和重建相比没有优势，
> 反而要额外装工具、还要 `push --force`，不如直接换新仓库干净。

---

## 二、删之前的检查清单（逐项打勾）

- [ ] **确认本地有完整备份**
      ```bash
      cd "C:/Users/Anqi Liu/Desktop"
      git clone --mirror "Cherrytale tool One" Cherrytale-mirror-backup-20261007
      ```
      镜像克隆会连**全部历史**一起备份。将来万一想翻旧提交还能翻。

- [ ] **确认工作区改动已提交**（当前有 36 个文件改动 + 2 个未跟踪笔记）

- [ ] **确认旧仓库没有你还需要的 Issue / Release / Wiki**
      有的话先导出，删掉就没了。

- [ ] **确认没有协作者**（有的话删仓库会通知他们）

- [ ] **记录当前远程地址**（重建后要重新指向）
      ```
      origin  https://github.com/laqlaq401-sudo/Cherrytale-tool-One.git
      ```

---

## 三、删除旧仓库

装好 `gh` 并登录后（见第五节），一条命令：

```bash
gh repo delete laqlaq401-sudo/Cherrytale-tool-One --yes
```

或走网页：`Settings` → 拉到底 `Danger Zone` → `Delete this repository`
→ 输入仓库全名确认。

> ⚠️ **删除不可回滚**。删完后 URL 立刻失效，`git push` 会 404。
> 所以第二节的镜像备份务必先做完。

---

## 四、重建干净仓库

### 4.1 新建空仓库（不要勾 README / .gitignore / License）

```bash
gh repo create laqlaq401-sudo/Cherrytale-tool-One --private --description "Cherrytale 米娅小助手"
```

### 4.2 先确认本地历史干净，再决定推什么

**方案 A —— 只推当前状态（推荐，历史彻底干净）**

```bash
cd "C:/Users/Anqi Liu/Desktop/Cherrytale tool One"

# 先自查一遍（必须为空输出）
git grep -nE "13167943|13170572|8176402|6255757|13394129|6138759|ER3bf75cca|ER23f20a95|trbpx1859|1009885373|13579qetuo" -- . || echo "✅ 工作区干净"

# 把现有历史归档到本地分支（不推送），再基于当前状态开一条全新历史
git branch legacy-history-archive        # 保住旧历史，万一要查
git checkout --orphan main-clean         # 无父提交的新分支
git add -A
git commit -m "chore: 正式版基线"
git branch -M main-clean main            # 覆盖本地 main
git push -u origin main
```

这样**新仓库只有 1 个提交**，旧历史不会上传。

> ⚠️ 但注意：`git branch -M` 只是让 `main` 指向新提交，**旧对象仍在本地 `.git` 里**
> （`legacy-history-archive` 分支还指着它们）。若要让本地也彻底干净：
> ```bash
> git branch -D legacy-history-archive
> git reflog expire --expire=now --all
> git gc --prune=now --aggressive
> ```
> 想留个后路就先做完第二节的镜像备份，再执行这几条。

**方案 B —— 推完整历史**

不推荐：`e23d14f` 那批残留会原样进新仓库，等于没删。

### 4.3 修正提交邮箱（新仓库的第一条提交开始生效）

```bash
git config user.name "你的显示名"
git config user.email "你想公开的邮箱"
```

> 电子邮件一旦进了提交就改不掉（除非重写该提交）。
> 新仓库第一条提交之前设好，后面就都是对的。
> 想彻底不暴露邮箱，可在 GitHub 设置里用 `<id>+<用户名>@users.noreply.github.com`。

---

## 五、gh 安装与登录（已完成一半）

- [x] **安装** —— 已通过 winget 装好：`C:\Program Files\GitHub CLI\gh.exe`（v2.102.0）
- [ ] **登录** —— 需你亲自完成（浏览器授权，我无法代劳）：

```bash
"C:\Program Files\GitHub CLI\gh.exe" auth login --hostname github.com --git-protocol https --web
```

终端会显示一个 8 位一次性验证码，浏览器打开后粘贴并授权。
完成后 `gh auth status` 应显示 `Logged in to github.com`。

需要 `delete_repo` 权限，登录时选好 scope，或之后补：

```bash
gh auth refresh -h github.com -s delete_repo
```

---

## 六、发布前的最后一道闸

推正式版**之前**，再跑一次终检（本地全仓库扫描）：

```bash
cd "C:/Users/Anqi Liu/Desktop/Cherrytale tool One"

echo "=== 1. 已跟踪文件中的敏感串 ==="
git ls-files -z | xargs -0 grep -nE "13579qetuo|1009885373|ER3bf75cca|ER23f20a95|trbpx1859|13167943|13170572|8176402|6255757|13394129|6138759"

echo "=== 2. 确认凭据文件未被跟踪 ==="
git ls-files | grep -E "\.auth_token|\.session\.json|\.platform_accounts|config_local|\.platform_credentials" || echo "✅ 无凭据文件入库"

echo "=== 3. 确认无「已跟踪却被 ignore」 ==="
git ls-files -i -c --exclude-standard || echo "✅ 无冲突"

echo "=== 4. 待推送内容自检 ==="
git log origin/main..HEAD --oneline
```

四条全绿再推。

---

## 七、长期习惯（避免重蹈覆辙）

1. **提交前先扫**：把第六节第 1 条存成 `tools/pre_commit_scan.sh`，
   或写进 git hook，别再依赖"我记得检查"。
2. **凭据永远不进仓库**：`.platform_accounts.json` / `.session.json` / `config_local.py`
   已经都被 `.gitignore` 挡住了，别再 `git add -f`。
3. **`notes/recon_findings.md`** 这类逆向笔记已忽略，
   但 `.gitignore` 漏一个就会漏一整份 —— 新增笔记时顺手确认。
4. **发 GitHub 前想一遍**：这个仓库将来会公开吗？
   会公开的话，提交邮箱、代码里的测试值都要按"公开"标准来。
