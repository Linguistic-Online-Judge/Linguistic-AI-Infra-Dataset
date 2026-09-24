"use strict";

const TASK_LABELS = { segmentation: "分词", upos: "通用词性 UPOS", xpos: "树库词性 XPOS", dependency: "依存句法", transliteration: "转写" };
const METRIC_LABELS = { las: "带标签依附准确率 LAS", uas: "无标签依附准确率 UAS", micro_accuracy: "词元准确率", micro_f1: "分词综合指标 micro F1", micro_precision: "分词精确率", micro_recall: "分词召回率", sentence_exact_match_rate: "整句完全匹配率", token_accuracy: "词元准确率" };
const STATUS_LABELS = { queued: "排队中", running: "评测中", succeeded: "已评分", failed: "评测失败", rejected: "输入被拒绝" };
const FAILURE_MESSAGES = {
  DATASET_INTEGRITY: "评测数据完整性检查未通过。", JOB_DEADLINE: "评测未能在截止时间前完成。",
  MODEL_IDENTITY_MISMATCH: "模型与本次评测约定的版本不一致。", PROVIDER_TIMEOUT: "模型响应超时。",
  PROVIDER_TRANSPORT: "模型连接中断。", RUNTIME_MISCONFIGURATION: "评测服务配置有误。",
  TOKEN_LIMIT_EXCEEDED: "提示词与样本合计超出模型输入长度限制。", WORKER_CRASH: "评测进程意外停止。",
};
const VALIDATION_LABELS = { INVALID_JSON: "不是有效 JSON", TOP_LEVEL_NOT_OBJECT: "顶层不是对象", MISSING_FIELD: "缺少字段", EXTRA_FIELD: "多余字段", WRONG_TYPE: "字段类型不符", EMPTY_VALUE: "存在空值", INVALID_VALUE: "值不符合规则", INVALID_TAG: "未知标签", DUPLICATE_TOKEN_ID: "词元编号重复", LENGTH_MISMATCH: "输出数量不符", TOKEN_ID_MISMATCH: "词元编号不符", INVALID_HEAD_ID: "中心词编号不符" };
const LANGUAGE_LABELS = { ar: "阿拉伯语", arabic: "阿拉伯语", da: "丹麦语", danish: "丹麦语", de: "德语", german: "德语", en: "英语", english: "英语", es: "西班牙语", spanish: "西班牙语", fr: "法语", french: "法语", he: "希伯来语", hebrew: "希伯来语", hi: "印地语", hindi: "印地语", hu: "匈牙利语", hungarian: "匈牙利语", it: "意大利语", italian: "意大利语", ja: "日语", japanese: "日语", ko: "韩语", korean: "韩语", nl: "荷兰语", dutch: "荷兰语", pt: "葡萄牙语", portuguese: "葡萄牙语", ru: "俄语", russian: "俄语", sv: "瑞典语", swedish: "瑞典语", th: "泰语", thai: "泰语", zh: "中文", chinese: "中文" };
const elements = Object.fromEntries([...document.querySelectorAll("[id]")].map((element) => [element.id, element]));
const state = {
  challenges: [], selectedChallengeId: null, view: "challenges", contextSequence: 0,
  activeSubmission: null, pollSequence: 0, pollTimer: null, promptDrafts: new Map(), attempts: new Map(), submitting: false,
  leaderboardIdentity: null, leaderboardCursor: null, leaderboardSequence: 0, leaderboardLoading: false,
  runsCursor: null, runsSequence: 0, runsLoading: false, catalogSequence: 0,
  token: null, user: null, authMode: "checking", authSequence: 0, accountSequence: 0,
  authPage: "login", authBusy: false, authToken: null, authConfig: null, development: null,
  authNeedsLogin: false, authRecheckPending: false, sessionCheckSequence: 0,
  promptRecord: null, promptSequence: 0, resultRouteId: null, resultRouteSequence: 0,
};

class APIRequestError extends Error {
  constructor(status, error) {
    super(error?.message || `Request failed: ${status}`);
    this.status = status;
    this.code = error?.code || "REQUEST_FAILED";
    this.details = error?.details || {};
  }
}

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function challengeById(id) { return state.challenges.find((challenge) => challenge.challenge_id === id); }
function selectedChallenge() { return challengeById(state.selectedChallengeId); }
function taskLabel(task) { return TASK_LABELS[task] || task; }
function languageLabel(language) { return LANGUAGE_LABELS[language.toLowerCase()] || language; }
function formatMetric(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? `${(value * 100).toFixed(2)}%` : "未提供"; }
function formatTime(value) {
  if (!value) return "尚未开始";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未提供" : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}
function promptBytes() { return new TextEncoder().encode(elements["student-prompt"].value).length; }
function attemptKey() { return JSON.stringify([state.user?.user_id, state.selectedChallengeId, elements["student-prompt"].value]); }
function idempotencyKey() { return Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join(""); }

async function apiRequest(path, options = {}, authenticated = false) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes((options.method || "GET").toUpperCase())) headers.set("X-LOJ-CSRF", "1");
  const accountSequence = state.accountSequence;
  const adminRequest = authenticated && path.startsWith("/v1/admin/");
  const adminSequence = adminState.sequence;
  const expectedUser = state.user?.user_id;
  if (authenticated) {
    if (typeof expectedUser !== "string" || !expectedUser || !["cookie", "legacy"].includes(state.authMode)) throw new APIRequestError(401, { code: "AUTHENTICATION_REQUIRED" });
    // A consistency assertion, never an authentication credential. Freeze it before fetch.
    headers.set("X-LOJ-Expected-User", expectedUser);
    if (state.authMode === "legacy") headers.set("Authorization", `Bearer ${state.token}`);
    if (adminRequest && !canManageTeaching()) throw new APIRequestError(403, { code: "ADMIN_REQUIRED" });
  }
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal });
    if (authenticated && response.status === 401 && accountSequence === state.accountSequence) {
      invalidateAccount("登录已失效。本页私人内容已清除，请重新登录。");
    }
    if (adminRequest && response.status === 403 && accountSequence === state.accountSequence && adminSequence === adminState.sequence) {
      revokeAdminAccess();
    }
    let payload;
    try { payload = await response.json(); } catch (_) { throw new APIRequestError(response.status, { code: "INVALID_RESPONSE" }); }
    if (authenticated && response.status === 409 && payload?.error?.code === "AUTH_ACCOUNT_CHANGED" && accountSequence === state.accountSequence) {
      invalidateAccount(errorMessage({ code: "AUTH_ACCOUNT_CHANGED" }));
    }
    while (adminRequest && adminState.validation && adminSequence === adminState.sequence && accountSequence === state.accountSequence) await adminState.validation;
    if (authenticated && accountSequence !== state.accountSequence) throw new APIRequestError(409, { code: "AUTH_ACCOUNT_CHANGED" });
    if (adminRequest && (adminSequence !== adminState.sequence || !canManageTeaching())) throw new APIRequestError(403, { code: "ADMIN_REQUIRED" });
    if (!response.ok) {
      throw new APIRequestError(response.status, payload?.error);
    }
    return payload;
  } catch (error) {
    if (error instanceof APIRequestError) throw error;
    throw new APIRequestError(0, { code: "NETWORK_UNAVAILABLE" });
  } finally { window.clearTimeout(timer); }
}

function errorMessage(error, action = "完成操作") {
  const messages = {
    AUTHENTICATION_REQUIRED: "登录状态已失效，请重新登录。",
    AUTH_ACCOUNT_CHANGED: "账户已在其他页面更改或退出。本页私人内容和草稿已清除，请重新登录；原提交不会自动重试。",
    AUTH_INVALID_CREDENTIALS: "邮箱或密码不正确。请重新输入，或选择“忘记密码”。",
    AUTH_INVALID_TOKEN: "邮件链接无效或已过期。请重新注册或申请密码重置邮件。",
    AUTH_RATE_LIMITED: "操作过于频繁。请稍后再试，不必连续发送请求。",
    AUTH_VALIDATION_ERROR: "账户信息不符合要求。请检查邮箱、密码长度与公开昵称后重试。",
    USER_NOT_REGISTERED: "此旧版账户尚未获准使用评测服务，请联系管理员。",
    CHALLENGE_NOT_OPEN: "此任务暂未开放提交，请选择可提交的任务。",
    CHALLENGE_PAUSED: "管理员已暂停此任务的新提交。已有评测仍可在“我的评测”查看。",
    ADMIN_REQUIRED: "教学管理权限不可用。管理内容已清除，请确认账户权限后重试。",
    CHALLENGE_RUNTIME_UNAVAILABLE: "此任务的评测服务暂不可用，请稍后重试。",
    INVALID_STUDENT_PROMPT: "请先写下非空的任务指令。", STUDENT_PROMPT_BYTE_LIMIT: "提示词过长，请缩短后提交。",
    OUTSTANDING_SUBMISSION_LIMIT: "仍在处理的评测过多。请等一条完成后再提交。",
    GLOBAL_QUEUE_FULL: "评测队列已满，请稍后重试。",
    IDEMPOTENCY_CONFLICT: "本次请求与已记录的提交不一致。请先在“我的评测”核对，避免重复提交。",
    SERVICE_NOT_READY: "服务暂未就绪。请稍后重试；登录与找回密码入口仍可使用。",
    NETWORK_UNAVAILABLE: "未能连接服务，请检查网络后重试。",
    INVALID_RESPONSE: "服务返回了无法读取的数据，请稍后重试。",
    SUBMISSION_NOT_FOUND: "找不到此评测记录，或它不属于当前账户。请返回“我的评测”重新选择。",
    LEADERBOARD_NOT_FOUND: "这个评测版本暂无排行榜，请稍后刷新。",
  };
  if (error.code === "SUBMISSION_RATE_LIMIT") {
    const seconds = Number(error.details?.retry_after_seconds);
    return seconds > 0 ? `已达到提交次数限制，请约 ${Math.ceil(seconds / 60)} 分钟后再试。` : "已达到提交次数限制，请稍后再试。";
  }
  return messages[error.code] || `未能${action}。请稍后重试。`;
}
let toastTimer;
function showToast(message) {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.add("is-visible");
  toastTimer = window.setTimeout(() => elements.toast.classList.remove("is-visible"), 6000);
}
function setGlobalStatus(message) { elements["global-status"].textContent = message; }
function renderMockBanner() {
  const mock = state.development?.evaluation_mode === "mock" || selectedChallenge()?.model_identity?.runtime === "mock";
  const real = state.development?.evaluation_mode === "qwen";
  elements["mock-banner"].classList.toggle("is-hidden", !mock && !real);
  elements["mock-banner"].classList.toggle("environment-banner--real", real);
  elements["mock-banner"].textContent = real
    ? 'Qwen3.5-9B · 学校服务器开发环境'
    : '本地模拟评测 · 分数与排名仅用于功能测试';
}

async function loadChallenges() {
  const sequence = ++state.catalogSequence;
  elements["catalog-retry"].disabled = true;
  setGlobalStatus("正在加载公开任务目录…");
  try {
    const challenges = await apiRequest("/v1/challenges");
    if (sequence !== state.catalogSequence) return;
    if (!Array.isArray(challenges)) throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
    state.challenges = challenges;
    const task = elements["task-filter"].value;
    elements["task-filter"].replaceChildren(new Option("全部类型", ""));
    for (const value of [...new Set(challenges.map((entry) => entry.task))].sort()) elements["task-filter"].append(new Option(taskLabel(value), value));
    elements["task-filter"].value = task;
    const language = elements["language-filter"].value;
    elements["language-filter"].replaceChildren(new Option("全部语言", ""));
    for (const value of [...new Set(challenges.map((entry) => languageLabel(entry.language)))].sort((a, b) => a.localeCompare(b, "zh-CN"))) elements["language-filter"].append(new Option(value, value));
    elements["language-filter"].value = language;
    let requested = null;
    try { if (location.hash.startsWith("#challenge/")) requested = decodeURIComponent(location.hash.slice(11)); } catch (_) { showToast("任务链接格式有误，请从目录重新选择。"); }
    const first = challengeById(requested) || selectedChallenge() || challenges.find((entry) => entry.accepting_submissions) || challenges[0];
    if (first) selectChallenge(first.challenge_id, false);
    else {
      state.contextSequence++;
      stopPolling();
      state.selectedChallengeId = null;
      state.activeSubmission = null;
      state.leaderboardIdentity = null;
      renderEmptyResult();
      loadLeaderboard(true);
      for (const id of ["sample-count", "primary-metric", "model-name", "challenge-version"]) elements[id].textContent = "未提供";
      elements["workbench-title"].textContent = "暂无公开任务";
      elements["availability-mark"].textContent = "未开放";
      elements["availability-note"].textContent = "目录中还没有任务。稍后重新加载，或联系课程管理员确认安排。";
      elements["task-guide"].classList.add("is-hidden");
      elements["template-zero"].disabled = true;
      elements["template-few"].disabled = true;
      updatePromptState();
    }
    renderChallengeList();
    elements["catalog-retry"].classList.toggle("is-hidden", challenges.length > 0);
    const languages = new Set(challenges.map((item) => item.language)).size;
    elements["catalog-coverage"].textContent = `${languages}种语言`;
    setGlobalStatus("");
    if (location.hash.startsWith("#challenge/") || location.hash.startsWith("#result/") || location.hash.startsWith("#leaderboard/")) handleHash();
  } catch (error) {
    if (sequence !== state.catalogSequence) return;
    setGlobalStatus(errorMessage(error, "加载任务目录"));
    elements["catalog-retry"].classList.remove("is-hidden");
    if (!state.challenges.length) {
      elements["catalog-empty"].textContent = "任务目录加载失败。请点击上方“重新加载目录”。";
      elements["catalog-empty"].classList.remove("is-hidden");
      elements["workbench-title"].textContent = "暂时无法加载任务";
      elements["availability-mark"].textContent = "未加载";
      elements["availability-note"].textContent = "任务尚未加载，不会发送评测请求。重新加载目录后继续。";
    }
  } finally { if (sequence === state.catalogSequence) elements["catalog-retry"].disabled = false; }
}

function renderChallengeList() {
  const query = elements["challenge-search"].value.trim().toLocaleLowerCase();
  const task = elements["task-filter"].value;
  const availability = elements["availability-filter"].value;
  const language = elements["language-filter"].value;
  const visible = state.challenges.filter((challenge) => {
    const text = `${challenge.language} ${languageLabel(challenge.language)} ${challenge.treebank} ${challenge.title} ${taskLabel(challenge.task)}`.toLocaleLowerCase();
    return (!query || text.includes(query)) && (!language || languageLabel(challenge.language) === language) && (!task || challenge.task === task) && (!availability || (availability === "open") === challenge.accepting_submissions);
  });
  elements["challenge-list"].replaceChildren();
  elements["catalog-count"].textContent = `${visible.length}项任务`;
  elements["catalog-count"].setAttribute("aria-label", `${visible.length} 项匹配任务，共 ${state.challenges.length} 项`);
  elements["catalog-empty"].textContent = state.challenges.length ? "没有匹配的任务。请修改搜索词或清除筛选。" : "暂无公开任务，请稍后重新加载目录。";
  elements["catalog-empty"].classList.toggle("is-hidden", visible.length !== 0);
  elements["filters-clear"].classList.toggle("is-hidden", !query && !task && !availability && !language);
  for (const challenge of visible) {
    const row = createElement("tr");
    const title = createElement("td");
    title.append(createElement("span", "task-title", taskLabel(challenge.task)), createElement("span", "task-treebank", challenge.treebank));
    const mark = createElement("span", "catalog-availability");
    mark.textContent = challenge.admissions_closed ? "已暂停" : challenge.accepting_submissions ? "可提交" : "未开放";
    mark.classList.toggle("is-open", challenge.accepting_submissions);
    const statusCell = createElement("td"); statusCell.append(mark);
    const action = createElement("td");
    const link = createElement("a", "task-enter", challenge.accepting_submissions ? "进入练习 →" : "查看任务 →");
    link.href = `#challenge/${encodeURIComponent(challenge.challenge_id)}`;
    link.setAttribute("aria-label", `${link.textContent.replace(" →", "")}：${languageLabel(challenge.language)} ${challenge.treebank} ${taskLabel(challenge.task)}`);
    action.append(link);
    row.append(title, createElement("td", "", languageLabel(challenge.language)), createElement("td", "", `${challenge.sample_count}个样本`), statusCell, action);
    elements["challenge-list"].append(row);
  }
}

function selectChallenge(id, updateHash = true) {
  const challenge = challengeById(id);
  if (!challenge) return;
  if (state.selectedChallengeId !== id) {
    state.contextSequence++;
    stopPolling();
    state.activeSubmission = null;
    renderEmptyResult();
    elements["prompt-error"].classList.add("is-hidden");
    elements["teaching-details"].open = false;
  }
  state.selectedChallengeId = id;
  if (updateHash) { history.pushState(null, "", `#challenge/${encodeURIComponent(id)}`); setView("practice"); }
  elements["student-prompt"].value = state.promptDrafts.get(id) || "";
  elements["challenge-coordinate"].textContent = `${challenge.treebank} · ${challenge.sample_count}个样本 · ${challenge.model_identity?.runtime === "mock" ? "本地模拟" : challenge.model_identity?.model || "模型未配置"}`;
  elements["workbench-title"].textContent = `${languageLabel(challenge.language)} · ${taskLabel(challenge.task)}`;
  elements["workbench-title"].title = challenge.title;
  elements["sample-count"].textContent = String(challenge.sample_count);
  elements["primary-metric"].textContent = METRIC_LABELS[challenge.primary_metric] || challenge.primary_metric;
  elements["model-name"].textContent = challenge.model_identity?.model || "未指定";
  elements["challenge-version"].textContent = challenge.version;
  const mark = elements["availability-mark"];
  const note = elements["availability-note"];
  mark.classList.toggle("is-open", challenge.accepting_submissions);
  note.classList.toggle("is-open", challenge.accepting_submissions);
  note.classList.toggle("is-warning", !challenge.accepting_submissions);
  mark.textContent = challenge.admissions_closed ? "已暂停" : challenge.accepting_submissions ? "可提交" : challenge.submission_enabled ? "服务不可用" : "暂未开放";
  note.textContent = challenge.admissions_closed ? "此任务已暂停新提交，仍可阅读和编辑草稿。" : challenge.accepting_submissions ? "" : challenge.submission_enabled ? "评测服务暂不可用，可先阅读和编辑草稿。" : "此任务尚未开放提交。";
  setLessonTab("description");
  renderTeaching(challenge);
  renderProvenance(challenge);
  renderMockBanner();
  updatePromptState();
  state.leaderboardIdentity = state.activeSubmission?.evaluation_identity_sha256 || challenge.evaluation_identity_sha256;
  loadLeaderboard(true);
  renderChallengeList();
}

function renderProvenance(challenge) {
  const fields = [["数据范围", challenge.security_level === "public_reproducible" ? "公开、可复现" : challenge.security_level], ["标注许可", challenge.annotation_license], ["来源版本", challenge.source_release], ["署名要求", challenge.attribution_requirements], ["相同方式共享要求", challenge.share_alike_requirements], ["原文权利", challenge.underlying_text_rights], ["适用范围与局限", challenge.benchmark_limitations], ["数据摘要 SHA-256", challenge.dataset_sha256], ["样本选择摘要 SHA-256", challenge.selection_sha256], ["当前评测身份", challenge.evaluation_identity_sha256]];
  const nodes = [];
  for (const [label, value] of fields) nodes.push(createElement("dt", "", label), createElement("dd", "", !value || ["unrecorded", "review_required"].includes(value) ? "未确认，需核查来源" : value));
  elements["provenance-details"].replaceChildren(...nodes);
}

function updatePromptState() {
  const challenge = selectedChallenge();
  const bytes = promptBytes();
  const limit = challenge?.student_prompt_utf8_bytes;
  const tooLong = Boolean(limit && bytes > limit);
  const attempt = state.attempts.get(attemptKey());
  elements["prompt-count"].textContent = limit ? `${bytes} / ${limit} 字节` : `${bytes} 字节`;
  elements["prompt-count"].classList.toggle("is-over-limit", tooLong);
  elements["student-prompt"].setAttribute("aria-invalid", String(tooLong));
  let hint = "请先选择可提交的任务。";
  let canRun = false;
  if (challenge) {
    if (state.submitting) hint = "正在确认提交，请勿重复点击。你仍可编辑草稿或切换任务。";
    else if (!challenge.accepting_submissions) hint = "此任务暂不可提交，可先编写草稿。";
     else if (!state.user) hint = "登录后即可提交；草稿保留在当前页面。";
     else if (state.authBusy || !["cookie", "legacy"].includes(state.authMode)) hint = "正在确认账户，请稍后提交。";
    else if (!elements["student-prompt"].value.trim()) hint = "写下指令，或载入一个可编辑模板。";
    else if (tooLong) hint = `请缩短至 ${limit} 字节以内，再提交。`;
    else { hint = attempt?.submission ? "此份提示词已提交。查看原记录不会重复创建评测。" : `以 @${state.user.public_handle} 提交，对 ${challenge.sample_count} 个样本应用同一提示词。`; canRun = true; }
  }
  elements["submit-hint"].textContent = hint;
  elements["run-button"].disabled = !canRun;
  elements["run-button"].textContent = state.submitting ? "正在提交…" : attempt?.submission ? "查看已提交评测" : attempt ? "重试同一提交" : "提交评测";
  elements["prompt-login"].classList.toggle("is-hidden", Boolean(state.user));
}

async function submitPrompt(event) {
  event.preventDefault();
  if (state.submitting) return;
  updatePromptState();
  if (elements["run-button"].disabled) return;
  const challenge = selectedChallenge();
  const key = attemptKey();
  let attempt = state.attempts.get(key);
  if (attempt?.submission) { openRun(attempt.submission); return; }
  if (!attempt) {
    attempt = { key: idempotencyKey(), body: JSON.stringify({ challenge_id: challenge.challenge_id, student_prompt: elements["student-prompt"].value }) };
    state.attempts.set(key, attempt);
  }
  const context = state.contextSequence;
  const account = state.accountSequence;
  state.submitting = true;
  elements["prompt-error"].classList.add("is-hidden");
  updatePromptState();
  try {
    const submission = await apiRequest("/v1/submissions", { method: "POST", headers: { "Idempotency-Key": attempt.key }, body: attempt.body }, true);
    if (account !== state.accountSequence) return;
    attempt.submission = submission;
    if (context === state.contextSequence && state.view === "practice") openRun(submission);
    showToast("评测已受理，可在“我的评测”查看进度。");
  } catch (error) {
    if (account !== state.accountSequence || context !== state.contextSequence) return;
    if (error.code === "CHALLENGE_PAUSED") {
      challenge.admissions_closed = true;
      challenge.accepting_submissions = false;
      elements["availability-mark"].textContent = "已暂停";
      elements["availability-mark"].classList.remove("is-open");
      elements["availability-note"].textContent = errorMessage(error);
      elements["availability-note"].classList.remove("is-open");
      elements["availability-note"].classList.add("is-warning");
      renderChallengeList();
    }
    if (key !== attemptKey()) { showToast("上一份提示词的提交尚未确认，请在“我的评测”核对。当前已修改的草稿尚未提交。"); return; }
    elements["prompt-error"].textContent = `${errorMessage(error, "提交评测")} 若网络中断，重试会复用本次请求编号；也可先查看“我的评测”。`;
    elements["prompt-error"].classList.remove("is-hidden");
  } finally {
    if (account === state.accountSequence) { state.submitting = false; updatePromptState(); }
  }
}

function stopPolling() { state.pollSequence++; window.clearTimeout(state.pollTimer); }
function startPolling(submissionId, challengeId) {
  stopPolling();
  const sequence = state.pollSequence;
  const account = state.accountSequence;
   const current = () => sequence === state.pollSequence && account === state.accountSequence && state.view === "result" && state.activeSubmission?.submission_id === submissionId;
  const poll = async () => {
    if (!current()) return;
    try {
      const submission = await apiRequest(`/v1/submissions/${encodeURIComponent(submissionId)}`, {}, true);
      if (!current()) return;
      state.activeSubmission = submission;
      if (["queued", "running"].includes(submission.status)) {
        renderPendingResult(submission);
        state.pollTimer = window.setTimeout(poll, 2000);
        return;
      }
      const result = await apiRequest(`/v1/submissions/${encodeURIComponent(submissionId)}/result`, {}, true);
      if (current()) renderTerminalResult(result, submission);
    } catch (error) {
      if (!current()) return;
      if (error.code === "RESULT_NOT_READY") { state.pollTimer = window.setTimeout(poll, 2000); return; }
      const retrying = error.status === 0 || error.status >= 500;
      const wrapper = createElement("div", "result-state");
      wrapper.append(createElement("h3", "", "暂时无法更新结果"), createElement("p", "", `${errorMessage(error, "读取结果")}${retrying ? " 正在自动重试，不会重新提交评测。" : ""}`));
      const retry = createElement("button", "secondary-action", "重新读取结果");
      retry.type = "button";
      retry.addEventListener("click", () => startPolling(submissionId, challengeId));
      wrapper.append(retry);
      showResultContent(wrapper);
      if (retrying) state.pollTimer = window.setTimeout(poll, 5000);
    }
  };
  poll();
}
function renderEmptyResult() {
  state.promptSequence++;
  state.promptRecord = null;
  elements["submitted-prompt"].textContent = "";
  elements["result-prompt-status"].textContent = "";
  elements["result-prompt-panel"].classList.add("is-hidden");
  elements["continue-editing"].disabled = true;
  elements["result-task"].textContent = "";
  elements["result-title"].textContent = "尚未选择评测记录";
  elements["result-empty-help"].textContent = "从“我的评测”打开一条记录。";
  elements["result-login"].classList.add("is-hidden");
  elements["result-content"].replaceChildren();
  elements["result-content"].classList.add("is-hidden");
  elements["result-empty"].classList.remove("is-hidden");
}
function showResultContent(...nodes) {
  elements["result-empty"].classList.add("is-hidden");
  elements["result-content"].replaceChildren(...nodes);
  elements["result-content"].classList.remove("is-hidden");
}
function resultMeta(submission) { return createElement("p", "result-meta", `${formatTime(submission.completed_at || submission.created_at)} · 记录 ${submission.submission_id.slice(0, 12)}`); }
function renderPendingResult(submission) {
  const wrapper = createElement("div", "result-state");
  const pending = ["queued", "running"].includes(submission.status);
  wrapper.append(createElement("h3", "", pending ? STATUS_LABELS[submission.status] : "正在读取结果…"), createElement("p", "", pending ? "状态会自动更新，也可稍后在“我的评测”查看。" : "正在读取评测结果。"), resultMeta(submission));
  showResultContent(wrapper);
}

function renderTerminalResult(result, submission) {
  if (!["succeeded", "failed", "rejected"].includes(result.outcome)) throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
  const nodes = [];
  if (result.outcome === "succeeded") {
    const summary = createElement("div", "result-summary");
    const score = createElement("div", "result-score");
    score.append(createElement("p", "eyebrow", METRIC_LABELS[result.primary_metric] || result.primary_metric), createElement("strong", "", formatMetric(result.score)), createElement("span", "", result.model_identity?.runtime === "mock" ? "模拟分数 · 非模型能力" : "本次评测得分"));
    const description = createElement("div", "result-state");
    description.append(createElement("h3", "", "评测已完成"), createElement("p", "", `共 ${result.samples_total} 个样本，${result.samples_valid} 个输出格式有效，${result.samples_invalid} 个无效。格式有效不等于答案正确。`));
    summary.append(score, description);
    const metrics = createElement("div", "metric-grid");
    for (const [name, value] of Object.entries(result.metrics)) {
      const cell = createElement("div", "metric-cell");
      cell.append(createElement("span", "", METRIC_LABELS[name] || name), createElement("strong", "", formatMetric(value)));
      metrics.append(cell);
    }
    nodes.push(summary);
    const extra = createElement("details", "result-detail");
    extra.append(createElement("summary", "", "评测版本与其他指标"), metrics);
    const versions = createElement("dl", "version-facts");
    for (const [label, value] of [["模型", result.model_identity?.model], ["模型版本", result.model_identity?.revision], ["评分器", result.scorer_version], ["汇总版本", result.aggregation_version], ["评测身份", submission.evaluation_identity_sha256], ["数据摘要", result.dataset_sha256], ["样本选择摘要", result.selection_sha256], ["提示词摘要", result.student_prompt_sha256]]) {
      if (value) versions.append(createElement("dt", "", label), createElement("dd", "", value));
    }
    extra.append(versions);
    const errors = Object.entries(result.errors || {}).filter(([, count]) => count > 0);
    if (errors.length) {
      const table = createElement("table", "error-breakdown");
      const caption = createElement("caption", "", "输出格式问题：先修正字段、数量与标签，再比较分数。");
      const head = createElement("thead");
      const row = createElement("tr");
      for (const label of ["检查问题", "数量"]) { const cell = createElement("th", "", label); cell.scope = "col"; row.append(cell); }
      head.append(row);
      const body = createElement("tbody");
      for (const [code, count] of errors) {
        const row = createElement("tr");
        row.append(createElement("td", "", `${VALIDATION_LABELS[code] || "格式检查问题"} (${code})`), createElement("td", "", String(count)));
        body.append(row);
      }
      table.append(caption, head, body);
      const problems = createElement("details", "result-detail");
      problems.append(createElement("summary", "", `格式问题 · ${result.samples_invalid}个样本`), table);
      nodes.push(problems);
    }
    nodes.push(extra);
  } else {
    const wrapper = createElement("div", "result-state");
    const guidance = result.code === "TOKEN_LIMIT_EXCEEDED" ? "请缩短提示词后再次提交。" : result.retryable ? "服务恢复后可以重新评测。" : "请联系管理员处理，不建议反复提交。";
    wrapper.append(createElement("p", "eyebrow", result.outcome === "rejected" ? "输入被拒绝" : "平台评测失败"), createElement("h3", "", "本次未产生分数"), createElement("p", "", `${FAILURE_MESSAGES[result.code] || "本次评测未能完成。"}${guidance}`), createElement("p", "result-meta", result.code));
    nodes.push(wrapper);
  }
  nodes.push(resultMeta(submission));
  const attempt = state.attempts.get(attemptKey());
  if (attempt?.submission?.submission_id === submission.submission_id) {
    const submittedKey = attemptKey();
    const rerun = createElement("button", "secondary-action", "用同一提示词再次评测");
    rerun.type = "button";
    rerun.addEventListener("click", () => {
      if (attemptKey() !== submittedKey) { showToast("当前草稿已修改。请使用编辑区的“提交评测”，避免误把新草稿当作原提示词。"); return; }
      if (!window.confirm("再次评测会创建新记录并计入提交次数，是否继续？")) return;
      state.attempts.delete(attemptKey());
      setView("practice");
      history.pushState(null, "", `#challenge/${encodeURIComponent(state.selectedChallengeId)}`);
      updatePromptState();
      elements["prompt-form"].requestSubmit();
    });
    const rerunDetails = createElement("details", "result-detail");
    rerunDetails.append(createElement("summary", "", "重新评测"), rerun);
    nodes.push(rerunDetails);
  }
  showResultContent(...nodes);
  state.leaderboardIdentity = submission.evaluation_identity_sha256;
  loadLeaderboard(true);
}

async function loadLeaderboard(reset = false) {
  if (!reset && state.leaderboardLoading) return;
  if (reset) {
    state.leaderboardSequence++;
    state.leaderboardCursor = null;
    elements["leaderboard-list"].replaceChildren();
    elements["leaderboard-more"].classList.add("is-hidden");
  }
  const sequence = state.leaderboardSequence;
  const identity = state.leaderboardIdentity;
  elements["identity-stamp"].textContent = identity?.slice(0, 10) || "未指定";
  elements["identity-stamp"].title = identity ? `评测身份 ${identity}，本榜仅比较这个版本` : "尚无评测身份";
  elements["leaderboard-empty"].classList.remove("is-hidden");
  if (!identity) { elements["leaderboard-empty"].textContent = "此任务尚无评测版本，暂不提供排名。"; state.leaderboardLoading = false; elements["leaderboard-refresh"].disabled = false; return; }
  state.leaderboardLoading = true;
  elements["leaderboard-empty"].textContent = "正在加载排名…";
  elements["leaderboard-more"].disabled = true;
  elements["leaderboard-refresh"].disabled = true;
  try {
    const query = new URLSearchParams({ limit: "20" });
    if (state.leaderboardCursor) query.set("cursor", state.leaderboardCursor);
    const page = await apiRequest(`/v1/leaderboards/${encodeURIComponent(identity)}?${query}`);
    if (sequence !== state.leaderboardSequence) return;
    for (const entry of page.items) {
      const fragment = elements["leaderboard-row-template"].content.cloneNode(true);
      fragment.querySelector(".rank").textContent = `#${entry.rank}`;
      const handle = fragment.querySelector(".handle");
      handle.textContent = `@${entry.public_handle}`;
      handle.title = `@${entry.public_handle}`;
      fragment.querySelector(".score").textContent = formatMetric(entry.score);
      elements["leaderboard-list"].append(fragment);
    }
    state.leaderboardCursor = page.next_cursor;
    elements["leaderboard-empty"].textContent = "暂无已评分记录。";
    elements["leaderboard-empty"].classList.toggle("is-hidden", elements["leaderboard-list"].childElementCount > 0);
    elements["leaderboard-more"].classList.toggle("is-hidden", !page.next_cursor);
  } catch (error) {
    if (sequence !== state.leaderboardSequence) return;
    elements["leaderboard-empty"].textContent = `${errorMessage(error, "加载排行榜")} 可点击“刷新排行榜”重试。`;
    elements["leaderboard-empty"].classList.remove("is-hidden");
  } finally {
    if (sequence === state.leaderboardSequence) {
      state.leaderboardLoading = false;
      elements["leaderboard-more"].disabled = false;
      elements["leaderboard-refresh"].disabled = false;
    }
  }
}

function runsMessage(title, message, connect = false) {
  elements["runs-empty"].classList.remove("is-hidden");
  elements["runs-empty"].querySelector("h2").textContent = title;
  elements["runs-empty"].querySelector("p:not(.eyebrow)").textContent = message;
  elements["runs-connect"].classList.toggle("is-hidden", !connect);
}
function renderRunsSignedOut() {
  elements["runs-list"].replaceChildren();
  elements["runs-more"].classList.add("is-hidden");
  runsMessage("登录后查看你的评测", "提示词不公开。排行榜仅显示公开昵称、分数与评测统计。", true);
}
async function loadRuns(reset = false) {
  if (!state.user) { renderRunsSignedOut(); return; }
  if (!reset && state.runsLoading) return;
  if (reset) {
    state.runsSequence++;
    state.runsCursor = null;
    elements["runs-list"].replaceChildren();
    elements["runs-more"].classList.add("is-hidden");
  }
  const sequence = state.runsSequence;
  const account = state.accountSequence;
  const current = () => sequence === state.runsSequence && account === state.accountSequence && state.view === "runs";
  state.runsLoading = true;
  elements["refresh-runs"].disabled = true;
  elements["runs-more"].disabled = true;
  runsMessage("正在加载评测记录…", "只读取当前账户的记录，不会创建新评测。");
  try {
    const query = new URLSearchParams({ limit: "20" });
    if (state.runsCursor) query.set("cursor", state.runsCursor);
    const page = await apiRequest(`/v1/submissions?${query}`, {}, true);
    if (!current()) return;
    for (const run of page.items) renderRunRow(run);
    state.runsCursor = page.next_cursor;
    runsMessage("还没有评测记录", "从任务目录选择任务，开始练习。");
    elements["runs-empty"].classList.toggle("is-hidden", elements["runs-list"].childElementCount > 0);
    elements["runs-more"].classList.toggle("is-hidden", !page.next_cursor);
  } catch (error) {
    if (current()) runsMessage("暂时无法加载记录", `${errorMessage(error, "加载评测记录")} 点击“刷新记录”重试。`);
  } finally {
    if (current()) {
      state.runsLoading = false;
      elements["refresh-runs"].disabled = false;
      elements["runs-more"].disabled = false;
    }
  }
}
function renderRunRow(run) {
  const challenge = challengeById(run.challenge_id);
  const fragment = elements["run-row-template"].content.cloneNode(true);
  fragment.querySelector(".run-coordinate").textContent = challenge ? `${languageLabel(challenge.language)} / ${taskLabel(challenge.task)}` : run.challenge_id;
  fragment.querySelector("h2").textContent = challenge ? `${languageLabel(challenge.language)} ${challenge.treebank} · ${taskLabel(challenge.task)}` : run.challenge_id;
  fragment.querySelector(".run-time").textContent = `${formatTime(run.created_at)} · ${run.submission_id.slice(0, 12)}`;
  const status = fragment.querySelector(".run-status");
  status.textContent = STATUS_LABELS[run.status] || run.status;
  status.classList.add(`is-${run.status}`);
  const button = fragment.querySelector("button");
  button.textContent = ["queued", "running"].includes(run.status) ? "追踪进度" : "查看结果";
  button.addEventListener("click", () => openRun(run));
  elements["runs-list"].append(fragment);
}
function openRun(run, focus = true, updateHash = true) {
  const challenge = challengeById(run.challenge_id);
  if (challenge) selectChallenge(challenge.challenge_id, false);
  setView("result");
  renderEmptyResult();
  state.resultRouteId = run.submission_id;
  if (updateHash) history.pushState(null, "", `#result/${encodeURIComponent(run.submission_id)}`);
  state.activeSubmission = run;
  elements["result-task"].textContent = challenge ? `${languageLabel(challenge.language)} · ${taskLabel(challenge.task)} · ${challenge.treebank}` : run.challenge_id;
  elements["result-back"].href = challenge ? `#challenge/${encodeURIComponent(challenge.challenge_id)}` : "#runs";
  elements["result-back"].textContent = challenge ? "← 返回练习" : "← 返回我的评测";
  elements["result-prompt-panel"].classList.remove("is-hidden");
  state.leaderboardIdentity = run.evaluation_identity_sha256;
  loadLeaderboard(true);
  renderPendingResult(run);
  loadSubmittedPrompt();
  startPolling(run.submission_id, run.challenge_id);
  if (focus) focusView("result");
}
async function loadSubmittedPrompt() {
  const run = state.activeSubmission;
  if (!run || !state.user || state.view !== "result") return;
  const sequence = ++state.promptSequence;
  const account = state.accountSequence;
  const context = state.contextSequence;
  const current = () => sequence === state.promptSequence && account === state.accountSequence && context === state.contextSequence && state.view === "result" && state.activeSubmission?.submission_id === run.submission_id;
  state.promptRecord = null;
  elements["submitted-prompt"].textContent = "";
  elements["continue-editing"].disabled = true;
  elements["result-prompt-retry"].classList.add("is-hidden");
  elements["result-prompt-status"].textContent = "正在加载本次提示词…";
  try {
    const payload = await apiRequest(`/v1/submissions/${encodeURIComponent(run.submission_id)}/prompt`, {}, true);
    if (!current()) return;
    if (payload.submission_id !== run.submission_id || payload.challenge_id !== run.challenge_id || typeof payload.student_prompt !== "string" || !/^[a-f0-9]{64}$/.test(payload.student_prompt_sha256)) throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
    state.promptRecord = payload;
    elements["submitted-prompt"].textContent = payload.student_prompt;
    const available = Boolean(challengeById(run.challenge_id));
    elements["result-prompt-status"].textContent = available ? "" : "任务已不在当前目录中，仍可查看本次提示词。";
    elements["continue-editing"].disabled = !available;
  } catch (error) {
    if (!current()) return;
    elements["result-prompt-status"].textContent = errorMessage(error, "加载本次提示词");
    elements["result-prompt-retry"].classList.remove("is-hidden");
  }
}
function continueEditing() {
  const prompt = state.promptRecord;
  if (!state.user || !prompt || prompt.submission_id !== state.activeSubmission?.submission_id || !challengeById(prompt.challenge_id)) return;
  const draft = state.promptDrafts.get(prompt.challenge_id);
  if (draft && draft !== prompt.student_prompt && !window.confirm("用本次提示词替换该任务当前的草稿？")) return;
  state.promptDrafts.set(prompt.challenge_id, prompt.student_prompt);
  selectChallenge(prompt.challenge_id);
  elements["student-prompt"].focus();
}
async function loadResultRoute() {
  const id = state.resultRouteId;
  if (!id || state.view !== "result") return;
  const sequence = ++state.resultRouteSequence;
  if (!state.user) {
    renderEmptyResult();
    elements["result-title"].textContent = "登录后查看此评测";
    elements["result-empty-help"].textContent = "请使用提交这条评测的账户登录。";
    elements["result-login"].classList.remove("is-hidden");
    return;
  }
  const account = state.accountSequence;
  const context = state.contextSequence;
  const current = () => sequence === state.resultRouteSequence && account === state.accountSequence && context === state.contextSequence && state.view === "result";
  renderEmptyResult();
  elements["result-title"].textContent = "正在加载评测…";
  try {
    const run = await apiRequest(`/v1/submissions/${encodeURIComponent(id)}`, {}, true);
    if (!current()) return;
    if (run.submission_id !== id || typeof run.challenge_id !== "string") throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
    openRun(run, false, false);
  } catch (error) {
    if (!current()) return;
    const content = createElement("div", "result-state");
    content.append(createElement("h3", "", "无法加载评测"), createElement("p", "", errorMessage(error, "加载评测")));
    const retry = createElement("button", "secondary-action", "重新加载");
    retry.type = "button";
    retry.addEventListener("click", loadResultRoute);
    content.append(retry);
    showResultContent(content);
  }
}
function openLeaderboard(fromResult = false) {
  const challenge = fromResult ? challengeById(state.activeSubmission?.challenge_id) : selectedChallenge();
  const identity = fromResult ? state.activeSubmission?.evaluation_identity_sha256 : challenge?.evaluation_identity_sha256;
  if (!identity) { showToast("此任务尚无可查看的排行榜。"); return; }
  elements["leaderboard-task"].textContent = challenge ? `${languageLabel(challenge.language)} · ${taskLabel(challenge.task)} · ${challenge.treebank}` : state.activeSubmission?.challenge_id || "";
  elements["leaderboard-back"].href = fromResult ? `#result/${encodeURIComponent(state.activeSubmission.submission_id)}` : `#challenge/${encodeURIComponent(challenge.challenge_id)}`;
  elements["leaderboard-back"].textContent = fromResult ? "← 返回评测结果" : "← 返回练习";
  state.leaderboardIdentity = identity;
  history.pushState(null, "", `#leaderboard/${encodeURIComponent(identity)}`);
  setView("leaderboard");
  loadLeaderboard(true);
}
function setLessonTab(name, focus = false) {
  for (const button of document.querySelectorAll(".lesson-tabs [role=tab]")) {
    const active = button.id === `lesson-tab-${name}`;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    elements[button.getAttribute("aria-controls")].hidden = !active;
    if (active && focus) button.focus();
  }
}
function currentViewHash() {
  if (state.view === "practice" && state.selectedChallengeId) return `#challenge/${encodeURIComponent(state.selectedChallengeId)}`;
  if (state.view === "result" && state.resultRouteId) return `#result/${encodeURIComponent(state.resultRouteId)}`;
  if (state.view === "leaderboard" && state.leaderboardIdentity) return `#leaderboard/${encodeURIComponent(state.leaderboardIdentity)}`;
  return `#${state.view}`;
}
function focusView(view) {
  const id = { challenges: "challenge-index-title", practice: "workbench-title", result: "result-page-title", runs: "runs-title", leaderboard: "leaderboard-title", admin: "admin-title" }[view];
  if (id) { elements[id].focus({ preventScroll: true }); window.scrollTo(0, 0); }
}
function setView(view) {
  const changed = state.view !== view;
  if (state.view !== view) {
    state.contextSequence++;
    stopPolling();
    state.runsSequence++;
    state.runsLoading = false;
    elements["refresh-runs"].disabled = false;
    elements["runs-more"].disabled = false;
  }
  state.view = view;
  for (const name of ["challenges", "practice", "result", "leaderboard", "runs", "admin"]) elements[`${name}-view`].classList.toggle("is-hidden", view !== name);
  for (const link of document.querySelectorAll("[data-view-link]")) {
    const active = link.dataset.viewLink === ({ practice: "challenges", leaderboard: "challenges", result: "runs" }[view] || view);
    link.classList.toggle("is-active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  if (view === "runs") loadRuns(true);
  if (view === "admin") { renderAdminAccess(); if (canManageTeaching() && !adminState.items.length && !adminState.busy) loadAdminList(); }
  if (changed) focusView(view);
}
function handleHash() {
  if (handleAccountHash()) return;
  if (elements["auth-dialog"].open && !state.authBusy) elements["auth-dialog"].close();
  if (location.hash.startsWith("#result/")) {
    setView("result");
    try { state.resultRouteId = decodeURIComponent(location.hash.slice(8)); loadResultRoute(); }
    catch (_) { renderEmptyResult(); showToast("评测链接格式有误，请从“我的评测”重新选择。"); }
    return;
  }
  if (location.hash.startsWith("#leaderboard/")) {
    setView("leaderboard");
    const identity = location.hash.slice(13);
    state.leaderboardIdentity = /^[a-f0-9]{64}$/.test(identity) ? identity : null;
    const challenge = state.challenges.find(item => item.evaluation_identity_sha256 === identity);
    elements["leaderboard-task"].textContent = challenge ? `${languageLabel(challenge.language)} · ${taskLabel(challenge.task)} · ${challenge.treebank}` : "";
    loadLeaderboard(true);
    return;
  }
  setView(location.hash === "#admin" ? "admin" : location.hash === "#runs" ? "runs" : location.hash.startsWith("#challenge/") ? "practice" : "challenges");
  if (state.view === "practice" && state.challenges.length) {
    try {
      const id = decodeURIComponent(location.hash.slice(11));
      if (challengeById(id)) selectChallenge(id, false); else { setView("challenges"); showToast("找不到链接中的任务，请从目录重新选择。"); }
    } catch (_) { setView("challenges"); showToast("任务链接格式有误，请从目录重新选择。"); }
  }
}

elements["challenge-search"].addEventListener("input", renderChallengeList);
elements["task-filter"].addEventListener("change", renderChallengeList);
elements["language-filter"].addEventListener("change", renderChallengeList);
elements["availability-filter"].addEventListener("change", renderChallengeList);
elements["filters-clear"].addEventListener("click", () => {
  for (const id of ["challenge-search", "language-filter", "task-filter", "availability-filter"]) elements[id].value = "";
  renderChallengeList();
});
elements["catalog-retry"].addEventListener("click", loadChallenges);
elements["student-prompt"].addEventListener("input", () => {
  if (state.selectedChallengeId) state.promptDrafts.set(state.selectedChallengeId, elements["student-prompt"].value);
  elements["prompt-error"].classList.add("is-hidden");
  updatePromptState();
});
elements["template-zero"].addEventListener("click", () => loadTemplate(false));
elements["template-few"].addEventListener("click", () => loadTemplate(true));
elements["teaching-retry"].addEventListener("click", () => { if (selectedChallenge()) renderTeaching(selectedChallenge()); });
elements["prompt-form"].addEventListener("submit", submitPrompt);
elements["leaderboard-refresh"].addEventListener("click", () => loadLeaderboard(true));
elements["leaderboard-more"].addEventListener("click", () => loadLeaderboard(false));
elements["refresh-runs"].addEventListener("click", () => loadRuns(true));
elements["runs-more"].addEventListener("click", () => loadRuns(false));
elements["practice-leaderboard"].addEventListener("click", () => openLeaderboard());
elements["result-leaderboard"].addEventListener("click", () => openLeaderboard(true));
elements["continue-editing"].addEventListener("click", continueEditing);
elements["result-prompt-retry"].addEventListener("click", loadSubmittedPrompt);
for (const button of document.querySelectorAll(".lesson-tabs [role=tab]")) {
  button.addEventListener("click", () => setLessonTab(button.id.slice(11)));
  button.addEventListener("keydown", event => {
    const names = ["description", "example", "scoring"];
    const index = names.indexOf(button.id.slice(11));
    const next = event.key === "ArrowRight" ? (index + 1) % 3 : event.key === "ArrowLeft" ? (index + 2) % 3 : event.key === "Home" ? 0 : event.key === "End" ? 2 : -1;
    if (next >= 0) { event.preventDefault(); setLessonTab(names[next], true); }
  });
}
window.addEventListener("hashchange", handleHash);
bindAccountEvents();
bindAdminEvents();
handleHash();
bootstrapAuth();
loadDevelopment();
loadChallenges();
