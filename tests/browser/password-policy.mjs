// Opt-in account-only acceptance on a private developer instance; no model requests.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { resolve } from 'node:path';
import { startBrowser } from './cdp.mjs';

const base = process.argv[process.argv.indexOf('--url') + 1];
if (!process.argv.includes('--url') || !/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw new Error('Use --url for a private loopback developer instance.');
const browser = await startBrowser(resolve(import.meta.dirname, '../../runtime/browser-tests', `password8-${Date.now()}`));
const email = `password8-${randomUUID().slice(0, 8)}@example.test`;
const login = async (password) => {
  await browser.fill('#auth-email', email);
  await browser.fill('#auth-password', password);
  await browser.click('#auth-submit');
  await browser.wait("!!state.user && !state.authBusy && !document.querySelector('#auth-dialog').open", 'login with eight characters');
};
const openMail = async (purpose) => {
  await browser.click('#auth-close');
  if (!await browser.evaluate("document.querySelector('#development-panel').open")) await browser.click('#development-panel summary');
  await browser.click('#development-mail-refresh');
  await browser.wait(`!document.querySelector('#development-mail-refresh').disabled && [...document.querySelectorAll('#development-mail li')].some(e=>e.textContent.includes(${JSON.stringify(email)}) && e.querySelector('a')?.hash.startsWith(${JSON.stringify('#' + purpose)}))`);
  await browser.evaluate(`[...document.querySelectorAll('#development-mail li')].find(e=>e.textContent.includes(${JSON.stringify(email)}) && e.querySelector('a')?.hash.startsWith(${JSON.stringify('#' + purpose)})).querySelector('a').click()`);
  await browser.wait(`state.authPage===${JSON.stringify(purpose)} && document.querySelector('#auth-dialog').open`);
};

try {
  await browser.navigate(base + '/#register');
  await browser.wait("state.authMode==='cookie' && !!state.development && state.authPage==='register' && !state.authBusy");
  await browser.fill('#auth-email', email);
  await browser.click('#auth-submit');
  await browser.wait("!state.authBusy && !document.querySelector('#auth-notice').classList.contains('is-hidden')");
  await openMail('verify-email');
  await browser.fill('#auth-handle', 'Password8-' + Date.now());
  await browser.fill('#auth-password', 'seven77');
  await browser.click('#auth-submit');
  assert.match(await browser.evaluate("document.querySelector('#auth-error').textContent"), /至少需要8位/);
  const hint = await browser.evaluate("document.querySelector('#password-help').textContent");
  assert.match(hint, /8–128位/);
  assert.doesNotMatch(hint, /Unicode|字节|15–128/);
  await browser.screenshot('eight-character-password-hint');
  await browser.fill('#auth-password', 'LojPass8');
  await browser.click('#auth-submit');
  await browser.wait("state.authPage==='login' && !state.authBusy");
  await login('LojPass8');
  await browser.evaluate("openAuthDialog('forgot-password')");
  await browser.fill('#auth-email', email);
  await browser.click('#auth-submit');
  await browser.wait("!state.authBusy && !document.querySelector('#auth-notice').classList.contains('is-hidden')");
  await openMail('reset-password');
  await browser.fill('#auth-password', '92847163');
  await browser.click('#auth-submit');
  await browser.wait("state.authPage==='login' && !state.authBusy && !state.user");
  await login('92847163');
  const logoutStatus = await browser.evaluate("fetch('/v1/auth/logout',{method:'POST',headers:{'Content-Type':'application/json','X-LOJ-CSRF':'1'},credentials:'same-origin',body:'{}'}).then(r=>r.status)");
  assert.equal(logoutStatus, 200);
  assert.equal(browser.exceptions.length, 0);
  console.log('PASS: seven characters rejected; eight-character registration/login/reset accepted; plain-language hint; no model requests.');
} finally {
  await browser.close();
}
