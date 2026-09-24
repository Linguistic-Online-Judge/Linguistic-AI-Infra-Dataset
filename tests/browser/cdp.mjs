// Dependency-free browser driver using Node 22+ WebSocket and Chromium CDP.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { createServer } from "node:net";

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function startBrowser(artifacts) {
  const executable = process.env.LOJ_BROWSER || [
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  ].find(existsSync);
  if (!executable) throw new Error("Set LOJ_BROWSER to an installed Chromium/Edge executable. No browser is downloaded.");
  if (!existsSync(artifacts)) mkdirSync(artifacts, { recursive: true });
  const profile = mkdtempSync(join(artifacts, "profile-"));
  const reservation = createServer();
  await new Promise((resolve) => reservation.listen(0, "127.0.0.1", resolve));
  const port = reservation.address().port;
  await new Promise((resolve) => reservation.close(resolve));
  const child = spawn(executable, ["--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-background-networking", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
  let launchError;
  child.on("error", (error) => { launchError = error; });
  let target;
  for (let attempt = 0; attempt < 150; attempt++) {
    if (launchError) throw launchError;
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" });
      target = await response.json();
      break;
    } catch (_) { await sleep(100); }
  }
  if (!target) { child.kill(); throw new Error("Browser remote debugging did not become ready."); }
  const browser = await connectPage(target, artifacts);
  const pages = [];
  browser.newPage = async () => {
    const response = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" });
    const page = await connectPage(await response.json(), artifacts);
    pages.push(page);
    return page;
  };
  const closePage = browser.close;
  browser.close = async () => {
    for (const page of pages) await page.close();
    try { await browser.send("Browser.close"); } catch (_) { child.kill(); }
    await closePage();
    await sleep(500);
    try { rmSync(profile, { recursive: true, force: true }); } catch (_) { /* Edge may still be releasing profile handles. */ }
  };
  return browser;
}

async function connectPage(target, artifacts) {
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  const pending = new Map();
  const listeners = new Map();
  const exceptions = [];
  const loaded = new Set();
  let nextId = 0;
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const request = pending.get(message.id);
      if (!request) return;
      pending.delete(message.id);
      clearTimeout(request.timer);
      if (message.error) request.reject(new Error(message.error.message)); else request.resolve(message.result);
    } else for (const callback of listeners.get(message.method) || []) callback(message.params);
  });
  const browser = {
    exceptions,
    send(method, params = {}) {
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timed out: ${method}`)); }, 25000);
        pending.set(id, { resolve, reject, timer });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    on(method, callback) { if (!listeners.has(method)) listeners.set(method, []); listeners.get(method).push(callback); },
    async evaluate(expression) {
      const value = await browser.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (value.exceptionDetails) throw new Error(value.exceptionDetails.exception?.description || value.exceptionDetails.text);
      return value.result.value;
    },
    async wait(expression, message = expression, timeout = 15000) {
      const deadline = Date.now() + timeout;
      while (Date.now() < deadline) {
        if (await browser.evaluate(expression)) return;
        await sleep(75);
      }
      throw new Error(`Timed out: ${message}`);
    },
    async navigate(url) {
      const navigation = await browser.send("Page.navigate", { url });
      if (navigation.errorText) throw new Error(navigation.errorText);
      if (navigation.loaderId) {
        const deadline = Date.now() + 15000;
        while (!loaded.has(navigation.loaderId) && Date.now() < deadline) await sleep(50);
        if (!loaded.delete(navigation.loaderId)) throw new Error("New frontend document did not finish loading");
      }
      await browser.wait("document.readyState === 'complete' && !!document.getElementById('auth-dialog')", "frontend document loaded");
    },
    async click(selector) {
      const point = await browser.evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e || e.disabled) throw new Error('Missing or disabled control: ' + ${JSON.stringify(selector)}); e.scrollIntoView({block:'center', behavior:'instant'}); const r=e.getBoundingClientRect(); if (!r.width || !r.height) throw new Error('Hidden control'); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`);
      await browser.send("Input.dispatchMouseEvent", { type: "mousePressed", ...point, button: "left", clickCount: 1 });
      await browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...point, button: "left", clickCount: 1 });
    },
    async fill(selector, text) {
      await browser.evaluate(`(() => { const e=document.querySelector(${JSON.stringify(selector)}); e.focus(); e.value=${JSON.stringify(text)}; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    },
    async viewport(width, height, mobile = false) {
      await browser.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile });
    },
    async screenshot(name) {
      const image = await browser.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      writeFileSync(join(artifacts, `${name}.png`), Buffer.from(image.data, "base64"));
    },
    async close() {
      if (socket.readyState !== WebSocket.OPEN) return;
      try { await browser.send("Target.closeTarget", { targetId: target.id }); } catch (_) { /* The browser may already be closed. */ }
      socket.close();
      for (const request of pending.values()) { clearTimeout(request.timer); request.reject(new Error("Browser closed")); }
      pending.clear();
    },
  };
  await browser.send("Page.enable");
  browser.on("Page.lifecycleEvent", (event) => { if (event.name === "load") loaded.add(event.loaderId); });
  await browser.send("Page.setLifecycleEventsEnabled", { enabled: true });
  await browser.send("Runtime.enable");
  browser.on("Runtime.exceptionThrown", (event) => exceptions.push(event.exceptionDetails.exception?.description || event.exceptionDetails.text));
  await browser.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  return browser;
}
