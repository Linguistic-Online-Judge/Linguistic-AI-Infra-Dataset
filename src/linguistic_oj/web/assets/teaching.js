"use strict";

// Public, handwritten teaching material. Never populated from evaluation answers.
const TASK_LESSONS = {
  segmentation: {
    description: "分词：把连续文本划分成词元（token，即本任务中的标注单位）。模型只收到 text，需要自行决定边界。树库是带有语言学标注的语料集合，不同树库的分词习惯可能不同。",
    rules: "输出 tokens 字符串数组。按原顺序保留全部字符，不改写、不翻译、不丢标点。分词按词元在原文中的完整区间匹配计分：起点与终点都相同才算正确。主要指标 micro F1 综合衡量分词精确率与召回率，不等于整句全对比例。",
    field: "tokens",
    instruction: "对输入 text 进行分词。按语言与当前树库的标注习惯确定词元边界，保留所有原始字符和顺序，标点也作为词元。不改写、不翻译。只返回一个 JSON 对象，唯一字段为 tokens，值为非空字符串数组。不要输出解释、Markdown 或额外字段。",
    examples: [
      { input: { text: "小猫睡觉。" }, output: { tokens: ["小猫", "睡觉", "。"] } },
      { input: { text: "我喝茶。" }, output: { tokens: ["我", "喝", "茶", "。"] } },
    ],
  },
  upos: {
    description: "通用词性标注：为已给定的每个词元选择一个 UPOS（Universal Part-of-Speech，通用词性）标签。模型收到 tokens，不需要重新分词。示例以英语说明格式；标签体系跨语言通用。",
    rules: "tags 与输入词元等长、同序。使用 17 种大写标签：ADJ、ADP、ADV、AUX、CCONJ、DET、INTJ、NOUN、NUM、PART、PRON、PROPN、PUNCT、SCONJ、SYM、VERB、X。例如 NOUN 是普通名词，VERB 是动词，PUNCT 是标点。micro accuracy 按全部词元汇总正确比例。",
    field: "tags",
    instruction: "为输入 tokens 中每个词元标注 UPOS 通用词性，结合完整句子判断其语法功能。标签仅限 ADJ ADP ADV AUX CCONJ DET INTJ NOUN NUM PART PRON PROPN PUNCT SCONJ SYM VERB X。不要重新分词。只返回 JSON 对象，唯一字段 tags 为非空字符串数组，与输入等长、同序。不要输出解释、Markdown 或额外字段。",
    examples: [
      { input: { tokens: ["Birds", "sing", "."] }, output: { tags: ["NOUN", "VERB", "PUNCT"] } },
      { input: { tokens: ["I", "read", "books", "."] }, output: { tags: ["PRON", "VERB", "NOUN", "PUNCT"] } },
    ],
  },
  xpos: {
    description: "树库词性标注：为每个给定词元选择 XPOS（Language-Specific Part-of-Speech，语言或树库专用词性）标签。它不等同于 UPOS。当前德语 HDT 示例采用 STTS（Stuttgart-Tübingen Tagset，德语词性标签集）。",
    rules: "输出 tags，与输入词元等长、同序，标签须符合当前树库约定。例如德语 NN 是普通名词，VVFIN 是限定形式的实义动词，$. 是句末标点。不要把其他语言或树库的标签直接套用。micro accuracy 按全部词元汇总正确比例。",
    field: "tags",
    instruction: "为输入 tokens 中每个词元标注当前语言和树库的 XPOS 词性。德语 HDT 使用 STTS 标签，不要替换成 UPOS 标签。根据句法功能和形态选择准确标签，标点也需标注。只返回 JSON 对象，唯一字段 tags 为非空字符串数组，与输入等长、同序。不要输出解释、Markdown 或额外字段。",
    examples: [
      { input: { tokens: ["Katzen", "schlafen", "."] }, output: { tags: ["NN", "VVFIN", "$."] } },
      { input: { tokens: ["Das", "Kind", "liest", "."] }, output: { tags: ["ART", "NN", "VVFIN", "$."] } },
    ],
  },
  dependency: {
    description: "依存句法分析：判断每个词元依附于哪个中心词，以及二者的语法关系。输入为带 token_id 和 form（词形）的 tokens。当前示例采用 UD（Universal Dependencies，通用依存语法）关系标签。",
    rules: "每个 token_id 恰好对应一条 arc（依存弧）。head_id 是中心词编号，0 表示句根；deprel 是关系标签，例如 nsubj 为名词性主语，root 为句根。目标是单根、连通、无环的树。LAS（Labeled Attachment Score，带标签依附准确率）要求中心词和关系均正确；UAS（Unlabeled Attachment Score，无标签依附准确率）只检查中心词。格式通过不等于句法正确。",
    field: "arcs",
    instruction: typeof TEMPLATE_RULES === "undefined" ? "按UD规则为输入词元标注依存关系。只返回含arcs数组的JSON对象，每项含token_id、head_id、deprel。" : TEMPLATE_RULES.dependency,
    examples: [
      { input: { tokens: [{ token_id: 1, form: "Katzen" }, { token_id: 2, form: "schlafen" }] }, output: { arcs: [{ token_id: 1, head_id: 2, deprel: "nsubj" }, { token_id: 2, head_id: 0, deprel: "root" }] } },
      { input: { tokens: [{ token_id: 1, form: "Vögel" }, { token_id: 2, form: "singen" }] }, output: { arcs: [{ token_id: 1, head_id: 2, deprel: "nsubj" }, { token_id: 2, head_id: 0, deprel: "root" }] } },
    ],
  },
  transliteration: {
    description: "转写：将给定词元转换成另一套书写形式，不是翻译。当前中文 GSDSimp 任务使用带声调符号的汉语拼音。模型同时收到 text 和固定 tokens，需按词元逐项转写。",
    rules: "输出 transliterations，与 tokens 等长、同序。当前中文约定使用小写带调拼音，词内不加空格；必要时使用 ASCII 单引号分隔音节，拉丁字母和数字保留。遵循 GSDSimp 标点形式，例如“，”→“,”、“。”→“.”。token accuracy 按词元统计正确率，sentence exact match rate 为整句完全匹配比例。",
    field: "transliterations",
    instruction: "将输入 tokens 按原顺序转为小写、带声调符号的汉语拼音，每个词元对应一个字符串，词内不加空格，必要时以 ASCII 单引号分隔音节。根据 text 判断读音，保留数字及拉丁文本的大小写。按 GSDSimp 约定处理标点：（→(，）→)，逗号和顿号→,，句号→.，冒号→:，分号→;，书名号《》→«»，单书名号〈〉→<>。只返回 JSON 对象，唯一字段 transliterations 为非空字符串数组，与输入 tokens 等长、同序。不输出解释、Markdown 或额外字段。",
    examples: [
      { input: { text: "小猫喝水。", tokens: ["小猫", "喝水", "。"] }, output: { transliterations: ["xiǎomāo", "hēshuǐ", "."] } },
      { input: { text: "天气好。", tokens: ["天气", "好", "。"] }, output: { transliterations: ["tiānqì", "hǎo", "."] } },
    ],
  },
};

const teachingState = { sequence: 0, challengeId: null, status: "idle", payload: null };

const SCORING_HELP = {
  upos: "词元准确率 = 标签正确的词元数 ÷ 全部评测词元数。",
  xpos: "词元准确率 = 标签正确的词元数 ÷ 全部评测词元数。标签须与对应位置的标准标签一致。",
  segmentation: "词的起止位置均与标准答案一致时，计为正确分词。汇总全部样本的正确词数、预测词数和标准词数，计算精确率、召回率及其调和平均值F1。",
  dependency: "中心词编号和关系标签均正确时，计为正确依附。分数为正确依附数占全部评测词元数的比例。",
  transliteration: "词元准确率 = 转写正确的词元数 ÷ 全部评测词元数。书写形式须与对应位置的标准答案一致。",
};

function lessonFor(challenge) {
  const lesson = TASK_LESSONS[challenge?.task];
  if (!lesson) return null;
  const language = challenge.language.toLowerCase();
  const treebank = challenge.treebank.toLowerCase();
  const english = ["en", "english"].includes(language);
  const chinese = ["zh", "chinese"].includes(language);
  const localExamples = typeof LANGUAGE_TASK_EXAMPLES === "undefined" ? null : Object.entries(LANGUAGE_TASK_EXAMPLES).find(([code, entry]) => code === language || entry.language.toLowerCase() === language)?.[1];
  if (localExamples && ["segmentation", "dependency"].includes(challenge.task) && treebank !== "localpractice") {
    const segmentation = challenge.task === "segmentation";
    const examples = localExamples.examples.map(example => segmentation ? { input: { text: example.tokens.join("") }, output: { tokens: example.tokens } } : {
      input: { tokens: example.tokens.map((form, index) => ({ token_id: index + 1, form })) },
      output: { arcs: example.tokens.map((_, index) => ({ token_id: index + 1, head_id: example.heads[index], deprel: example.relations[index] })) },
    });
    return {
      ...lesson, examples,
      exampleLabel: `${languageLabel(challenge.language)} · 手写格式示例`,
      description: segmentation ? `本练习将树库词元拼接为连续文本，请恢复词元边界并保留全部字符。${localExamples.note}` : `为给定的每个词元标注中心词编号和依存关系。根节点的中心词编号为0，使用当前树库的通用依存关系标签。`,
      instruction: segmentation ? (typeof TEMPLATE_RULES === "undefined" ? lesson.instruction : TEMPLATE_RULES.segmentation.replace("{note}", localExamples.note)) : lesson.instruction,
    };
  }
  if (challenge.task === "xpos") {
    const profile = typeof XPOS_LESSONS === "undefined" ? null : Object.entries(XPOS_LESSONS).find(([code, entry]) => (code === language || entry.language.toLowerCase() === language) && entry.treebank.toLowerCase() === treebank)?.[1];
    if (profile) return {
      ...lesson,
      description: `本任务采用${profile.tagset}。${profile.guide}`,
      rules: "标签与输入词元等长、同序。区分大小写，完整保留标签中的竖线、加号、连接符和各级组成部分。",
      instruction: `为输入 tokens 中的每个词元标注${profile.tagset}。${profile.guide}标签区分大小写，保留标签中的竖线、加号、连接符及全部组成部分。不要重新分词。输出顺序和数量必须与输入一致。只返回一个 JSON 对象，唯一字段 tags 为非空字符串数组，不输出解释或额外字段。`,
      exampleLabel: `${languageLabel(challenge.language)} / ${profile.treebank} · 手写专用标签示例`,
      examples: profile.examples.map(example => ({ input: { tokens: example.tokens }, output: { tags: example.tags } })),
      inventory: typeof XPOS_LABEL_INVENTORIES === "undefined" ? [] : XPOS_LABEL_INVENTORIES[`${profile.language}/${profile.treebank}`] || [],
    };
    if (english && treebank === "localpractice") return {
      ...lesson,
      description: "英语本地练习的树库词性标注：XPOS 与 UPOS 都属于词性标注，但 XPOS 使用当前语言与树库的专用标签。本任务采用 Penn Treebank（宾州树库）词性标签，不使用德语 STTS。",
      rules: "输出 tags，与输入词元等长、同序。英语 NN 表示单数普通名词，NNS 表示复数普通名词，VBP 表示非第三人称单数现在时动词，句末标点标签为 .。不要替换成 UPOS 或德语标签。词元准确率按全部词元汇总正确比例。",
      instruction: "为英语 LocalPractice 输入 tokens 标注 Penn Treebank XPOS 词性。普通单数名词用 NN，普通复数名词用 NNS，非第三人称单数现在时动词用 VBP，句末标点用 .。结合句子选择标签，不重新分词，不使用 UPOS 或德语 STTS。只返回 JSON 对象，唯一字段 tags 为非空字符串数组，与输入等长、同序。不输出解释、Markdown 或额外字段。",
      exampleLabel: "英语 / LocalPractice · Penn 标签手写示例",
      examples: [{ input: { tokens: ["Birds", "sing", "."] }, output: { tags: ["NNS", "VBP", "."] } }, { input: { tokens: ["We", "drink", "tea", "."] }, output: { tags: ["PRP", "VBP", "NN", "."] } }],
    };
    if (["de", "german"].includes(language) && treebank === "hdt") return { ...lesson, exampleLabel: "德语 / HDT · STTS 标签手写示例" };
    return { ...lesson, description: "树库词性 XPOS：与通用词性 UPOS 同属词性标注，使用当前语言及树库的专用标签。", rules: "tags 与输入词元等长、同序。标签以当前树库约定为准，不能直接套用其他语言的标签。", instruction: "为输入 tokens 标注当前语言和树库规定的 XPOS 标签。只返回唯一字段 tags 的 JSON 对象，数组与输入等长、同序。不输出解释或额外字段。", examples: [], exampleLabel: "此语言与树库暂无专用手写示例" };
  }
  if (challenge.task === "transliteration") {
    if (chinese && treebank === "localpractice") return {
      ...lesson,
      description: "中文本地练习的简化转写：将给定词元转为小写、不带声调的汉语拼音，不是翻译。此练习不是 GSDSimp 的带调拼音约定。模型收到 text 和固定 tokens。",
      rules: "transliterations 与 tokens 等长、同序，使用不带声调的小写拼音，词内不加空格，标点保留原样（。仍为。）。这是本地手写练习的简化约定，不可套用于 GSDSimp 正式任务。词元准确率统计逐项正确比例，整句完全匹配率要求全句相同。",
      instruction: "按中文 LocalPractice 简化约定，将输入 tokens 逐项转为小写、不带声调的汉语拼音，词内不加空格。结合 text 判断读音，保持词元顺序与数量，标点原样保留，不采用 GSDSimp 的带调或标点转换约定。只返回 JSON 对象，唯一字段 transliterations 为非空字符串数组，与 tokens 等长、同序。不输出解释、Markdown 或额外字段。",
      exampleLabel: "中文 / LocalPractice · 无声调转写手写示例",
      examples: [{ input: { text: "我喝茶。", tokens: ["我", "喝", "茶", "。"] }, output: { transliterations: ["wo", "he", "cha", "。"] } }, { input: { text: "小鸟飞。", tokens: ["小鸟", "飞", "。"] }, output: { transliterations: ["xiaoniao", "fei", "。"] } }],
    };
    if (chinese && treebank === "gsdsimp") return { ...lesson, exampleLabel: "中文 / GSDSimp · 带声调拼音手写示例" };
    return { ...lesson, description: "转写：按当前语言与树库约定，把给定词元转换为另一套书写形式，不是翻译。", rules: "transliterations 与 tokens 等长、同序。声调、大小写与标点以当前树库约定为准。", instruction: "按当前语言与树库的转写约定处理 tokens。只返回唯一字段 transliterations 的 JSON 对象，数组与输入等长、同序。不输出解释或额外字段。", examples: [], exampleLabel: "此语言与树库暂无专用手写示例" };
  }
  if (challenge.task === "dependency" && english) return { ...lesson, exampleLabel: "英语 · UD 依存关系手写示例", examples: ["Birds", "Children"].map((word) => ({ input: { tokens: [{ token_id: 1, form: word }, { token_id: 2, form: "sing" }] }, output: { arcs: [{ token_id: 1, head_id: 2, deprel: "nsubj" }, { token_id: 2, head_id: 0, deprel: "root" }] } })) };
  return { ...lesson, exampleLabel: { upos: "英语格式示例 · UPOS 标签体系跨语言通用", segmentation: "中文分词手写示例 · 实际边界以当前树库为准", dependency: "德语格式示例 · UD 依存关系" }[challenge.task] };
}

async function renderTeaching(challenge) {
  const sequence = ++teachingState.sequence;
  Object.assign(teachingState, { challengeId: challenge.challenge_id, status: "loading", payload: null });
  const lesson = lessonFor(challenge);
  elements["xpos-reference"].classList.toggle("is-hidden", !lesson?.inventory?.length);
  elements["xpos-label-list"].textContent = lesson?.inventory?.join("\n") || "";
  elements["xpos-reference-help"].textContent = lesson?.inventory?.length ? `${lesson.inventory.length}种来源标签，保留完整写法。` : "";
  elements["task-guide"].classList.toggle("is-hidden", !lesson);
  elements["template-zero"].disabled = true;
  elements["template-few"].disabled = true;
  elements["teaching-retry"].classList.add("is-hidden");
  elements["published-teaching"].classList.add("is-hidden");
  for (const id of ["published-title", "published-version", "published-summary", "published-instructions"]) elements[id].textContent = "";
  elements["teaching-status"].textContent = "正在加载教学内容…";
  if (!lesson) return;
  elements["task-description"].textContent = lesson.description;
  elements["task-family-help"].textContent = "";
  elements["scoring-description"].textContent = SCORING_HELP[challenge.task] || "评分规则暂未提供。";
  elements["task-rules"].textContent = lesson.rules;
  elements["task-example-language"].textContent = lesson.exampleLabel;
  elements["task-example-input"].textContent = lesson.examples.length ? JSON.stringify(lesson.examples[0].input, null, 2) : "暂无此语言与树库的手写示例，请参照固定输出结构。";
  elements["task-example-output"].textContent = lesson.examples.length ? JSON.stringify(lesson.examples[0].output, null, 2) : "暂无示例。";
  const item = challenge.task === "dependency" ? {
    type: "object", additionalProperties: false,
    required: ["token_id", "head_id", "deprel"],
    properties: { token_id: { type: "integer", minimum: 1 }, head_id: { type: "integer", minimum: 0 }, deprel: { type: "string", minLength: 1 } },
  } : { type: "string", minLength: 1 };
  if (challenge.task === "upos") item.enum = "ADJ ADP ADV AUX CCONJ DET INTJ NOUN NUM PART PRON PROPN PUNCT SCONJ SYM VERB X".split(" ");
  elements["task-schema"].textContent = JSON.stringify({
    type: "object", additionalProperties: false, required: [lesson.field],
    properties: { [lesson.field]: { type: "array", minItems: 1, items: item } },
  }, null, 2);
  elements["task-schema-help"].textContent = "JSON Schema（JSON 结构规范）描述允许的字段与类型。上方的数量、顺序、标签及词元编号规则也必须满足；仅输出 JSON，不附解释或代码围栏。";
  try {
    const payload = await apiRequest(`/v1/challenges/${encodeURIComponent(challenge.challenge_id)}/teaching`);
    if (sequence !== teachingState.sequence || state.selectedChallengeId !== challenge.challenge_id) return;
    if (payload.challenge_id !== challenge.challenge_id || !Number.isInteger(payload.published_revision) || (payload.content !== null && (!payload.content || Object.keys(TEACHING_FIELDS).some((key) => typeof payload.content[key] !== "string")))) throw new APIRequestError(200, { code: "INVALID_RESPONSE" });
    teachingState.payload = payload;
    teachingState.status = "ready";
    if (payload.content) {
      elements["published-version"].textContent = `教师已发布 · 版本 ${payload.published_revision}`;
      elements["published-title"].textContent = payload.content.title;
      elements["published-summary"].textContent = payload.content.summary;
      elements["published-instructions"].textContent = payload.content.instructions;
      elements["published-teaching"].classList.remove("is-hidden");
    }
    elements["teaching-status"].textContent = payload.content ? `模板版本 ${payload.published_revision}` : "使用平台教学说明与示例。";
    elements["template-zero"].disabled = false;
    elements["template-few"].disabled = !payload.content && !lesson.examples.length;
    elements["teaching-retry"].textContent = "刷新已发布教学";
  } catch (error) {
    if (sequence !== teachingState.sequence || state.selectedChallengeId !== challenge.challenge_id) return;
    teachingState.status = "error";
    elements["teaching-status"].textContent = `${errorMessage(error, "读取教学内容")} 模板暂不可用。`;
    elements["teaching-retry"].textContent = "重新加载教学内容";
  } finally {
    if (sequence === teachingState.sequence) elements["teaching-retry"].classList.remove("is-hidden");
  }
}

function loadTemplate(fewShot) {
  const challenge = selectedChallenge();
  const lesson = lessonFor(challenge);
  if (!lesson || teachingState.status !== "ready" || teachingState.challengeId !== challenge.challenge_id) return;
  const published = teachingState.payload.content;
  if (fewShot && !published && !lesson.examples.length) return;
  if (elements["student-prompt"].value && !window.confirm("载入模板会替换当前任务的草稿，是否继续？")) return;
  let prompt = published ? published[fewShot ? "few_shot_prompt" : "zero_shot_prompt"] : `任务：${challenge.language} / ${challenge.treebank} / ${taskLabel(challenge.task)}。\n${lesson.instruction}`;
  if (fewShot && !published) {
    prompt += "\n\n以下公开手写例子仅说明任务与格式，不是待评测输入。请处理随后给出的实际输入：";
    for (const example of lesson.examples) prompt += `\n输入：${JSON.stringify(example.input)}\n输出：${JSON.stringify(example.output)}`;
  }
  elements["student-prompt"].value = prompt;
  elements["student-prompt"].dispatchEvent(new Event("input"));
  elements["student-prompt"].focus();
  showToast(published ? `已载入版本 ${teachingState.payload.published_revision} 的模板。` : "模板已载入。");
}
