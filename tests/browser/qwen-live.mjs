// Explicit opt-in: creates one real Qwen submission for each of the 18 UPOS languages.
// Never run this as part of the fast Mock suite. Requires the private application SSH tunnel.
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { startBrowser } from './cdp.mjs';

const base = 'http://127.0.0.1:8090';
if (!process.argv.includes('--run-real-qwen')) throw new Error('Explicit --run-real-qwen is required; this consumes GPU resources.');
const expected = ['Arabic', 'Chinese', 'Danish', 'Dutch', 'English', 'French', 'German', 'Hebrew', 'Hindi', 'Hungarian', 'Italian', 'Japanese', 'Korean', 'Portuguese', 'Russian', 'Spanish', 'Swedish', 'Thai'];
const directory = resolve(import.meta.dirname, '../../runtime/browser-tests', `qwen18-${Date.now()}`);
mkdirSync(directory, { recursive: true });
const report = { schema_version: 'qwen18-browser-acceptance-v1', started_at: new Date().toISOString(), browser_verified: true, evaluation_mode: 'qwen', independent_benchmark: false, required_languages: expected, rows: [], passed: false };
const save = () => writeFileSync(resolve(directory, 'report.json'), `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
const browser = await startBrowser(directory);
const nonce = randomUUID().replaceAll('-', '').slice(0, 16);
const email = `browser18-${nonce}@example.test`;
const password = `Dev-Qwen18-${randomUUID()}`;
const prompt = 'Assign exactly one Universal Dependencies UPOS tag to each input token. Preserve token order and token count. Use only ADJ, ADP, ADV, AUX, CCONJ, DET, INTJ, NOUN, NUM, PART, PRON, PROPN, PUNCT, SCONJ, SYM, VERB, X. Return only the required JSON object with the tags array. Do not include explanations or markdown.';

try {
  await browser.viewport(1440, 1000);
  await browser.navigate(base + '/');
  await browser.wait("state.authMode === 'cookie' && state.development?.evaluation_mode === 'qwen' && state.challenges.length > 0", 'real development app ready', 45000);
  const candidates = await browser.evaluate("state.challenges.filter(c=>c.task==='upos' && c.accepting_submissions).map(c=>({language:c.language,id:c.challenge_id,samples:c.sample_count,identity:c.evaluation_identity_sha256,model:c.model_identity.model}))");
  assert.deepEqual([...new Set(candidates.map(c=>c.language))].sort(), expected);
  assert.equal(candidates.length, 18);
  assert.ok(candidates.every(c=>c.samples === 50 && c.model === 'Qwen/Qwen3.5-9B'));
  await browser.evaluate("openAuthDialog('register')");
  await browser.fill('#auth-email', email);
  await browser.click('#auth-submit');
  await browser.wait("!state.authBusy && !document.querySelector('#auth-notice').classList.contains('is-hidden')");
  await browser.click('#auth-close');
  await browser.click('#development-panel summary');
  await browser.click('#development-mail-refresh');
  await browser.wait(`!document.querySelector('#development-mail-refresh').disabled && [...document.querySelectorAll('#development-mail li')].some(e=>e.textContent.includes(${JSON.stringify(email)}))`);
  await browser.evaluate(`[...document.querySelectorAll('#development-mail li')].find(e=>e.textContent.includes(${JSON.stringify(email)})).querySelector('a').click()`);
  await browser.wait("state.authPage==='verify-email' && document.querySelector('#auth-dialog').open");
  assert.equal(await browser.evaluate('location.hash'), '#verify-email');
  await browser.fill('#auth-handle', `Browser18-${nonce}`);
  await browser.fill('#auth-password', password);
  await browser.click('#auth-submit');
  await browser.wait("state.authPage==='login' && !state.authBusy");
  await browser.fill('#auth-email', email);
  await browser.fill('#auth-password', password);
  await browser.click('#auth-submit');
  await browser.wait("!!state.user && !document.querySelector('#auth-dialog').open");
  report.registration_and_login = true;
  await browser.screenshot('real-qwen-18-language-catalog');
  save();
  for (const language of expected) {
    const task = candidates.find(c=>c.language===language);
    await browser.evaluate(`selectChallenge(${JSON.stringify(task.id)})`);
    await browser.wait(`state.selectedChallengeId===${JSON.stringify(task.id)} && teachingState.status==='ready'`);
    await browser.fill('#student-prompt', prompt);
    await browser.click('#run-button');
    await browser.wait(`!state.submitting && state.activeSubmission?.challenge_id===${JSON.stringify(task.id)}`, `${language} submission accepted`, 30000);
    const submissionId = await browser.evaluate('state.activeSubmission.submission_id');
    const started = Date.now();
    await browser.wait("['succeeded','failed','rejected'].includes(state.activeSubmission?.status)", `${language} real evaluation`, 420000);
    const result = await browser.evaluate(`fetch('/v1/submissions/${submissionId}/result',{credentials:'same-origin',headers:{'X-LOJ-Expected-User':state.user.user_id}}).then(async r=>({status:r.status,body:await r.json()}))`);
    assert.equal(result.status, 200);
    const row = { language, challenge_id: task.id, evaluation_identity_sha256: task.identity, submission_id: submissionId, outcome: result.body.outcome, elapsed_seconds: Math.round((Date.now()-started)/1000) };
    if (result.body.outcome === 'succeeded') {
      assert.equal(result.body.model_identity.model, 'Qwen/Qwen3.5-9B');
      assert.equal(result.body.model_identity.runtime, 'vllm');
      assert.equal(result.body.samples_total, 50);
      assert.equal(result.body.samples_valid + result.body.samples_invalid, 50);
      Object.assign(row, { score: result.body.score, samples_total: 50, samples_valid: result.body.samples_valid, samples_invalid: result.body.samples_invalid, errors: result.body.errors });
      await browser.wait("document.querySelector('#result-content').textContent.includes('评测已完成')");
      const board = await browser.evaluate(`fetch('/v1/leaderboards/${task.identity}').then(r=>r.json())`);
      assert.ok(board.items.some(item=>item.public_handle.startsWith('Browser18-') && item.score === row.score));
    } else row.failure_code = result.body.code;
    report.rows.push(row);
    save();
    console.log(`${language}: ${row.outcome}; valid=${row.samples_valid ?? 'n/a'}/50; seconds=${row.elapsed_seconds}`);
    if (language === 'English') await browser.screenshot('real-qwen-result');
  }
  await browser.evaluate("setView('runs')");
  await browser.wait("!state.runsLoading && document.querySelector('#runs-list').childElementCount===18");
  report.history_verified = true;
  await browser.screenshot('real-qwen-18-language-history');
  report.passed = report.rows.length === 18 && report.rows.every(row=>row.outcome==='succeeded');
  assert.equal(browser.exceptions.length, 0, 'No uncaught browser exceptions');
} catch (error) {
  report.failure = error.message;
  await browser.screenshot('failure').catch(()=>{});
  process.exitCode = 1;
} finally {
  report.finished_at = new Date().toISOString();
  save();
  await browser.close();
}
if (!report.passed) process.exitCode = 1;
console.log(`Report: ${resolve(directory,'report.json')}`);
