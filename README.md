# Linguistic-AI-Infra-Dataset
本项目用于构建一个面向大模型语言学能力评测的标准数据库。当前版本以 Universal Dependencies 的 CoNLL-U 测试集为基础，将多语言、多树库的标注数据整理为统一 JSONL 格式，方便后续用于在线评测、Prompt 评测、模型对比和错误分析。

> [!WARNING]
> 当前仓库及其 UD 来源数据是公开的，不能视为严格保密的隐藏题库。它们适合开发、教学和公开基准评测；需要严格防作弊时，应使用未公开、经人工审核的独立标注集，并仅在后端保存黄金答案。

平台 MVP 的架构、安全边界和开发阶段见：

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/AGGREGATION.md`](docs/AGGREGATION.md)
- [`docs/CHALLENGES.md`](docs/CHALLENGES.md)
- [`docs/OFFLINE_RUNNER.md`](docs/OFFLINE_RUNNER.md)
- [`docs/ROADMAP.md`](docs/ROADMAP.md)
- [`docs/RESPONSE_CONTRACTS.md`](docs/RESPONSE_CONTRACTS.md)

## 本地开发

大型 JSONL 已由 Git LFS 管理。干净克隆后先获取 LFS 内容，再安装 Python
3.11 或更高版本的开发环境：

```powershell
git lfs install
git lfs pull
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
```

所有正式评分由代码完成，不使用第二个大模型充当裁判。

## 项目目标

这个仓库的目标不是简单保存原始语料，而是把不同语言、不同树库、不同标注习惯的数据整理成可复用、可追踪、可自动评分的标准数据库。

当前阶段重点服务以下需求：

- 为大模型提供统一格式的多语言语言学评测样本。
- 支持分词、词性标注、依存句法分析、转写等任务的自动评测。
- 保留样本来源、语言、树库、文件名和句子 ID，方便追踪数据来源。
- 在未来评测运行时由后端从标准样本构造不含答案的输入 DTO；当前 JSONL
  为构建和评分方便，仍在同一条记录中保存 `text` 与 `answers`。
- 为项目后续演进留下清晰的数据版本和构建逻辑。

## 建立初衷

大模型可以完成许多自然语言任务，但不同模型在跨语言、低资源语言、句法结构、词性识别和细粒度语言学判断上的能力差异并不容易直接比较。Universal Dependencies 提供了高质量的跨语言标注资源，但原始 CoNLL-U 文件更适合语言学研究和传统 NLP 工具链，不适合直接作为在线评测系统的数据接口。

因此，本项目先把 UD 测试集转换成统一的标准数据库，使后续系统可以稳定完成三件事：

- 抽取题目：只向模型提供句子文本和任务要求。
- 收集回答：让模型输出分词、词性、依存关系等预测结果。
- 自动评分：未来由后端读取服务器侧 `answers` 字段进行对比和统计。

当前公开仓库和 UD 上游都能访问这些答案，因此现阶段不能称为隐藏答案。正式服务必须增加安全输入 DTO 和私有数据存储，确保浏览器与学生提交的 Prompt 不接触 `answers`。

## 当前标准数据库

当前标准数据库位于 `Standard_Dataset/`，由 `Target_Conllus/` 中整理后的正式 UD test 文件生成。

目录结构：

```text
Standard_Dataset/
├─ standard_dataset.jsonl
├─ metadata.json
└─ by_language/
   ├─ Chinese_中文.jsonl
   ├─ English_英语.jsonl
   └─ ...
```

当前版本包含：

- 18 种目标语言。
- 135,180 个句子级样本。
- 97 个正式 UD test `.conllu` 文件来源。
- 全量合并文件：`Standard_Dataset/standard_dataset.jsonl`。
- 按语言拆分文件：`Standard_Dataset/by_language/*.jsonl`。
- 数据统计和 schema 说明：`Standard_Dataset/metadata.json`。

当前支持任务：

- `segmentation`: 分词结果，保留标点。
- `upos`: UD 通用词性标注。
- `xpos`: 语言或树库特定词性标注，仅当整句所有 token 都有 XPOS 时提供。
- `dependency`: 依存句法标注，格式为 `[token_id, token_form, head_id, head_form, deprel]`。
- `transliteration`: token 级转写，仅当整句所有 token 都有 `MISC.Translit` 时提供。

## 标准样本格式

每一行 JSONL 对应一个 UD 句子。标准样本保留来源信息和标准答案，但不保留原始 CoNLL-U 块，也不保留 lemma。

核心字段：

- `id`: 标准样本 ID。
- `language`: 语言英文名。
- `treebank`: UD treebank 名称。
- `source_file`: 来源 `.conllu` 文件名。
- `sent_id`: 原始 UD 句子 ID。
- `parallel_id`: 可选，原始数据中存在时保留。
- `text`: 句子文本。
- `sentence_translit`: 可选，句子级转写。
- `answers`: 标准答案。
- `tasks_available`: 当前样本可用于评测的任务列表。

`dependency` 中当 `head_id` 为 `0` 时，`head_form` 固定为 `ROOT`。

## 数据构建方式

转换脚本位于：

```text
scripts/build_standard_dataset.py
```

默认运行方式：

```bash
python scripts/build_standard_dataset.py
```

默认输入：

```text
Target_Conllus/
```

默认输出：

```text
Standard_Dataset/
```

`config/treebank_names.json` 固定记录当前 97 个来源文件的官方 Treebank
名称。构建脚本默认强制读取该映射，因此不依赖未跟踪的 `TreeBanks/` 也能
稳定生成 `GSDSimp`、`GUMReddit` 等名称和样本 ID。`TreeBanks/` 仅作为新增
或尚未映射来源的可选辅助目录。

当前数据尚未记录统一的上游 UD release 编号和逐 Treebank 许可证清单；下一次
数据更新或对外发布前必须补齐来源版本和许可证元数据。

构建规则：

- 一个 UD 句子转换为一个标准 JSONL 样本。
- 跳过 multiword token 行，例如 `1-2`。
- 跳过 empty node 行，例如 `3.1`。
- 保留标点作为普通 token。
- `xpos` 和 `transliteration` 采用整句可用策略。
- 依存关系同时保留 token ID、token form、head ID、head form 和 deprel。

## 当前代码模块

```text
src/linguistic_oj/evaluation.py  单样本确定性评分
src/linguistic_oj/aggregation.py 挑战级 Micro 指标汇总
src/linguistic_oj/contracts.py   评分、汇总和响应协议版本
src/linguistic_oj/model_inputs.py 不含答案的模型输入 DTO
src/linguistic_oj/providers.py   Provider 协议与确定性 Mock
src/linguistic_oj/runner.py      离线端到端评测流程
src/linguistic_oj/responses.py   严格模型输出协议与解析
src/linguistic_oj/dataset.py     流式 JSONL 读取和筛选
src/linguistic_oj/challenge.py   版本化挑战集生成
challenges/public/               可公开的挑战描述
tests/                           自动化测试
```

当前已完成标准数据构建、单样本评分、挑战级汇总、Response 协议、挑战集
选择、安全模型输入、Mock Provider 和离线 Runner。真实模型、API、数据库、
前端和排行榜仍未实现。

## 项目演进记录

当前版本已经从单纯的数据转换扩展到评测核心和挑战集基础设施。后续演进包括：

- 数据版本更新：目标语言、树库来源、样本数量变化。
- Schema 更新：新增字段、任务或 Response 格式。
- 评测服务更新：挑战级汇总、Mock/真实模型调用、API 和排行榜。
- 数据质量检查：异常样本、树库差异、语言特定处理策略。
- 发布方式更新：当前 JSONL 已使用 Git LFS，后续增加 API、数据库或私有对象存储。


## About ConLL-U (.conllu)

#### You can check this link for more details:[CoNLL-U Format](https://universaldependencies.org/format.html)
#### Labels
Sentences consist of one or more word lines, and word lines contain the following fields:

*ID*: Word index, integer starting at 1 for each new sentence; may be a range for multiword tokens; may be a decimal number for empty nodes (decimal numbers can be lower than 1 but must be greater than 0).

*FORM*: Word form or punctuation symbol.

*LEMMA*: Lemma or stem of word form.

*UPOS*: Universal part-of-speech tag.

*XPOS*: Optional language-specific (or treebank-specific) part-of-speech / morphological tag; **underscore** "__" if not available.

*FEATS*: List of morphological features from the universal feature inventory or from a defined language-specific extension; **underscore** "__" if not available.

*HEAD*: Head of the current word, which is either a value of ID or zero (0).

*DEPREL*: Universal dependency relation to the HEAD (root iff HEAD = 0) or a defined language-specific subtype of one.

*DEPS*: Enhanced dependency graph in the form of a list of head-deprel pairs.

*MISC*: Any other annotation.

The fields DEPS and MISC replace the obsolete fields PHEAD and PDEPREL of the CoNLL-X format. In addition, we have modified the usage of the ID, FORM, LEMMA, XPOS, FEATS and HEAD fields as explained below.


**The fields must additionally meet the following constraints:**

Fields must not be empty.

Fields other than FORM, LEMMA, and MISC must not contain space characters.

**Underscore** ( _ ) is used to denote unspecified values in all fields except ID. Note that no format-level distinction is made for the rare cases where the FORM or LEMMA is the literal underscore – processing in such cases is application-dependent. Further, in UD treebanks the UPOS, HEAD, and DEPREL columns are not allowed to be left unspecified except in multiword tokens, where all must be unspecified, and empty nodes, where UPOS is optional and HEAD and DEPREL must be unspecified. The enhanced DEPS annotation is optional in UD treebanks, but if it is provided, it must be provided for all sentences in the treebank.

#### Remember
Different languages chose different tags to show its special semantic relations.
