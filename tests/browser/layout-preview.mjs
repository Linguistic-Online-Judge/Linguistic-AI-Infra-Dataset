import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { startBrowser } from './cdp.mjs';

const root = resolve(import.meta.dirname, '../..');
const fileMode = process.argv.includes('--file');
const address = fileMode
  ? pathToFileURL(resolve(root, 'src/linguistic_oj/web/assets/layout-preview.html')).href
  : 'http://127.0.0.1:8080/assets/layout-preview.html';
const browser = await startBrowser(resolve(root, 'runtime/browser-tests/layout-preview', fileMode ? 'file' : 'http'));
const requests = [];
const show = async (page) => {
  await browser.evaluate(`location.hash=${JSON.stringify(page === 'practice' ? '#practice/en-upos' : '#' + page)}`);
  await browser.wait(`!document.querySelector('#${page}-view').hidden`);
};
try {
  await browser.send('Network.enable');
  browser.on('Network.requestWillBeSent', event => requests.push(event.request));
  await browser.viewport(1440, 900);
  await browser.send('Page.navigate', { url: address });
  await browser.wait("document.querySelector('#task-rows')?.children.length===22", '22 fixture tasks rendered');
  await browser.wait("document.querySelector('.brand-mark')?.naturalWidth > 0");
  assert.match(await browser.evaluate("document.querySelector('.brand-mark').src"), /brand-option-a\.svg\?v=fudan-1$/, 'Preferred A is the default comparison mark');
  assert.equal(await browser.evaluate("getComputedStyle(document.querySelector('nav a[aria-current=page]')).color"), 'rgb(14, 65, 156)');
  assert.equal(await browser.evaluate("document.querySelector('.brand-name').textContent.trim()"), 'Linguistic Online Judge');
  assert.equal(await browser.evaluate("getComputedStyle(document.querySelector('#catalog-title')).outlineStyle"), 'none');
  const idleBorder = await browser.evaluate("getComputedStyle(document.querySelector('#search')).borderColor");
  await browser.click('#search');
  assert.equal(await browser.evaluate("getComputedStyle(document.querySelector('#search')).outlineStyle"), 'none');
  assert.notEqual(await browser.evaluate("getComputedStyle(document.querySelector('#search')).borderColor"), idleBorder);
  await browser.screenshot('catalog-search-focus-1440');
  await browser.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
  await browser.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9 });
  assert.equal(await browser.evaluate("document.activeElement.id"), 'language');
  assert.equal(await browser.evaluate("getComputedStyle(document.activeElement).outlineStyle"), 'none');
  assert.notEqual(await browser.evaluate("getComputedStyle(document.activeElement).borderColor"), idleBorder);
  await browser.screenshot('catalog-select-focus-1440');
  assert.equal(await browser.evaluate("document.querySelector('#language').options.length"), 19);
  assert.match(await browser.evaluate("document.querySelector('.prototype-notice').textContent"), /示例数据，不执行评测/);
  assert.equal(await browser.evaluate("typeof window.state"), 'undefined', 'No production application state is loaded');
  const languages = await browser.evaluate("Array.from(document.querySelector('#language').options).map(option=>option.value).filter(Boolean)");
  for (const language of languages) {
    await browser.fill('#language', language);
    assert.ok(await browser.evaluate("document.querySelector('#task-rows').children.length >= 1"));
  }
  await browser.fill('#search', 'no-matching-task');
  assert.equal(await browser.evaluate("document.querySelector('#empty').hidden"), false);
  await browser.click('#clear-filters');
  assert.equal(await browser.evaluate("document.querySelector('#task-rows').children.length"), 22);
  await browser.click('[data-task-id="en-upos"] a');
  await browser.wait("!document.querySelector('#practice-view').hidden");
  assert.equal(await browser.evaluate("document.querySelector('#preview-result').disabled"), true);
  const bounds = await browser.evaluate("document.querySelector('#preview-result').getBoundingClientRect().bottom");
  assert.ok(bounds <= 900, 'Primary practice action fits the desktop viewport');
  await browser.click('#tab-example');
  assert.equal(await browser.evaluate("document.querySelector('#panel-example').hidden"), false);
  await browser.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'ArrowRight', code: 'ArrowRight' });
  await browser.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'ArrowRight', code: 'ArrowRight' });
  assert.equal(await browser.evaluate("document.activeElement.id"), 'tab-scoring');
  await browser.screenshot('practice-scoring-1440');
  await browser.fill('#template', 'zero');
  assert.ok(await browser.evaluate("document.querySelector('#prompt').value.length > 20"));
  assert.equal(await browser.evaluate("document.activeElement.id"), 'prompt');
  assert.equal(await browser.evaluate("getComputedStyle(document.activeElement).outlineStyle"), 'none');
  await browser.screenshot('practice-editor-focus-1440');
  const prompt = 'Preview only. <img src=x onerror=alert(1)> 不执行模型。';
  await browser.fill('#prompt', prompt);
  await browser.click('#preview-result');
  await browser.wait("!document.querySelector('#result-view').hidden");
  assert.equal(await browser.evaluate("document.querySelector('#submitted-prompt').textContent"), prompt);
  assert.equal(await browser.evaluate("document.querySelector('#submitted-prompt img')"), null);
  assert.match(await browser.evaluate("document.querySelector('#score-label').textContent"), /示例分数/);
  await browser.click('#continue-editing');
  await browser.wait("!document.querySelector('#practice-view').hidden");
  assert.equal(await browser.evaluate("document.querySelector('#prompt').value"), prompt);
  await browser.fill('#prompt', '为每个输入词元选择一个通用词性标签。保持词元顺序和数量，只返回包含 tags 数组的 JSON 对象。');
  await browser.click('#preview-result');
  await browser.wait("!document.querySelector('#result-view').hidden");
  for (const width of [1440, 768, 390, 320]) {
    await browser.viewport(width, 900, width < 640);
    for (const page of ['catalog', 'practice', 'result']) {
      await show(page);
      assert.equal(await browser.evaluate(`getComputedStyle(document.querySelector('#${page}-title')).outlineStyle`), 'none', 'Route-focused headings have no frame');
      assert.ok(await browser.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), `${page} at ${width}px must not overflow horizontally`);
      await browser.screenshot(`${page}-${width}`);
    }
  }
  await browser.viewport(1440, 1000);
  await browser.send('Page.navigate', { url: new URL('brand-options.html?set=initial', address).href });
  await browser.wait("document.querySelectorAll('.option').length === 8");
  await browser.wait("Array.from(document.querySelectorAll('.option img')).every(img => img.complete && img.naturalWidth > 0)", 'All eight SVG designs load, including small versions');
  await browser.screenshot('brand-options-initial-fudan-1440');
  for (const id of 'abcdefgh') {
    await browser.click(`[data-option="${id}"]`);
    assert.equal(await browser.evaluate("document.querySelectorAll('.option[aria-pressed=true]').length"), 1);
    await browser.wait(`document.querySelector('#selected-mark').complete && document.querySelector('#selected-mark').naturalWidth > 0 && new URL(document.querySelector('#selected-mark').src).pathname.endsWith('brand-option-${id}.svg')`);
    const url = await browser.evaluate("document.querySelector('#try-design').href");
    assert.equal(new URL(url).searchParams.get('mark'), id);
  }
  await browser.send('Page.navigate', { url: new URL('brand-options.html', address).href });
  await browser.wait("document.querySelectorAll('.option').length === 6");
  await browser.wait("Array.from(document.querySelectorAll('.option img')).every(img => img.complete && img.naturalWidth > 0)");
  assert.equal(await browser.evaluate("document.querySelector('[name=color]:checked').value"), 'blue');
  assert.equal(await browser.evaluate("getComputedStyle(document.querySelector('#try-design')).color"), 'rgb(14, 65, 156)');
  await browser.screenshot('brand-options-l-six-fudan-1440');
  for (const id of ['a1', 'a2', 'a3', 'a4', 'a5', 'a6']) {
    await browser.click(`[data-option="${id}"]`);
    assert.equal(await browser.evaluate("document.querySelectorAll('.option[aria-pressed=true]').length"), 1);
    await browser.wait(`document.querySelector('#selected-mark').complete && document.querySelector('#selected-mark').naturalWidth > 0 && new URL(document.querySelector('#selected-mark').src).pathname.endsWith('brand-option-${id}.svg')`);
    assert.equal(new URL(await browser.evaluate("document.querySelector('#try-design').href")).searchParams.get('mark'), id);
  }
  await browser.click('#compare-original');
  assert.equal(await browser.evaluate("document.querySelectorAll('.option[aria-pressed=true]').length"), 0);
  assert.equal(new URL(await browser.evaluate("document.querySelector('#try-design').href")).searchParams.get('mark'), 'a');
  await browser.click('[data-option="a6"]');
  await browser.click('[name="color"][value="ink"]');
  assert.equal(new URL(await browser.evaluate("document.querySelector('#try-design').href")).searchParams.get('color'), 'ink');
  await browser.evaluate('window.scrollTo(0, 0)');
  await browser.screenshot('brand-options-l-six-ink-1440');
  await browser.click('[name="color"][value="blue"]');
  assert.equal(new URL(await browser.evaluate("document.querySelector('#try-design').href")).searchParams.get('color'), 'blue');
  await browser.evaluate('window.scrollTo(0, 0)');
  await browser.screenshot('brand-options-l-selected-1440');
  for (const width of [768, 390, 320]) {
    await browser.viewport(width, 900, width < 640);
    await browser.evaluate('window.scrollTo(0, 0)');
    assert.ok(await browser.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), `Brand options at ${width}px must not overflow`);
    await browser.screenshot(`brand-options-l-${width}`);
  }
  await browser.viewport(1440, 900);
  await browser.click('#try-design');
  await browser.wait("document.querySelector('#task-rows')?.children.length === 22");
  await browser.wait("document.querySelector('.brand-mark')?.naturalWidth > 0");
  assert.match(await browser.evaluate("document.querySelector('.brand-mark').src"), /brand-option-a6\.svg\?v=fudan-1$/);
  assert.equal(await browser.evaluate("getComputedStyle(document.querySelector('nav a[aria-current=page]')).color"), 'rgb(14, 65, 156)');
  await browser.screenshot('catalog-with-option-a6-1440');
  await show('practice');
  await browser.fill('#prompt', '为每个词元返回一个通用词性标签。');
  assert.equal(await browser.evaluate("getComputedStyle(document.querySelector('#preview-result')).backgroundColor"), 'rgb(14, 65, 156)');
  await browser.screenshot('practice-fudan-blue-1440');
  assert.equal(browser.exceptions.length, 0);
  assert.ok(!requests.some(request => new URL(request.url).pathname.startsWith('/v1/')), 'No account/evaluation API requests');
  assert.ok(requests.every(request => request.method === 'GET'), 'No submission writes');
  console.log(`PASS (${fileMode ? 'standalone file' : 'local HTTP'}): frameless headings, field focus, 18 languages, three views, keyboard, draft reuse, six L variants plus eight earlier marks, original-A comparison, Fudan-blue links/buttons, two-color selection, page navigation, 4 viewport widths, no API calls or writes.`);
} finally {
  await browser.close();
}
