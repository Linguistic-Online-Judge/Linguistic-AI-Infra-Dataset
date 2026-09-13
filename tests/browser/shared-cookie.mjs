import assert from "node:assert/strict";
import { sleep } from "./cdp.mjs";

// Two real pages in one browser profile. Faults below delay reads or suppress
// notifications, never replace login cookies, identities, or submission responses.
export async function testSharedCookie(browser, base, { alice, bob }, check) {
  const a = await browser.newPage();
  const b = await browser.newPage();
  const rules = new Map([[a, []], [b, []]]);
  const posts = [];
  let interceptionFailure;
  for (const page of [a, b]) {
    page.on("Fetch.requestPaused", async (event) => {
      try {
        const stage = event.responseStatusCode || event.responseErrorReason ? "Response" : "Request";
        const path = new URL(event.request.url).pathname;
        if (stage === "Request" && path.startsWith("/v1/submissions")) {
          const headers = Object.fromEntries(Object.entries(event.request.headers).map(([key, value]) => [key.toLowerCase(), value]));
          assert.ok(headers["x-loj-expected-user"], "Every private browser read/write has an expected-user snapshot");
          if (event.request.method === "POST") posts.push({ page, expected: headers["x-loj-expected-user"], key: headers["idempotency-key"], body: event.request.postData });
        }
        const pending = rules.get(page);
        const index = pending.findIndex((rule) => rule.stage === stage && rule.path === path && rule.method === event.request.method);
        if (index >= 0) pending.splice(index, 1)[0].event = event;
        else await page.send(stage === "Response" ? "Fetch.continueResponse" : "Fetch.continueRequest", { requestId: event.requestId });
      } catch (error) { interceptionFailure ||= error; }
    });
    await page.send("Fetch.enable", { patterns: [{ urlPattern: "*/v1/*", requestStage: "Request" }, { urlPattern: "*/v1/*", requestStage: "Response" }] });
    await page.viewport(1440, 1000);
  }
  const pause = (page, path, method = "GET") => {
    const rule = { path, method, stage: "Response", event: null };
    rules.get(page).push(rule);
    return {
      async wait() {
        for (let i = 0; i < 300 && !rule.event; i++) await sleep(50);
        assert.ok(rule.event, `Expected paused ${method} ${path}`);
      },
      release: () => page.send("Fetch.continueResponse", { requestId: rule.event.requestId }),
    };
  };
  let navigation = 0;
  const ready = (page) => page.wait("state.authMode === 'cookie' && state.challenges.length >= 5 && !!state.selectedChallengeId && !state.sessionCheckSequence");
  const navigate = async (page) => { await page.navigate(`${base}/?shared-cookie=${++navigation}`); await ready(page); await page.evaluate('selectChallenge(state.selectedChallengeId)'); };
  const login = async (page, account, wait = true) => {
    await page.evaluate("openAuthDialog('login')");
    await page.fill("#auth-email", account.email);
    await page.fill("#auth-password", account.password);
    await page.click("#auth-submit");
    if (wait) await page.wait(`state.user?.public_handle === ${JSON.stringify(account.public_handle)} && !state.authBusy && !state.sessionCheckSequence && !document.querySelector('#auth-dialog').open`);
  };
  const logout = async (page) => {
    await page.evaluate("openAuthDialog('account')");
    await page.click("#auth-submit");
    await page.wait("!state.user && !state.authBusy && !document.querySelector('#auth-dialog').open");
  };
  const resultReady = (page) => page.wait("!state.submitting && document.querySelector('#result-content').textContent.includes('评测已完成')", "shared-cookie submission result", 45000);
  const clean = async (page) => {
    await page.wait("!state.user && !state.submitting && state.authNeedsLogin", "changed account requires explicit login");
    assert.equal(await page.evaluate("state.activeSubmission"), null);
    assert.equal(await page.evaluate("state.attempts.size + state.promptDrafts.size"), 0);
    assert.equal(await page.evaluate("document.querySelector('#student-prompt').value"), "");
    assert.equal(await page.evaluate("document.querySelector('#result-content').textContent"), "");
    assert.equal(await page.evaluate("document.querySelector('#runs-list').childElementCount"), 0);
    assert.equal(await page.evaluate("document.querySelector('#submitted-prompt').textContent"), '');
    assert.equal(await page.evaluate('state.promptRecord'), null);
    assert.equal(await page.evaluate("document.querySelector('#run-button').disabled"), true);
    assert.doesNotMatch(await page.evaluate("document.querySelector('#auth-help').textContent"), /LocalAlice|LocalBob/);
  };
  try {
    await navigate(a);
    await login(a, alice);
    const aliceId = await a.evaluate("state.user.user_id");
    await navigate(b);
    assert.equal(await b.evaluate("state.user.user_id"), aliceId, "Second target shares the first target's real cookie");
    await a.evaluate("window.observedAuthMessages = []; window.authObserver = new BroadcastChannel('loj-account-v1'); authObserver.onmessage = event => observedAuthMessages.push(event.data)");
    await a.fill("#student-prompt", `Two-page Alice submission ${Date.now()}`);
    await a.click("#run-button");
    await resultReady(a);
    const aliceRun = await a.evaluate("state.activeSubmission.submission_id");
    await a.evaluate("setView('runs')");
    await a.wait("!state.runsLoading && document.querySelector('#runs-list').childElementCount > 0");
    await a.evaluate("setView('result')");
    await a.fill("#student-prompt", "Alice private unsent draft, not Bob's prompt");
    const lateResult = pause(a, `/v1/submissions/${aliceRun}/result`);
    await a.evaluate("startPolling(state.activeSubmission.submission_id, state.activeSubmission.challenge_id)");
    await lateResult.wait();
    await login(b, bob);
    await clean(a);
    await lateResult.release();
    await sleep(200);
    await clean(a);
    const messages = await a.evaluate("observedAuthMessages");
    assert.ok(messages.length > 0);
    for (const message of messages) assert.deepEqual(message, { type: "auth-changed" });
    check("shared-cookie tabs: Bob login clears Alice caches/DOM/drafts and rejects her delayed result; broadcasts contain no identity or credentials");

    await navigate(a);
    assert.equal(await a.evaluate("state.user.public_handle"), bob.public_handle);
    await a.fill("#student-prompt", `Two-page Bob submission ${Date.now()}`);
    await a.click("#run-button");
    await resultReady(a);
    await a.fill("#student-prompt", "Bob unsent draft before other-tab logout");
    await logout(b);
    await clean(a);
    check("shared-cookie tabs: logout clears another tab's private content without navigating or clicking that tab");

    await navigate(a);
    await login(a, alice);
    await navigate(b);
    // Explicit missed-notification fault: emulate a suspended/older page. The
    // following POST still uses real cookies and must be rejected by the server.
    await a.evaluate("authChannel.close(); window.removeEventListener('focus', revalidateSession); revalidateSession = async () => {};");
    await a.fill("#student-prompt", `Never transfer this Alice prompt to Bob ${Date.now()}`);
    await login(b, bob);
    const bobId = await b.evaluate("state.user.user_id");
    const before = await b.evaluate("apiRequest('/v1/submissions?limit=100',{},true).then(p=>p.items.length)");
    assert.equal(await a.evaluate("state.user.user_id"), aliceId);
    for (const path of ["/v1/submissions", `/v1/submissions/${aliceRun}`, `/v1/submissions/${aliceRun}/result`, `/v1/submissions/${aliceRun}/prompt`]) {
      const rejected = await b.evaluate(`fetch(${JSON.stringify(path)},{credentials:'same-origin',headers:{'X-LOJ-Expected-User':${JSON.stringify(aliceId)}}}).then(async r=>({status:r.status,code:(await r.json()).error?.code}))`);
      assert.deepEqual(rejected, { status: 409, code: "AUTH_ACCOUNT_CHANGED" });
    }
    await a.click("#run-button");
    await clean(a);
    assert.equal(posts.at(-1).expected, aliceId);
    assert.notEqual(posts.at(-1).expected, bobId);
    assert.equal(await b.evaluate("apiRequest('/v1/submissions?limit=100',{},true).then(p=>p.items.length)"), before);
    assert.match(await a.evaluate("document.querySelector('#toast').textContent"), /账户已在其他页面/);
    const count = posts.length;
    await a.evaluate("document.querySelector('#prompt-form').requestSubmit()");
    await sleep(150);
    assert.equal(posts.length, count, "Mismatch never retries the old body under the new cookie owner");
    check("shared-cookie race: stale Alice POST carries Alice's expected-user snapshot, gets 409, and creates no Bob submission or automatic retry");

    await navigate(a);
    await a.evaluate("authChannel.close(); window.visibilityChecks = 0; document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') visibilityChecks++; })");
    await a.fill("#student-prompt", "Bob draft before missed-broadcast recovery");
    await b.send("Page.bringToFront");
    await login(b, alice);
    assert.equal(await a.evaluate("state.user.user_id"), bobId);
    await a.send("Page.bringToFront");
    await clean(a);
    assert.ok(await a.evaluate("visibilityChecks > 0"));
    check("shared-cookie tabs: real visibility/focus recovery detects an account switch when BroadcastChannel delivery is unavailable");

    await navigate(a);
    const sequence = await a.evaluate("state.accountSequence");
    await a.fill("#student-prompt", `Same-account focus during submit ${Date.now()}`);
    const accepted = pause(a, "/v1/submissions", "POST");
    await a.click("#run-button");
    await accepted.wait();
    await a.evaluate("window.dispatchEvent(new Event('focus'))");
    await a.wait("!state.sessionCheckSequence");
    assert.equal(await a.evaluate("state.accountSequence"), sequence);
    assert.equal(await a.evaluate("state.submitting"), true);
    assert.equal(await a.evaluate("state.attempts.size"), 1);
    await accepted.release();
    await resultReady(a);
    check("same-account focus revalidation preserves the original in-flight submission and account generation");

    const oldSession = pause(a, "/v1/auth/session");
    await a.evaluate("void revalidateSession()");
    await oldSession.wait();
    const ownLogin = pause(a, "/v1/auth/login", "POST");
    await login(a, bob, false);
    await ownLogin.wait();
    await a.evaluate("window.dispatchEvent(new Event('focus'))");
    assert.equal(await a.evaluate("state.authRecheckPending"), true);
    await ownLogin.release();
    await a.wait(`state.user?.user_id === ${JSON.stringify(bobId)} && !state.authBusy`);
    await oldSession.release();
    await a.wait("!state.sessionCheckSequence && !state.authRecheckPending");
    assert.equal(await a.evaluate("state.user.user_id"), bobId);
    assert.equal(await a.evaluate("state.authNeedsLogin"), false);
    check("late session revalidation cannot overwrite own login; focus during login is rechecked afterward");

    const beforeLogout = pause(a, "/v1/auth/session");
    await a.evaluate("void revalidateSession()");
    await beforeLogout.wait();
    await logout(a);
    await beforeLogout.release();
    await a.wait("!state.sessionCheckSequence");
    await clean(a);
    check("late session revalidation cannot restore a session after own logout");

    // Retire old-document listeners before intercepting the new document's
    // bootstrap read, otherwise a delayed notification can consume this rule.
    await a.evaluate("authChannel.close(); window.removeEventListener('focus', revalidateSession); revalidateSession = async () => {};");
    await login(b, bob);
    const bootstrap = pause(a, "/v1/auth/session");
    await a.navigate(`${base}/?shared-cookie=${++navigation}`);
    await bootstrap.wait();
    assert.equal(await a.evaluate("state.user"), null);
    await a.evaluate("window.discoveredLabels = []; new MutationObserver(() => discoveredLabels.push(document.querySelector('#session-label').textContent)).observe(document.querySelector('#session-label'),{childList:true,subtree:true,characterData:true})");
    await login(b, alice);
    await a.wait("state.authRecheckPending");
    await bootstrap.release();
    await ready(a);
    assert.equal(await a.evaluate("state.user.user_id"), aliceId);
    assert.equal(await a.evaluate("discoveredLabels.some(label=>label.includes('LocalBob'))"), false);
    check("an auth-change notification during bootstrap prevents the stale discovered account from being displayed");
    await logout(b);
    await clean(a);

    assert.deepEqual(a.exceptions, []);
    assert.deepEqual(b.exceptions, []);
    if (interceptionFailure) throw interceptionFailure;
  } catch (error) {
    await a.screenshot("shared-cookie-failure-a");
    await b.screenshot("shared-cookie-failure-b");
    throw error;
  } finally {
    await a.close();
    await b.close();
  }
}
