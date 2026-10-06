/* ============================================================
   米娅小助手 · 两页共享工具库（原生 JS，无框架、无构建）
   ------------------------------------------------------------
   【为什么存在这个文件（2026-09-30）】
   登录/选区拆到 login.html 之后，有两页各自都要用的东西：
   fetch 封装（令牌头 + 错误负载解析）、DOM 工具、防抖、错误转人话。
   这类"协议性"代码若复制两份，迟早出现"一边改了令牌头、另一边忘了"的漂移
   —— 所以抽成唯一的公共库，挂在 window.Ct 命名空间下。

   【它不做任何事】：没有自动执行的逻辑、不碰 DOM、不发请求；
   只有页面各自的 login.js / app.js 调用这些工具时才会动。

   【安全约定（与旧版一致）】
   - 令牌只进 sessionStorage（关标签页即失效），不写 localStorage；
   - URL 上的 ?token= 读到后立刻从地址栏抹掉（防截图/历史记录留存）。
   ============================================================ */
window.Ct = (() => {
  "use strict";

  const STORAGE_KEYS = {
    token: "cherrytale.token",
    tab: "cherrytale.tab",
    selection: "cherrytale.selection.v2",
    playerName: "cherrytale.playerName",
    // 演练模式开关（true = 只组装不发包）。存本地是为了**记住用户的选择**。
    dryRun: "cherrytale.dryRun",
    // ★ 自动任务的"今天跑过哪些"记录。
    // 结构：{ game_day, server_id, player_id, tasks: [...] }。
    // game_day 由**后端**按"游戏服务器每天 05:00 换日"的口径给出
    // （见 models/daily_reset.py）；前端不自己算日期。
    autoRun: "cherrytale.autoRun",
    // 底部日志坞（2026-10-01）："1"/"0" = 是否在底部悬浮运行日志；
    // 高度 = 像素数字符串（resizer 拖拽后记住）。都只是界面偏好。
    bottomLog: "cherrytale.bottomLog",
    bottomLogH: "cherrytale.bottomLogH",
    // 外观主题（2026-10-03）："light" / "dark"，themeAuto: "true" / "false"
    theme: "cherrytale.theme",
    themeAuto: "cherrytale.themeAuto",
    // 钻石使用总闸（2026-10-05）："true"/"false"。
    // 【为什么要持久化】开关以前只读勾选框的当前值、从不保存，于是每次
    // 重新登录（页面重载）勾选框都回到"关闭"——用户明确要求"开了就一直是开的，
    // 关了就一直关着，不要下次登录又把它关掉"。与 dryRun 同样是**记住用户的选择**。
    // 【为什么可以只存本地】它只是"这次运行要不要允许花钱"的入参，真正的
    // fail-closed 判据仍在后端 SpendPolicy（config.ALLOW_DIAMOND_SPEND）——
    // 存坏了顶多是"回到默认关闭"，永远落在安全一侧。
    allowDiamond: "cherrytale.allowDiamond",
  };

  //: 玩家 / 角色头像的统一占位图（★ 2026-10-06 用户拍板）。
  //:
  //: 【它服务谁】两类人：① **从没设过自定义头像**的玩家（登录 1004 的
  //: ``PlayerClass.icon = 0``，真实编号在 ``roleMainID`` 里且指向
  //: ``[頭像]預設``）；② **有编号但图读不出来**的玩家。这两类人
  //: **都看这张自制图** —— 刻意**不引入**游戏官方的默认剪影（``a000_01``），
  //: 那张图只在冷存目录、且"官方默认头像"与"我们的占位图"语义不同。
  //:
  //: 【为什么叫 000h】与角色头像用**同一张**（``web/assets/avatars/000h.png``），
  //: 命名保持 ``[a-z]+\d+h`` 同构，使"后端给的 code 要么为空、要么必有同名
  //: 文件"这条守卫继续成立。后端 ``services/avatar_service.py`` 的
  //: ``PLACEHOLDER_ROLE_AVATAR`` 与本值**必须一致**（有测试钉住）。
  const AVATAR_PLACEHOLDER = "assets/avatars/000h.png";

  const $ = (id) => document.getElementById(id);

  /** 建元素：`el("div", {class: "x"}, ["文本", 子元素])`。 */
  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2), value);
      } else if (value !== null && value !== undefined && value !== false) {
        node.setAttribute(key, value === true ? "" : String(value));
      }
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined) continue;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    }
    return node;
  }

  /**
   * 防抖：连续触发时只在停顿后执行最后一次。
   * 用于选区搜索 —— 167 个区服不该跟着每一次键入整体重建。
   */
  function debounce(fn, delay = 120) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), delay);
    };
  }

  /** 从错误响应里取出人能看懂的一句话（后端把问题清单放在 detail.problems）。 */
  function describeError(error) {
    const payload = error && error.payload;
    const detail = payload && payload.detail;
    if (detail && Array.isArray(detail.problems)) return detail.problems.join("\n");
    if (typeof detail === "string") return detail;
    if (detail) return JSON.stringify(detail);
    return (error && error.message) || "请求失败";
  }

  /** 千分位显示（金币动辄几千万，不分组根本读不出数量级）。 */
  function formatNumber(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    return number.toLocaleString("zh-CN");
  }

  // ------------------------------------------------------------------
  // 访问令牌：模块内单例。令牌头只走请求头，不进 URL、不进 localStorage。
  // ------------------------------------------------------------------
  let token = "";

  function tokenFromUrl() {
    return (new URLSearchParams(location.search).get("token") || "").trim();
  }

  /** 启动时调用一次：优先 URL 的 ?token=（手机点链接最方便），其次会话存储。 */
  function initToken() {
    const fromUrl = tokenFromUrl();
    token = fromUrl || sessionStorage.getItem(STORAGE_KEYS.token) || "";
    if (fromUrl) {
      sessionStorage.setItem(STORAGE_KEYS.token, fromUrl);
      // 立刻从地址栏抹掉：避免被截图或被浏览器历史留存
      history.replaceState(null, "", location.pathname);
    }
    return token;
  }

  function getToken() {
    return token;
  }

  function saveToken(value) {
    token = value.trim();
    if (token) sessionStorage.setItem(STORAGE_KEYS.token, token);
    else sessionStorage.removeItem(STORAGE_KEYS.token);
  }

  /** 清令牌并回登录页（401 时调用；登录页负责让用户重新填）。 */
  function clearToken() {
    token = "";
    sessionStorage.removeItem(STORAGE_KEYS.token);
  }

  function getApiOrigin() {
    // 优先允许通过 URL 参数覆盖 API 地址，如 ?api=http://127.0.0.1:8765
    const override = new URLSearchParams(location.search).get("api");
    if (override) return override.replace(/\/+$/, "");

    // 若通过 VS Code Live Preview (通常 3000 端口) 或其他非后端端口的静态服务器打开，
    // 自动重定向到 web_server 的 8765 端口。
    // 【8000 是安卓端】Chaquopy 封包后 WebView 从 app 内置服务器的 8000 端口
    // 打开页面（见 MainActivity.kt），页面与 API 同源，必须原样使用 ——
    // 否则 fetch 会被指到 8765 而全部 "Failed to fetch"（2026-10-02 封包踩坑）。
    const BACKEND_PORTS = new Set(["8765", "8000"]);
    if (location.port && !BACKEND_PORTS.has(location.port)) {
      const host = location.hostname || "127.0.0.1";
      return `${location.protocol}//${host}:8765`;
    }
    return location.origin;
  }

  /** 统一的后端调用：拼 URL 参数、带头、解析错误负载为带 status 的 Error。 */
  async function call(path, { method = "GET", body = null, params = null } = {}) {
    const url = new URL(path, getApiOrigin());
    if (params) {
      for (const [key, value] of Object.entries(params)) {
        url.searchParams.set(key, String(value));
      }
    }
    const headers = {};
    if (token) headers["X-Auth-Token"] = token; // 令牌只走请求头
    if (body !== null) headers["Content-Type"] = "application/json";

    const response = await fetch(url, {
      method,
      headers,
      body: body === null ? null : JSON.stringify(body),
    });

    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = { detail: text };
    }

    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  // ------------------------------------------------------------------
  // 自绘对话框（2026-10-02）
  // ------------------------------------------------------------------
  // 【为什么不用原生 alert / confirm】
  // Android WebView 在页面没有 WebChromeClient 时会**静默丢弃**这两个调用：
  // confirm 直接返回 false（等于"用户永远点取消"），alert 什么都不显示。
  // 手机端的真实症状就是"选定关卡点执行没反应" —— 真实扫荡前的二次确认
  // 被系统吞掉，前端以为用户点了取消。自绘一层暗色对话框后任何
  // WebView / 浏览器行为都一致；App 侧另行补了 WebChromeClient 兜底。

  const DIALOG_STYLE_ID = "ct-dialog-style";

  function ensureDialogStyle() {
    if (document.getElementById(DIALOG_STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = DIALOG_STYLE_ID;
    style.textContent = [
      ".ct-dialog-mask{position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.62);",
      "display:flex;align-items:center;justify-content:center;padding:24px;}",
      ".ct-dialog{max-width:420px;width:100%;background:#111827;color:#e5e7eb;border:1px solid #374151;",
      "border-radius:14px;padding:20px 22px;box-shadow:0 18px 50px rgba(0,0,0,.55);",
      "font-size:14px;line-height:1.65;white-space:pre-wrap;word-break:break-word;}",
      ".ct-dialog-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px;}",
      ".ct-dialog-actions button{border:1px solid #4b5563;border-radius:9px;background:#1f2937;",
      "color:#e5e7eb;padding:8px 18px;font-size:14px;cursor:pointer;}",
      ".ct-dialog-actions button.ct-dialog-primary{background:#3b82f6;border-color:#3b82f6;color:#fff;}",
      ".ct-dialog-actions button.ct-dialog-primary:hover{background:#2563eb;}",
    ].join("");
    document.head.appendChild(style);
  }

  function openDialog(message, confirmMode) {
    ensureDialogStyle();
    return new Promise((resolve) => {
      const previous = document.activeElement;
      const mask = document.createElement("div");
      mask.className = "ct-dialog-mask";
      const card = document.createElement("div");
      card.className = "ct-dialog";
      card.textContent = message;
      const actions = document.createElement("div");
      actions.className = "ct-dialog-actions";
      const onKey = (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          close(false);
        } else if (event.key === "Enter") {
          event.preventDefault();
          close(true);
        }
      };
      const close = (result) => {
        mask.remove();
        document.removeEventListener("keydown", onKey, true);
        if (previous && previous.focus) previous.focus();
        resolve(result);
      };
      document.addEventListener("keydown", onKey, true);
      if (confirmMode) {
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.textContent = "取消";
        cancel.addEventListener("click", () => close(false));
        actions.appendChild(cancel);
      }
      const primary = document.createElement("button");
      primary.type = "button";
      primary.className = "ct-dialog-primary";
      primary.textContent = confirmMode ? "确定执行" : "知道了";
      primary.addEventListener("click", () => close(true));
      actions.appendChild(primary);
      card.appendChild(actions);
      mask.appendChild(card);
      document.body.appendChild(mask);
      primary.focus();
    });
  }

  /** 替代原生 alert：Promise 化，任何 WebView 都能显示。 */
  function uiAlert(message) {
    return openDialog(String(message), false);
  }

  /** 替代原生 confirm：resolve(true)=确认，resolve(false)=取消。 */
  function uiConfirm(message) {
    return openDialog(String(message), true);
  }

  // ------------------------------------------------------------------
  // 自绘下拉（★ 2026-10-05 从 app.js 上移到公共库，两页共用）
  // ------------------------------------------------------------------
  // 【为什么不用原生 <select>】
  // 手机上浏览器会**接管**原生 select，从屏幕底部弹出系统滚轮 / 全屏单选弹窗
  // （iOS 的 Picker、Android 的 Modal Sheet）。Android WebView 下尤其糟：
  // 长文本选项会**换行重叠成一团**、完全没法读，而且它与本应用的暗黑一体化
  // 界面彻底割裂（见用户提供的录屏）。
  //
  // 改成自绘下拉之后，PC 与手机是**同一套**交互：点一下在按钮下方原地展开，
  // 点选项即生效，点别处 / 按 Esc 收起。
  //
  // 【为什么放在 common.js 而不是 app.js】
  // 它原本只服务任务参数区（app.js）。但登录页的"已保存的账号"同样是原生
  // select、同样有那个丑陋弹框 —— 两页都要用，就只能放在两页都加载的公共库。
  // 样式见 ``web/assets/input.css`` 的 ``.custom-select*``，
  // 改动后需要重新构建 CSS：``npm run build:css``。

  //: 当前展开的下拉（同一时刻只允许一个展开）。挂在模块作用域里，
  //: 所以两页共用同一份"同时只开一个"的约束。
  let openDropdown = null;

  /** 收起当前展开的下拉（没有展开时是空操作）。 */
  function closeDropdown() {
    if (!openDropdown) return;
    openDropdown.menu.classList.add("hidden");
    openDropdown.trigger.setAttribute("aria-expanded", "false");
    openDropdown = null;
  }

  // 点空白处 / 按 Esc 收起：监听挂在 document 上，只需要注册一次。
  // 两个页面都会加载本文件，因此这段注册天然只跑一遍。
  document.addEventListener("click", (event) => {
    if (!openDropdown) return;
    if (openDropdown.root.contains(event.target)) return;
    closeDropdown();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDropdown();
  });

  /**
   * 构造一个自定义下拉。
   *
   * :param value: 当前选中的值（统一按字符串比较）。
   * :param options: ``[{ value, label, disabled, group }]``；``value === ""``
   *     通常表示"不指定"。给了 ``group`` 的选项会被归到同名分组标题下 ——
   *     登录页用它表达"已保存区服角色 / 平台账号"两类。
   * :param disabled: 整个下拉是否不可用（例如候选还没拉到时）。
   * :param emptyText: 触发器上显示的"没有候选"提示。
   * :param onChange: 选中的回调，收到字符串值。可以返回 Promise（内部不等待）。
   * :return: 可直接塞进任意容器的根元素。
   */
  function customSelect({
    value = "",
    options = [],
    disabled = false,
    emptyText = "（没有可选项）",
    onChange = null,
  } = {}) {
    const current = value === null || value === undefined ? "" : String(value);
    const trigger = el("button", {
      type: "button",
      class: "custom-select-trigger",
      "aria-haspopup": "listbox",
      "aria-expanded": "false",
    });
    const menu = el("div", { class: "custom-select-menu hidden", role: "listbox" });
    const root = el("div", { class: `custom-select${disabled ? " custom-select--disabled" : ""}` }, [
      trigger,
      menu,
    ]);
    const state = { root, trigger, menu, value: current, options, emptyText };

    /** 触发器上要显示的文案：优先找选项里的 label，找不到就用 emptyText。 */
    function labelOf(val) {
      const hit = options.find((option) => String(option.value) === val);
      return hit ? hit.label : emptyText;
    }

    function paint() {
      const text = labelOf(state.value);
      trigger.textContent = text;
      trigger.title = text;
      Array.from(menu.children).forEach((node) => {
        if (!node.classList.contains("custom-select-option")) return;
        node.classList.toggle(
          "custom-select-option--on",
          node.getAttribute("data-value") === state.value
        );
      });
    }

    function setOpen(open) {
      if (disabled) return;
      if (!open) {
        closeDropdown();
        return;
      }
      closeDropdown(); // 同时只允许一个下拉展开
      // 检查下方空间：若距视口底部不足 240px 且上方更宽裕，则向上弹出
      const rect = trigger.getBoundingClientRect();
      const menuHeight = 240;
      const spaceBelow = window.innerHeight - rect.bottom;
      const openUp = spaceBelow < menuHeight && rect.top > menuHeight;
      menu.classList.toggle("custom-select-menu--up", openUp);
      menu.classList.remove("hidden");
      trigger.setAttribute("aria-expanded", "true");
      openDropdown = state;
    }

    if (!disabled) {
      trigger.addEventListener("click", () => {
        const isOpen = openDropdown === state;
        setOpen(!isOpen);
      });
    } else {
      trigger.setAttribute("aria-disabled", "true");
    }

    // 分组标题（只画一次；同名 group 连续出现时共用同一个标题）
    let lastGroup = null;
    for (const option of options) {
      const group = option.group || "";
      if (group && group !== lastGroup) {
        menu.appendChild(el("div", { class: "custom-select-group", text: group }));
      }
      lastGroup = group;

      const optionNode = el("button", {
        type: "button",
        class: "custom-select-option",
        "data-value": String(option.value),
        role: "option",
        text: option.label,
      });
      if (option.disabled) {
        optionNode.setAttribute("aria-disabled", "true");
      } else {
        optionNode.addEventListener("click", () => {
          state.value = String(option.value);
          paint();
          closeDropdown();
          if (typeof onChange === "function") onChange(state.value);
        });
      }
      menu.appendChild(optionNode);
    }

    paint();
    return root;
  }

  return {
    STORAGE_KEYS,
    AVATAR_PLACEHOLDER,
    $,
    el,
    debounce,
    describeError,
    formatNumber,
    call,
    initToken,
    getToken,
    saveToken,
    clearToken,
    uiAlert,
    uiConfirm,
    customSelect,
  };
})();
