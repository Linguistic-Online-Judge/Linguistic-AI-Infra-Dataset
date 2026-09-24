import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import vm from "node:vm";

const root = resolve(import.meta.dirname, "../..");
const assets = join(root, "src/linguistic_oj/web/assets");
function context() {
  const sandbox = vm.createContext({ TextEncoder, URL, URLSearchParams, Headers, crypto: globalThis.crypto, location: { origin: "http://127.0.0.1:8080" }, document: { querySelectorAll: () => [] } });
  for (const file of ["language-lessons.js", "template-rules.js", "xpos-lessons.js", "xpos-labels.js", "teaching.js", "account.js", "admin.js", "app.js"]) {
    let source = readFileSync(join(assets, file), "utf8");
    if (file === "app.js") source = source.slice(0, source.indexOf('elements["challenge-search"].addEventListener'));
    vm.runInContext(source, sandbox, { filename: file });
  }
  return sandbox;
}

test("all five tasks have two handwritten examples matching their output contracts", () => {
  const lessons = JSON.parse(vm.runInContext("JSON.stringify(TASK_LESSONS)", context()));
  assert.deepEqual(Object.keys(lessons).sort(), ["dependency", "segmentation", "transliteration", "upos", "xpos"]);
  for (const [task, lesson] of Object.entries(lessons)) {
    assert.equal(lesson.examples.length, 2);
    assert.ok(lesson.instruction.length > 30);
    for (const { input, output } of lesson.examples) {
      assert.deepEqual(Object.keys(output), [lesson.field]);
      const values = output[lesson.field];
      assert.ok(values.length > 0);
      if (task === "segmentation") assert.equal(values.join(""), input.text);
      else assert.equal(values.length, input.tokens.length);
      if (task === "dependency") {
        assert.deepEqual(values.map((arc) => arc.token_id), input.tokens.map((token) => token.token_id));
        assert.equal(values.filter((arc) => arc.head_id === 0).length, 1);
        for (const arc of values) assert.ok(arc.head_id >= 0 && arc.head_id <= values.length && arc.head_id !== arc.token_id);
      } else for (const value of values) assert.equal(typeof value, "string");
      if (task === "upos") for (const tag of values) assert.ok("ADJ ADP ADV AUX CCONJ DET INTJ NOUN NUM PART PRON PROPN PUNCT SCONJ SYM VERB X".split(" ").includes(tag));
    }
  }
});

test("development inbox allowlists same-origin, root-only, token-only account fragments", () => {
  const sandbox = context();
  for (const path of ["/#verify-email?token=sample", "/#reset-password?token=sample"]) assert.ok(vm.runInContext(`safeDevelopmentLink(${JSON.stringify(path)}) !== null`, sandbox));
  for (const path of ["javascript:alert(1)", "https://attacker.test/#verify-email?token=x", "//attacker.test/#verify-email?token=x", "/other#verify-email?token=x", "/?token=x#verify-email?token=x", "/#login?token=x", "/#verify-email?token=", "/#verify-email?token=x&token=y", "/#verify-email?token=x&redirect=https://attacker.test/"]) assert.equal(vm.runInContext(`safeDevelopmentLink(${JSON.stringify(path)})`, sandbox), null, path);
});

test("metric formatting never invents percentages for absent or out-of-range values", () => {
  const sandbox = context();
  for (const value of ["null", "undefined", "NaN", "1.5", "-1", "'0.5'"]) assert.equal(vm.runInContext(`formatMetric(${value})`, sandbox), "未提供");
  assert.equal(vm.runInContext("formatMetric(0)", sandbox), "0.00%");
  assert.equal(vm.runInContext("formatMetric(1)", sandbox), "100.00%");
});

test("HTML IDs are unique and all literal element references exist", () => {
  const html = readFileSync(join(root, "src/linguistic_oj/web/index.html"), "utf8");
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  for (const file of ["language-lessons.js", "template-rules.js", "xpos-lessons.js", "xpos-labels.js", "app.js", "account.js", "teaching.js", "admin.js"]) {
    const source = readFileSync(join(assets, file), "utf8");
    new vm.Script(source, { filename: file });
    for (const [, id] of source.matchAll(/elements\["([^"]+)"\]/g)) assert.ok(ids.includes(id), `${file}: ${id}`);
    assert.doesNotMatch(source, /localStorage|sessionStorage|innerHTML|insertAdjacentHTML/);
  }
});

test("private requests snapshot the state user, override caller headers, and reject old-generation responses", async () => {
  const sandbox = context();
  sandbox.AbortController = AbortController;
  sandbox.window = { setTimeout, clearTimeout };
  vm.runInContext("state.user = {user_id:'alice-id'}; state.authMode = 'cookie'", sandbox);
  let sent;
  let respond;
  sandbox.fetch = (_path, options) => {
    sent = options;
    return new Promise((resolve) => { respond = () => resolve({ status: 200, ok: true, json: async () => ({ items: [] }) }); });
  };
  const request = vm.runInContext("apiRequest('/v1/submissions', {method:'POST',headers:{'X-LOJ-Expected-User':'spoofed-id'},body:'{}'}, true)", sandbox);
  assert.equal(sent.headers.get("X-LOJ-Expected-User"), "alice-id");
  assert.equal(sent.credentials, "same-origin");
  vm.runInContext("state.user = {user_id:'bob-id'}; state.accountSequence++", sandbox);
  respond();
  await assert.rejects(request, (error) => error.code === "AUTH_ACCOUNT_CHANGED");
  assert.equal(sent.headers.get("X-LOJ-Expected-User"), "alice-id");
});

test("local lessons select English Penn XPOS and no-tone Chinese, not production treebank conventions", () => {
  const sandbox = context();
  const lesson = (language, treebank, task) => JSON.parse(vm.runInContext(`JSON.stringify(lessonFor(${JSON.stringify({ language, treebank, task })}))`, sandbox));
  const english = lesson("English", "LocalPractice", "xpos");
  assert.deepEqual(english.examples[0].output.tags, ["NNS", "VBP", "."]);
  assert.match(english.instruction, /NN/);
  assert.deepEqual(lesson("German", "HDT", "xpos").examples[0].output.tags, ["NN", "VVFIN", "$."]);
  const local = lesson("Chinese", "LocalPractice", "transliteration");
  assert.deepEqual(local.examples[0].output.transliterations, ["wo", "he", "cha", "\u3002"]);
  assert.match(lesson("Chinese", "GSDSimp", "transliteration").examples[0].output.transliterations[0], /ǎ/);
  assert.equal(lesson("English", "Unknown", "xpos").examples.length, 0);
  assert.equal(lesson("Japanese", "Unknown", "transliteration").examples.length, 0);
});

test("teaching limits count Unicode characters and the entire UTF-8 save envelope", () => {
  const sandbox = context();
  const valid = { title: "x".repeat(120), summary: "", instructions: "text", zero_shot_prompt: "zero", few_shot_prompt: "few" };
  const issues = (content) => JSON.parse(vm.runInContext(`JSON.stringify(teachingValidation(${JSON.stringify(content)}).issues)`, sandbox));
  assert.deepEqual(issues(valid), []);
  assert.deepEqual(issues({ ...valid, title: "\u{1F600}".repeat(120) }), []);
  for (const field of ["title", "instructions", "zero_shot_prompt", "few_shot_prompt"]) assert.ok(issues({ ...valid, [field]: "" }).length);
  assert.ok(issues({ ...valid, summary: "x".repeat(501) }).length);
  assert.ok(issues({ ...valid, instructions: "字".repeat(4000), zero_shot_prompt: "字".repeat(3000) }).length);
});

test("admin PUTs set CSRF and expected-user snapshots; same-user demotion epochs reject delayed reads", async () => {
  const sandbox = context();
  sandbox.AbortController = AbortController;
  sandbox.window = { setTimeout, clearTimeout };
  vm.runInContext("state.user = {user_id:'admin-id', role:'admin'}; state.authMode = 'cookie'", sandbox);
  let sent;
  let respond;
  sandbox.fetch = (_path, options) => { sent = options; return new Promise((resolve) => { respond = () => resolve({ status: 200, ok: true, json: async () => ({ draft: { title: 'secret' } }) }); }); };
  const request = vm.runInContext("apiRequest('/v1/admin/challenges/id/teaching-draft',{method:'PUT',headers:{'X-LOJ-CSRF':'0','X-LOJ-Expected-User':'spoof'},body:'{}'},true)", sandbox);
  assert.equal(sent.headers.get("X-LOJ-CSRF"), "1");
  assert.equal(sent.headers.get("X-LOJ-Expected-User"), "admin-id");
  assert.equal(sent.headers.get("Authorization"), null);
  vm.runInContext("state.user.role = 'user'; adminState.sequence++", sandbox);
  respond();
  await assert.rejects(request, (error) => error.code === "ADMIN_REQUIRED");
});
