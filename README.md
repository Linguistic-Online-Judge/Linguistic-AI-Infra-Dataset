# Linguistic-AI-Infra-Dataset

这是 Linguistic Online Judge 的数据与评测核心。

项目把 Universal Dependencies（UD）的多语言标注数据整理成统一 JSONL，
并提供从“构造安全题目”到“解析模型回答、自动评分、汇总结果”的完整离线流程。

一句话说明当前进度：**现在可以在本地用 Mock Provider 跑完一次完整评测，
但真实模型、后端 API、数据库、前端和排行榜还没有实现。**

## 当前能做什么

| 能力 | 状态 |
| --- | --- |
| 18 种语言的标准 JSONL 数据集 | 已完成 |
| 分词、UPOS、XPOS、依存分析、转写评分 | 已完成 |
| 严格 JSON 回答格式与错误分类 | 已完成 |
| 可复现、带版本的挑战集 | 已完成 |
| 不含标准答案的安全模型输入 | 已完成 |
| Mock Provider 离线端到端评测 | 已完成 |
| 真实自托管模型 | 未开始 |
| API、数据库、任务队列、前端、排行榜 | 未开始 |

当前评测流程：

```text
标准数据集
   ↓
版本化挑战集
   ↓
不含答案的模型输入
   ↓
模型 Provider
   ↓
严格解析模型 JSON 回答
   ↓
代码评分并汇总最终结果
```

所有正式分数都由确定性代码计算，**不使用第二个大模型充当裁判**。

> [!WARNING]
> 当前仓库和 UD 来源数据都是公开的，不能当作严格保密的隐藏题库。
> 它们适合开发、教学和公开基准评测。需要防作弊时，必须使用未公开、
> 经人工审核的数据，并把标准答案只保存在后端。

## 快速开始

需要预先安装 Git、Git LFS，以及 Python 3.11 或更高版本。以下命令均在
仓库根目录运行。大型 JSONL 使用 Git LFS 管理。

```powershell
git lfs install
git lfs pull
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
```

测试通过说明数据读取、回答解析、单样本评分、挑战汇总和离线 Runner
可以协同工作。

## 运行一次 Mock 评测

Mock Provider 不调用网络，也不代表真实模型能力。它只用来验证完整评测流程。

### 1. 生成本地私有 manifest

```powershell
.\.venv\Scripts\python.exe -m linguistic_oj.challenge `
  --dataset "Standard_Dataset\by_language\Chinese_中文.jsonl" `
  --language Chinese `
  --treebank GSDSimp `
  --task segmentation `
  --count 50 `
  --seed 2026 `
  --version v2
```

公开挑战描述保存在 `challenges/public/`。包含样本 ID 和可信分母的私有
manifest 保存在 `runtime/private/`，该目录不会提交到 Git。

这里的“私有”只表示文件保存在本地运行环境中，未来部署后仅由服务器持有，
不会直接返回给学生；它不代表当前样本选择无法推导。因为 UD 数据、筛选条件、
count 和 seed 都是公开的，当前开发挑战仍然可以被重新构建。

### 2. 准备 Prompt 文件

把 Prompt 保存为 UTF-8 文件，例如：

```text
runtime/private/prompts/segmentation.txt
```

可以先用一个非空示例 Prompt：

```powershell
New-Item -ItemType Directory -Force -Path "runtime\private\prompts"
"Segment the input text and return only the required JSON." |
  Set-Content -Encoding utf8 "runtime\private\prompts\segmentation.txt"
```

Runner 使用文件读取 Prompt，避免把学生 Prompt 直接放进命令历史和进程参数。

### 3. 运行挑战

```powershell
.\.venv\Scripts\python.exe -m linguistic_oj.runner `
  --public "challenges\public\zh-gsdsimp-segmentation-v2.json" `
  --private "runtime\private\challenges\zh-gsdsimp-segmentation-v2.json" `
  --dataset "Standard_Dataset\by_language\Chinese_中文.jsonl" `
  --provider mock `
  --prompt-file "runtime\private\prompts\segmentation.txt"
```

当前 50 样本 Mock 回归结果摘要如下。Runner 实际输出为 JSON：

```json
{
  "samples_total": 50,
  "samples_valid": 50,
  "samples_invalid": 0,
  "metrics": {
    "micro_f1": 0.3949447077409162
  }
}
```

这个分数只是 Mock 固定策略的结果。它的用途是确认重复运行得到完全相同的输出。

## 支持的任务

| 任务 | 含义 | 主要指标 |
| --- | --- | --- |
| `segmentation` | 把连续文本切分成 token | 基于精确 token span 的 Micro-F1 |
| `upos` | 预测 UD 通用词性 | Micro Accuracy |
| `xpos` | 预测 Treebank 自定义词性 | Micro Accuracy |
| `dependency` | 预测词之间的依存关系 | LAS，辅助报告 UAS |
| `transliteration` | 预测每个 token 的转写 | Token Accuracy |

模型回答必须是约定的 JSON。格式错误会得到明确错误码，并以零正确项计入
可信分母。网络故障、Provider 故障或平台配置错误会中止整次运行，不会错误地
算成学生零分。

## 数据集

标准数据位于 `Standard_Dataset/`：

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

- 18 种语言。
- 135,180 个句子级样本。
- 97 个 UD test `.conllu` 来源文件。
- 一个全量 JSONL 和按语言拆分的 JSONL。

每行 JSONL 是一个句子，主要字段包括：

- `id`：稳定、可追踪的样本 ID。
- `language`：语言。
- `treebank`：UD Treebank。
- `text`：原始句子文本。
- `answers`：服务器评分使用的标准答案。
- `tasks_available`：该样本可以用于哪些任务。
- `source_file`、`sent_id`：来源信息。

`answers` 为数据构建和服务器评分保留，不能直接发送给模型、浏览器或学生。
Runner 会逐字段重新构造安全输入，而不是先序列化整条样本再删除答案。

## 数据构建

转换脚本：

```powershell
.\.venv\Scripts\python.exe scripts\build_standard_dataset.py
```

默认从 `Target_Conllus/` 读取 `.conllu`，输出到 `Standard_Dataset/`。

主要规则：

- 一个 UD 句子转换为一条 JSONL。
- 跳过 multiword token 行和 empty node 行。
- 标点按普通 token 保存。
- `xpos` 和 `transliteration` 只有在整句数据完整时才启用。
- `config/treebank_names.json` 固定 97 个来源文件的 Treebank 名称。

当前数据还没有统一记录 UD release 编号和每个 Treebank 的许可证。
下一次数据更新或正式对外发布前必须补齐。

## 代码结构

```text
src/linguistic_oj/dataset.py      流式读取和筛选 JSONL
src/linguistic_oj/challenge.py    创建和验证版本化挑战
src/linguistic_oj/contracts.py    统一保存指标和协议版本
src/linguistic_oj/model_inputs.py 构造不含答案的模型输入
src/linguistic_oj/providers.py    Provider 协议和 Mock Provider
src/linguistic_oj/responses.py    严格解析模型 JSON 回答
src/linguistic_oj/evaluation.py   单样本确定性评分
src/linguistic_oj/aggregation.py  挑战级指标汇总
src/linguistic_oj/runner.py       离线端到端评测流程
tests/                            自动化测试
```

## 下一步

下一阶段先接通一个固定的真实模型，不急着开发网页。

1. 获取学校服务器的 GPU 型号、显存、CPU、内存、磁盘、系统和 Docker 权限。
2. 选择 2 至 3 个候选开源模型。
3. 定义统一的测试 Prompt Envelope，也就是平台固定的任务指令、输入位置和
   输出约束；所有候选模型使用相同生成参数进行比较。
4. 实现一个本地或自托管 Model Provider，保持现有评分代码不变。
5. 在服务器比较速度、显存占用、JSON 有效率、任务得分和重复运行稳定性。
6. 根据测试结果选择并固定最终模型、模型版本、推理运行时和生成参数。
7. 模型确定后，再设计超时、并发、数据库、后台任务队列、API 和前端。

这样安排的原因是：模型速度和显存需求会直接决定任务并发、超时、队列和
服务器部署方式。先做 API 或前端，之后很可能因为模型限制而返工。

## 进一步阅读

- [系统架构与安全边界](docs/ARCHITECTURE.md)
- [挑战集与公开/私有文件](docs/CHALLENGES.md)
- [模型回答 JSON 协议](docs/RESPONSE_CONTRACTS.md)
- [挑战级汇总规则](docs/AGGREGATION.md)
- [离线 Runner](docs/OFFLINE_RUNNER.md)
- [MVP 路线图](docs/ROADMAP.md)
- [CoNLL-U 官方格式说明](https://universaldependencies.org/format.html)
