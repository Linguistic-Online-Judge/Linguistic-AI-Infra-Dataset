# Linguistic Online Judge

面向语言学教学与提示词练习的多语言在线评测平台。学生选择任务、编写提示词，
由固定的 Qwen3.5-9B 执行评测，再查看确定性代码计算的分数、历史和对应排行榜。

**正式服务的目标是校内外均可访问。当前已部署的是学校服务器上的开发实例，
仍通过SSH转发访问；正式公网地址、HTTPS和真实邮件尚未完成部署。**
公开GitHub仓库用于代码协作，不等于公开网站已经上线。

本说明对应截至2026-09-24的开发版本：**18种语言、74项目录、70个可执行配置**。
仓库同时保留完整原生工作台和独立的Next.js只读题目目录，运行入口见下文。
自动检查以对应提交的实际结果为准；推送代码不代表学校服务或公网部署已更新。

## 当前能做什么

| 能力 | 状态 |
| --- | --- |
| 任务目录、练习页、结果页 | 已接入实际业务，支持搜索、筛选、模板与提交跟踪 |
| 账户 | 邮箱注册、验证、登录、退出、重置；开发环境使用测试收件箱 |
| 本人历史与提示词复用 | 支持原提示词读取、结果刷新及继续修改，其他用户不可读取 |
| 排行榜 | 按相同任务、模型和评测配置分榜，提示词不公开 |
| 教学管理 | 已有任务的草稿、发布和新提交开关，含版本冲突与权限检查 |
| 评测后端 | FastAPI、PostgreSQL、Redis任务队列及固定Qwen Worker |
| 本地开发 | SQLite、内存队列、五项手写模拟任务，可独立于学校模型运行 |
| 数据保护 | 当前请求模式备份、隔离恢复和最新本机副本已验证 |
| 独立公开目录 | `web/`保留Next.js目录/详情浏览组件，与原生工作台分别运行和验证 |
| 自动检查 | Python、页面契约、浏览器、数据库及构件检查已配置，运行证据见文档 |
| 公网发布 | 已确认目标；域名/入口、HTTPS、真实邮件、来源开放条件和运行容量待落实 |

### 当前可执行任务覆盖

| 类型 | 覆盖 | 配置数 |
| --- | --- | ---: |
| 分词 | 18种语言 | 18 |
| 通用词性 UPOS | 18种语言 | 18 |
| 依存句法 | 18种语言 | 18 |
| 专用词性 XPOS | 15种语言 | 15 |
| 转写 | 中文 | 1 |
| **合计** | | **70** |

每项配置使用50个固定样本。目录另有4项历史描述没有执行配置。
丹麦语、匈牙利语当前缺少完整XPOS数据；希伯来语现有XPOS逐词重复UPOS，未另建重复任务。
具体树库与候选扩充见[任务覆盖](docs/TASK_COVERAGE_PLAN.md)、
[基础任务扩充](docs/FOUNDATION_TASK_EXPANSION.md)和[专用词性扩充](docs/XPOS_TASK_EXPANSION.md)。

已有真实执行验收包括18语言UPOS、34项新增基础任务、14项新增XPOS。
**执行成功不等于模型质量达标。** 例如本轮XPOS少样本模板的格式有效数仅为5—26/50，
仍需改进输出稳定性和教学内容。真实数据与模拟数据不混排，历史实验不等于正式发布认证。

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

## 代码在哪里运行

| 位置 | 用途 |
| --- | --- |
| 本机开发工程 | 编写和测试源码、配置、构建工具、文档；不承担正式网站服务 |
| GitHub仓库 | 保存经过提交的版本、协作与自动检查；不直接运行Python后端或模型 |
| 学校服务器 | 运行已部署代码、Qwen模型、PostgreSQL和任务队列，保存业务数据 |

当前前端是原生HTML/CSS/JavaScript，与FastAPI同源提供。正式访问架构为：

```text
校内外用户浏览器 → 公网域名与HTTPS入口 → 学校应用服务
                                      ├─ 网页与账户/评测接口
                                      ├─ 数据库与任务队列
                                      └─ 固定Qwen3.5-9B执行器
```

正式访问者不应需要安装项目、模型或建立SSH转发。当前8090仅是开发通道，
`127.0.0.1`不是可分享给公众的网站地址。公网准备与学校需要提供的信息见
[公网部署计划](docs/PUBLIC_DEPLOYMENT.md)。

## 开发者快速开始

**18语言真实开发站：**双击 `scripts/start_qwen_dev.cmd` 建立学校应用连接，保持窗口
开启，然后访问 `http://127.0.0.1:8090/`。这个入口实际调用学校 Qwen3.5-9B，并使用
独立开发数据库。需要已有学校SSH权限及可用网络连接，同学应使用自己的SSH配置。
详情见 [18语言真实开发站](docs/QWEN_DEVELOPMENT.md)。

学校开发站现已使用 **32个全局样本请求槽位、4份活跃作业**，模型与应用均由用户级
服务监督。五类任务共250次真实调用及正式端口结果重读已通过；原74条成绩保留，
验收后共79条。部署、备份与运行边界见[部署结果](docs/REQUEST_WORKBENCH_DEPLOYMENT.md)。

原有 `http://127.0.0.1:8080/` 仍是本地模拟环境，用于快速界面与业务回归。

### 本地网站

首次获取本轮整合版本：

```text
git clone --branch integration/public-release-prep-20260913 https://github.com/Linguistic-Online-Judge/Linguistic-AI-Infra-Dataset.git
```

进入克隆得到的项目目录后执行下面的命令。当前维护者的主目录是
`D:\MyWebsite\Online Linguistic Judge`，其他成员可以使用自己的目录。
需要 Python 3.11+；已有 `.venv` 时保留它，跳过创建步骤：

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e '.[api,dev]'
& .\scripts\start_local_dev.ps1
```

打开 **http://127.0.0.1:8080**，并保持启动终端运行。`localhost` 会发送不同的 Host，
此启动器不接受它。端口被占用时会报错，不会换端口或终止其他进程。

若 PowerShell 执行策略阻止脚本，直接运行下面的模块，仍使用固定的 8080 端口；
不需要绕过执行策略或修改全局策略：

```powershell
./.venv/Scripts/python.exe -m linguistic_oj.local_dev --root .
```

本地种子账户为 `alice@example.test`（`LocalAlice`）、`bob@example.test`（`LocalBob`）
和 `admin@example.test`（`LocalAdmin`），共同**初始密码**为
`Local-only-passphrase-2026!`，仅限隔离的本机模拟环境。重置后请使用自己设置的密码；
重启不会覆盖它，本地测试面板显示的初始密码可能已过时。

管理员登录后打开“教学管理”或 **http://127.0.0.1:8080/#admin**，可保存、检查、发布
已有任务的教学内容，以及暂停/恢复新提交。发布不等于开放评测；已有队列、历史与结果
保留。完整权限边界与操作步骤见[教学管理](docs/ADMINISTRATION.md)。

本地服务使用 SQLite、内存队列和 Mock Worker，不需要 PostgreSQL/Redis 服务器、
GPU 或 SMTP（Simple Mail Transfer Protocol，简单邮件传输协议）服务。注册验证和
密码重置邮件在页脚“开发者工具”中查看，最多保留本次进程的100封邮件；重启清空收件箱，
不清空密码。账户与评测记录保存在忽略的 `runtime/local-development/`，不自动清理旧文件。

这里是五种任务、每个挑战两个手写样本的模拟流程，**不是实际 18 语言数据集或 Qwen
能力基准**。本地每用户每挑战每 24 小时 1000 次是技术测试预算，真实合同原有的
5 次/24 小时限制不变，不代表累计终身限额或无限提交。

Node 22+ 仅用于 JavaScript 检查和浏览器自动化，已安装的 Edge 仅用于浏览器自动化，
不是启动应用的依赖。完整命令、重启边界、隔离浏览器测试及真实 Qwen 后端验收范围见
[本地开发](docs/LOCAL_DEVELOPMENT.md)；安全与生产配置见
[认证说明](docs/AUTHENTICATION.md)。

### 数据与开发检查

实际大型 JSONL 数据使用 Git LFS 管理；数据工作还需要 Git 和 Git LFS：

```powershell
git lfs install
git lfs pull
./.venv/Scripts/python.exe -m pytest
./.venv/Scripts/ruff.exe check .
```

测试结论以当前实际输出和跳过项为准，不沿用旧测试总数。

### 已验证的整合基线

2026-09-13，提交`a972601`在GitHub的Linux环境完成：589项Python测试全部通过、
必要数据库测试无跳过、8项前端契约检查、32组学生浏览器流程、16组管理员流程，
以及实际安装构件后的启动和模拟评测验收。
[查看该次自动检查](https://github.com/Linguistic-Online-Judge/Linguistic-AI-Infra-Dataset/actions/runs/34760180305)。
这证明该代码基线可在独立环境运行，不代表公网已经上线或模型输出质量达标。

## 运行公开题目网页

这是`main`原有的独立只读目录组件；学校当前使用的完整工作台在
`src/linguistic_oj/web/`。请将`web/.env.local`中的`LINGUISTIC_OJ_API_URL`指向应用接口
（本机模拟站为`http://127.0.0.1:8080`），不要指向学校8000模型端口。

网页需要 Node.js 24.15 或更高版本。先启动配置好题目登记表的 API，然后在
`web/` 目录安装依赖并启动开发服务器：

```powershell
Set-Location web
npm ci
Copy-Item .env.example .env.local
npm run dev
```

浏览器访问 `http://localhost:3000/challenges`。`LINGUISTIC_OJ_API_URL`
只供 Next.js 服务端读取，不会作为公开环境变量发送到浏览器。

提交前运行完整前端检查：

```powershell
npm run lint
npm run typecheck
npm test
npm run build
```

网页的视觉方向、信息架构、文案规则和无障碍要求见
[`docs/WEB_DESIGN.md`](docs/WEB_DESIGN.md)。

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
- 所有任务都要求每个整数 ID token 具有可见 `FORM`；各任务只有在其整句 gold
  字段完整且结构有效时才启用。
- `config/treebank_names.json` 固定 97 个来源文件的 Treebank 名称。

V1 public catalog 使用的 22 个 Treebank 已在 `config/v1_source_provenance.json`
中固定到 UD 2.18，并记录源文件、LICENSE 和 README 哈希。该记录不等于法律批准；
underlying text、NC/ND、academic-use、隐私和逐项 attribution 仍须在对外发布前审查。
其余未进入 V1 catalog 的 raw Treebank 尚未纳入该 manifest。

## 代码结构

```text
src/linguistic_oj/dataset.py      流式读取和筛选 JSONL
src/linguistic_oj/challenge.py    创建和验证版本化挑战
src/linguistic_oj/challenge_registry.py 验证公开目录与评测合同注册表
src/linguistic_oj/contracts.py    统一保存指标和协议版本
src/linguistic_oj/model_inputs.py 构造不含答案的模型输入
src/linguistic_oj/providers.py    Provider 协议、Mock 和 OpenAI-compatible Provider
src/linguistic_oj/responses.py    严格解析模型 JSON 回答
src/linguistic_oj/evaluation.py   单样本确定性评分
src/linguistic_oj/aggregation.py  挑战级指标汇总
src/linguistic_oj/runner.py       离线端到端评测流程
src/linguistic_oj/mvp_contract.py 读取和验证冻结评测合同
src/linguistic_oj/challenge_registry.py 启动前验证题目登记表与评测合同
src/linguistic_oj/submission_store.py SQLite 提交、outbox、结果和排行榜
src/linguistic_oj/submission_jobs.py 进程内队列、outbox dispatcher 和 Mock Worker
src/linguistic_oj/redis_job_queue.py Redis Streams 队列和 visibility recovery
src/linguistic_oj/qwen_runtime.py   固定 tokenizer 预检和 Qwen 运行时身份核验
src/linguistic_oj/qwen_api.py      多题目 API、数据库与独立 Redis 队列装配
src/linguistic_oj/qwen_worker.py   按登记表题目标识启动单合同 Qwen Worker
src/linguistic_oj/api.py          FastAPI 提交、状态、结果和排行榜接口
src/linguistic_oj/auth*.py        账户、会话、邮件与同源权限检查
src/linguistic_oj/admin*.py       教学内容、修订审计与提交开关
src/linguistic_oj/postgres_submission_store.py PostgreSQL 持久化与本人数据访问
src/linguistic_oj/qwen_development.py 学校开发实例组合
src/linguistic_oj/qwen_api.py     生产应用入口与配置检查
src/linguistic_oj/web/            同源响应式学生端 HTML、CSS 和 JavaScript
scripts/build_v1_challenge_catalog.py 生成 18 语言代表性 draft challenge
scripts/build_foundation_catalog.py  分词/依存基础任务扩充
scripts/build_xpos_catalog.py        专用词性任务扩充
scripts/qwen_dev_ops.py              当前开发实例备份、恢复与诊断
web/                              Next.js 公开题目目录与详情网页
tests/                            自动化测试
.github/workflows/                 GitHub 自动检查配置
```

## 当前下一步

1. 固定通过完整检查的发布版本，保留独立的服务器部署与回滚记录。
2. 落实学校公网域名、HTTPS入口、应用转发方式和真实邮件通道。
3. 为正式服务准备生产账户配置与数据，开发共享账号和测试收件箱不进入公网实例。
4. 验证可公开开放的任务来源、运行容量、长任务排队、服务恢复及发布回退。
5. 完成校外网络的实际访问验收，再公布正式网址。

详细交付标准见[工程化计划](docs/SYSTEM_ENGINEERING_PLAN.md)和
[公网部署计划](docs/PUBLIC_DEPLOYMENT.md)。模型实验、各次部署和旧阶段记录见对应文档，
不以历史通过次数代替当前版本的发布验证。

## 进一步阅读

- [公网访问与发布计划](docs/PUBLIC_DEPLOYMENT.md)
- [当前18语言开发站](docs/QWEN_DEVELOPMENT.md)
- [当前实例备份、恢复与诊断](docs/QWEN_DEVELOPMENT_OPERATIONS.md)
- [专用词性覆盖与实测质量](docs/XPOS_TASK_EXPANSION.md)
- [整体工程化计划](docs/SYSTEM_ENGINEERING_PLAN.md)
- [系统架构与安全边界](docs/ARCHITECTURE.md)
- [V1 API 合同](docs/API_CONTRACT.md)
- [V1 学生端](docs/FRONTEND.md)
- [挑战集与公开/私有文件](docs/CHALLENGES.md)
- [V1 来源与权利审查状态](docs/SOURCE_PROVENANCE.md)
- [模型回答 JSON 协议](docs/RESPONSE_CONTRACTS.md)
- [挑战级汇总规则](docs/AGGREGATION.md)
- [离线 Runner](docs/OFFLINE_RUNNER.md)
- [Qwen3.5 模型部署与首个基准](docs/MODEL_RUNTIME.md)
- [Qwen3.5 中英文 UPOS 基准](docs/UPOS_BENCHMARK.md)
- [UPOS Prompt 评测校准](docs/PROMPT_CALIBRATION.md)
- [私有合成 UPOS 校准协议与结果](docs/PRIVATE_UPOS_CALIBRATION_PROTOCOL.md)
- [ADR 0001：MVP 生产评测合同](docs/adr/0001-mvp-production-evaluation-contract.md)
- [MVP 路线图](docs/ROADMAP.md)
- [CoNLL-U 官方格式说明](https://universaldependencies.org/format.html)
