/* ============================================================
   米娅小助手 · 登录门面页逻辑（login.html 专用）
   ------------------------------------------------------------
   【职责边界】这一页只做"门"的两步：
       ① 登录（账号库免密 / 账号密码）──→ ② 选区 ──进入游戏──→ 跳转 /（任务界面）
   任务执行、运行进度、日志都在 index.html（app.js）——
   两页共享的工具（call/el/令牌管理…）来自 common.js 的 window.Ct。

   【为什么跳转而不是原地渲染任务界面】
   登录页与任务页的信息密度完全不同（门要窄、控制台要宽）；
   一次真实的页面跳转让 sessionStorage 里的令牌/角色名天然成为
   两页之间的"交接单"，不需要任何额外的同步逻辑。
   ============================================================ */
(() => {
  "use strict";

  // ★ 2026-10-06：``debounce`` 已从解构里摘掉 —— 它原本只服务选区搜索框的
  //   input 防抖，搜索框删除后本页再无使用点。（它仍留在 common.js 里，
  //   那是两页共享库，app.js 还在用。）
  const {
    $,
    el,
    call,
    describeError,
    formatNumber,
    STORAGE_KEYS,
    AVATAR_PLACEHOLDER,
  } = window.Ct;

  const state = {
    accounts: [],
    zoneAccounts: [],
    platformAccounts: [],
    servers: [],
    selectedServerId: null,
    //: 是否"只看有角色的区"（★ 2026-10-06 起默认 true，由 loginAndLoadServers
    //: 按账号实况决定初值；改选后由 checkbox 的 change 事件写回）。
    onlyCharacter: false,
    //: 当前选中的账号键（``zone:<user_id>:<server_id>`` / ``platform:<user_id>``）。
    //: 它是"选了哪个账号"的唯一真相来源（账号选择控件历经
    //: 原生 select → 自绘下拉 → 卡列表三次换代，取值代码始终只认这个键）。
    selectedKey: "",
  };

  const SCREEN_IDS = {
    login: "gate-login",
    server: "gate-server",
  };

  /** 每个步骤对应的浏览器标签页标题（"我在哪"的第二个提示位）。 */
  const TAB_TITLES = {
    login: "登录",
    server: "选择区服",
  };

  /** 门内两步的切换：登录 ↔ 选区。
   *  同时联动两处"我在哪"的提示：步骤指示条高亮 + 浏览器标签页标题。 */
  function setGateScreen(name) {
    for (const [key, id] of Object.entries(SCREEN_IDS)) {
      $(id).classList.toggle("hidden", key !== name);
    }
    syncGateSteps(name);
    document.title = `${TAB_TITLES[name] || "登录"} · 米娅小助手`;
    window.scrollTo({ top: 0 });
  }

  /** 步骤指示条联动：已走过的一步涂成"完成"，当前一步是唯一的强调点。 */
  function syncGateSteps(name) {
    const list = $("gate-steps");
    if (!list) return; // 老样式表/缓存里没有指示条时静默跳过，不影响登录
    const order = Object.keys(SCREEN_IDS); // ["login", "server"]
    const current = order.indexOf(name);
    for (const node of list.querySelectorAll(".gate-step")) {
      const index = order.indexOf(node.dataset.step);
      node.classList.toggle("gate-step--done", index > -1 && index < current);
      node.classList.toggle("gate-step--on", index === current);
    }
  }

  // 登录页的连接状态点（2026-10-06 改版）：与任务页顶栏同一套 —— 节点上不再
  // 渲染可见文字，只靠颜色判断（灰 = 正在连接、红 = 连接失败、绿 = 连接成功）。
  // 文字转到 title / aria-label，色盲用户与读屏仍有非视觉通道；
  // 状态同时挂 data-state，便于将来加提示或埋点。
  // ⚠️ 必须与 app.js::setConn 保持一致 —— 两页共用 .conn 的 CSS。
  function setConn(kind, text) {
    const node = $("conn");
    node.className = `conn conn--${kind}`;
    node.dataset.state = kind;
    const label = text || "连接状态";
    node.title = label;
    node.setAttribute("aria-label", label);
    node.textContent = ""; // 防御「旧 HTML 缓存 + 新 JS」混搭，清空是幂等的
  }

  // ------------------------------------------------------------------
  // 访问令牌（服务端 --token 启动时才需要）
  // ------------------------------------------------------------------
  function showTokenCard(message) {
    $("token-card").classList.remove("hidden");
    if (message) {
      $("token-error").textContent = message;
      $("token-error").classList.remove("hidden");
    }
  }

  // ------------------------------------------------------------------
  // 启动：确认后端可达 + 是否需要令牌
  // ------------------------------------------------------------------
  async function loadHealth() {
    const payload = await call("/api/health");
    $("app-version").textContent = payload.app_version ? `v${payload.app_version}` : "";
    if (payload.auth_required && !window.Ct.getToken()) {
      showTokenCard("这个服务启用了访问令牌，请填入后继续。");
    }
    return payload;
  }

  // ------------------------------------------------------------------
  // 步骤 ①：登录与账号选择（直进区服 / 免密选区）
  // ------------------------------------------------------------------

  /**
   * 把"已保存的账号"渲染成**账号卡列表**（★ 2026-10-05，替代自绘下拉）。
   *
   * 【为什么换代】
   * 上一代自绘下拉在 360px 手机视口上撑爆了整页：触发器里一行不换行的
   * "邮箱 ── 区服 ── 角色 ── 等级"会把网格最小宽度顶到视口之外（整页左右被裁）。
   * 卡列表把同一份信息拆行呈现，每行都短且各自截断，天然装得下；
   * 点击选中、主按钮统一「登录」，视觉与选区步的 server-card 同源。
   *
   * 【卡片构成】
   *   区服角色卡：头像=角色名首字；主行=角色名+Lv 徽章；
   *              副行=区服（编号 · 名字）与邮箱两行小字。
   *   平台账号卡：主行=邮箱；副行=用途说明（免密进新区 / 选区）。
   */
  function renderAccountCards() {
    const mount = $("login-accounts-mount");
    mount.textContent = "";

    const totalItems = state.zoneAccounts.length + state.platformAccounts.length;
    if (totalItems === 0) {
      state.selectedKey = "";
      mount.appendChild(
        el("p", {
          class: "empty",
          text: "账号库为空：用下方账号密码登录一次，成功后会自动记入账号库",
        })
      );
      return;
    }

    // 账号库换了一批（刷新 / 重新登录）→ 旧选中项可能已不存在，回退到默认第一个
    const keys = new Set([
      ...state.zoneAccounts.map((item) => item.key),
      ...state.platformAccounts.map((item) => item.key),
    ]);
    if (!keys.has(state.selectedKey)) {
      state.selectedKey =
        (state.zoneAccounts[0] && state.zoneAccounts[0].key) ||
        (state.platformAccounts[0] && state.platformAccounts[0].key) ||
        "";
    }

    const list = el("div", { class: "acct-list" });
    for (const item of state.zoneAccounts) {
      list.appendChild(accountCard(item, "zone"));
    }
    for (const item of state.platformAccounts) {
      list.appendChild(accountCard(item, "platform"));
    }
    mount.appendChild(list);
  }

  /** 单张账号卡：点击选中（高亮 + ✓），登录动作统一交给 #btn-login-saved。
   *
   *  ★ 2026-10-05：区服角色卡渲染**真头像**（登录 1004 抓的编号 →
   *  services/avatar_service.py 解析成 data-url，随 /api/accounts 下发）。
   *  ★ 2026-10-06：没图时改为回退**自制占位图**（原来回退昵称首字）——
   *  用户拍板：没选头像的人与读取失败的人**一律看这张图**。
   *  平台账号卡没有游戏头像，仍是 🔑。 */
  function accountCard(item, kind) {
    const selected = item.key === state.selectedKey;
    const avatarEl =
      kind === "zone"
        ? el("span", { class: "acct-avatar" }, [
            el("img", {
              src: item.avatar_data_url || AVATAR_PLACEHOLDER,
              alt: "",
              "aria-hidden": "true",
            }),
          ])
        : el("span", {
            class: "acct-avatar acct-avatar--key",
            text: "🔑",
            "aria-hidden": "true",
          });
    const node = el(
      "button",
      { type: "button", class: `acct-card${selected ? " acct-card--on" : ""}` },
      [
        avatarEl,
        el("span", { class: "acct-body" }, [
          kind === "zone"
            ? el("span", { class: "acct-main" }, [
                el("span", { text: item.player_name || "角色" }),
                item.character_level > 0
                  ? el("span", { class: "badge badge--ok", text: `Lv.${item.character_level}` })
                  : null,
              ])
            : el("span", { class: "acct-main", text: item.account || item.user_id }),
          kind === "zone"
            ? el("span", {
                class: "acct-sub",
                text: `${item.server_id}区 · ${item.server_name}`,
              })
            : el("span", { class: "acct-sub", text: "平台账号 · 免密进新区 / 选区" }),
          kind === "zone"
            ? el("span", { class: "acct-sub acct-sub--dim", text: item.account || item.user_id })
            : null,
        ]),
        el("span", { class: "acct-check", "aria-hidden": "true", text: "✓" }),
      ]
    );
    node.addEventListener("click", () => {
      state.selectedKey = item.key;
      renderAccountCards();
      updateSavedAccountUI();
    });
    return node;
  }

  async function loadAccounts() {
    const mount = $("login-accounts-mount");
    try {
      const payload = await call("/api/accounts");
      if (payload.error) {
        state.zoneAccounts = [];
        state.platformAccounts = [];
        state.selectedKey = "";
        mount.textContent = "";
        mount.appendChild(el("p", { class: "error", text: `账号库不可用：${payload.error}` }));
        updateSavedAccountUI();
        return;
      }
      state.accounts = payload.accounts || [];
      state.zoneAccounts = payload.zone_accounts || [];
      state.platformAccounts = payload.platform_accounts || [];
      renderAccountCards();
      updateSavedAccountUI();
    } catch (error) {
      state.zoneAccounts = [];
      state.platformAccounts = [];
      state.selectedKey = "";
      mount.textContent = "";
      mount.appendChild(
        el("p", { class: "error", text: `读取账号库失败：${describeError(error)}` })
      );
      updateSavedAccountUI();
    }
  }

  /** 联动更新主按钮：卡片上已把"登的是谁"说清楚，按钮只留一个「登录」。
   *  （旧版把角色名/邮箱拼进按钮文案，正是手机端横向溢出的元凶之一。） */
  function updateSavedAccountUI() {
    const btn = $("btn-login-saved");
    btn.textContent = "登录";
    btn.className = "btn btn--primary";
    btn.disabled = !state.selectedKey;
  }

  function setLoginBusy(busy) {
    $("login-busy").classList.toggle("hidden", !busy);
    const saved = $("btn-login-saved");
    const password = $("btn-login-password");
    saved.disabled = busy;
    password.disabled = busy;
    // 视觉上让"正在做事"的那颗按钮自己转圈：哪个禁用就绪就给哪个加 loading
    // （按钮文案由 updateSavedAccountUI / 各自的处理器负责，这里不覆盖）
    saved.classList.toggle("btn--loading", busy && !saved.classList.contains("hidden"));
    password.classList.toggle("btn--loading", busy);
  }

  function showLoginError(message) {
    const box = $("login-error");
    box.textContent = message || "";
    box.classList.toggle("hidden", !message);
  }

  /** 登录 → 拿区服列表 → 切到选区步。 */
  async function loginAndLoadServers(payload) {
    setLoginBusy(true);
    showLoginError("");
    try {
      const result = await call("/api/login/servers", { method: "POST", body: payload });
      state.servers = result.servers || [];
      // 默认选中"上次进的区"，其次"按规则会进的区"（都能减少进错区的概率）
      state.selectedServerId =
        result.current_server_id || result.suggested_server_id || null;

      // ★ 2026-10-06：默认「只看有角色的区」。
      //   但**全新账号可能一个角色都没有** —— 那时默认打开会把列表清空，
      //   用户面对一片空白却不知道要关哪个开关。所以这里按实际情况决定初值：
      //   有角色才默认打开，没角色就显示全部（并让 renderServers 把原因说出来）。
      state.onlyCharacter = state.servers.some((item) => item.has_character);
      $("server-only-character").checked = state.onlyCharacter;
      renderServers();
      setGateScreen("server");
      setConn("ok", "已连接");
    } catch (error) {
      if (error.status === 401) {
        setConn("bad", "需要令牌");
        showTokenCard(describeError(error));
        return;
      }
      showLoginError(describeError(error));
    } finally {
      setLoginBusy(false);
    }
  }

  /** 一键直达已保存的区服凭据进入游戏 */
  async function doDirectEnterZone(userId, serverId) {
    setLoginBusy(true);
    showLoginError("");
    try {
      const payload = {
        account: userId,
        server_id: serverId,
        direct: true,
      };
      const result = await call("/api/login/enter", { method: "POST", body: payload });
      if (result.player && result.player.name) {
        sessionStorage.setItem(STORAGE_KEYS.playerName, result.player.name);
      }
      setConn("ok", "已连接");
      location.href = "index.html" + location.search;
    } catch (error) {
      if (error.status === 401) {
        setConn("bad", "需要令牌");
        showTokenCard(describeError(error));
        return;
      }
      showLoginError(`进入游戏失败：${describeError(error)}`);
    } finally {
      setLoginBusy(false);
    }
  }

  function handleSavedAccountAction() {
    const selector = state.selectedKey;
    if (!selector) {
      showLoginError("请先选择一个已保存的账号（想用密码登录就展开下面的「首次登录 / 换号」）");
      return;
    }

    if (selector.startsWith("platform:")) {
      // 纯平台账号：免密获取区服列表，进入 Step 2（选区）
      const userId = selector.slice("platform:".length);
      loginAndLoadServers({ account: userId });
    } else if (selector.startsWith("zone:")) {
      // 区服角色：一键直达！
      const parts = selector.split(":");
      const userId = parts[1];
      const serverId = parseInt(parts[2], 10);
      doDirectEnterZone(userId, serverId);
    } else {
      // 兼容直接填序号或邮箱的情况
      loginAndLoadServers({ account: selector });
    }
  }

  function loadServersWithPassword() {
    const email = $("login-email").value.trim();
    const password = $("login-password").value;
    if (!email || !password) {
      showLoginError("账号与密码都要填");
      return;
    }
    loginAndLoadServers({ credentials: { email, password } });
  }

  // ------------------------------------------------------------------
  // 步骤 ②：选区
  // ------------------------------------------------------------------
  /** 按当前开关过滤区服。★ 2026-10-06：关键字搜索已删除 ——
   *  默认"只看有角色的区"后候选通常只剩二三十条，滚动成本低于打字成本。 */
  function filteredServers() {
    if (!state.onlyCharacter) return state.servers;
    return state.servers.filter((server) => server.has_character);
  }

  function renderServers() {
    const list = $("server-list");
    list.textContent = "";
    const items = filteredServers();
    const withCharacter = state.servers.filter((item) => item.has_character).length;

    // 数量汇总只留一行（原 .hero 副标题已并入这里）：
    // 开关打开时是"有角色的 N 个 / 全部 M 个"，关掉时直接说全量。
    $("server-count").textContent = state.onlyCharacter
      ? `共 ${state.servers.length} 个区服，${withCharacter} 个已有角色`
      : `共 ${state.servers.length} 个区服（已展开全部，含未创建角色）`;

    if (items.length === 0) {
      // 唯一可能的空列表成因：这个账号一个角色都没有（关键字搜索已删除）。
      list.appendChild(
        el("p", {
          class: "empty muted",
          text: "这个账号还没有任何角色。关掉「只看有角色的区」可以看到全部区服。",
        })
      );
    }

    for (const server of items) {
      const selected = server.id === state.selectedServerId;
      const roleText = server.has_character && server.character_name
        ? `${server.character_name} Lv.${server.character_level || 1}`
        : (server.has_character ? "已有角色" : "");

      const metaParts = [];
      if (roleText) {
        metaParts.push(`👤 ${roleText}`);
      }
      metaParts.push(`分组 ${server.group_id}`);
      metaParts.push(`人气 ${formatNumber(server.people_count)}`);

      const card = el(
        "button",
        { type: "button", class: `server-card ${selected ? "server-card--on" : ""}` },
        [
          el("div", { class: "server-name" }, [
            el("span", { text: server.name || `区服 #${server.id}` }),
            el("span", { class: "badge", text: `#${server.id}` }),
            server.has_character
              ? el("span", { class: "badge badge--ok", text: roleText || "已有角色" })
              : null,
          ]),
          el("div", {
            class: "server-meta muted small",
            text: metaParts.join("　"),
          }),
        ]
      );
      card.addEventListener("click", () => selectServer(server.id));
      list.appendChild(card);
    }

    const chosen = state.servers.find((item) => item.id === state.selectedServerId);
    const button = $("btn-enter-server");
    button.disabled = !chosen;
    button.textContent = chosen ? `进入 #${chosen.id} ${chosen.name}` : "进入游戏";
  }

  function selectServer(serverId) {
    state.selectedServerId = serverId;
    renderServers();
  }

  /** 进入游戏：成功后会话已写在服务端 → 记下角色名，跳转任务界面。 */
  async function doEnterServer() {
    if (state.selectedServerId === null) return;
    const button = $("btn-enter-server");
    button.disabled = true;
    button.textContent = "进入中（会写入会话，约 2~4 秒）…";
    $("server-error").classList.add("hidden");

    try {
      const payload = { server_id: state.selectedServerId };
      let account = state.selectedKey;
      if (account.startsWith("platform:")) {
        account = account.slice("platform:".length);
      } else if (account.startsWith("zone:")) {
        account = account.split(":")[1];
      }
      const email = $("login-email").value.trim();
      const password = $("login-password").value;
      if (account) payload.account = account;
      else if (email && password) payload.credentials = { email, password };

      const result = await call("/api/login/enter", { method: "POST", body: payload });

      if (result.player && result.player.name) {
        // 角色名只在 1004 里出现，而它**不进会话文件**（那里只存令牌/playerId/区服）。
        // 所以这里在浏览器会话里记一份，任务界面（index.html）的顶栏与信息面板会读它。
        sessionStorage.setItem(STORAGE_KEYS.playerName, result.player.name);
      }
      $("login-password").value = ""; // 用过的密码立刻从界面上抹掉
      setConn("ok", "已连接");
      location.href = "index.html" + location.search; // 登录 + 选区完成 → 交给任务执行界面
    } catch (error) {
      const box = $("server-error");
      box.textContent = describeError(error);
      box.classList.remove("hidden");
    } finally {
      button.disabled = false;
      renderServers();
    }
  }

  // ------------------------------------------------------------------
  // 事件绑定与启动
  // ------------------------------------------------------------------
  function wireEvents() {
    $("btn-token-save").addEventListener("click", async () => {
      window.Ct.saveToken($("token-input").value);
      $("token-card").classList.add("hidden");
      await loadHealth();
    });

    // --- 步骤 ① ---
    $("btn-refresh-accounts").addEventListener("click", loadAccounts);
    // 账号卡列表在选中时由 accountCard 的 click 回调直接调
    // updateSavedAccountUI（见 renderAccountCards）。
    $("btn-login-saved").addEventListener("click", handleSavedAccountAction);
    $("btn-login-password").addEventListener("click", loadServersWithPassword);
    $("login-password").addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadServersWithPassword();
    });

    // --- 步骤 ② ---
    $("btn-back-login").addEventListener("click", () => setGateScreen("login"));
    $("btn-enter-server").addEventListener("click", doEnterServer);
    // ★ 2026-10-06：开关是唯一的筛选手段了（搜索框已删），
    //   所以它自己要把状态写进 state —— 否则 renderServers 无从知道用户改过。
    $("server-only-character").addEventListener("change", (event) => {
      state.onlyCharacter = event.target.checked;
      renderServers();
    });
  }

  function start() {
    wireEvents();
    window.Ct.initToken();
    setConn("wait", "正在连接…");
    loadHealth()
      .then(() => loadAccounts())
      .catch((error) => {
        if (error.status === 401) {
          setConn("bad", "需要令牌");
          showTokenCard("令牌缺失或不正确，请填入后重试。");
          return;
        }
        setConn("bad", "服务不可用");
        showLoginError(`无法连接后端：${describeError(error)}`);
      });
  }

  document.addEventListener("DOMContentLoaded", start);
})();
