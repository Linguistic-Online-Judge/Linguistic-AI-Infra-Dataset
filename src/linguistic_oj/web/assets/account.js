"use strict";

const ACCOUNT_PAGES = {
  login: ["邮箱登录", "使用邮箱和密码登录。", "登录"],
  register: ["注册学习账户", "输入邮箱后，我们将发送验证链接。打开邮件链接，再设置密码与公开昵称。", "发送验证邮件"],
  "forgot-password": ["找回密码", "输入注册邮箱，接收密码重置链接。", "发送重置邮件"],
  "verify-email": ["验证邮箱并设置账户", "设置密码与公开昵称后完成注册。完成后需自行登录，不会自动进入账户。", "完成邮箱验证"],
  "reset-password": ["设置新密码", "输入新密码以完成重置。完成后请使用邮箱和新密码重新登录。", "更新密码"],
  account: ["当前账户", "公开昵称用于排行榜。退出会清除当前页面的私人结果和草稿，不会删除已提交的评测。", "退出登录"],
};

let authChannel;

function clearAccount() {
  clearAdminState();
  state.accountSequence++;
  state.contextSequence++;
  state.runsSequence++;
  state.user = null;
  state.token = null;
  state.submitting = false;
  state.activeSubmission = null;
  state.attempts.clear();
  state.promptDrafts.clear();
  elements["student-prompt"].value = "";
  elements["prompt-error"].classList.add("is-hidden");
  state.runsLoading = false;
  state.runsCursor = null;
  state.authToken = null;
  elements["auth-form"].reset();
  if (state.authPage === "account") state.authPage = "login";
  elements["refresh-runs"].disabled = false;
  elements["runs-more"].disabled = false;
  stopPolling();
  renderEmptyResult();
  if (state.view === "result" && state.resultRouteId) loadResultRoute();
  renderRunsSignedOut();
  renderSession();
  renderAccountForm();
  updatePromptState();
}

function invalidateAccount(message) {
  state.authSequence++;
  state.authNeedsLogin = true;
  clearAccount();
  showAccountError(message);
  showToast(message);
}

async function revalidateSession() {
  if (state.authMode === "legacy") return;
  if (state.authBusy || state.authMode === "checking") { state.authRecheckPending = true; return; }
  if (!state.authConfig) return;
  if (state.sessionCheckSequence === state.authSequence) { state.authRecheckPending = true; return; }
  const sequence = ++state.authSequence;
  const account = state.accountSequence;
  state.sessionCheckSequence = sequence;
  suspendAdminRequests();
  const current = () => sequence === state.authSequence && account === state.accountSequence;
  try {
    const session = await apiRequest("/v1/auth/session");
    if (!current()) return;
    if (typeof session?.user?.user_id !== "string" || !session.user.user_id || !session.user.public_handle || !session.user.role || !session.expires_at) throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
    if (session.user.user_id !== state.user?.user_id) {
      if (!state.authNeedsLogin || state.user) invalidateAccount(errorMessage({ code: "AUTH_ACCOUNT_CHANGED" }));
      return;
    }
    // A same-account read must not cancel a valid submission, poll, or frozen retry.
    if (state.user.role !== session.user.role) clearAdminState();
    state.user = session.user;
    state.authMode = "cookie";
    finishAdminRevalidation();
    renderSession();
    renderAccountForm();
    updatePromptState();
  } catch (error) {
    if (!current()) return;
    if (error.status === 401) {
      if (state.user) invalidateAccount(errorMessage({ code: "AUTH_ACCOUNT_CHANGED" }));
    } else {
      state.authMode = "unavailable";
      invalidateAccount("暂时无法确认登录状态。本页私人内容已清除，请重新登录后继续。");
    }
  } finally {
    if (state.sessionCheckSequence === sequence) state.sessionCheckSequence = 0;
    if (state.authRecheckPending && !state.authBusy && state.authMode !== "checking") { state.authRecheckPending = false; revalidateSession(); }
  }
}

function acceptSession(payload) {
  if (typeof payload?.user?.user_id !== "string" || !payload.user.user_id || !payload.user.public_handle || !payload.user.role || !payload.expires_at) throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
  if (state.user && state.user.user_id !== payload.user.user_id) clearAccount();
  else { state.accountSequence++; state.runsSequence++; }
  clearAdminState();
  // Old-session requests are ignored; retain their frozen keys so retries stay idempotent.
  state.submitting = false;
  stopPolling();
  state.user = payload.user;
  state.authMode = "cookie";
  renderSession();
  updatePromptState();
  if (state.view === "runs") loadRuns(true);
  else if (state.view === "result" && state.resultRouteId) loadResultRoute();
}

async function bootstrapAuth() {
  if (state.authBusy) return;
  const sequence = ++state.authSequence;
  state.authMode = "checking";
  renderSession();
  renderAccountForm();
  try {
    let config;
    try { config = await apiRequest("/v1/auth/config"); }
    catch (error) {
      if (sequence !== state.authSequence) return;
      // A shipped deployment without the account API is the only Bearer fallback.
      if (error.status === 404) {
        state.authMode = "legacy";
        state.authConfig = null;
        if (state.authPage !== "account") state.authPage = "login";
        return;
      }
      throw error;
    }
    if (sequence !== state.authSequence) return;
    if (config?.enabled !== true || !["local", "email"].includes(config.mode) || !["local", "smtp"].includes(config.mail_delivery)) throw new APIRequestError(503, { code: "SERVICE_NOT_READY" });
    state.authConfig = config;
    try {
      const session = await apiRequest("/v1/auth/session");
      if (sequence !== state.authSequence) return;
      if (state.authRecheckPending) { state.authRecheckPending = false; return bootstrapAuth(); }
      if (!state.authNeedsLogin) acceptSession(session);
      else state.authMode = "cookie";
    } catch (error) {
      if (sequence !== state.authSequence) return;
      if (error.status !== 401) throw error;
      if (state.user) clearAccount();
      state.authMode = "cookie";
    }
  } catch (error) {
    if (sequence !== state.authSequence) return;
    if (state.user) clearAccount();
    state.authMode = "unavailable";
    showAccountError(errorMessage(error, "检查账户服务"));
  } finally {
    if (sequence === state.authSequence) {
      renderSession(); renderAccountForm(); updatePromptState();
      if (state.authRecheckPending) { state.authRecheckPending = false; revalidateSession(); }
    }
  }
}

function renderSession() {
  elements["session-button"].classList.toggle("is-connected", Boolean(state.user));
  elements["session-label"].textContent = state.user ? `@${state.user.public_handle}` : state.authMode === "checking" ? "正在检查账户…" : "登录 / 注册";
  elements["session-button"].title = state.user ? "查看账户与退出登录" : "打开账户窗口";
  renderAdminAccess();
  if (state.view === "admin" && canManageTeaching() && !adminState.items.length && !adminState.busy && !adminState.conflict) loadAdminList();
}

function showAccountError(message) {
  elements["auth-error"].textContent = message;
  elements["auth-error"].classList.remove("is-hidden");
}
function accountNotice(message) {
  elements["auth-notice"].textContent = message;
  elements["auth-notice"].classList.remove("is-hidden");
}

function renderAccountForm() {
  const page = state.authPage;
  const legacy = state.authMode === "legacy";
  const settingPassword = ["verify-email", "reset-password"].includes(page);
  const info = ACCOUNT_PAGES[page] || ACCOUNT_PAGES.login;
  elements["auth-title"].textContent = legacy && page !== "account" ? "连接旧版账户" : info[0];
  elements["auth-help"].textContent = legacy && page !== "account" ? "此部署未提供邮箱账户接口。请输入管理员签发的 Bearer 访问令牌；仅在当前页面内存中保存，刷新即清除。" : info[1];
  if (page === "account" && state.user) {
    const role = state.user.role === "admin" ? "管理员" : ["user", "student"].includes(state.user.role) ? "学习账户" : state.user.role || "旧版账户（未提供角色）";
    elements["auth-help"].textContent = `已登录 @${state.user.public_handle} · ${role}。${info[1]}`;
  }
  const fields = {
    email: !legacy && ["login", "register", "forgot-password"].includes(page),
    password: !legacy && (page === "login" || settingPassword),
    handle: !legacy && page === "verify-email",
    token: legacy && page !== "account",
  };
  for (const [name, visible] of Object.entries(fields)) {
    elements[`auth-${name}-field`].classList.toggle("is-hidden", !visible);
    const input = elements[name === "token" ? "access-token" : `auth-${name}`];
    input.disabled = !visible || state.authBusy;
    input.required = visible;
  }
  elements["auth-password"].autocomplete = settingPassword ? "new-password" : "current-password";
  elements["password-help"].classList.toggle("is-hidden", !settingPassword);
  elements["auth-submit"].textContent = state.authBusy ? "正在处理…" : legacy && page !== "account" ? "连接账户" : info[2];
  elements["auth-submit"].disabled = state.authBusy || state.authMode === "checking" || (!legacy && settingPassword && !state.authToken);
  elements["auth-close"].disabled = state.authBusy;
  for (const [id, target] of [["auth-login-link", "login"], ["auth-register-link", "register"], ["auth-reset-link", "forgot-password"]]) {
    elements[id].classList.toggle("is-hidden", legacy || page === target || state.authBusy || page === "account");
  }
  elements["auth-retry"].classList.toggle("is-hidden", state.authMode !== "unavailable");
  elements["auth-retry"].disabled = state.authBusy;
  if (settingPassword && !state.authToken && !legacy) showAccountError("此页面没有可用的邮件凭据。请重新打开邮件链接，或返回注册 / 找回密码申请新邮件。");
  if (state.authMode === "unavailable" && elements["auth-error"].classList.contains("is-hidden")) showAccountError("暂时无法确认账户服务。可重试登录或找回密码；确认登录前不能提交或读取私人记录。");
}

function openAuthDialog(page = state.user ? "account" : "login", updateHash = true) {
  if (state.authBusy) return;
  if (!ACCOUNT_PAGES[page]) page = "login";
  if (page === "account" && !state.user) page = "login";
  if (state.authPage !== page) {
    elements["auth-form"].reset();
    elements["auth-error"].classList.add("is-hidden");
    elements["auth-notice"].classList.add("is-hidden");
  }
  state.authPage = page;
  if (!["verify-email", "reset-password"].includes(page)) state.authToken = null;
  if (updateHash) history.pushState(null, "", `#${page}`);
  renderAccountForm();
  if (!elements["auth-dialog"].open) elements["auth-dialog"].showModal();
  const input = [...elements["auth-form"].querySelectorAll("input")].find((field) => !field.disabled);
  (input || elements["auth-submit"]).focus();
}

function handleAccountHash() {
  const fragment = location.hash.slice(1);
  const [page, query] = fragment.split("?", 2);
  if (!ACCOUNT_PAGES[page]) return false;
  if (["verify-email", "reset-password"].includes(page) && query !== undefined) {
    const params = new URLSearchParams(query);
    const token = params.get("token");
    state.authToken = token && params.getAll("token").length === 1 && token.length <= 8192 ? { page, token } : null;
    // Remove secrets before any fetch. Opening a link never consumes a token.
    history.replaceState(null, "", `${location.pathname}${location.search}#${page}`);
  }
  if (state.authToken?.page !== page) state.authToken = null;
  openAuthDialog(page, false);
  return true;
}

async function submitAccount(event) {
  event.preventDefault();
  if (state.authBusy || elements["auth-submit"].disabled) return;
  const page = state.authPage;
  const email = elements["auth-email"].value.trim();
  const password = elements["auth-password"].value;
  const handle = elements["auth-handle"].value;
  elements["auth-error"].classList.add("is-hidden");
  elements["auth-notice"].classList.add("is-hidden");
  if (["verify-email", "reset-password"].includes(page) && state.authMode !== "legacy") {
    const length = Array.from(password).length;
    if (length < 8) { showAccountError("密码至少需要8位，请补充后重试。"); return; }
    if (length > 128 || new TextEncoder().encode(password).length > 512) { showAccountError("密码不能超过128位，请缩短后重试。"); return; }
    if (page === "verify-email" && (Array.from(handle).length < 3 || Array.from(handle).length > 32 || handle !== handle.trim() || /[@\p{C}]/u.test(handle))) { showAccountError("公开昵称需3–32个字符，不能含 @、首尾空白或控制字符。"); return; }
    if (!state.authToken || state.authToken.page !== page) { showAccountError("邮件链接已失效，请申请新链接。"); return; }
  }
  state.authBusy = true;
  const sequence = ++state.authSequence;
  const sessionMutation = state.authMode !== "legacy" && ["login", "account", "reset-password"].includes(page);
  renderAccountForm();
  updatePromptState();
  try {
    if (page === "account") {
      if (state.authMode !== "legacy") {
        const payload = await apiRequest("/v1/auth/logout", { method: "POST", body: "{}" });
        if (sequence !== state.authSequence) return;
        if (payload?.status !== "signed_out") throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
      }
      clearAccount();
      state.authNeedsLogin = true;
      elements["auth-dialog"].close();
      showToast("已退出登录。本页私人结果、提示词草稿和账户凭据已清除。");
      return;
    }
    if (state.authMode === "legacy") {
      const token = elements["access-token"].value.trim();
      const user = await apiRequest("/v1/users/me", { headers: { Authorization: `Bearer ${token}` } });
      if (sequence !== state.authSequence) return;
      if (!user?.user_id || !user.public_handle) throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
      if (state.user) clearAccount();
      state.accountSequence++;
      state.token = token;
      state.user = user;
      renderSession();
      updatePromptState();
      if (state.view === "runs") loadRuns(true);
      elements["auth-dialog"].close();
      showToast("已连接旧版账户；刷新页面后需重新连接。");
      return;
    }
    const routes = {
      login: ["/v1/auth/login", { email, password }],
      register: ["/v1/auth/register", { email }],
      "forgot-password": ["/v1/auth/password-reset/request", { email }],
      "verify-email": ["/v1/auth/verify-email", { token: state.authToken?.token, password, public_handle: handle }],
      "reset-password": ["/v1/auth/password-reset/confirm", { token: state.authToken?.token, password }],
    };
    const [path, body] = routes[page];
    const payload = await apiRequest(path, { method: "POST", body: JSON.stringify(body) });
    if (sequence !== state.authSequence) return;
    const expectedStatus = { register: "accepted", "forgot-password": "accepted", "verify-email": "verified", "reset-password": "password_updated" }[page];
    if (expectedStatus && payload?.status !== expectedStatus) throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
    if (page === "login") {
      acceptSession(payload);
      state.authNeedsLogin = false;
      elements["auth-dialog"].close();
      showToast(`已登录 @${state.user.public_handle}。`);
    } else if (["register", "forgot-password"].includes(page)) {
      accountNotice(`请求已受理。若此邮箱可用于该操作，请查看收件箱与垃圾邮件。${state.authConfig?.mail_delivery === "local" || state.development?.mail_delivery === "local" ? "开发环境不发送真实邮件，请关闭窗口后展开“开发者工具”并刷新收件箱。" : "未收到时可稍后重试，请勿连续发送。"}`);
    } else {
      if (page === "reset-password") { clearAccount(); state.authNeedsLogin = true; }
      state.authToken = null;
      state.authPage = "login";
      history.replaceState(null, "", "#login");
      accountNotice(page === "verify-email" ? "邮箱已验证，账户已建立。请使用邮箱和刚设置的密码登录。" : "密码已更新。请使用邮箱和新密码登录。");
    }
  } catch (error) {
    if (sequence !== state.authSequence) return;
    showAccountError(errorMessage(error, "完成账户操作"));
    if (error.code === "AUTH_INVALID_TOKEN") state.authToken = null;
    if (page === "account") accountNotice("退出未获服务确认，登录 Cookie 可能仍有效。请重试退出；不要将设备交给他人。");
  } finally {
    state.authBusy = false;
    elements["auth-password"].value = "";
    elements["access-token"].value = "";
    renderAccountForm();
    updatePromptState();
    // Notify even after an ambiguous failure: the server may have changed the cookie.
    if (sessionMutation) authChannel?.postMessage({ type: "auth-changed" });
    if (state.authRecheckPending) { state.authRecheckPending = false; revalidateSession(); }
  }
}

async function loadDevelopment() {
  try {
    const config = await apiRequest("/v1/development", { headers: { "X-LOJ-Development": "1" } });
    if (!['mock', 'qwen'].includes(config?.evaluation_mode) || !Array.isArray(config.accounts)) return;
    if (config.evaluation_mode === 'qwen' && (config.model !== 'Qwen/Qwen3.5-9B' || config.language_count !== 18)) return;
    state.development = { evaluation_mode: config.evaluation_mode, mail_delivery: config.mail_delivery };
    if (config.evaluation_mode === 'qwen') {
      elements['development-help'].textContent = '学校服务器上的开发者内测环境：提交将调用真实 Qwen3.5-9B。测试账号、收件箱和成绩与本机模拟站及正式服务分别保存，不发送真实邮件。';
    }
    elements["development-panel"].classList.remove("is-hidden");
    for (const account of config.accounts) {
      const card = createElement("div", "development-account");
      card.append(createElement("strong", "", `${account.email} · ${account.role === "admin" ? "管理员" : "测试用户"}`), createElement("code", "", account.password));
      const fill = createElement("button", "secondary-action", `填入 ${account.public_handle} 登录信息`);
      fill.type = "button";
      fill.addEventListener("click", () => {
        openAuthDialog("login");
        elements["auth-email"].value = account.email;
        elements["auth-password"].value = account.password;
      });
      card.append(fill);
      elements["development-accounts"].append(card);
    }
    renderMockBanner();
  } catch (_) { /* Development discovery is optional and never enables authentication. */ }
}

function safeDevelopmentLink(link) {
  try {
    const url = new URL(link, location.origin);
    if (url.origin !== location.origin || url.pathname !== "/" || url.search || url.username || url.password) return null;
    const match = /^#(verify-email|reset-password)\?(.+)$/.exec(url.hash);
    if (!match) return null;
    const params = new URLSearchParams(match[2]);
    if ([...params.keys()].some((key) => key !== "token") || params.getAll("token").length !== 1 || !params.get("token") || params.get("token").length > 8192) return null;
    return url;
  } catch (_) { return null; }
}
async function loadDevelopmentMail() {
  if (!state.development) return;
  elements["development-mail-refresh"].disabled = true;
  elements["development-mail-status"].textContent = "正在读取本地测试收件箱…";
  try {
    const page = await apiRequest("/v1/development/mail", { headers: { "X-LOJ-Development": "1" } });
    elements["development-mail"].replaceChildren();
    for (const mail of page.items) {
      const item = createElement("li");
      const url = safeDevelopmentLink(mail.link);
      item.append(createElement("p", "", `${mail.recipient} · ${url?.hash.startsWith("#verify-email") ? "邮箱验证" : "密码重置"} · ${formatTime(mail.created_at)}`));
      if (url) {
        const link = createElement("a", "text-action", "打开本地测试邮件链接");
        link.href = url.href;
        link.addEventListener("click", (event) => {
          event.preventDefault();
          history.replaceState(null, "", url.hash);
          handleHash();
        });
        item.append(link);
      } else item.append(createElement("p", "", "已拦截不符合本站验证 / 重置格式的链接。"));
      elements["development-mail"].append(item);
    }
    elements["development-mail-status"].textContent = page.items.length ? "仅列出本次本地服务运行期间截获的邮件。链接仅供本机测试。" : "暂无测试邮件。先注册或申请密码重置，再刷新此收件箱。";
  } catch (error) { elements["development-mail-status"].textContent = errorMessage(error, "读取本地收件箱"); }
  finally { elements["development-mail-refresh"].disabled = false; }
}

function bindAccountEvents() {
  if (typeof BroadcastChannel !== "undefined") {
    authChannel = new BroadcastChannel("loj-account-v1");
    authChannel.addEventListener("message", (event) => {
      if (event.data?.type === "auth-changed") revalidateSession();
    });
  }
  window.addEventListener("focus", revalidateSession);
  window.addEventListener("pageshow", (event) => { if (event.persisted) revalidateSession(); });
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") revalidateSession(); });
  for (const id of ["session-button", "runs-connect", "prompt-login", "result-login"]) elements[id].addEventListener("click", () => openAuthDialog());
  elements["auth-form"].addEventListener("submit", submitAccount);
  elements["auth-retry"].addEventListener("click", bootstrapAuth);
  elements["auth-close"].addEventListener("click", () => elements["auth-dialog"].close());
  elements["auth-dialog"].addEventListener("cancel", (event) => { if (state.authBusy) event.preventDefault(); });
  elements["auth-dialog"].addEventListener("close", () => {
    state.authToken = null;
    elements["auth-form"].reset();
    elements["auth-error"].classList.add("is-hidden");
    elements["auth-notice"].classList.add("is-hidden");
    const page = location.hash.slice(1).split("?")[0];
    if (ACCOUNT_PAGES[page]) history.replaceState(null, "", currentViewHash());
  });
  elements["development-mail-refresh"].addEventListener("click", loadDevelopmentMail);
}
