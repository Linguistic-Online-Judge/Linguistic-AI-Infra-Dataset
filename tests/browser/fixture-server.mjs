// Contract fixtures only, NOT a backend implementation or evidence of backend correctness.
// Serves only allowlisted frontend assets; never exposes repository or dataset paths.
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createHash, randomUUID } from "node:crypto";

export async function startFixtureServer(root) {
  const accounts = ["alice", "bob", "admin"].map((name) => ({ email: `${name}@example.test`, public_handle: `Local${name[0].toUpperCase()}${name.slice(1)}`, role: name === "admin" ? "admin" : "user", password: "Local-only-passphrase-2026!", user_id: name }));
  const sessions = new Map();
  const mail = [];
  const submissions = [];
  const keys = new Map();
  const prompts = new Map();
  const tasks = ["segmentation", "upos", "xpos", "dependency", "transliteration"];
  const challenges = tasks.map((task, index) => ({ challenge_id: `fixture-${task}`, title: `浏览器契约测试 · ${task}`, version: "fixture-v1", language: ["zh", "en", "de", "de", "zh"][index], treebank: ["GSDSimp", "EWT", "HDT", "HDT", "GSDSimp"][index], task, sample_count: 2, primary_metric: ["micro_f1", "micro_accuracy", "micro_accuracy", "las", "token_accuracy"][index], secondary_metrics: [], evaluation_identity_sha256: String(index + 1).repeat(64), model_identity: { model: "Browser contract fixture (not Qwen)", runtime: "mock", revision: "fixture", runtime_version: "fixture" }, student_prompt_utf8_bytes: 16384, submission_enabled: true, runtime_available: true, accepting_submissions: true, security_level: "public_reproducible", benchmark_limitations: "Synthetic browser contract fixture. No model evaluation is performed." }));
  const teaching = new Map(challenges.map((challenge) => {
    challenge.admissions_closed = false;
    return [challenge.challenge_id, { challenge, revision: 0, draft: null, published: null, published_revision: 0, admissions_closed: false, can_reopen: true }];
  }));
  const server = createServer(async (request, response) => {
    const origin = `http://127.0.0.1:${server.address().port}`;
    const url = new URL(request.url, origin);
    const send = (status, payload, headers = {}) => { response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers }); response.end(JSON.stringify(payload)); };
    const error = (status, code) => send(status, { error: { code, message: "Browser fixture response", details: {} } });
    if (["/", "/assets/app.js", "/assets/account.js", "/assets/admin.js", "/assets/teaching.js", "/assets/language-lessons.js", "/assets/template-rules.js", "/assets/xpos-lessons.js", "/assets/xpos-labels.js", "/assets/app.css", "/assets/app-shell.css", "/assets/brand-palette.css", "/assets/brand-option-a.svg"].includes(url.pathname)) {
      const file = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      response.writeHead(200, { "Content-Type": file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : file.endsWith(".svg") ? "image/svg+xml" : "text/html", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" });
      response.end(readFileSync(join(root, "src/linguistic_oj/web", file)));
      return;
    }
    let body = {};
    if (["POST", "PUT"].includes(request.method)) {
      if (request.headers["x-loj-csrf"] !== "1" || request.headers.origin !== origin || !request.headers["content-type"]?.startsWith("application/json")) { error(403, "AUTH_VALIDATION_ERROR"); return; }
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      if (url.pathname.startsWith("/v1/admin/") && Buffer.concat(chunks).length > 16384) { error(413, "REQUEST_BODY_TOO_LARGE"); return; }
      try { body = JSON.parse(Buffer.concat(chunks).toString()); } catch (_) { error(422, "AUTH_VALIDATION_ERROR"); return; }
    }
    const cookie = /(?:^|;\s*)fixture_session=([^;]+)/.exec(request.headers.cookie || "")?.[1];
    const user = sessions.get(cookie);
    const publicUser = (account) => ({ user_id: account.user_id, public_handle: account.public_handle, role: account.role });
    if (url.pathname === "/v1/auth/config") { send(200, { enabled: true, mode: "local", mail_delivery: "local" }); return; }
    if (url.pathname === "/v1/auth/session") { user ? send(200, { user: publicUser(user), expires_at: "2030-01-01T00:00:00Z" }) : error(401, "AUTHENTICATION_REQUIRED"); return; }
    if (url.pathname === "/v1/auth/login") {
      const account = accounts.find((item) => item.email === body.email && item.password === body.password);
      if (!account) { error(401, "AUTH_INVALID_CREDENTIALS"); return; }
      const token = randomUUID(); sessions.set(token, account);
      send(200, { user: publicUser(account), expires_at: "2030-01-01T00:00:00Z" }, { "Set-Cookie": `fixture_session=${token}; HttpOnly; SameSite=Strict; Path=/` }); return;
    }
    if (url.pathname === "/v1/auth/logout") { sessions.delete(cookie); send(200, { status: "signed_out" }, { "Set-Cookie": "fixture_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/" }); return; }
    if (["/v1/auth/register", "/v1/auth/password-reset/request"].includes(url.pathname)) {
      const purpose = url.pathname.endsWith("register") ? "verify-email" : "reset-password";
      const token = randomUUID();
      mail.push({ id: randomUUID(), recipient: body.email, purpose, link: `${origin}/#${purpose}?token=${token}`, created_at: new Date().toISOString(), token, used: false });
      send(202, { status: "accepted" }); return;
    }
    if (["/v1/auth/verify-email", "/v1/auth/password-reset/confirm"].includes(url.pathname)) {
      const purpose = url.pathname.endsWith("verify-email") ? "verify-email" : "reset-password";
      const item = mail.find((entry) => entry.token === body.token && entry.purpose === purpose && !entry.used);
      if (!item) { error(401, "AUTH_INVALID_TOKEN"); return; }
      item.used = true;
      if (purpose === "verify-email") accounts.push({ email: item.recipient, public_handle: body.public_handle, role: "user", password: body.password, user_id: randomUUID() });
      else {
        const account = accounts.find((entry) => entry.email === item.recipient);
        if (account) {
          account.password = body.password;
          for (const [token, owner] of sessions) if (owner === account) sessions.delete(token);
        }
      }
      send(200, { status: purpose === "verify-email" ? "verified" : "password_updated" }, purpose === "reset-password" ? { "Set-Cookie": "fixture_session=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/" } : {}); return;
    }
    if (url.pathname === "/v1/development" && request.headers["x-loj-development"] === "1") { send(200, { evaluation_mode: "mock", accounts: accounts.slice(0, 3), mail_delivery: "local" }); return; }
    if (url.pathname === "/v1/development/mail" && request.headers["x-loj-development"] === "1") { send(200, { items: mail.map(({ token, used, ...item }) => item) }); return; }
    if (url.pathname === "/v1/challenges") { send(200, challenges); return; }
    const publicTeaching = /^\/v1\/challenges\/([^/]+)\/teaching$/.exec(url.pathname);
    if (publicTeaching && request.method === "GET") {
      const detail = teaching.get(decodeURIComponent(publicTeaching[1]));
      if (!detail) { error(404, "CHALLENGE_NOT_FOUND"); return; }
      send(200, { challenge_id: detail.challenge.challenge_id, published_revision: detail.published_revision, content: detail.published }); return;
    }
    if (url.pathname.startsWith("/v1/admin/")) {
      if (!user) { error(401, "AUTHENTICATION_REQUIRED"); return; }
      if (request.headers["x-loj-expected-user"] !== user.user_id) { error(409, "AUTH_ACCOUNT_CHANGED"); return; }
      if (user.role !== "admin") { error(403, "ADMIN_REQUIRED"); return; }
      if (url.pathname === "/v1/admin/challenges" && request.method === "GET") {
        send(200, { items: [...teaching.values()].map(({ challenge, revision, published_revision, admissions_closed, can_reopen }) => ({ challenge_id: challenge.challenge_id, title: challenge.title, language: challenge.language, treebank: challenge.treebank, task: challenge.task, has_contract: true, revision, published_revision, admissions_closed, can_reopen })) }); return;
      }
      const match = /^\/v1\/admin\/challenges\/([^/]+)(?:\/(teaching-draft|check|publish|admissions))?$/.exec(url.pathname);
      const detail = match && teaching.get(decodeURIComponent(match[1]));
      if (!detail) { error(404, "CHALLENGE_NOT_FOUND"); return; }
      const action = match[2];
      if (!action && request.method === "GET") { send(200, detail); return; }
      if ((action === "teaching-draft" ? "PUT" : "POST") !== request.method) { error(405, "METHOD_NOT_ALLOWED"); return; }
      if (body.expected_revision !== detail.revision) { error(409, "ADMIN_REVISION_CONFLICT"); return; }
      if (action === "teaching-draft") {
        const limits = { title: [1, 120], summary: [0, 500], instructions: [1, 4000], zero_shot_prompt: [1, 3000], few_shot_prompt: [1, 3000] };
        if (!body.content || Object.keys(body.content).length !== 5 || Object.entries(limits).some(([key, [min, max]]) => typeof body.content[key] !== "string" || Array.from(body.content[key]).length < min || Array.from(body.content[key]).length > max || (min && !body.content[key].trim()) || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(body.content[key].replaceAll("\n", "")))) { error(422, "REQUEST_VALIDATION_ERROR"); return; }
        detail.draft = body.content;
      } else if (action === "check") {
        send(200, { can_publish: Boolean(detail.draft), issues: detail.draft ? [] : [{ code: "ADMIN_DRAFT_REQUIRED", message: "Save a teaching draft first." }], revision: detail.revision }); return;
      } else if (action === "publish") {
        if (!detail.draft) { error(409, "ADMIN_DRAFT_REQUIRED"); return; }
        detail.published = structuredClone(detail.draft);
        detail.published_revision = detail.revision + 1;
      } else if (action === "admissions") {
        if (typeof body.closed !== "boolean") { error(422, "REQUEST_VALIDATION_ERROR"); return; }
        if (!body.closed && !detail.can_reopen) { error(409, "CHALLENGE_NOT_OPEN"); return; }
        detail.admissions_closed = body.closed;
        detail.challenge.admissions_closed = body.closed;
        detail.challenge.accepting_submissions = !body.closed && detail.challenge.submission_enabled && detail.challenge.runtime_available;
      }
      detail.revision++;
      send(200, detail); return;
    }
    if (url.pathname.startsWith("/v1/leaderboards/")) {
      const identity = url.pathname.split("/").at(-1);
      const rows = submissions.filter((entry) => entry.evaluation_identity_sha256 === identity).map((entry, index) => ({ rank: index + 1, public_handle: accounts.find((account) => account.user_id === entry.owner).public_handle, score: 0.5, evaluation_identity_sha256: identity }));
      send(200, { items: rows, next_cursor: null }); return;
    }
    if (url.pathname.startsWith("/v1/submissions")) {
      if (!user) { error(401, "AUTHENTICATION_REQUIRED"); return; }
      if (request.headers["x-loj-expected-user"] !== user.user_id) { error(409, "AUTH_ACCOUNT_CHANGED"); return; }
      if (url.pathname === "/v1/submissions" && request.method === "POST") {
        const key = `${user.user_id}:${request.headers["idempotency-key"]}`;
        let submission = keys.get(key);
        if (!submission) {
          const challenge = challenges.find((entry) => entry.challenge_id === body.challenge_id);
          if (!challenge) { error(404, "CHALLENGE_NOT_FOUND"); return; }
          if (challenge.admissions_closed) { error(409, "CHALLENGE_PAUSED"); return; }
          submission = { submission_id: randomUUID(), challenge_id: challenge.challenge_id, evaluation_identity_sha256: challenge.evaluation_identity_sha256, status: "succeeded", created_at: new Date().toISOString(), started_at: new Date().toISOString(), completed_at: new Date().toISOString() };
          keys.set(key, submission); submissions.push({ ...submission, owner: user.user_id });
          prompts.set(submission.submission_id, { submission_id: submission.submission_id, challenge_id: challenge.challenge_id, student_prompt: body.student_prompt, student_prompt_sha256: createHash('sha256').update(body.student_prompt).digest('hex') });
        }
        send(202, submission); return;
      }
      if (url.pathname === "/v1/submissions") { send(200, { items: submissions.filter((entry) => entry.owner === user.user_id).map(({ owner, ...entry }) => entry).reverse(), next_cursor: null }); return; }
      const entry = submissions.find((item) => item.submission_id === url.pathname.split("/")[3] && item.owner === user.user_id);
      if (!entry) { error(404, "SUBMISSION_NOT_FOUND"); return; }
      const { owner, ...submission } = entry;
      if (url.pathname.endsWith('/prompt')) { send(200, prompts.get(entry.submission_id)); return; }
      if (url.pathname.endsWith("/result")) {
        const challenge = challenges.find((item) => item.challenge_id === entry.challenge_id);
        send(200, { outcome: "succeeded", task: challenge.task, primary_metric: challenge.primary_metric, model_identity: challenge.model_identity, score: 0.5, metrics: { [challenge.primary_metric]: 0.5 }, samples_total: 2, samples_valid: 2, samples_invalid: 0, errors: {} });
      } else send(200, submission);
      return;
    }
    error(404, "NOT_FOUND");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); }) };
}
