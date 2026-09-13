"use strict";
(() => {
  const initial = new URLSearchParams(location.search).get("set") === "initial";
  const concepts = initial
    ? [["a", "折页 L"], ["b", "对页"], ["c", "引文"], ["d", "连写 lo"], ["e", "双声"], ["f", "合字 LJ"], ["g", "字形 a"], ["h", "语流"]]
    : [["a1", "修长折页"], ["a2", "宽幅折页"], ["a3", "圆角折页"], ["a4", "折痕留白"], ["a5", "上翻折页"], ["a6", "斜切字肩"]];
  const options = document.getElementById("options");
  const status = document.getElementById("selection-status");
  const selectedMark = document.getElementById("selected-mark");
  const tryDesign = document.getElementById("try-design");
  const compareOriginal = document.getElementById("compare-original");
  let selected = "a";
  if (initial) {
    options.classList.add("is-initial");
    options.setAttribute("aria-label", "上一轮八种图标方案");
    document.getElementById("options-title").textContent = "上一轮方案";
    const previous = document.getElementById("previous-round");
    previous.href = "brand-options.html";
    previous.textContent = "查看六版 L 方案";
  }
  const span = (className, text = "") => { const node = document.createElement("span"); node.className = className; node.textContent = text; return node; };
  const asset = id => `brand-option-${id}.svg?v=fudan-1`;
  const image = (id, size) => { const node = document.createElement("img"); node.src = asset(id); node.width = size; node.height = size; node.alt = ""; return node; };
  function updateLink() {
    if (selected) tryDesign.href = `layout-preview.html?mark=${selected}&color=${document.querySelector('[name="color"]:checked').value}`;
  }
  function select(id, name) {
    selected = id;
    selectedMark.src = asset(id);
    selectedMark.hidden = false;
    status.textContent = `${id.toUpperCase()} · ${name}`;
    compareOriginal.disabled = id === "a";
    for (const option of options.children) {
      const active = option.dataset.option === id;
      option.setAttribute("aria-pressed", String(active));
      option.querySelector(".option-action").textContent = active ? "已选 ✓" : "试用";
    }
    tryDesign.hidden = false;
    updateLink();
  }
  for (const [id, name] of concepts) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "option";
    button.dataset.option = id;
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-label", `试用方案${id.toUpperCase()}：${name}`);
    const title = span("option-title", name);
    title.append(span("option-id", id.toUpperCase()));
    const art = span("option-art"); art.append(image(id, 80));
    const footer = span("option-footer");
    const sizes = span("small-sizes"); sizes.append(image(id, 32), image(id, 16));
    sizes.setAttribute("aria-label", "32与16像素效果");
    footer.append(sizes, span("option-action", "试用"));
    button.append(title, art, footer);
    button.addEventListener("click", () => select(id, name));
    options.append(button);
  }
  compareOriginal.addEventListener("click", () => select("a", "原方案"));
  select("a", "原方案");
  for (const radio of document.querySelectorAll('[name="color"]')) radio.addEventListener("change", () => {
    document.body.classList.toggle("blue", radio.value === "blue" && radio.checked);
    updateLink();
  });
})();
