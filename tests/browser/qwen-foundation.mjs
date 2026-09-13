// Opt-in, resumable real evaluation of only the 34 new foundation contracts.
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { startBrowser } from './cdp.mjs';

if (!process.argv.includes('--run-real-qwen')) throw new Error('Explicit --run-real-qwen required.');
const index = process.argv.indexOf('--limit');
const limit = index < 0 ? 34 : Number(process.argv[index + 1]);
if (!Number.isInteger(limit) || limit < 1 || limit > 34) throw new Error('limit must be 1..34');
const directory = resolve(import.meta.dirname, '../../runtime/browser-tests/foundation-20260913-v1');
mkdirSync(directory, { recursive: true });
const reportFile = resolve(directory, 'report.json');
const accountFile = resolve(directory, 'account.json');
const report = existsSync(reportFile) ? JSON.parse(readFileSync(reportFile, 'utf8')) : {
  schema_version: 'foundation-browser-acceptance-v1', started_at: new Date().toISOString(),
  rows: {}, registered: false, passed: false, independent_benchmark: false,
};
const account = existsSync(accountFile) ? JSON.parse(readFileSync(accountFile, 'utf8')) : {
  email: `foundation-${randomUUID().slice(0, 12)}@example.test`,
  password: `Foundation-${randomUUID()}`, handle: `Foundation-${randomUUID().slice(0, 8)}`,
};
if (!existsSync(accountFile)) writeFileSync(accountFile, JSON.stringify(account), { mode: 0o600 });
const save = () => {
  report.completed_jobs = Object.values(report.rows).filter(row => row.outcome === 'succeeded').length;
  report.last_checked_at = new Date().toISOString();
  writeFileSync(reportFile, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
};
const base = 'http://127.0.0.1:8090';
const browser = await startBrowser(directory);
try {
  await browser.viewport(1440, 1000);
  await browser.navigate(base + '/');
  await browser.wait("state.authMode === 'cookie' && state.development?.evaluation_mode === 'qwen' && state.challenges.length >= 60", 'expanded school app', 60000);
  const catalog = await browser.evaluate('state.challenges');
  assert.ok(catalog.filter(item => item.accepting_submissions).length >= 56);
  for (const task of ['segmentation', 'upos', 'dependency']) assert.equal(new Set(catalog.filter(item => item.task === task && item.accepting_submissions).map(item => item.language)).size, 18);
  assert.equal(await browser.evaluate('Object.keys(LANGUAGE_TASK_EXAMPLES).length'), 18);
  report.coverage_verified = true;
  await browser.screenshot('catalog-expanded-1440');
  if (!report.registered) {
    await browser.evaluate("openAuthDialog('register')");
    await browser.fill('#auth-email', account.email);
    await browser.click('#auth-submit');
    await browser.wait("!state.authBusy && !elements['auth-notice'].classList.contains('is-hidden')");
    await browser.click('#auth-close');
    await browser.click('#development-panel summary');
    await browser.click('#development-mail-refresh');
    await browser.wait(`!elements['development-mail-refresh'].disabled && elements['development-mail'].textContent.includes(${JSON.stringify(account.email)})`);
    await browser.evaluate(`Array.from(elements['development-mail'].querySelectorAll('li')).find(item=>item.textContent.includes(${JSON.stringify(account.email)})).querySelector('a').click()`);
    await browser.wait("state.authPage === 'verify-email'");
    await browser.fill('#auth-handle', account.handle);
    await browser.fill('#auth-password', account.password);
    await browser.click('#auth-submit');
    await browser.wait("state.authPage === 'login' && !state.authBusy");
    report.registered = true; save();
  } else await browser.evaluate("openAuthDialog('login')");
  await browser.fill('#auth-email', account.email);
  await browser.fill('#auth-password', account.password);
  await browser.click('#auth-submit');
  await browser.wait("!!state.user && !elements['auth-dialog'].open");
  report.user_id = await browser.evaluate('state.user.user_id');
  const history = await browser.evaluate("apiRequest('/v1/submissions?limit=100',{},true)");
  const tasks = catalog.filter(item => item.challenge_id.endsWith('-foundation-v1')).sort((a, b) => b.task.localeCompare(a.task) || a.language.localeCompare(b.language));
  assert.equal(tasks.length, 34);
  let handled = 0;
  for (const task of tasks) {
    const id = task.challenge_id;
    if (report.rows[id]?.outcome === 'succeeded') continue;
    if (++handled > limit) break;
    await browser.evaluate(`location.hash=${JSON.stringify('#challenge/' + id)}`);
    await browser.wait(`state.view==='practice' && state.selectedChallengeId===${JSON.stringify(id)} && teachingState.status==='ready'`);
    await browser.click('#lesson-tab-example');
    assert.ok(await browser.evaluate('lessonFor(selectedChallenge()).examples.length === 2'));
    await browser.click('#lesson-tab-description');
    const existing = history.items.filter(row => row.challenge_id === id);
    assert.ok(existing.length <= 1, 'Acceptance account must not contain duplicate task runs');
    let row = report.rows[id];
    if (!existing.length && !row?.submission_id) {
      if (row?.attempted) throw new Error(`Unconfirmed previous submission for ${id}; inspect history, do not duplicate.`);
      await browser.click('#template-zero');
      const prompt = await browser.evaluate("elements['student-prompt'].value");
      row = { language: task.language, task: task.task, challenge_id: id,
              identity: task.evaluation_identity_sha256, prompt, attempted: true };
      report.rows[id] = row; save();
      await browser.screenshot(`practice-${id}`);
      await browser.click('#run-button');
      await browser.wait(`!state.submitting && state.activeSubmission?.challenge_id === ${JSON.stringify(id)}`, 'submission acceptance', 45000);
      row.submission_id = await browser.evaluate('state.activeSubmission.submission_id');
      save();
    } else {
      row = report.rows[id] ||= { language: task.language, task: task.task,
                                 challenge_id: id, identity: task.evaluation_identity_sha256 };
      row.submission_id ||= existing[0].submission_id;
      await browser.evaluate(`location.hash=${JSON.stringify('#result/' + row.submission_id)}`);
    }
    await browser.wait("state.view==='result' && ['succeeded','failed','rejected'].includes(state.activeSubmission?.status)", id, 930000);
    const result = await browser.evaluate("apiRequest('/v1/submissions/'+state.activeSubmission.submission_id+'/result',{},true)");
    row.outcome = result.outcome;
    row.score = result.score;
    row.valid = result.samples_valid;
    row.total = result.samples_total;
    row.errors = result.errors;
    row.failure = result.code;
    const submission = await browser.evaluate('state.activeSubmission');
    row.created_at = submission.created_at;
    row.completed_at = submission.completed_at;
    save();
    assert.equal(result.outcome, 'succeeded', `${id}: ${result.code}`);
    assert.equal(result.model_identity.model, 'Qwen/Qwen3.5-9B');
    assert.equal(result.model_identity.runtime, 'vllm');
    assert.equal(result.samples_total, 50);
    await browser.wait("!!state.promptRecord && elements['result-content'].textContent.includes('评测已完成')");
    if (row.prompt) assert.equal(await browser.evaluate("elements['submitted-prompt'].textContent"), row.prompt);
    await browser.click('#result-leaderboard');
    await browser.wait('!state.leaderboardLoading');
    assert.equal(await browser.evaluate('state.leaderboardIdentity'), row.identity);
    assert.ok(await browser.evaluate(`elements['leaderboard-list'].textContent.includes(${JSON.stringify(account.handle)})`));
    await browser.click('#leaderboard-back');
    await browser.wait("state.view==='result' && !!state.promptRecord");
    await browser.screenshot(`result-${id}`);
    console.log(`PASS ${Object.values(report.rows).filter(item=>item.outcome==='succeeded').length}/34 ${id}: valid=${row.valid}/50 score=${row.score}`);
  }
  report.passed = tasks.every(task => report.rows[task.challenge_id]?.outcome === 'succeeded');
  report.completed_jobs = Object.values(report.rows).filter(row => row.outcome === 'succeeded').length;
  await browser.evaluate("location.hash='#runs'");
  await browser.wait('!state.runsLoading');
  await browser.screenshot('history-1440');
  await browser.viewport(390, 900, true);
  await browser.evaluate("location.hash='#challenges'");
  await browser.wait("state.view==='challenges'");
  assert.ok(await browser.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'));
  await browser.screenshot('catalog-expanded-390');
  assert.deepEqual(browser.exceptions, []);
  delete report.failure;
} catch (error) {
  report.failure = error.message;
  await browser.screenshot('failure').catch(() => {});
  process.exitCode = 1;
} finally {
  report.last_checked_at = new Date().toISOString();
  save();
  await browser.close();
}
console.log(`Report: ${reportFile}`);
