// Explicit real-Qwen cutover acceptance: five owners, five full50 tasks, no implicit resubmission.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { startBrowser } from './cdp.mjs';

if (!process.argv.includes('--run-real-qwen')) throw new Error('Explicit --run-real-qwen required.');
const argument = (key, fallback) => {
  const index = process.argv.indexOf(key);
  return index < 0 ? fallback : process.argv[index + 1];
};
const directory = resolve(argument('--directory', 'runtime/browser-tests/request-rollout-20260924-v1'));
const phase = argument('--phase', 'accept');
assert.ok(['prepare', 'accept', 'readback'].includes(phase));
const base = phase === 'accept' ? 'http://127.0.0.1:18090' : 'http://127.0.0.1:8090';
mkdirSync(directory, { recursive: true });
const accounts = JSON.parse(readFileSync(resolve(directory, 'accounts.private.json'), 'utf8'));
assert.equal(Object.keys(accounts).length, 5);
const reportFile = resolve(directory, 'report.json');
const report = existsSync(reportFile) ? JSON.parse(readFileSync(reportFile, 'utf8')) : {
  schema_version: 'request-workbench-browser-cutover-v1', passed: false,
  started_at: new Date().toISOString(), submission_posts: 0, rows: {},
  real_model_request_budget: 250, independent_benchmark: false,
};
const harnessSha = createHash('sha256').update(readFileSync(new URL(import.meta.url))).digest('hex');
if (report.browser_harness_sha256) assert.equal(report.browser_harness_sha256, harnessSha);
report.browser_harness_sha256 = harnessSha;
const save = () => {
  writeFileSync(reportFile + '.tmp', JSON.stringify(report, null, 2), { mode: 0o600 });
  renameSync(reportFile + '.tmp', reportFile);
};
const browsers = [];
const prepared = [];
let stopping = false;
const abortTimer = setInterval(() => {
  if (!stopping && existsSync(resolve(directory, 'abort.request'))) {
    stopping = true;
    report.passed = false; report.failure = 'Operator controller stopped acceptance'; save();
    void Promise.allSettled(browsers.map(browser => browser.close())).finally(() => process.exit(1));
  }
}, 1000);
abortTimer.unref();
try {
  for (const [label, account] of Object.entries(accounts)) {
    const browser = await startBrowser(resolve(directory, label + '-' + phase));
    browsers.push(browser);
    let allowedPost = false;
    await browser.send('Fetch.enable', { patterns: [{ urlPattern: '*://127.0.0.1:*/v1/submissions*', requestStage: 'Request' }] });
    browser.on('Fetch.requestPaused', event => {
      if (event.request.method === 'POST') {
        if (!allowedPost || phase !== 'accept') {
          report.unexpected_submission_blocked = true; save();
          void browser.send('Fetch.failRequest', { requestId: event.requestId, errorReason: 'BlockedByClient' });
          return;
        }
        allowedPost = false;
        report.submission_posts++; save();
      }
      void browser.send('Fetch.continueRequest', { requestId: event.requestId });
    });
    await browser.viewport(1440, 1000);
    await browser.navigate(base + '/');
    await browser.wait("state.authMode==='cookie' && state.development?.evaluation_mode==='qwen' && state.challenges.length===74", 'new real workbench', 60000);
    const catalog = await browser.evaluate('state.challenges');
    assert.equal(catalog.filter(item => item.submission_enabled).length, 70);
    assert.equal(new Set(catalog.filter(c => c.task === 'upos' && c.submission_enabled).map(c => c.language)).size, 18);
    const task = catalog.find(c => c.challenge_id === account.challenge_id);
    assert.ok(task && task.task === label);
    if (phase !== 'readback') assert.ok(task.accepting_submissions);
    let row = report.rows[label];
    if (!row?.registered) {
      await browser.evaluate("openAuthDialog('register')");
      await browser.fill('#auth-email', account.email);
      await browser.click('#auth-submit');
      await browser.wait("!state.authBusy && !elements['auth-notice'].classList.contains('is-hidden')");
      await browser.click('#auth-close');
      await browser.click('#development-panel summary');
      await browser.click('#development-mail-refresh');
      await browser.wait(`!elements['development-mail-refresh'].disabled && elements['development-mail'].textContent.includes(${JSON.stringify(account.email)})`);
      await browser.evaluate(`Array.from(elements['development-mail'].querySelectorAll('li')).find(item=>item.textContent.includes(${JSON.stringify(account.email)})).querySelector('a').click()`);
      await browser.wait("state.authPage==='verify-email'");
      await browser.fill('#auth-handle', account.handle);
      await browser.fill('#auth-password', account.password);
      await browser.click('#auth-submit');
      await browser.wait("state.authPage==='login' && !state.authBusy");
      row = report.rows[label] = { registered: true, challenge_id: account.challenge_id, task: label };
      save();
    } else await browser.evaluate("openAuthDialog('login')");
    await browser.fill('#auth-email', account.email);
    await browser.fill('#auth-password', account.password);
    await browser.click('#auth-submit');
    await browser.wait("!!state.user && !elements['auth-dialog'].open");
    const userId = await browser.evaluate('state.user.user_id');
    if (row.user_id) assert.equal(userId, row.user_id);
    row.user_id = userId; save();
    const history = await browser.evaluate("apiRequest('/v1/submissions?limit=100',{},true)");
    const existing = history.items.filter(item => item.challenge_id === account.challenge_id);
    assert.ok(existing.length <= 1, 'Do not duplicate an accepted evaluation');
    if (existing.length) row.submission_id ||= existing[0].submission_id;
    if (phase === 'readback' || row.submission_id) {
      assert.ok(row.submission_id);
      await browser.evaluate(`location.hash=${JSON.stringify('#result/' + row.submission_id)}`);
    } else {
      assert.ok(!row.attempted, 'Previous submission unconfirmed; inspect before retrying');
      await browser.evaluate(`location.hash=${JSON.stringify('#challenge/' + account.challenge_id)}`);
      await browser.wait(`state.view==='practice' && state.selectedChallengeId===${JSON.stringify(account.challenge_id)} && teachingState.status==='ready'`);
      await browser.click(label === 'xpos' ? '#template-few' : '#template-zero');
      const prompt = await browser.evaluate("elements['student-prompt'].value");
      if (row.prompt) assert.equal(prompt, row.prompt, 'template changed between preparation and acceptance');
      row.prompt_sha256 = createHash('sha256').update(prompt).digest('hex');
      row.prompt = prompt;
      row.identity = task.evaluation_identity_sha256;
      await browser.screenshot('prepared'); save();
    }
    prepared.push({ browser, label, row, enable: () => { allowedPost = true; } });
  }
  if (phase !== 'prepare') {
  await Promise.all(prepared.map(async ({ browser, label, row, enable }) => {
    if (phase === 'accept' && !row.submission_id) {
      row.attempted = true; row.started_at = new Date().toISOString(); save();
      enable();
      await browser.click('#run-button');
      await browser.wait(`!state.submitting && state.activeSubmission?.challenge_id===${JSON.stringify(row.challenge_id)}`, 'accepted submission', 60000);
      row.submission_id = await browser.evaluate('state.activeSubmission.submission_id'); save();
    }
    await browser.wait(`state.view==='result' && state.activeSubmission?.submission_id===${JSON.stringify(row.submission_id)} && ['succeeded','failed','rejected'].includes(state.activeSubmission.status)`, label, 930000);
    const result = await browser.evaluate("apiRequest('/v1/submissions/'+state.activeSubmission.submission_id+'/result',{},true)");
    assert.equal(result.outcome, 'succeeded', `${label}: ${result.code}`);
    assert.equal(result.samples_total, 50);
    assert.equal(result.task, label);
    assert.equal(result.model_identity.model, 'Qwen/Qwen3.5-9B');
    assert.equal(result.model_identity.runtime, 'vllm');
    assert.equal(result.student_prompt_sha256, row.prompt_sha256);
    await browser.wait("!!state.promptRecord && elements['result-content'].textContent.includes('评测已完成')");
    assert.equal(await browser.evaluate("elements['submitted-prompt'].textContent"), row.prompt);
    if (phase === 'readback') assert.equal(result.score, row.score);
    Object.assign(row, { outcome: result.outcome, score: result.score, total: result.samples_total,
      valid: result.samples_valid, errors: result.errors, verified: true });
    await browser.screenshot('result'); save();
    console.log(`PASS ${phase} ${label}: ${row.valid}/50 valid, score=${row.score}`);
  }));
  for (let index = 0; index < prepared.length; index++) {
    const { browser, row } = prepared[index];
    const other = prepared[(index + 1) % prepared.length].row.submission_id;
    assert.equal(await browser.evaluate(`fetch('/v1/submissions/${other}/result').then(r=>r.status)`), 404);
    await browser.click('#result-leaderboard');
    await browser.wait('!state.leaderboardLoading');
    assert.equal(await browser.evaluate('state.leaderboardIdentity'), row.identity);
    await browser.viewport(390, 900, true);
    await browser.evaluate("location.hash='#challenges'");
    await browser.wait("state.view==='challenges'");
    assert.ok(await browser.evaluate('document.documentElement.scrollWidth <= innerWidth+1'));
    await browser.screenshot('catalog-mobile');
    assert.deepEqual(browser.exceptions, []);
  }
  assert.equal(report.submission_posts, 5);
  assert.ok(!report.unexpected_submission_blocked);
  report.passed = true;
  if (phase === 'readback') report.production_readback_passed = true;
  } else {
    assert.equal(report.submission_posts, 0);
    assert.ok(!report.unexpected_submission_blocked);
    report.prepared_without_submission = true;
  }
  delete report.failure;
} catch (error) {
  report.passed = false; report.failure = String(error.message);
  process.exitCode = 1;
  for (const browser of browsers) await browser.screenshot('failure').catch(() => {});
} finally {
  clearInterval(abortTimer);
  report.updated_at = new Date().toISOString(); save();
  for (const browser of browsers) await browser.close();
}
