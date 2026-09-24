// Real Edge UI checks. --fixture is synthetic; --url requires an isolated loopback Mock app.
// Permission/runtime faults are explicitly injected, never edits to an application database.
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { startBrowser, sleep } from "./cdp.mjs";
import { startFixtureServer } from "./fixture-server.mjs";

const root = resolve(import.meta.dirname, "../..");
const fixture = process.argv.includes("--fixture") ? await startFixtureServer(root) : null;
const index = process.argv.indexOf("--url");
const base = fixture?.url || (index >= 0 && process.argv[index + 1]);
if (!base || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base) || new URL(base).port === "8080") throw new Error("Use --fixture or --url http://127.0.0.1:ISOLATED_TEST_PORT. The daily port 8080 is forbidden.");
const artifacts = resolve(root, "runtime/browser-tests", `admin-${fixture ? "fixtures" : "local-app"}-${Date.now()}`);
const a = await startBrowser(artifacts);
const student = await startBrowser(resolve(artifacts, "student"));
const b = await a.newPage();
const checks = [];
const requests = [];
const rules = new Map([[a, []], [b, []], [student, []]]);
let failure;
const check = (name) => { checks.push(name); console.log(`PASS ${name}`); };
const json = (page, event, status, payload) => page.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: status, responseHeaders: [{ name: "Content-Type", value: "application/json" }], body: Buffer.from(JSON.stringify(payload)).toString("base64") });
const inject = (page, path, handler, { method = "GET", stage = "Request" } = {}) => rules.get(page).push({ path, method, stage, handler });
const pause = (page, path, method = "GET") => {
  let held;
  inject(page, path, (event) => { held = event; }, { method, stage: "Response" });
  return {
    async wait() { for (let i = 0; i < 200 && !held; i++) await sleep(50); assert.ok(held, `Held ${method} ${path}`); },
    release: () => page.send("Fetch.continueResponse", { requestId: held.requestId }),
  };
};
for (const page of [a, b, student]) {
  page.on("Fetch.requestPaused", async (event) => {
    try {
      const path = new URL(event.request.url).pathname;
      const stage = event.responseStatusCode || event.responseErrorReason ? "Response" : "Request";
      if (stage === "Request") requests.push({ page, path, method: event.request.method, headers: Object.fromEntries(Object.entries(event.request.headers).map(([key, value]) => [key.toLowerCase(), value])), body: event.request.postData });
      const list = rules.get(page);
      const index = list.findIndex((rule) => rule.path === path && rule.method === event.request.method && rule.stage === stage);
      if (index >= 0) await list.splice(index, 1)[0].handler(event);
      else await page.send(stage === "Response" ? "Fetch.continueResponse" : "Fetch.continueRequest", { requestId: event.requestId });
    } catch (error) { failure ||= error; }
  });
  page.on("Page.javascriptDialogOpening", () => { page.send("Page.handleJavaScriptDialog", { accept: true }).catch((error) => { failure ||= error; }); });
  await page.send("Fetch.enable", { patterns: [{ urlPattern: "*/v1/*", requestStage: "Request" }, { urlPattern: "*/v1/*", requestStage: "Response" }] });
  await page.viewport(1440, 1050);
}
let navigation = 0;
const navigate = async (page, fragment = "") => { await page.navigate(`${base}/?admin-test=${++navigation}${fragment}`); await page.wait("state.authMode !== 'checking' && state.challenges.length >= 5 && !state.sessionCheckSequence"); };
const hash = async (page, value) => { await page.evaluate(`location.hash=${JSON.stringify(value)}`); await sleep(100); };
const login = async (page, account) => {
  await page.send("Page.bringToFront");
  await page.wait("!state.sessionCheckSequence");
  await page.evaluate("openAuthDialog('login')");
  await page.fill("#auth-email", account.email);
  await page.fill("#auth-password", account.password);
  await page.click("#auth-submit");
  await page.wait(`state.user?.public_handle === ${JSON.stringify(account.public_handle)} && !state.authBusy && !state.sessionCheckSequence && !elements['auth-dialog'].open`);
};
const select = async (page, id) => { await hash(page, `#challenge/${id}`); await page.wait(`state.selectedChallengeId === ${JSON.stringify(id)} && teachingState.status === 'ready'`); };
const manage = async (page, id) => {
  await page.send("Page.bringToFront");
  await hash(page, "#admin");
  await page.wait("adminState.items.length > 0 && !adminState.busy && canManageTeaching()");
  await page.click(`#admin-list [data-challenge-id="${id}"]`);
  await page.wait(`adminState.detail?.challenge.challenge_id === ${JSON.stringify(id)} && !adminState.busy`);
};
const fillContent = async (page, content) => { for (const [key, value] of Object.entries(content)) await page.fill(`#admin-content-${key}`, value); };
const action = async (page, name) => {
  await page.send("Page.bringToFront");
  await sleep(100);
  await page.wait("!state.sessionCheckSequence && !adminState.suspended && !adminState.busy");
  const sequence = await page.evaluate("adminState.sequence");
  await page.click(`#admin-${name}`);
  await page.wait(`adminState.sequence > ${sequence} && !adminState.busy`, `admin ${name} completed`);
};
const publicTeaching = (id) => student.evaluate(`apiRequest('/v1/challenges/${id}/teaching')`);
const rawPrivate = (page, path, expectedExpression = "state.user.user_id") => page.evaluate(`fetch(${JSON.stringify(path)}, {credentials:'same-origin',headers:{'X-LOJ-Expected-User':${expectedExpression}}}).then(async r=>({status:r.status,body:await r.json()}))`);
const cleanAdmin = async (page) => {
  await page.wait("!adminState.detail && adminState.items.length === 0 && elements['admin-nav'].classList.contains('is-hidden')");
  assert.equal(await page.evaluate("Object.keys(TEACHING_FIELDS).map(k=>elements['admin-content-'+k].value).join('')"), "");
  for (const id of ["admin-list", "admin-source", "admin-preview-content", "admin-published-content", "admin-task-title"]) assert.equal(await page.evaluate(`elements[${JSON.stringify(id)}].textContent`), "");
  assert.equal(await page.evaluate("elements['admin-save'].disabled && elements['admin-publish'].disabled"), true);
};

try {
  await navigate(a, "#admin");
  assert.equal(await a.evaluate("elements['admin-nav'].classList.contains('is-hidden')"), true);
  assert.equal((await rawPrivate(a, "/v1/admin/challenges", "'anonymous'")).status, 401);
  const development = await a.evaluate("fetch('/v1/development',{headers:{'X-LOJ-Development':'1'}}).then(r=>r.json())");
  assert.equal(development.evaluation_mode, "mock", "This suite must never run against a non-Mock service");
  const admin = development.accounts.find((item) => item.email === "admin@example.test");
  const alice = development.accounts.find((item) => item.email === "alice@example.test");
  const bob = development.accounts.find((item) => item.email === "bob@example.test");
  assert.ok(admin && alice && bob);
  const id = await a.evaluate("state.challenges.find(c=>c.task === 'upos' && c.accepting_submissions).challenge_id");
  const path = `/v1/admin/challenges/${id}`;
  await login(a, alice);
  for (const endpoint of ["/v1/admin/challenges", path, "/v1/admin/challenges/unknown-task"]) {
    const result = await rawPrivate(a, endpoint);
    assert.equal(result.status, 403);
    assert.equal(result.body.error.code, "ADMIN_REQUIRED");
    assert.equal(result.body.challenge, undefined);
  }
  await cleanAdmin(a);
  check("anonymous management is rejected; student GETs return generic ADMIN_REQUIRED even for unknown IDs");

  await login(a, admin);
  await manage(a, id);
  const adminId = await a.evaluate("state.user.user_id");
  assert.equal(await a.evaluate("elements['admin-nav'].classList.contains('is-hidden')"), false);
  assert.equal(await a.evaluate("document.querySelectorAll('#admin-source input, #admin-source textarea').length"), 0);
  assert.ok(await a.evaluate("elements['admin-source'].textContent.includes(adminState.detail.challenge.challenge_id)"));
  assert.equal(await a.evaluate("adminState.detail.draft"), null, "Use a fresh, isolated database with no teacher drafts");
  const sourceIdentity = await a.evaluate("adminState.detail.challenge.evaluation_identity_sha256");
  await navigate(student);
  await login(student, alice);
  await select(student, id);
  assert.equal((await publicTeaching(id)).content, null);
  const schema = await student.evaluate("elements['task-schema'].textContent");
  await student.fill("#student-prompt", `Admin pause history test ${Date.now()}`);
  await student.click("#run-button");
  await student.wait("elements['result-content'].textContent.includes('评测已完成') && !state.submitting", "existing student result", 45000);
  const runId = await student.evaluate("state.activeSubmission.submission_id");
  await select(student, id);
  check("cookie admin sees existing tasks and read-only sources; unpublished student guide keeps fixed output schemas");

  const contentA = { title: "公开教学 <img src=x onerror=alert(1)>", summary: "英语词性教学；这是纯文本，不是 HTML。", instructions: "先按句子判断每个词元的语法功能。\n输出结构由平台固定，教学发布不改变评分。", zero_shot_prompt: "Teacher version A: label the given English tokens with UPOS. Return only JSON with tags.", few_shot_prompt: 'Teacher version A with example: Birds sing . => {"tags":["NOUN","VERB","PUNCT"]}. Return only the requested JSON.' };
  await fillContent(a, contentA);
  await a.fill("#admin-content-title", "x".repeat(121));
  assert.equal(await a.evaluate("elements['admin-save'].disabled"), true);
  await a.fill("#admin-content-title", contentA.title);
  await a.fill("#admin-content-instructions", "字".repeat(4000));
  await a.fill("#admin-content-zero_shot_prompt", "字".repeat(3000));
  assert.equal(await a.evaluate("elements['admin-save'].disabled"), true);
  assert.match(await a.evaluate("elements['admin-validation'].textContent"), /16,384/);
  await fillContent(a, contentA);
  await a.click("#admin-preview-button");
  assert.equal(await a.evaluate("elements['admin-preview-title'].textContent"), contentA.title);
  assert.equal(await a.evaluate("elements['admin-preview'].querySelectorAll('img,script').length"), 0);
  await action(a, "save");
  assert.equal(await a.evaluate("adminState.detail.revision"), 1);
  assert.equal((await publicTeaching(id)).content, null);
  assert.equal(await a.evaluate("elements['admin-publish'].disabled"), true);
  check("plaintext preview, field and UTF-8 envelope limits, draft save and pre-publish disabled states");

  await action(a, "pause");
  assert.equal(await a.evaluate("adminState.detail.admissions_closed"), true);
  await student.evaluate("loadChallenges()");
  await student.wait("selectedChallenge().admissions_closed && !elements['availability-mark'].classList.contains('is-open')");
  await student.fill("#student-prompt", "Student draft must not be silently replaced");
  assert.equal(await student.evaluate("elements['run-button'].disabled"), true);
  assert.match(await student.evaluate("elements['availability-note'].textContent"), /暂停/);
  const paused = await student.evaluate(`apiRequest('/v1/submissions',{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({challenge_id:${JSON.stringify(id)},student_prompt:'Must reject new paused submission'})},true).then(()=>null,e=>({status:e.status,code:e.code}))`);
  assert.deepEqual(paused, { status: 409, code: "CHALLENGE_PAUSED" });
  const history = await student.evaluate("apiRequest('/v1/submissions?limit=100',{},true)");
  assert.ok(history.items.some((item) => item.submission_id === runId));
  assert.equal((await rawPrivate(student, `/v1/submissions/${runId}/result`)).status, 200);
  check("pause disables new submissions with CHALLENGE_PAUSED, while owner history and existing results remain available");

  await action(a, "check");
  assert.equal(await a.evaluate("elements['admin-publish'].disabled"), false);
  await action(a, "publish");
  assert.equal(await a.evaluate("adminState.detail.admissions_closed"), true);
  assert.equal(await a.evaluate("adminState.detail.challenge.evaluation_identity_sha256"), sourceIdentity);
  const publishedA = await publicTeaching(id);
  assert.deepEqual(publishedA.content, contentA);
  assert.deepEqual(Object.keys(publishedA).sort(), ["challenge_id", "content", "published_revision"]);
  await student.click("#teaching-retry");
  await student.wait(`teachingState.payload?.published_revision === ${publishedA.published_revision}`);
  assert.equal(await student.evaluate("elements['student-prompt'].value"), "Student draft must not be silently replaced");
  assert.equal(await student.evaluate("elements['task-schema'].textContent"), schema);
  assert.equal(await student.evaluate("elements['published-title'].textContent"), contentA.title);
  assert.equal(await student.evaluate("elements['published-teaching'].querySelectorAll('img,script').length"), 0);
  await student.click("#template-zero");
  assert.equal(await student.evaluate("elements['student-prompt'].value"), contentA.zero_shot_prompt);
  check("publish exposes only saved plaintext teaching, not drafts, scoring changes, or an admissions reopen");

  const contentB = { ...contentA, title: "英语词性：已发布版本 B", zero_shot_prompt: "Teacher version B: classify tokens. Return tags JSON only.", few_shot_prompt: "Teacher version B: Birds => NOUN. Follow the fixed JSON contract." };
  await fillContent(a, contentB);
  await action(a, "save");
  assert.deepEqual((await publicTeaching(id)).content, contentA);
  await action(a, "check");
  await action(a, "publish");
  assert.deepEqual((await publicTeaching(id)).content, contentB);
  await student.click("#template-few");
  assert.equal(await student.evaluate("elements['student-prompt'].value"), contentA.few_shot_prompt, "Template must come from the same version as the displayed lesson, not a newer fetch");
  await student.click("#teaching-retry");
  await student.wait(`elements['published-title'].textContent === ${JSON.stringify(contentB.title)}`);
  assert.equal(await student.evaluate("elements['student-prompt'].value"), contentA.few_shot_prompt);
  await student.click("#template-zero");
  assert.equal(await student.evaluate("elements['student-prompt'].value"), contentB.zero_shot_prompt);
  check("later draft edits leave publication unchanged; explicit template clicks use exactly the displayed published version");

  const detail = await a.evaluate("adminState.detail");
  inject(a, path, (event) => json(a, event, 200, { ...detail, can_reopen: false }));
  await action(a, "reload");
  assert.equal(await a.evaluate("elements['admin-resume'].disabled"), true);
  assert.match(await a.evaluate("elements['admin-admissions-help'].textContent"), /不允许恢复/);
  const beforeResume = requests.filter((r) => r.path.endsWith("/admissions") && r.method === "POST").length;
  await a.evaluate("elements['admin-resume'].click()");
  assert.equal(requests.filter((r) => r.path.endsWith("/admissions") && r.method === "POST").length, beforeResume);
  await action(a, "reload");
  assert.equal(await a.evaluate("adminState.detail.can_reopen"), true);
  await action(a, "resume");
  await student.evaluate("loadChallenges()");
  await student.wait("selectedChallenge().accepting_submissions && !elements['run-button'].disabled");
  check("injected ineligible policy disables resume without a request; real eligible resume re-enables new submissions");

  await navigate(b, "#admin");
  await manage(b, id);
  const revisionBeforeConflict = await b.evaluate("adminState.detail.revision");
  await a.fill("#admin-content-title", "New saved draft after B; still not published");
  await action(a, "save");
  await b.fill("#admin-content-title", "Stale edit that must not overwrite the newer draft");
  const beforeConflict = requests.filter((r) => r.page === b && r.method === "PUT").length;
  await action(b, "save");
  assert.equal(await b.evaluate("adminState.detail.revision"), revisionBeforeConflict);
  assert.equal(await b.evaluate("adminState.conflict && elements['admin-save'].disabled && elements['admin-publish'].disabled"), true);
  assert.match(await b.evaluate("elements['admin-error'].textContent"), /重新加载此任务/);
  await sleep(200);
  assert.equal(requests.filter((r) => r.page === b && r.method === "PUT").length, beforeConflict + 1);
  await action(b, "reload");
  assert.equal(await b.evaluate("elements['admin-content-title'].value"), "New saved draft after B; still not published");
  assert.deepEqual((await publicTeaching(id)).content, contentB);
  check("two admin tabs produce a real revision conflict, block blind overwrites, and reload only on explicit confirmation");

  const currentRevision = await a.evaluate("adminState.detail.revision");
  inject(a, `${path}/check`, (event) => json(a, event, 200, { revision: currentRevision, can_publish: false, issues: [{ code: "INJECTED_CHECK", message: "Resolve this teaching issue <img src=x>." }] }), { method: "POST" });
  await action(a, "check");
  assert.equal(await a.evaluate("elements['admin-publish'].disabled"), true);
  assert.match(await a.evaluate("elements['admin-issues'].textContent"), /INJECTED_CHECK/);
  assert.equal(await a.evaluate("elements['admin-issues'].querySelectorAll('img').length"), 0);
  check("failed publish-check issues stay plaintext and keep publication disabled (injected issue)");

  const beforeFocusRevision = await a.evaluate("adminState.detail.revision");
  const beforeFocusAccount = await a.evaluate("state.accountSequence");
  await a.fill("#admin-content-title", "同一管理员切回页面后的教学草稿");
  const sameAccountSave = pause(a, `${path}/teaching-draft`, "PUT");
  await a.click("#admin-save");
  await sameAccountSave.wait();
  const sameAccountSession = pause(a, "/v1/auth/session");
  await a.evaluate("void revalidateSession()");
  await sameAccountSession.wait();
  await sameAccountSave.release();
  await sleep(150);
  assert.equal(await a.evaluate("adminState.busy"), true, "A private response must wait for pending role confirmation");
  assert.equal(await a.evaluate("adminState.detail.revision"), beforeFocusRevision);
  await a.evaluate("void revalidateSession()");
  await sameAccountSession.release();
  await a.wait("!adminState.busy && !state.sessionCheckSequence && !state.authRecheckPending");
  assert.equal(await a.evaluate("adminState.detail.revision"), beforeFocusRevision + 1);
  assert.equal(await a.evaluate("state.accountSequence"), beforeFocusAccount);
  assert.equal(await a.evaluate("elements['admin-content-title'].value"), "同一管理员切回页面后的教学草稿");
  check("consecutive same-account role checks hold private write delivery, then preserve the successful save without retry");

  await a.click("#admin-preview-button");
  for (const width of [1440, 768, 390, 320]) {
    await a.viewport(width, 1050, width < 760);
    await a.evaluate("elements['admin-detail'].scrollIntoView({block:'start',behavior:'instant'})");
    assert.equal(await a.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), true, `Admin overflow at ${width}`);
    await a.screenshot(`admin-editor-${width}`);
    assert.equal(await a.evaluate("elements['admin-nav'].getBoundingClientRect().width > 0"), true);
  }
  await a.evaluate("elements['admin-preview'].scrollIntoView({block:'center',behavior:'instant'})");
  await a.screenshot("admin-preview-320");
  await student.viewport(320, 1050, true);
  await student.evaluate("elements['published-teaching'].scrollIntoView({block:'center',behavior:'instant'})");
  assert.equal(await student.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), true);
  await student.screenshot("student-published-320");
  await a.viewport(1440, 1050);
  check("admin desktop/tablet/mobile/320px layouts, three-item mobile navigation and plaintext preview screenshots");

  await a.fill("#admin-content-title", "Private draft before cross-tab switch");
  const lateSave = pause(a, `${path}/teaching-draft`, "PUT");
  await a.click("#admin-save");
  await lateSave.wait();
  await login(b, bob);
  await cleanAdmin(a);
  await lateSave.release();
  await sleep(150);
  await cleanAdmin(a);
  const mismatch = await rawPrivate(b, path, JSON.stringify(adminId));
  assert.equal(mismatch.status, 409);
  assert.equal(mismatch.body.error.code, "AUTH_ACCOUNT_CHANGED");
  assert.equal((await rawPrivate(b, path)).status, 403);
  check("cross-tab cookie switch clears admin drafts/DOM and rejects late successful saves; wrong expected-user GET is fenced by the server");

  await navigate(a);
  await login(a, admin);
  await manage(a, id);
  await a.fill("#admin-content-title", "Same-user private draft before demotion");
  await a.click("#admin-preview-button");
  const lateDetail = pause(a, path);
  await a.click("#admin-reload");
  await lateDetail.wait();
  const sameUser = await a.evaluate("state.user");
  inject(a, "/v1/auth/session", (event) => json(a, event, 200, { user: { ...sameUser, role: "user" }, expires_at: "2030-01-01T00:00:00Z" }));
  await a.evaluate("revalidateSession()");
  await cleanAdmin(a);
  assert.equal(await a.evaluate("state.user.user_id"), sameUser.user_id);
  await lateDetail.release();
  await sleep(150);
  await cleanAdmin(a);
  check("same-user demotion session fault clears management content and fences an earlier successful detail response");

  await navigate(a);
  await manage(a, id);
  const lateAfter403 = pause(a, path);
  await a.click("#admin-reload");
  await lateAfter403.wait();
  inject(a, "/v1/admin/challenges", (event) => a.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: 403, responseHeaders: [{ name: "Content-Type", value: "text/plain" }], body: Buffer.from("Forbidden").toString("base64") }));
  inject(a, "/v1/auth/session", (event) => json(a, event, 503, { error: { code: "SERVICE_NOT_READY" } }));
  await a.evaluate("apiRequest('/v1/admin/challenges',{},true).catch(()=>null)");
  await cleanAdmin(a);
  await a.wait("state.authMode === 'unavailable'");
  await lateAfter403.release();
  await sleep(150);
  await cleanAdmin(a);
  assert.equal(await a.evaluate("state.token"), null);
  assert.equal(await a.evaluate("elements['access-token'].disabled"), true);
  check("non-JSON admin 403 immediately clears sensitive content; session outage never enables Bearer or restores delayed responses");

  await student.viewport(1440, 1050);
  const otherId = await student.evaluate("state.challenges.find(c=>c.challenge_id !== state.selectedChallengeId).challenge_id");
  const staleTeaching = pause(student, `/v1/challenges/${id}/teaching`);
  await student.click("#teaching-retry");
  await staleTeaching.wait();
  await select(student, otherId);
  await staleTeaching.release();
  assert.equal(await student.evaluate("teachingState.payload.challenge_id"), otherId);
  assert.equal(await student.evaluate("elements['published-title'].textContent"), "");
  inject(student, `/v1/challenges/${otherId}/teaching`, (event) => json(student, event, 503, { error: { code: "SERVICE_NOT_READY" } }));
  await student.click("#teaching-retry");
  await student.wait("teachingState.status === 'error'");
  assert.equal(await student.evaluate("elements['template-zero'].disabled && elements['template-few'].disabled"), true);
  await student.click("#teaching-retry");
  await student.wait("teachingState.status === 'ready'");
  check("published teaching ignores stale task responses; outage blocks templates until an explicit successful reload");

  // Direct forbidden/mismatch probes above deliberately set their own expected user.
  const adminWrites = requests.filter((r) => r.path.startsWith("/v1/admin/") && ["PUT", "POST"].includes(r.method));
  assert.ok(adminWrites.some((r) => r.method === "PUT"));
  for (const request of adminWrites) {
    assert.equal(request.headers["x-loj-csrf"], "1");
    assert.equal(request.headers["x-loj-expected-user"], adminId);
    assert.equal(request.headers.origin, base);
    assert.match(request.headers["content-type"], /^application\/json/);
    assert.equal(request.headers.authorization, undefined);
    assert.equal(Number.isInteger(JSON.parse(request.body).expected_revision), true);
  }
  for (const page of [a, b, student]) assert.deepEqual(page.exceptions, []);
  if (failure) throw failure;
  check("all admin writes carry frozen expected-user, CSRF, same-origin JSON and revision; no uncaught browser errors");
  console.log(`\n${checks.length} admin browser groups passed (${fixture ? "SYNTHETIC CONTRACT FIXTURES" : "ISOLATED REAL LOCAL MOCK APP; role/runtime faults explicitly injected"}).\nScreenshots: ${artifacts}`);
} catch (error) {
  try { await a.screenshot("failure-admin"); await student.screenshot("failure-student"); } catch (_) { /* Preserve the primary failure. */ }
  console.error(`Artifacts: ${artifacts}`);
  throw error;
} finally {
  await student.close();
  await a.close();
  await fixture?.close();
}
