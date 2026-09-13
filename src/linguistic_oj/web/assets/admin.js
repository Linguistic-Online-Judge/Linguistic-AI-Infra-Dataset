"use strict";

const TEACHING_FIELDS = { title: [1, 120, "教学标题"], summary: [0, 500, "摘要"], instructions: [1, 4000, "任务讲解"], zero_shot_prompt: [1, 3000, "零样本提示词"], few_shot_prompt: [1, 3000, "少样本提示词"] };
const adminState = { sequence: 0, items: [], detail: null, busy: false, dirty: false, checked: null, conflict: false, blocked: false, suspended: false, validation: null, finishValidation: null };

function canManageTeaching() { return state.authMode === "cookie" && state.user?.role === "admin" && !adminState.blocked && !adminState.suspended; }
function adminContent() { return Object.fromEntries(Object.keys(TEACHING_FIELDS).map((key) => [key, elements[`admin-content-${key}`].value])); }

function clearAdminState() {
  adminState.sequence++;
  finishAdminRevalidation();
  Object.assign(adminState, { items: [], detail: null, busy: false, dirty: false, checked: null, conflict: false, blocked: false, suspended: false });
  elements["admin-form"].reset();
  for (const id of ["admin-list", "admin-source", "admin-preview-content", "admin-published-content", "admin-issues"]) elements[id].replaceChildren();
  for (const id of ["admin-coordinate", "admin-task-title", "admin-revisions", "admin-preview-title", "admin-published-status", "admin-error", "admin-action-status", "admin-validation", "admin-admissions-help", "admin-admission-mark"]) elements[id].textContent = "";
  for (const id of ["admin-detail", "admin-preview", "admin-error"]) elements[id].classList.add("is-hidden");
  elements["admin-status"].textContent = "请使用管理员账户登录。";
}

function suspendAdminRequests() {
  if (!canManageTeaching()) return;
  // Hold response delivery until this read rules out a same-user demotion.
  adminState.suspended = true;
  adminState.validation = new Promise((resolve) => { adminState.finishValidation = resolve; });
  updateAdminControls();
}

function finishAdminRevalidation() {
  adminState.suspended = false;
  adminState.finishValidation?.();
  adminState.finishValidation = null;
  adminState.validation = null;
}

function revokeAdminAccess() {
  clearAdminState();
  adminState.blocked = true;
  renderAdminAccess();
  showToast(errorMessage({ code: "ADMIN_REQUIRED" }));
  revalidateSession();
}

function renderAdminAccess() {
  const allowed = canManageTeaching();
  elements["admin-nav"].classList.toggle("is-hidden", !allowed);
  if (!allowed && !adminState.suspended) {
    elements["admin-detail"].classList.add("is-hidden");
    elements["admin-status"].textContent = state.authMode === "checking" ? "正在确认管理权限…" : "教学管理仅向管理员开放。请通过右上角账户入口登录；权限变更后需重新登录。";
  }
  updateAdminControls();
}

function teachingValidation(content, revision = 0) {
  const issues = [];
  for (const [key, [min, max, label]] of Object.entries(TEACHING_FIELDS)) {
    const value = content[key];
    const count = typeof value === "string" ? Array.from(value).length : -1;
    if (count < min || count > max || (min && !value.trim())) issues.push(`${label}需 ${min}–${max} 个字符${min ? "，不能只有空白" : ""}。`);
  }
  const bytes = new TextEncoder().encode(JSON.stringify({ expected_revision: revision, content })).length;
  if (bytes > 16384) issues.push("保存请求超过 16,384 字节，请缩短内容。");
  return { issues, bytes };
}

function updateAdminControls() {
  const detail = adminState.detail;
  const locked = !canManageTeaching() || adminState.busy;
  const validation = teachingValidation(adminContent(), detail?.revision || 0);
  const saved = detail?.draft && !adminState.dirty;
  const editable = !locked && Boolean(detail) && !adminState.conflict;
  const hasContract = adminState.items.find((item) => item.challenge_id === detail?.challenge.challenge_id)?.has_contract === true;
  for (const key of Object.keys(TEACHING_FIELDS)) elements[`admin-content-${key}`].disabled = !editable;
  elements["admin-refresh"].disabled = locked;
  elements["admin-reload"].disabled = locked || !detail;
  elements["admin-preview-button"].disabled = locked || !detail;
  elements["admin-save"].disabled = !editable || !adminState.dirty || validation.issues.length > 0;
  elements["admin-check"].disabled = !editable || !saved;
  elements["admin-publish"].disabled = !editable || !saved || adminState.checked?.can_publish !== true || adminState.checked?.revision !== detail?.revision;
  elements["admin-pause"].disabled = !editable || !hasContract || adminState.dirty || detail?.admissions_closed === true;
  elements["admin-resume"].disabled = !editable || !hasContract || adminState.dirty || detail?.admissions_closed !== true || detail?.can_reopen !== true;
  for (const button of elements["admin-list"].querySelectorAll("button")) button.disabled = locked;
  if (detail) elements["admin-validation"].textContent = `${validation.bytes} / 16,384 字节。${validation.issues.join(" ") || (adminState.dirty ? "有未保存修改，请先保存草稿。" : "草稿已保存；发布前请检查当前版本。")}`;
}

function renderAdminList() {
  elements["admin-list"].replaceChildren();
  for (const item of adminState.items) {
    const button = createElement("button", "challenge-card");
    button.type = "button";
    button.dataset.challengeId = item.challenge_id;
    button.classList.toggle("is-selected", item.challenge_id === adminState.detail?.challenge.challenge_id);
    button.setAttribute("aria-pressed", String(item.challenge_id === adminState.detail?.challenge.challenge_id));
    button.append(createElement("span", "coordinate-line", `${languageLabel(item.language)} / ${item.treebank}`), createElement("strong", "challenge-card-title", taskLabel(item.task)), createElement("span", "challenge-card-meta", item.title), createElement("span", "challenge-card-meta", `${item.admissions_closed ? "已暂停" : "未暂停"} · ${item.has_contract ? "已配置评测约定" : "无评测约定"} · 修订 ${item.revision} / 发布 ${item.published_revision}`));
    button.addEventListener("click", () => loadAdminDetail(item.challenge_id));
    elements["admin-list"].append(button);
  }
}

async function loadAdminList() {
  if (!canManageTeaching() || adminState.busy) return;
  const sequence = ++adminState.sequence;
  adminState.busy = true;
  elements["admin-status"].textContent = "正在读取已有任务…";
  updateAdminControls();
  try {
    const page = await apiRequest("/v1/admin/challenges", {}, true);
    if (sequence !== adminState.sequence) return;
    if (!Array.isArray(page.items)) throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
    adminState.items = page.items;
    renderAdminList();
    elements["admin-status"].textContent = page.items.length ? `${page.items.length} 项已有任务。选择任务后编辑教学内容；任务来源保持只读。` : "暂无可管理任务。此页面不创建任务，请联系维护者核对目录。";
  } catch (error) {
    if (sequence === adminState.sequence) elements["admin-status"].textContent = `${errorMessage(error, "读取管理目录")} 点击“刷新任务列表”重试。`;
  } finally {
    if (sequence === adminState.sequence) { adminState.busy = false; updateAdminControls(); }
  }
}

function renderTeachingContent(container, content) {
  container.replaceChildren();
  if (!content) return;
  for (const [key, [, , label]] of Object.entries(TEACHING_FIELDS)) container.append(createElement("h4", "", label), createElement("p", "plain-text", content[key] || "（未填写）"));
}

function renderAdminDetail(detail) {
  if (!detail?.challenge?.challenge_id || !Number.isInteger(detail.revision)) throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
  adminState.detail = detail;
  adminState.dirty = false;
  adminState.checked = null;
  adminState.conflict = false;
  const challenge = detail.challenge;
  elements["admin-coordinate"].textContent = `${languageLabel(challenge.language)} / ${challenge.treebank} / ${taskLabel(challenge.task)}`;
  elements["admin-task-title"].textContent = challenge.title;
  elements["admin-admission-mark"].textContent = detail.admissions_closed ? "已暂停" : challenge.accepting_submissions ? "可提交" : "未开放";
  elements["admin-admission-mark"].classList.toggle("is-open", challenge.accepting_submissions);
  elements["admin-revisions"].textContent = `当前修订 ${detail.revision} · 已发布版本 ${detail.published_revision} · ${detail.draft ? "有已保存草稿" : "尚无教学草稿"}`;
  const source = [["任务 ID", challenge.challenge_id], ["语言 / 树库", `${languageLabel(challenge.language)} / ${challenge.treebank}`], ["任务 / 版本", `${taskLabel(challenge.task)} / ${challenge.version}`], ["模型", challenge.model_identity?.model], ["主要指标", METRIC_LABELS[challenge.primary_metric] || challenge.primary_metric], ["样本数", challenge.sample_count], ["评测身份", challenge.evaluation_identity_sha256], ["来源版本", challenge.source_release], ["数据范围", challenge.security_level], ["标注许可", challenge.annotation_license], ["署名要求", challenge.attribution_requirements], ["相同方式共享", challenge.share_alike_requirements], ["原文权利", challenge.underlying_text_rights], ["适用范围与局限", challenge.benchmark_limitations], ["数据摘要", challenge.dataset_sha256], ["样本选择摘要", challenge.selection_sha256]];
  elements["admin-source"].replaceChildren(...source.flatMap(([label, value]) => [createElement("dt", "", label), createElement("dd", "", value ?? "未提供")]));
  for (const key of Object.keys(TEACHING_FIELDS)) elements[`admin-content-${key}`].value = detail.draft?.[key] ?? detail.published?.[key] ?? "";
  elements["admin-preview"].classList.add("is-hidden");
  elements["admin-preview-title"].textContent = "";
  elements["admin-preview-content"].replaceChildren();
  elements["admin-error"].classList.add("is-hidden");
  elements["admin-issues"].replaceChildren();
  elements["admin-published-status"].textContent = detail.published ? `学生只会读取已发布版本 ${detail.published_revision}，不会读取此处草稿。` : "尚未发布。学生继续使用平台手写教学说明。";
  renderTeachingContent(elements["admin-published-content"], detail.published);
  elements["admin-admissions-help"].textContent = `${detail.admissions_closed ? "已暂停新提交。" : "提交开关未暂停，是否可提交仍取决于原有评测条件。"} 已受理的评测和历史记录不受影响。${detail.can_reopen ? "恢复也不会改变数据、模型或许可。" : "当前来源权利或运行条件不允许恢复；本页不能绕过这些限制。"} 如有未保存修改，请先保存或重新加载。`;
  elements["admin-detail"].classList.remove("is-hidden");
  const index = adminState.items.findIndex((item) => item.challenge_id === challenge.challenge_id);
  if (index >= 0) Object.assign(adminState.items[index], { revision: detail.revision, published_revision: detail.published_revision, admissions_closed: detail.admissions_closed, can_reopen: detail.can_reopen });
  renderAdminList();
  updateAdminControls();
}

async function loadAdminDetail(id = adminState.detail?.challenge.challenge_id) {
  if (!id || !canManageTeaching() || adminState.busy) return;
  if (adminState.dirty && !window.confirm("重新加载或切换任务会丢弃此页未保存的教学修改，是否继续？")) return;
  const sequence = ++adminState.sequence;
  adminState.busy = true;
  elements["admin-action-status"].textContent = "正在读取任务详情…";
  updateAdminControls();
  try {
    const detail = await apiRequest(`/v1/admin/challenges/${encodeURIComponent(id)}`, {}, true);
    if (sequence !== adminState.sequence) return;
    renderAdminDetail(detail);
    elements["admin-action-status"].textContent = "已读取当前版本。保存、检查、发布分别执行，不会自动开放评测。";
  } catch (error) {
    if (sequence === adminState.sequence) elements["admin-status"].textContent = `${errorMessage(error, "读取任务详情")} 请重新选择任务重试。`;
  } finally {
    if (sequence === adminState.sequence) { adminState.busy = false; updateAdminControls(); }
  }
}

async function adminAction(action) {
  const button = elements[`admin-${action}`];
  if (button.disabled || !canManageTeaching() || !adminState.detail) return;
  if (action === "publish" && !window.confirm("发布后，所有学生都能读取当前已保存的教学内容。此操作不会开放评测，是否继续？")) return;
  if (action === "pause" && !window.confirm("暂停此任务的新提交？已受理的评测继续处理，历史记录仍可查看。")) return;
  const detail = adminState.detail;
  const body = { expected_revision: detail.revision };
  if (action === "save") body.content = adminContent();
  if (["pause", "resume"].includes(action)) body.closed = action === "pause";
  const path = { save: "teaching-draft", check: "check", publish: "publish", pause: "admissions", resume: "admissions" }[action];
  const sequence = ++adminState.sequence;
  adminState.busy = true;
  elements["admin-error"].classList.add("is-hidden");
  elements["admin-action-status"].textContent = "正在处理，请勿重复操作…";
  updateAdminControls();
  try {
    const payload = await apiRequest(`/v1/admin/challenges/${encodeURIComponent(detail.challenge.challenge_id)}/${path}`, { method: action === "save" ? "PUT" : "POST", body: JSON.stringify(body) }, true);
    if (sequence !== adminState.sequence) return;
    if (action === "check") {
      if (payload.revision !== detail.revision || typeof payload.can_publish !== "boolean" || !Array.isArray(payload.issues)) throw new APIRequestError(409, { code: "REVISION_CONFLICT" });
      adminState.checked = payload;
      elements["admin-issues"].replaceChildren(...payload.issues.map((issue) => createElement("li", "", `${issue.message} (${issue.code})`)));
      elements["admin-action-status"].textContent = payload.can_publish ? "当前已保存版本通过发布检查。点击“发布教学内容”后才会公开；检查不代表提示词效果最优。" : "当前版本未通过发布检查。请按下方问题修改草稿并重新保存。";
    } else {
      renderAdminDetail(payload);
      elements["admin-action-status"].textContent = { save: "草稿已保存。学生看到的已发布内容保持不变，请检查后再发布。", publish: "教学内容已发布。提交开关保持不变；学生重新加载教学内容后可读取此版本。", pause: "已暂停新提交。已有评测与历史记录保留。", resume: "已恢复提交开关。实际提交仍以服务端检查为准。" }[action];
      if (["pause", "resume"].includes(action)) loadChallenges();
    }
  } catch (error) {
    if (sequence !== adminState.sequence) return;
    adminState.checked = null;
    // A conflict or uncertain write must be reconciled explicitly, never replayed.
    adminState.conflict = error.status === 409 || (action !== "check" && (error.status === 0 || error.status >= 500 || error.code === "INVALID_RESPONSE"));
    elements["admin-error"].textContent = adminState.conflict ? "版本或提交条件已变更，或上次写入结果无法确认。请点击“重新加载此任务”核对最新版本，再决定是否修改；不会自动重试或覆盖。" : error.status === 422 ? "教学内容未通过服务端检查。请核对字段长度，并移除换行以外的控制字符，再保存草稿。" : error.status === 413 ? "保存请求超过服务端字节限制。请缩短教学内容后重试。" : `${errorMessage(error, "完成教学管理操作")} 请检查内容后重试。`;
    elements["admin-error"].classList.remove("is-hidden");
    elements["admin-action-status"].textContent = "操作未获确认。";
  } finally {
    if (sequence === adminState.sequence) { adminState.busy = false; updateAdminControls(); }
  }
}

function bindAdminEvents() {
  elements["admin-refresh"].addEventListener("click", loadAdminList);
  elements["admin-reload"].addEventListener("click", () => loadAdminDetail());
  elements["admin-form"].addEventListener("submit", (event) => { event.preventDefault(); adminAction("save"); });
  for (const action of ["check", "publish", "pause", "resume"]) elements[`admin-${action}`].addEventListener("click", () => adminAction(action));
  elements["admin-form"].addEventListener("input", () => {
    adminState.dirty = JSON.stringify(adminContent()) !== JSON.stringify(adminState.detail?.draft || adminState.detail?.published || Object.fromEntries(Object.keys(TEACHING_FIELDS).map((key) => [key, ""])));
    adminState.checked = null;
    elements["admin-preview"].classList.add("is-hidden");
    elements["admin-issues"].replaceChildren();
    updateAdminControls();
  });
  elements["admin-preview-button"].addEventListener("click", () => {
    if (!canManageTeaching()) return;
    elements["admin-preview-title"].textContent = adminContent().title || "未填写教学标题";
    renderTeachingContent(elements["admin-preview-content"], adminContent());
    elements["admin-preview"].classList.remove("is-hidden");
  });
  window.addEventListener("beforeunload", (event) => { if (adminState.dirty) { event.preventDefault(); event.returnValue = ""; } });
}
