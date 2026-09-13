"use strict";
(() => {
  // Handwritten layout fixtures only. No fetch, cookies, persistent storage, or model calls.
  const languages = [
    ["ar", "阿拉伯语"], ["zh", "中文"], ["da", "丹麦语"], ["nl", "荷兰语"],
    ["en", "英语"], ["fr", "法语"], ["de", "德语"], ["he", "希伯来语"],
    ["hi", "印地语"], ["hu", "匈牙利语"], ["it", "意大利语"], ["ja", "日语"],
    ["ko", "韩语"], ["pt", "葡萄牙语"], ["ru", "俄语"], ["es", "西班牙语"],
    ["sv", "瑞典语"], ["th", "泰语"],
  ];
  const lessons = {
    upos: { name: "通用词性标注", family: "pos", code: "UPOS", heading: "为每个词元选择词性", description: "根据上下文，为每个输入词元选择一个通用词性标签。保持顺序，不增删词元。", language: "英语格式示例；标签体系跨语言通用。", input: { tokens: ["Birds", "sing", "."] }, output: { tags: ["NOUN", "VERB", "PUNCT"] }, metric: "词元准确率", scoring: "词元准确率 = 标签正确的词元数 ÷ 全部评测词元数。", instruction: "为每个输入词元返回一个通用词性标签。保持原顺序，只返回包含 tags 数组的 JSON 对象。" },
    xpos: { name: "树库词性标注", family: "pos", code: "XPOS", heading: "使用指定的词性标签", description: "根据上下文，按题目指定的树库标签体系标注词性。不要与通用词性标签混用。", language: "德语 STTS 标签格式示例。", input: { tokens: ["Vögel", "singen", "."] }, output: { tags: ["NN", "VVFIN", "$."] }, metric: "词元准确率", scoring: "词元准确率 = 标签正确的词元数 ÷ 全部评测词元数。标签须与对应位置的标准标签一致。", instruction: "按德语 STTS 标签体系为输入词元标注词性。保持顺序，只返回包含 tags 数组的 JSON 对象。" },
    segmentation: { name: "分词", family: "segmentation", code: "", heading: "找出文本中的词", description: "将连续文本切分为词元。保持原文顺序和内容，不遗漏、不增加字符。", language: "中文分词示例。", input: { text: "我喜欢语言学" }, output: { tokens: ["我", "喜欢", "语言学"] }, metric: "分词 F1", scoring: "词的起止位置均与标准答案一致时，计为正确分词。汇总全部样本的正确词数、预测词数和标准词数，计算精确率、召回率及其调和平均值 F1。", instruction: "将输入文本切分为词元，保持内容与顺序。只返回包含 tokens 数组的 JSON 对象。" },
    dependency: { name: "依存句法分析", family: "dependency", code: "", heading: "标注词之间的依存关系", description: "为每个词元指定中心词编号和依存关系。根节点的中心词编号为0。", language: "德语依存句法格式示例。", input: { tokens: [{ token_id: 1, form: "Vögel" }, { token_id: 2, form: "singen" }] }, output: { arcs: [{ token_id: 1, head_id: 2, deprel: "nsubj" }, { token_id: 2, head_id: 0, deprel: "root" }] }, metric: "带标签依附准确率", scoring: "中心词编号和关系标签均正确时，计为正确依附。分数为正确依附数占全部评测词元数的比例。", instruction: "分析输入词元的依存关系。只返回包含 arcs 数组的 JSON 对象；每项含 token_id、head_id 和 deprel。" },
    transliteration: { name: "中文转写", family: "transliteration", code: "", heading: "把词元转成拼音", description: "根据上下文为词元生成拼音。保持词元数量和顺序，声调等约定以任务说明为准。", language: "中文带声调拼音格式示例。", input: { text: "我爱中文", tokens: ["我", "爱", "中文"] }, output: { transliterations: ["wǒ", "ài", "zhōngwén"] }, metric: "词元准确率", scoring: "词元准确率 = 转写正确的词元数 ÷ 全部评测词元数。转写须与对应位置的标准答案一致。", instruction: "将输入 tokens 转为带声调汉语拼音。保持数量与顺序，只返回包含 transliterations 数组的 JSON 对象。" },
  };
  const tasks = languages.map(([language, label]) => ({ id: `${language}-upos`, language, label, type: "upos" }));
  for (const [language, type] of [["zh", "segmentation"], ["de", "xpos"], ["de", "dependency"], ["zh", "transliteration"]]) tasks.push({ id: `${language}-${type}`, language, label: languages.find(item => item[0] === language)[1], type });
  const el = id => document.getElementById(id);
  // Approved default is original A in Fudan blue, rendered directly in HTML.
  // Explicit selections remain available for archived design comparisons.
  const design = new URLSearchParams(location.search);
  const mark = design.get("mark");
  if (/^(?:[a-h]|a[1-6])$/.test(mark || "")) {
    const image = document.querySelector(".brand-mark");
    image.src = `brand-option-${mark}.svg?v=fudan-1`;
    image.hidden = false;
    image.classList.toggle("is-ink", design.get("color") === "ink");
    el("brand-favicon").href = image.src;
  }
  const drafts = new Map();
  let currentTask = tasks.find(task => task.id === "en-upos");
  let submitted = null;
  const create = (tag, text, className) => { const node = document.createElement(tag); node.textContent = text; if (className) node.className = className; return node; };

  for (const [code, name] of languages) el("language").add(new Option(name, code));
  if (location.protocol === "file:") el("back-to-app").hidden = true;

  function renderCatalog() {
    const query = el("search").value.trim().toLowerCase();
    const filtered = tasks.filter(task => {
      const lesson = lessons[task.type];
      return (!el("language").value || el("language").value === task.language)
        && (!el("task-type").value || el("task-type").value === lesson.family)
        && `${task.label} ${lesson.name} ${lesson.code}`.toLowerCase().includes(query);
    });
    el("task-rows").replaceChildren();
    for (const task of filtered) {
      const lesson = lessons[task.type];
      const row = document.createElement("tr");
      row.dataset.taskId = task.id;
      const title = document.createElement("td");
      title.append(create("span", lesson.name, "task-title"));
      if (lesson.code) title.append(create("span", lesson.code, "task-type"));
      const action = document.createElement("td");
      const link = create("a", "进入练习 →", "enter");
      link.href = `#practice/${task.id}`;
      link.setAttribute("aria-label", `进入${task.label}${lesson.name}练习预览`);
      action.append(link);
      row.append(title, create("td", task.label, "language-cell"), create("td", "50个样本", "count-cell"), action);
      el("task-rows").append(row);
    }
    el("catalog-count").textContent = `${filtered.length}项示例任务`;
    el("empty").hidden = filtered.length !== 0;
  }

  function tab(name, focus = false) {
    for (const button of document.querySelectorAll('[role="tab"]')) {
      const active = button.id === `tab-${name}`;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      el(button.getAttribute("aria-controls")).hidden = !active;
      if (active && focus) button.focus();
    }
  }

  function renderPractice() {
    const task = currentTask;
    const lesson = lessons[task.type];
    el("practice-title").textContent = `${task.label} · ${lesson.name}`;
    el("practice-meta").textContent = "50个样本 · Qwen3.5-9B";
    el("lesson-heading").textContent = lesson.heading;
    el("lesson-description").textContent = lesson.description;
    el("token-example").hidden = task.type !== "upos";
    el("example-note").hidden = task.type !== "upos";
    el("example-language").textContent = lesson.language;
    el("example-input").textContent = JSON.stringify(lesson.input, null, 2);
    el("example-output").textContent = JSON.stringify(lesson.output, null, 2);
    el("scoring-description").textContent = lesson.scoring;
    el("prompt").value = drafts.get(task.id) || "";
    el("template").value = "";
    el("preview-result").disabled = !el("prompt").value.trim();
    tab("description");
  }

  function renderResult() {
    const task = submitted?.task || currentTask;
    const lesson = lessons[task.type];
    el("result-task").textContent = `${task.label} · ${lesson.name}`;
    el("score-label").textContent = `${lesson.metric} · 示例分数`;
    el("submitted-prompt").textContent = submitted?.prompt || lesson.instruction;
    for (const id of ["back-to-practice", "continue-editing"]) el(id).href = `#practice/${task.id}`;
  }

  function route() {
    const hash = location.hash.slice(1);
    let page = "catalog";
    if (hash.startsWith("practice/")) {
      const task = tasks.find(item => item.id === hash.slice(9));
      if (task) { currentTask = task; page = "practice"; }
    } else if (hash === "result") page = "result";
    for (const name of ["catalog", "practice", "result"]) el(`${name}-view`).hidden = name !== page;
    el("practice-link").href = `#practice/${currentTask.id}`;
    for (const link of document.querySelectorAll("[data-page]")) {
      if (link.dataset.page === page) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
    }
    if (page === "practice") renderPractice();
    if (page === "result") renderResult();
    el(`${page}-title`).focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }

  for (const id of ["search", "language", "task-type"]) el(id).addEventListener(id === "search" ? "input" : "change", renderCatalog);
  el("clear-filters").addEventListener("click", () => { for (const id of ["search", "language", "task-type"]) el(id).value = ""; renderCatalog(); el("search").focus(); });
  for (const button of document.querySelectorAll('[role="tab"]')) {
    button.addEventListener("click", () => tab(button.id.slice(4)));
    button.addEventListener("keydown", event => {
      const names = ["description", "example", "scoring"];
      const index = names.indexOf(button.id.slice(4));
      const next = event.key === "ArrowRight" ? (index + 1) % 3 : event.key === "ArrowLeft" ? (index + 2) % 3 : event.key === "Home" ? 0 : event.key === "End" ? 2 : -1;
      if (next >= 0) { event.preventDefault(); tab(names[next], true); }
    });
  }
  el("prompt").addEventListener("input", () => { drafts.set(currentTask.id, el("prompt").value); el("preview-result").disabled = !el("prompt").value.trim(); });
  el("template").addEventListener("change", () => {
    if (!el("template").value) return;
    if (el("prompt").value && !window.confirm("用模板替换当前预览草稿？")) { el("template").value = ""; return; }
    const lesson = lessons[currentTask.type];
    const text = lesson.instruction + (el("template").value === "few" ? `\n\n格式示例：\n输入：${JSON.stringify(lesson.input)}\n输出：${JSON.stringify(lesson.output)}` : "");
    el("prompt").value = text;
    el("prompt").dispatchEvent(new Event("input"));
    el("prompt").focus();
  });
  el("preview-result").addEventListener("click", () => { if (!el("prompt").value.trim()) return; submitted = { task: currentTask, prompt: el("prompt").value }; location.hash = "result"; });
  el("continue-editing").addEventListener("click", () => {
    const task = submitted?.task || currentTask;
    drafts.set(task.id, submitted?.prompt || lessons[task.type].instruction);
  });
  window.addEventListener("hashchange", route);
  renderCatalog();
  route();
})();
