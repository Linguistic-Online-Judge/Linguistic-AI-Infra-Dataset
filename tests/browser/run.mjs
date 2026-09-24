import assert from "node:assert/strict";
import { resolve } from "node:path";
import { startBrowser, sleep } from "./cdp.mjs";
import { startFixtureServer } from "./fixture-server.mjs";
import { testSharedCookie } from "./shared-cookie.mjs";

const root = resolve(import.meta.dirname, "../..");
const fixtureMode = process.argv.includes("--fixture");
const fixture = fixtureMode ? await startFixtureServer(root) : null;
const urlArgument = process.argv.indexOf("--url");
const base = fixture?.url || (urlArgument >= 0 && process.argv[urlArgument + 1]);
if (!base || !/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(base)) throw new Error("Use --fixture or --url http://127.0.0.1:TESTPORT. This destructive account test is restricted to local development.");
const artifacts = resolve(root, "runtime/browser-tests", fixtureMode ? "fixtures" : "local-app");
const browser = await startBrowser(artifacts);
const rules = [];
const postHeaders = [];
const submissionKeys = [];
const submissionBodies = [];
const checks = [];
let failure;
const check = (name) => { checks.push(name); console.log(`PASS ${name}`); };
const json = async (event, status, value) => browser.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: status, responseHeaders: [{ name: "Content-Type", value: "application/json" }], body: Buffer.from(JSON.stringify(value)).toString("base64") });
const error = (event, status, code) => json(event, status, { error: { code, message: "Injected browser test failure", details: {} } });
const addRule = (match, handle, stage = "Request", once = true) => rules.push({ match, handle, stage, once });
browser.on("Fetch.requestPaused", async (event) => {
  try {
    const stage = event.responseStatusCode || event.responseErrorReason ? "Response" : "Request";
    const path = new URL(event.request.url).pathname;
    if (stage === "Request" && event.request.method === "POST") {
      const headers = Object.fromEntries(Object.entries(event.request.headers).map(([name, value]) => [name.toLowerCase(), value]));
      postHeaders.push({ csrf: headers["x-loj-csrf"], type: headers["content-type"], origin: headers.origin });
      if (path === "/v1/submissions") { submissionKeys.push(headers["idempotency-key"]); submissionBodies.push(event.request.postData); }
    }
    const index = rules.findIndex((rule) => rule.stage === stage && rule.match(event, path));
    if (index >= 0) {
      const rule = rules[index];
      if (rule.once) rules.splice(index, 1);
      await rule.handle(event);
    } else await browser.send(stage === "Response" ? "Fetch.continueResponse" : "Fetch.continueRequest", { requestId: event.requestId });
  } catch (error) { failure ||= error; }
});

let navigation = 0;
const navigate = async (hash = "") => {
  await browser.navigate(`${base}/?browser-test=${++navigation}${hash}`);
  await browser.wait("typeof state !== 'undefined' && state.authMode !== 'checking'", "account discovery complete");
};
const hash = async (value) => { await browser.evaluate(`location.hash=${JSON.stringify(value)}`); await sleep(120); };
const signedOut = async () => {
  if (await browser.evaluate("!!state.user")) {
    await browser.click("#session-button");
    await browser.wait("state.authPage === 'account' && !state.authBusy");
    await browser.click("#auth-submit");
    await browser.wait("!state.user && !document.querySelector('#auth-dialog').open", "logout completed");
  }
};
const login = async (email, password) => {
  await hash("#login");
  await browser.wait("document.querySelector('#auth-dialog').open && !document.querySelector('#auth-submit').disabled", "login dialog ready");
  await browser.fill("#auth-email", email);
  await browser.fill("#auth-password", password);
  await browser.click("#auth-submit");
  await browser.wait("!!state.user && !document.querySelector('#auth-dialog').open", "login completed");
};
const select = async (id) => { await hash(`#challenge/${id}`); await browser.wait(`state.selectedChallengeId === ${JSON.stringify(id)} && !state.leaderboardLoading && teachingState.status === 'ready'`); };
const resultReady = () => browser.wait("!state.submitting && document.querySelector('#result-content').textContent.includes('评测已完成')", "aggregate result rendered", 45000);
const editPrompt = async (text) => {
  if (await browser.evaluate("state.view === 'result'")) await browser.click('#result-back');
  await browser.wait("state.view === 'practice'");
  await browser.fill('#student-prompt', text);
};

try {
  await browser.send("Fetch.enable", { patterns: [{ urlPattern: "*/v1/*", requestStage: "Request" }, { urlPattern: "*/v1/*", requestStage: "Response" }] });
  await browser.viewport(1440, 1000);
  addRule((_event, path) => path === "/v1/challenges", (event) => error(event, 503, "SERVICE_NOT_READY"));
  await navigate();
  await browser.wait("!document.querySelector('#catalog-retry').classList.contains('is-hidden')");
  assert.match(await browser.evaluate("document.querySelector('#workbench-title').textContent"), /无法加载/);
  await browser.click("#catalog-retry");
  await browser.wait("state.challenges.length >= 5 && !!state.selectedChallengeId", "catalog retry succeeded");
  check("catalog error has a working retry, not a permanently loading workbench");

  const development = await browser.evaluate(`fetch('/v1/development',{headers:{'X-LOJ-Development':'1'},credentials:'same-origin'}).then(r=>r.json()).then(d=>({mode:d.evaluation_mode,accounts:d.accounts.map(a=>({email:a.email,public_handle:a.public_handle,password:a.password}))}))`);
  assert.equal(development.mode, "mock", "Only the explicit development mock service may be tested");
  const alice = development.accounts.find((account) => account.email === "alice@example.test");
  const bob = development.accounts.find((account) => account.email === "bob@example.test");
  assert.ok(alice && bob);
  assert.deepEqual(development.accounts.map(({ email, public_handle, password }) => ({ email, public_handle, password })).sort((a, b) => a.email.localeCompare(b.email)), [
    { email: "admin@example.test", public_handle: "LocalAdmin", password: "Local-only-passphrase-2026!" },
    { email: "alice@example.test", public_handle: "LocalAlice", password: "Local-only-passphrase-2026!" },
    { email: "bob@example.test", public_handle: "LocalBob", password: "Local-only-passphrase-2026!" },
  ]);
  await browser.wait("!document.querySelector('#mock-banner').classList.contains('is-hidden')");
  await browser.fill("#challenge-search", "no-such-language-browser-test");
  assert.equal(await browser.evaluate("document.querySelector('#challenge-list').childElementCount"), 0);
  await browser.click("#filters-clear");
  assert.ok(await browser.evaluate("document.querySelector('#challenge-list').childElementCount > 0"));
  check("mock labelling and empty-filter recovery use catalog API data");

  // Only the directory response is expanded. These are explicit language-coverage
  // fixtures, never real 18-language model runs or submissions.
  const originalCatalog = await browser.evaluate('state.challenges');
  const directoryFixture = ['ar', 'zh', 'da', 'nl', 'en', 'fr', 'de', 'he', 'hi', 'hu', 'it', 'ja', 'ko', 'pt', 'ru', 'es', 'sv', 'th'].map(language => ({ ...originalCatalog.find(item => item.task === 'upos'), challenge_id: `directory-fixture-${language}`, language, treebank: '目录覆盖测试', accepting_submissions: false }));
  addRule((_event, path) => path === '/v1/challenges', event => json(event, 200, directoryFixture));
  await browser.evaluate('loadChallenges()');
  await browser.wait("state.challenges.length === 18 && elements['language-filter'].options.length === 19");
  for (const language of await browser.evaluate("Array.from(elements['language-filter'].options).map(option=>option.value).filter(Boolean)")) {
    await browser.fill('#language-filter', language);
    assert.equal(await browser.evaluate("elements['challenge-list'].children.length"), 1);
    assert.equal(await browser.evaluate("elements['challenge-list'].firstElementChild.children[1].textContent"), language);
  }
  await browser.click('#filters-clear');
  assert.equal(await browser.evaluate('state.view'), 'challenges');
  for (const width of [1440, 768, 390, 320]) {
    await browser.viewport(width, 900, width < 640);
    assert.ok(await browser.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'));
    await browser.screenshot(`catalog-18-fixture-${width}`);
  }
  await browser.viewport(1440, 1000);
  await browser.evaluate('loadChallenges()');
  await browser.wait(`state.challenges[0]?.challenge_id === ${JSON.stringify(originalCatalog[0].challenge_id)}`);
  check('independent catalog displays all 18 fixture languages, filters each, and fits four viewport widths');

  await hash("#login");
  await browser.fill("#auth-email", alice.email);
  await browser.fill("#auth-password", "Definitely-wrong-password!");
  await browser.click("#auth-submit");
  await browser.wait("!state.authBusy && !document.querySelector('#auth-error').classList.contains('is-hidden')");
  assert.equal(await browser.evaluate("state.authMode"), "cookie");
  assert.equal(await browser.evaluate("document.querySelector('#access-token').disabled"), true);
  await browser.fill("#auth-password", alice.password);
  await browser.click("#auth-submit");
  await browser.wait("!!state.user && !document.querySelector('#auth-dialog').open");
  await navigate();
  assert.equal(await browser.evaluate("state.user.public_handle"), alice.public_handle);
  assert.equal(await browser.evaluate("state.token"), null);
  assert.equal(await browser.evaluate("localStorage.length + sessionStorage.length"), 0);
  check("email login errors do not downgrade auth; cookie session survives reload without browser storage tokens");

  const challenges = await browser.evaluate("state.challenges.filter(c=>c.accepting_submissions).filter((c,i,a)=>a.findIndex(x=>x.task===c.task)===i).map(c=>({id:c.challenge_id,task:c.task,identity:c.evaluation_identity_sha256}))");
  assert.equal(challenges.length, 5);
  for (const challenge of challenges) {
    await select(challenge.id);
    await browser.click("#template-zero");
    const zero = await browser.evaluate("document.querySelector('#student-prompt').value");
    assert.ok(zero.length > 60);
    await browser.fill("#student-prompt", "");
    await browser.click("#template-few");
    const few = await browser.evaluate("document.querySelector('#student-prompt').value");
    assert.ok(few.length > zero.length);
    await browser.fill("#student-prompt", `${few}\nBrowser test ${Date.now()} <img src=x onerror=alert(1)>`);
    const expectedBytes = Buffer.byteLength(await browser.evaluate("document.querySelector('#student-prompt').value"), "utf8");
    assert.ok((await browser.evaluate("document.querySelector('#prompt-count').textContent")).startsWith(`${expectedBytes} /`));
    await browser.click("#run-button");
    await resultReady();
    assert.equal(await browser.evaluate("document.querySelector('#run-button').textContent"), "查看已提交评测");
  }
  check("all five task lessons, zero/few-shot editable templates, byte counts, submissions and discriminated results");
  const ownedRun = await browser.evaluate("state.activeSubmission.submission_id");

  // Lose a response AFTER the backend accepted the request, then retry the same key.
  await editPrompt(`Idempotency test ${Date.now()}`);
  let acceptedId;
  addRule((event, path) => path === "/v1/submissions" && event.request.method === "POST", async (event) => {
    const response = await browser.send("Fetch.getResponseBody", { requestId: event.requestId });
    const body = JSON.parse(response.base64Encoded ? Buffer.from(response.body, "base64").toString() : response.body);
    acceptedId = body.submission_id;
    await browser.send("Fetch.failRequest", { requestId: event.requestId, errorReason: "ConnectionClosed" });
  }, "Response");
  await browser.click("#run-button");
  await browser.wait("!state.submitting && !document.querySelector('#prompt-error').classList.contains('is-hidden')", "lost-response error stays visible");
  assert.ok(acceptedId, "The first request was really accepted");
  await browser.click("#run-button");
  await resultReady();
  assert.equal(await browser.evaluate("state.activeSubmission.submission_id"), acceptedId);
  assert.equal(submissionKeys.at(-1), submissionKeys.at(-2));
  assert.equal(submissionBodies.at(-1), submissionBodies.at(-2));
  check("ambiguous accepted submission retries the same body/key and returns the original record");

  let heldReauthentication;
  const reauthenticationPrompt = `Same-account reauthentication ${Date.now()}`;
  await editPrompt(reauthenticationPrompt);
  addRule((event, path) => path === "/v1/submissions" && event.request.method === "POST", (event) => { heldReauthentication = event; }, "Response");
  await browser.click("#run-button");
  for (let i = 0; i < 200 && !heldReauthentication; i++) await sleep(50);
  assert.ok(heldReauthentication);
  await login(alice.email, alice.password);
  await browser.send("Fetch.continueResponse", { requestId: heldReauthentication.requestId });
  await browser.wait("!state.submitting", "same-account login releases the old submission's busy state");
  assert.equal(await browser.evaluate("document.querySelector('#student-prompt').value"), reauthenticationPrompt);
  await browser.click("#run-button");
  await resultReady();
  assert.equal(submissionKeys.at(-1), submissionKeys.at(-2));
  assert.equal(submissionBodies.at(-1), submissionBodies.at(-2));
  check("same-account reauthentication preserves drafts and permits an idempotent retry of an in-flight submission");

  let heldSubmission;
  await editPrompt(`Context race ${Date.now()}`);
  addRule((event, path) => path === "/v1/submissions" && event.request.method === "POST", (event) => { heldSubmission = event; }, "Response");
  await browser.click("#run-button");
  for (let i = 0; i < 200 && !heldSubmission; i++) await sleep(50);
  assert.ok(heldSubmission);
  await select(challenges[0].id);
  await browser.send("Fetch.continueResponse", { requestId: heldSubmission.requestId });
  await browser.wait("!state.submitting");
  assert.equal(await browser.evaluate("state.activeSubmission"), null);
  assert.equal(await browser.evaluate("document.querySelector('#result-content').textContent"), "");
  check("late submission response cannot overwrite a different task's result");

  let heldRanks;
  await browser.click('#practice-leaderboard');
  await browser.wait('!state.leaderboardLoading');
  addRule((_event, path) => path.startsWith("/v1/leaderboards/"), (event) => { heldRanks = event; });
  await browser.click("#leaderboard-refresh");
  for (let i = 0; i < 100 && !heldRanks; i++) await sleep(30);
  await select(challenges[1].id);
  await error(heldRanks, 503, "SERVICE_NOT_READY");
  await sleep(150);
  assert.equal(await browser.evaluate("state.leaderboardIdentity"), challenges[1].identity);
  assert.doesNotMatch(await browser.evaluate("document.querySelector('#leaderboard-empty').textContent"), /暂未就绪/);
  check("late leaderboard errors cannot replace the current task's ranks");

  // Inject pages, not product counts. These assertions test cursor use and single-flight behavior.
  await browser.click('#practice-leaderboard');
  await browser.wait('!state.leaderboardLoading');
  let rankPageCalls = 0;
  addRule((_event, path) => path.startsWith("/v1/leaderboards/"), async (event) => {
    rankPageCalls++;
    const cursor = new URL(event.request.url).searchParams.get("cursor");
    await json(event, 200, { items: [{ rank: cursor ? 21 : 1, public_handle: cursor ? "fixture-page-two" : "fixture-page-one", score: 0.5 }], next_cursor: cursor ? null : "opaque-rank-cursor" });
  }, "Request", false);
  await browser.click("#leaderboard-refresh");
  await browser.wait("!state.leaderboardLoading");
  await browser.evaluate("document.querySelector('#leaderboard-more').click(); document.querySelector('#leaderboard-more').click()");
  await browser.wait("!state.leaderboardLoading");
  assert.equal(rankPageCalls, 2);
  assert.equal(await browser.evaluate("document.querySelector('#leaderboard-list').childElementCount"), 2);
  rules.splice(0);
  await browser.click("#leaderboard-refresh");
  await browser.wait("!state.leaderboardLoading");
  check("leaderboard pages use opaque cursors without double-loading or invented rank numbering");

  await hash("#runs");
  await browser.wait("!state.runsLoading && document.querySelector('#runs-list').childElementCount >= 5", "owner history loaded");
  const historyRun = await browser.evaluate("apiRequest('/v1/submissions?limit=1', {}, true).then(p=>p.items[0])");
  let historyPageCalls = 0;
  addRule((event, path) => path === "/v1/submissions" && event.request.method === "GET", async (event) => {
    historyPageCalls++;
    const cursor = new URL(event.request.url).searchParams.get("cursor");
    await json(event, 200, { items: [{ ...historyRun, submission_id: cursor ? "fixture-older-history-record" : historyRun.submission_id }], next_cursor: cursor ? null : "opaque-history-cursor" });
  }, "Request", false);
  await browser.click("#refresh-runs");
  await browser.wait("!state.runsLoading");
  await browser.evaluate("document.querySelector('#runs-more').click(); document.querySelector('#runs-more').click()");
  await browser.wait("!state.runsLoading");
  assert.equal(historyPageCalls, 2);
  assert.equal(await browser.evaluate("document.querySelector('#runs-list').childElementCount"), 2);
  rules.splice(0);
  await browser.click("#refresh-runs");
  await browser.wait("!state.runsLoading");
  check("history pagination preserves opaque cursors and prevents concurrent duplicate pages");
  await browser.click("#runs-list button");
  await resultReady();
  await browser.wait('!!state.promptRecord');
  const historicalPrompt = await browser.evaluate('state.promptRecord.student_prompt');
  const resultHash = await browser.evaluate('location.hash');
  const writesBeforeReload = submissionKeys.length;
  await navigate(resultHash);
  await resultReady();
  await browser.wait('!!state.promptRecord');
  assert.equal(await browser.evaluate("elements['submitted-prompt'].textContent"), historicalPrompt);
  assert.equal(await browser.evaluate("elements['submitted-prompt'].children.length"), 0);
  await browser.click('#continue-editing');
  assert.equal(await browser.evaluate('state.view'), 'practice');
  assert.equal(await browser.evaluate("elements['student-prompt'].value"), historicalPrompt);
  assert.equal(submissionKeys.length, writesBeforeReload, 'Opening, reloading and reusing a prompt never submits it');
  await hash(resultHash);
  await resultReady();
  let heldPrompt;
  addRule((_event, path) => path.endsWith('/prompt'), event => { heldPrompt = event; }, 'Response');
  await browser.evaluate('void loadSubmittedPrompt()');
  for (let i = 0; i < 100 && !heldPrompt; i++) await sleep(30);
  assert.ok(heldPrompt);
  const otherChallenge = challenges.find(item => item.id !== historyRun.challenge_id);
  await select(otherChallenge.id);
  await browser.send('Fetch.continueResponse', { requestId: heldPrompt.requestId });
  await sleep(120);
  assert.equal(await browser.evaluate('state.promptRecord'), null);
  assert.equal(await browser.evaluate("elements['submitted-prompt'].textContent"), '');
  await hash(resultHash);
  await resultReady();
  await browser.wait('!!state.promptRecord');
  const beforeRankingWrites = submissionKeys.length;
  const resultIdentity = await browser.evaluate('state.activeSubmission.evaluation_identity_sha256');
  await browser.click('#result-leaderboard');
  await browser.wait('!state.leaderboardLoading');
  assert.equal(await browser.evaluate('state.leaderboardIdentity'), resultIdentity);
  await browser.click('#leaderboard-back');
  await resultReady();
  assert.equal(submissionKeys.length, beforeRankingWrites);
  for (const width of [1440, 768, 390, 320]) {
    await browser.viewport(width, 900, width < 640);
    assert.ok(await browser.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'));
    await browser.screenshot(`result-pages-${width}`);
  }
  await browser.viewport(1440, 1000);
  check('history prompt survives reload, is reused without a POST, rejects late task reads, and links to the exact result leaderboard');
  check("owner history opens the selected result using that submission's evaluation identity");
  for (const [outcome, code, retryable] of [["failed", "PROVIDER_TIMEOUT", true], ["rejected", "TOKEN_LIMIT_EXCEEDED", false]]) {
    addRule((_event, path) => path.endsWith("/result"), (event) => json(event, 200, { outcome, code, retryable, failure_contract_version: "fixture-v1" }));
    await browser.evaluate("startPolling(state.activeSubmission.submission_id, state.activeSubmission.challenge_id)");
    await browser.wait(`document.querySelector('#result-content').textContent.includes(${JSON.stringify(code)})`);
    assert.equal(await browser.evaluate("document.querySelector('#result-content .result-score')"), null);
  }
  let unavailableOnce = false;
  addRule((_event, path) => path.endsWith("/result"), async (event) => { unavailableOnce = true; await error(event, 503, "SERVICE_NOT_READY"); });
  await browser.evaluate("startPolling(state.activeSubmission.submission_id, state.activeSubmission.challenge_id)");
  await browser.wait("document.querySelector('#result-content').textContent.includes('正在自动重试')");
  assert.ok(unavailableOnce);
  await browser.click("#result-content button");
  await resultReady();
  check("failed/rejected outcomes show no fake score; result transport errors retry reads, not submissions");
  let heldHistory;
  addRule((event, path) => path === "/v1/submissions" && event.request.method === "GET", (event) => { heldHistory = event; }, "Response");
  await hash("#runs");
  for (let i = 0; i < 100 && !heldHistory; i++) await sleep(30);
  assert.ok(heldHistory);
  await signedOut();
  await browser.send("Fetch.continueResponse", { requestId: heldHistory.requestId });
  await sleep(150);
  assert.equal(await browser.evaluate("document.querySelector('#runs-list').childElementCount"), 0);
  assert.equal(await browser.evaluate("document.querySelector('#result-content').textContent"), "");
  await login(bob.email, bob.password);
  const privateStatus = await browser.evaluate(`fetch('/v1/submissions/${ownedRun}/result',{credentials:'same-origin',headers:{'X-LOJ-Expected-User':state.user.user_id}}).then(r=>r.status)`);
  assert.equal(privateStatus, 404);
  assert.equal(await browser.evaluate(`apiRequest('/v1/submissions/${ownedRun}/prompt',{},true).then(()=>200,e=>e.status)`), 404);
  assert.equal(await browser.evaluate("elements['submitted-prompt'].textContent"), '');
  check("logout invalidates late private requests and a second account cannot read the first account's result");
  await browser.wait("!state.runsLoading");
  addRule((event, path) => path === "/v1/submissions" && event.request.method === "GET", (event) => browser.send("Fetch.fulfillRequest", { requestId: event.requestId, responseCode: 401, responseHeaders: [{ name: "Content-Type", value: "text/plain" }], body: Buffer.from("Injected non-JSON unauthorized response").toString("base64") }));
  await browser.click("#refresh-runs");
  await browser.wait("!state.user", "non-JSON unauthorized response clears the displayed account");
  assert.equal(await browser.evaluate("document.querySelector('#runs-list').childElementCount"), 0);
  assert.equal(await browser.evaluate("document.querySelector('#result-content').textContent"), "");
  // The injected 401 did not revoke the server cookie. Rediscover it before logout.
  await navigate();
  check("private 401 responses invalidate the page session even when the error body is not JSON");
  addRule((_event, path) => path === "/v1/auth/logout", (event) => error(event, 503, "SERVICE_NOT_READY"));
  await browser.click("#session-button");
  await browser.click("#auth-submit");
  await browser.wait("!state.authBusy && !document.querySelector('#auth-error').classList.contains('is-hidden')");
  assert.equal(await browser.evaluate("state.user.public_handle"), bob.public_handle);
  assert.equal(await browser.evaluate("document.querySelector('#auth-dialog').open"), true);
  await browser.click("#auth-submit");
  await browser.wait("!state.user && !document.querySelector('#auth-dialog').open");
  check("failed logout keeps the session visible and offers a working retry");
  await signedOut();

  const email = `browser-${Date.now()}@example.test`;
  const password = "Test28Az";
  const resetPassword = "New36Bx!";
  await hash("#register");
  await browser.fill("#auth-email", email);
  await browser.click("#auth-submit");
  await browser.wait("!state.authBusy && !document.querySelector('#auth-notice').classList.contains('is-hidden')");
  await browser.click("#auth-close");
  if (!(await browser.evaluate("document.querySelector('#development-panel').open"))) await browser.click("#development-panel summary");
  await browser.click("#development-mail-refresh");
  await browser.wait(`!document.querySelector('#development-mail-refresh').disabled && [...document.querySelectorAll('#development-mail li')].some(e=>e.textContent.includes(${JSON.stringify(email)}))`);
  await browser.evaluate(`[...document.querySelectorAll('#development-mail li')].find(e=>e.textContent.includes(${JSON.stringify(email)})).querySelector('a').click()`);
  await browser.wait("state.authPage === 'verify-email' && document.querySelector('#auth-dialog').open");
  assert.equal(await browser.evaluate("location.hash"), "#verify-email");
  assert.equal(await browser.evaluate("state.user"), null);
  await browser.fill("#auth-handle", `browser-${Date.now()}`);
  await browser.fill("#auth-password", "short");
  await browser.click("#auth-submit");
  assert.match(await browser.evaluate("document.querySelector('#auth-error').textContent"), /至少需要8位/);
  await browser.fill("#auth-password", password);
  await browser.click("#auth-submit");
  await browser.wait("state.authPage === 'login' && !state.authBusy");
  assert.equal(await browser.evaluate("state.user"), null);
  await login(email, password);
  await hash("#forgot-password");
  await browser.fill("#auth-email", email);
  await browser.click("#auth-submit");
  await browser.wait("!state.authBusy && !document.querySelector('#auth-notice').classList.contains('is-hidden')");
  await browser.click("#auth-close");
  await browser.click("#development-mail-refresh");
  await browser.wait("!document.querySelector('#development-mail-refresh').disabled");
  await browser.evaluate(`[...document.querySelectorAll('#development-mail li')].find(e=>e.textContent.includes(${JSON.stringify(email)}) && e.querySelector('a')?.hash.startsWith('#reset-password')).querySelector('a').click()`);
  await browser.wait("state.authPage === 'reset-password'");
  assert.equal(await browser.evaluate("location.hash"), "#reset-password");
  await browser.fill("#auth-password", resetPassword);
  await browser.click("#auth-submit");
  await browser.wait("state.authPage === 'login' && !state.authBusy");
  assert.equal(await browser.evaluate("state.user"), null);
  assert.equal(await browser.evaluate("fetch('/v1/auth/session',{credentials:'same-origin'}).then(r=>r.status)"), 401);
  await browser.fill("#auth-email", email);
  await browser.fill("#auth-password", password);
  await browser.click("#auth-submit");
  await browser.wait("!state.authBusy && !document.querySelector('#auth-error').classList.contains('is-hidden')");
  assert.equal(await browser.evaluate("state.user"), null);
  await login(email, resetPassword);
  await signedOut();
  check("registration, local inbox, explicit verification and signed-in password reset revoke the old session/password");

  // Authentication discovery failures must not turn into an insecure legacy fallback.
  addRule((_event, path) => path === "/v1/auth/config", (event) => error(event, 503, "SERVICE_NOT_READY"));
  await navigate("#login");
  assert.equal(await browser.evaluate("state.authMode"), "unavailable");
  assert.equal(await browser.evaluate("document.querySelector('#auth-password').disabled"), false);
  assert.equal(await browser.evaluate("document.querySelector('#access-token').disabled"), true);
  assert.equal(await browser.evaluate("document.querySelector('#run-button').disabled"), true);
  await browser.click("#auth-reset-link");
  await browser.wait("state.authPage === 'forgot-password'");
  assert.equal(await browser.evaluate("document.querySelector('#auth-submit').disabled"), false);
  await browser.click("#auth-retry");
  await browser.wait("state.authMode === 'cookie'");
  check("auth service outage keeps login and recovery usable while protected actions fail closed");
  for (const scenario of [401, "network", "malformed", "disabled"]) {
    addRule((_event, path) => path === "/v1/auth/config", (event) => {
      if (scenario === 401) return error(event, 401, "AUTH_INVALID_TOKEN");
      if (scenario === "network") return browser.send("Fetch.failRequest", { requestId: event.requestId, errorReason: "ConnectionFailed" });
      return json(event, 200, scenario === "disabled" ? { enabled: false, mode: "local", mail_delivery: "local" } : { enabled: true });
    });
    await navigate("#login");
    assert.equal(await browser.evaluate("state.authMode"), "unavailable", String(scenario));
    assert.equal(await browser.evaluate("document.querySelector('#access-token').disabled"), true);
    assert.equal(await browser.evaluate("document.querySelector('#auth-submit').disabled"), false);
    assert.equal(await browser.evaluate("document.querySelector('#run-button').disabled"), true);
  }
  check("unauthorized, network, malformed and disabled discovery never enable Bearer fallback");
  addRule((_event, path) => path === "/v1/auth/config", (event) => error(event, 404, "NOT_FOUND"));
  await navigate("#login");
  assert.equal(await browser.evaluate("state.authMode"), "legacy");
  assert.equal(await browser.evaluate("document.querySelector('#access-token').disabled"), false);
  addRule((_event, path) => path === "/v1/users/me", (event) => json(event, 200, { user_id: "legacy-fixture", public_handle: "legacy-fixture" }));
  await browser.fill("#access-token", "browser-test-legacy-fixture");
  await browser.click("#auth-submit");
  await browser.wait("!!state.user && !state.authBusy");
  assert.equal(await browser.evaluate("state.user.role"), undefined);
  check("only explicit config 404 enables the shipped Bearer flow; old users/me needs no role");

  await navigate("#verify-email?token=invalid-browser-fixture");
  assert.equal(await browser.evaluate("location.hash"), "#verify-email");
  assert.equal(await browser.evaluate("state.user"), null);
  await browser.fill("#auth-handle", "browser-test");
  await browser.fill("#auth-password", password);
  await browser.click("#auth-submit");
  await browser.wait("!state.authBusy && !document.querySelector('#auth-error').classList.contains('is-hidden')");
  assert.equal(await browser.evaluate("state.authToken"), null);
  assert.equal(await browser.evaluate("document.querySelector('#auth-submit').disabled"), true);
  await browser.click("#auth-login-link");
  await browser.wait("state.authPage === 'login'");
  await browser.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
  await browser.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Tab", code: "Tab", windowsVirtualKeyCode: 9 });
  assert.equal(await browser.evaluate("document.querySelector('#auth-dialog').contains(document.activeElement)"), true);
  await browser.click("#auth-close");
  check("invalid email links are not consumed on GET, have recovery, and dialogs contain keyboard focus");

  await navigate();
  await browser.wait("state.challenges.length >= 5 && !state.leaderboardLoading");
  await select(challenges[0].id);
  for (const [width, height, mobile] of [[1440, 1000, false], [768, 1024, false], [390, 844, true], [320, 740, true]]) {
    await browser.viewport(width, height, mobile);
    await browser.evaluate("window.scrollTo({top:0,behavior:'instant'})");
    await sleep(150);
    assert.ok(await browser.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), `Unexpected horizontal page overflow at ${width}px`);
    await browser.screenshot(`workbench-${width}`);
  }
  for (const challenge of challenges) {
    await select(challenge.id);
    await browser.click('#lesson-tab-example');
    await browser.evaluate("document.querySelector('#teaching-details').open = true");
    assert.equal(await browser.evaluate("JSON.stringify(JSON.parse(document.querySelector('#task-example-input').textContent))"), await browser.evaluate("JSON.stringify(lessonFor(selectedChallenge()).examples[0].input)"));
    assert.equal(await browser.evaluate("JSON.stringify(JSON.parse(document.querySelector('#task-example-output').textContent))"), await browser.evaluate("JSON.stringify(lessonFor(selectedChallenge()).examples[0].output)"));
    if (await browser.evaluate("selectedChallenge().treebank === 'LocalPractice'")) {
      const output = await browser.evaluate("JSON.parse(elements['task-example-output'].textContent)");
      if (challenge.task === "xpos") assert.deepEqual(output.tags, ["NNS", "VBP", "."]);
      if (challenge.task === "transliteration") assert.deepEqual(output.transliterations, ["wo", "he", "cha", "\u3002"]);
    }
    assert.equal(await browser.evaluate("JSON.parse(document.querySelector('#task-schema').textContent).required[0]"), await browser.evaluate(`TASK_LESSONS[${JSON.stringify(challenge.task)}].field`));
    assert.ok(await browser.evaluate("document.documentElement.scrollWidth <= innerWidth + 1 && [...document.querySelectorAll('#task-guide pre')].every(e=>e.scrollWidth <= e.clientWidth + 1)"), `Expanded ${challenge.task} teaching overflow at 320px`);
    await browser.evaluate("document.querySelector('#task-example-input').scrollIntoView({block:'start',behavior:'instant'})");
    await browser.screenshot(`teaching-mobile-${challenge.task}`);
  }
  check("all five rendered teaching examples and output schemas remain readable at 320px");
  await browser.viewport(390, 844, true);
  await select(challenges[0].id);
  await browser.evaluate("document.querySelector('#student-prompt').scrollIntoView({block:'center',behavior:'instant'})");
  await browser.screenshot("editor-mobile");
  await hash("#login");
  await browser.screenshot("login-mobile");
  await browser.viewport(320, 740, true);
  assert.ok(await browser.evaluate("document.querySelector('#auth-dialog').scrollWidth <= document.querySelector('#auth-dialog').clientWidth + 1"));
  await browser.screenshot("login-320");
  assert.ok(postHeaders.length > 10);
  for (const headers of postHeaders) {
    assert.equal(headers.csrf, "1");
    assert.ok(headers.type.startsWith("application/json"));
    assert.equal(headers.origin, base);
  }
  assert.deepEqual(browser.exceptions, []);
  if (failure) throw failure;
  check("desktop/tablet/mobile/320px layouts, account dialogs and all browser POST CSRF/JSON/Origin headers");
  await testSharedCookie(browser, base, { alice, bob }, check);
  assert.deepEqual(browser.exceptions, []);
  if (failure) throw failure;
  console.log(`\n${checks.length} browser groups passed (${fixtureMode ? "SYNTHETIC CONTRACT FIXTURES, NOT BACKEND VERIFICATION" : "REAL LOCAL APPLICATION; fault cases use explicit CDP injection"}).`);
  console.log(`Screenshots: ${artifacts}`);
} catch (error) {
  try { await browser.screenshot("failure"); } catch (_) { /* Preserve original failure. */ }
  throw error;
} finally {
  await browser.close();
  if (fixture) await fixture.close();
}
