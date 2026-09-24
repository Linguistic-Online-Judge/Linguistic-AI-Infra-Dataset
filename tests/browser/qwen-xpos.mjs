// Explicit, resumable school acceptance. Reuses the private foundation test account.
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync } from 'node:fs';
import { resolve } from 'node:path';
import { startBrowser } from './cdp.mjs';

if (!process.argv.includes('--run-real-qwen')) throw new Error('Requires --run-real-qwen.');
const index = process.argv.indexOf('--limit');
const limit = index < 0 ? 14 : Number(process.argv[index + 1]);
assert.ok(Number.isInteger(limit) && limit >= 1 && limit <= 14);
const root = resolve(import.meta.dirname, '../..');
const directory = resolve(root, 'runtime/browser-tests/xpos-20260913-v1');
const account = JSON.parse(readFileSync(resolve(root, 'runtime/browser-tests/foundation-20260913-v1/account.json'), 'utf8'));
mkdirSync(directory, { recursive: true });
const reportFile = resolve(directory, 'report.json');
const report = existsSync(reportFile) ? JSON.parse(readFileSync(reportFile, 'utf8')) : {
  schema_version: 'xpos-browser-acceptance-v1', started_at: new Date().toISOString(),
  rows: {}, passed: false, independent_benchmark: false, submission_posts: 0,
};
const save = () => {
  report.completed_jobs = Object.values(report.rows).filter(row => row.verified).length;
  report.last_checked_at = new Date().toISOString();
  writeFileSync(reportFile + '.tmp', JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  renameSync(reportFile + '.tmp', reportFile);
};
const base = 'http://127.0.0.1:8090';
const browser = await startBrowser(directory);
const resultReady = id => browser.wait(`state.view==='result' && state.activeSubmission?.submission_id===${JSON.stringify(id)} && !!state.promptRecord && elements['result-content'].textContent.includes('评测已完成')`, 'result and owner prompt', 60000);
try {
  await browser.send('Network.enable');
  browser.on('Network.requestWillBeSent', event => {
    if (event.request.method === 'POST' && new URL(event.request.url).pathname === '/v1/submissions') {
      report.submission_posts++; save();
    }
  });
  await browser.viewport(1440, 1000);
  await browser.navigate(base + '/');
  await browser.wait("state.authMode==='cookie' && state.development?.evaluation_mode==='qwen' && state.challenges.length===74", 'XPOS school application', 60000);
  const catalog = await browser.evaluate('state.challenges');
  assert.equal(catalog.filter(task => task.accepting_submissions).length, 70);
  const languages = new Set(catalog.filter(task => task.task === 'xpos' && task.accepting_submissions).map(task => task.language));
  assert.equal(languages.size, 15);
  for (const language of ['Hebrew', 'Danish', 'Hungarian']) assert.ok(!languages.has(language));
  report.catalog_count = 74; report.executable_count = 70; report.xpos_languages = 15;
  await browser.screenshot('catalog-1440');
  await browser.evaluate("openAuthDialog('login')");
  await browser.fill('#auth-email', account.email);
  await browser.fill('#auth-password', account.password);
  await browser.click('#auth-submit');
  await browser.wait("!!state.user && !elements['auth-dialog'].open");
  const user = await browser.evaluate('state.user.user_id');
  if (report.user_id) assert.equal(user, report.user_id);
  report.user_id = user;
  const history = await browser.evaluate("apiRequest('/v1/submissions?limit=100',{},true)");
  assert.equal(history.next_cursor, null, 'Acceptance account history must fit one page');
  report.previous_ids ||= history.items.map(row => row.submission_id);
  const tasks = catalog.filter(task => task.task === 'xpos' && task.challenge_id.endsWith('-specialized-v1')).sort((a, b) => a.language.localeCompare(b.language));
  assert.equal(tasks.length, 14);
  let handled = 0;
  for (const task of tasks) {
    const id = task.challenge_id;
    if (report.rows[id]?.verified) continue;
    if (++handled > limit) break;
    await browser.evaluate(`location.hash=${JSON.stringify('#challenge/' + id)}`);
    await browser.wait(`state.view==='practice' && state.selectedChallengeId===${JSON.stringify(id)} && teachingState.status==='ready'`);
    await browser.click('#lesson-tab-example');
    assert.equal(await browser.evaluate('lessonFor(selectedChallenge()).examples.length'), 2);
    assert.equal(await browser.evaluate("elements['xpos-reference'].classList.contains('is-hidden')"), false);
    await browser.click('#xpos-reference summary');
    assert.ok(await browser.evaluate("elements['xpos-label-list'].textContent.length > 0"));
    await browser.screenshot(`labels-${task.language}`);
    await browser.click('#lesson-tab-description');
    const existing = history.items.filter(row => row.challenge_id === id);
    assert.ok(existing.length <= 1, 'Duplicate acceptance records require investigation');
    let row = report.rows[id];
    if (!existing.length && !row?.submission_id) {
      if (row?.attempted) throw new Error(`Unconfirmed submission ${id}; inspect before repeating.`);
      await browser.click('#template-few');
      const prompt = await browser.evaluate("elements['student-prompt'].value");
      row = report.rows[id] = { challenge_id: id, language: task.language,
        identity: task.evaluation_identity_sha256, prompt, attempted: true, verified: false };
      save();
      await browser.screenshot(`practice-${task.language}`);
      await browser.click('#run-button');
      await browser.wait(`!state.submitting && state.activeSubmission?.challenge_id===${JSON.stringify(id)}`, 'submission accepted', 60000);
      row.submission_id = await browser.evaluate('state.activeSubmission.submission_id'); save();
    } else {
      row = report.rows[id] ||= { challenge_id: id, language: task.language,
        identity: task.evaluation_identity_sha256, verified: false };
      row.submission_id ||= existing[0].submission_id;
      await browser.evaluate(`location.hash=${JSON.stringify('#result/' + row.submission_id)}`);
    }
    await browser.wait(`state.view==='result' && state.activeSubmission?.submission_id===${JSON.stringify(row.submission_id)} && ['succeeded','failed','rejected'].includes(state.activeSubmission.status)`, id, 930000);
    const result = await browser.evaluate("apiRequest('/v1/submissions/'+state.activeSubmission.submission_id+'/result',{},true)");
    Object.assign(row, { outcome: result.outcome, score: result.score, valid: result.samples_valid,
      total: result.samples_total, errors: result.errors, failure: result.code });
    save();
    assert.equal(result.outcome, 'succeeded', `${id}: ${result.code}`);
    assert.equal(result.task, 'xpos'); assert.equal(result.samples_total, 50);
    assert.equal(result.model_identity.model, 'Qwen/Qwen3.5-9B');
    assert.equal(result.model_identity.runtime, 'vllm');
    await resultReady(row.submission_id);
    row.prompt ||= await browser.evaluate("elements['submitted-prompt'].textContent");
    assert.equal(await browser.evaluate("elements['submitted-prompt'].textContent"), row.prompt);
    const posts = report.submission_posts;
    const hash = '#result/' + row.submission_id;
    await browser.navigate(base + '/' + hash); await resultReady(row.submission_id);
    await browser.click('#continue-editing');
    assert.equal(await browser.evaluate("elements['student-prompt'].value"), row.prompt);
    await browser.evaluate(`location.hash=${JSON.stringify(hash)}`); await resultReady(row.submission_id);
    await browser.click('#result-leaderboard'); await browser.wait('!state.leaderboardLoading');
    assert.equal(await browser.evaluate('state.leaderboardIdentity'), row.identity);
    assert.ok(await browser.evaluate(`elements['leaderboard-list'].textContent.includes(${JSON.stringify(account.handle)})`));
    await browser.click('#leaderboard-back'); await resultReady(row.submission_id);
    assert.equal(report.submission_posts, posts, 'Result reads and reuse must not resubmit');
    await browser.screenshot(`result-${task.language}`);
    row.verified = true; save();
    console.log(`PASS ${report.completed_jobs}/14 ${id}: valid=${row.valid}/50 score=${row.score}`);
  }
  const after = await browser.evaluate("apiRequest('/v1/submissions?limit=100',{},true)");
  assert.ok(report.previous_ids.every(id => after.items.some(row => row.submission_id === id)));
  report.owner_history_preserved = true;
  report.passed = tasks.every(task => report.rows[task.challenge_id]?.verified);
  await browser.viewport(390, 900, true);
  await browser.evaluate("location.hash='#challenges'"); await browser.wait("state.view==='challenges'");
  assert.ok(await browser.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'));
  await browser.screenshot('catalog-390');
  assert.deepEqual(browser.exceptions, []);
  delete report.failure;
} catch (error) {
  report.failure = error.message; process.exitCode = 1;
  await browser.screenshot('failure').catch(() => {});
} finally { save(); await browser.close(); }
console.log(`Report: ${reportFile}`);
