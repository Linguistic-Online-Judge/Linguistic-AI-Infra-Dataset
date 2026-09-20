# Qwen性能基线与受控并发实验

课堂规模按约30人准备。本阶段先测直接模型调用的性能，再决定生产调度与课堂负载
测试方案。入口为`python -m linguistic_oj.qwen_performance`，不会提交到应用、写入
学生成绩、修改评测合同、重启模型或调整vLLM参数。

## 当前进度

原协议前缀缓存开关对照已完成76次请求并恢复服务，见
[前缀缓存实测](PREFIX_CACHE_STUDY.md)。单并发小样本未观察到提速，德语输出有变化，
当前保持原线上配置。

2026-09-20的输入准备、分词预检CPU对照和前缀缓存源码调查见
[输入处理实测](INPUT_PROCESSING_RESULTS.md)。这是零模型请求的独立测量，
不能将准备阶段的提速倍数当作模型或课堂吞吐提升。

**后续更新**：学校连接已恢复，先完成了单并发基线，随后在用户授权的0号显卡
维护窗口完成1／2／4对照。真实结果、输出重复性变化和服务恢复证据见
[0号显卡并发实测](QWEN_CONCURRENCY_RESULTS.md)。以下连接关闭记录保留为历史经过。

- 已实现固定样本、逐样本耗时、输入/输出长度、评分耗时、显卡采样及报告对比。
- 已使用本机测试HTTP服务验证两个真实客户端的并行请求、统计、停止及异常保护。
- 本机完整回归648项Python通过、33项环境相关检查跳过；GitHub上的Linux结果
  以对应提交的实际运行记录为准。本机测试服务不代表9B模型速度。
- 2026-09-14尝试学校连接：`75`返回`Connection closed by UNKNOWN port 65535`，
  直接连接`jump`返回`Connection closed by 10.35.10.163 port 22`。
  新的学校性能基线尚未采集，也没有向学校模型发送本轮测试请求。
  连接记录保留在忽略目录`runtime/qwen-performance-connection-20260914.json`。

## 1. 实验安排

首轮选择两种已有任务，使用固定的独立性能提示词：

| 类型 | 来源合同 | 性能提示词 |
| --- | --- | --- |
| 英语词性标注 | `config/evaluation_contracts/v1/en-childes-upos-v1.json` | `prompts/performance/upos-v1.txt` |
| 德语依存分析 | `config/evaluation_contracts/v1/de-hdt-dependency-v1.json` | `prompts/performance/dependency-v1.txt` |

每种任务取其冻结清单顺序中的前6个样本，重复3轮；预热1次并单独计时。
每次实验明确为19次模型调用尝试，两种任务的单并发基线共38次，不是全量50样本
评测，也不是30名学生的课堂成绩。扩大样本前先看输入长度分布，不将6个样本视为
全任务性能的充分代表。程序默认重复1轮，以下命令显式指定3轮。

分别比较客户端并发1、2、4。每次只运行一个配置，报告保存各自的模型启动证据
摘要和声明的服务容量。只有对应服务容量足够，程序才允许执行该客户端并发数。
当前学校已核验配置为`max_num_seqs=1`，不能直接运行2或4并发。

独立测试服务可通过`--model-port 8001`选择端口，仍只连接本机`127.0.0.1`，
默认端口保持8000。`--gpu-index`用于选择显卡采样的物理编号，应与模型实际使用的
显卡对应；它不会替你分配显卡或改变模型进程的设备选择。

## 2. 先准备实际运行环境

在学校实际模型所在机器使用包含本提交的独立应用环境和现有分词器快照。
源码目录还需包含合同、公开描述及本轮性能提示词；模型环境不因安装工具而升级。
通过现有运维检查确认当前执行任务情况，并协调独占的模型测试窗口。

若使用现有隔离验收打包工具，需显式包含性能提示词及任务配置：

```powershell
.\.venv\Scripts\python.exe scripts/build_acceptance_bundle.py --output runtime/qwen-performance-source-20260914.tar.gz --include-catalog --registry config/challenge_contract_registry_xpos_v1.json --include-performance
```

输出必须不存在且父目录已存在。此命令只生成带文件摘要的源码包，不传输数据、
凭据或状态，也不会部署到学校。

准备两个相互独立的私有本地目录：长期保留的实验状态目录，以及每轮报告的父目录。
Linux目录由实际执行账号拥有，权限700；真实路径不写入公开配置。输出目录必须全新，
工具拒绝覆盖。若报告位于Git检出内部，只允许放在`runtime/`下。

以下环境变量由部署者设置为核对过的实际绝对路径：

```text
PYTHON       安装了本提交及分词器依赖的应用解释器
SOURCE       本提交源码/配置目录
PRIVATE      当前任务私有清单目录
EN_DATASET   与英语任务摘要匹配的数据文件
MODEL        固定Qwen3.5-9B分词器快照目录
EVIDENCE     实际模型服务对应的启动证据文件
PERF_STATE   长期保留的私有实验状态目录
PERF_REPORTS 已存在的私有报告父目录
```

英语单并发基线命令（Linux）：

```bash
"$PYTHON" -m linguistic_oj.qwen_performance \
  --contract "$SOURCE/config/evaluation_contracts/v1/en-childes-upos-v1.json" \
  --public-challenge "$SOURCE/challenges/public/en-childes-upos-v1.json" \
  --private-challenge "$PRIVATE/en-childes-upos-v1.json" \
  --dataset "$EN_DATASET" \
  --prompt-file "$SOURCE/prompts/performance/upos-v1.txt" \
  --tokenizer-snapshot "$MODEL" --launch-evidence "$EVIDENCE" \
  --state-dir "$PERF_STATE" --output "$PERF_REPORTS/en-upos-c1-run1" \
  --sample-limit 6 --repetitions 3 --warmup-requests 1 --concurrency 1
```

**不加`--run-real-qwen`时只做本地配置、数据、分词器与预算检查并打印计划**，不请求
模型、不创建实验状态或报告目录。首次实际运行在同一命令后加
`--run-real-qwen --initialize-state`；初始化只接受空状态目录。
之后运行只加`--run-real-qwen`，复用同一个状态目录并使用全新的报告目录名。

德语依存实验替换合同、公开/私有描述、数据文件、提示词以及输出目录，其他控制参数
保持一致。它是独立任务组，不能与英语报告直接计算并发提速比。

## 3. 如何进行2／4并发实验

模型并发数也参与现有生产运行校验。需要在已协调的窗口暂停既有评测，使用实际
对应的模型测试配置，并保存启动证据；不能只修改证据JSON或旧评测合同。
本工具不执行上述服务变更。测试结束后恢复既有服务配置并重新核验。

保持模型版本、数据、提示词、生成参数、上下文、硬件和其他启动参数相同，再分别
运行`--concurrency 2`及`--concurrency 4`。程序独立核对模型别名、分词器文件摘要、
启动证据及容量，不改写来源合同，也不放宽生产执行器的并发校验。

启动证据是部署方记录，不是正在运行权重或全部服务参数的密码学证明。实验时还应
保留脱敏后的实际启动参数，尤其是精度、量化、前缀缓存和批处理设置，并记录是否有
其他显卡负载。工具不会猜测这些未知值。

后续30人／50秒目标使用完整工作量的预算筛查，支持8／16／32并发及
`--measurement-budget-seconds`，验收口径见`CLASSROOM_CAPACITY_TARGET.md`。
预算超时的部分批次不能使用完整批次对比命令来宣称提速或课堂达标。

启动测试模型时还需保留经过核验的模型运行环境，包括构建工具所在的PATH；
直接指定虚拟环境解释器并不等于该环境的`bin`目录已加入PATH。应在进入维护窗口前
检查`ninja`等实际需要的工具。恢复旧应用时应等待监听端口变得可重新绑定，不能把
进程退出后的短暂端口保留直接判成恢复失败。

## 4. 报告中各项时间的含义

| 字段 | 含义 |
| --- | --- |
| `preparation.artifact_load_seconds` | 读取、核验任务描述与数据的时间 |
| `prepare_samples_seconds` | 准备安全模型输入的时间 |
| `token_preflight_and_count_seconds` | 固定分词器预算检查及输入计数时间 |
| `rows[].input_tokens_local` | 对实际请求消息用固定分词器计算的输入量 |
| `rows[].input_tokens_reported` | 模型服务返回的输入计数，缺失时为null |
| `rows[].output_tokens` | 实际生成量，不是生成上限；缺失时为null |
| `rows[].client_queue_wait_seconds` | 本轮测试中等待客户端执行槽位的时间 |
| `rows[].request_seconds` | 完整请求的墙钟时间，包括服务内部等待与生成 |
| `rows[].scoring_seconds` | 使用原确定性评分器解析和评分的本机耗时 |
| `rows[].completion_seconds` | 从正式测量批次开始到该样本完成的总时间 |
| `measurement.batch_wall_seconds` | 正式测量整批完成所花时间，预热另列 |
| `output_tokens_per_batch_second` | 整批有效输出量除以整批时间，不是纯解码速率 |

报告记录平均值、中位数、95%分位数与最大值；分位数采用最近秩，小样本时容易等于
最大值。并发请求的耗时有重叠，不能用整批时间减去请求时间总和推算“平台开销”。

显卡采样通过`nvidia-smi`约每秒进行一次，覆盖预热和正式实验，记录设备、驱动、
显存和利用率；这是整张显卡的观察，不能逐样本归因，也不证明没有其他使用者。
采样失败明确标记不可用。当前非流式调用不测首字延迟和服务内部纯解码时间。

程序没有经过应用数据库、队列或网页，因此`application_queue_wait_seconds`、
`database_write_seconds`为null，`is_classroom_load_test`为false。这些维度需要后续
端到端测试，不能把本轮客户端排队时间称为学生实际排队时间。

## 5. 对比与故障处理

```bash
"$PYTHON" -m linguistic_oj.qwen_performance compare \
  "$PERF_REPORTS/en-upos-c1-run1/report.json" \
  "$PERF_REPORTS/en-upos-c2-run1/report.json" \
  "$PERF_REPORTS/en-upos-c4-run1/report.json"
```

对比要求输入请求集合、来源合同、模型/分词器身份、生成参数、样本数、重复轮数
和预热数一致，且每份报告完整。输出吞吐比、延迟和格式问题，并对照输出文本摘要
与逐样本评分统计，避免把截断或答错导致的变快当成无代价提升。
输出摘要不同不必然代表答案不同，例如空格也会改变摘要。

每个客户端槽位使用独立提供器，最多有配置数量的已分配请求。发生异常后停止分配
新样本，等待已分配请求收尾；缺失统计不会填成0。正常停止信号同样停止分配。
预热失败时不继续正式实验。

整轮实验发送请求前会持久写入一个操作记录，只有全部请求确认终止才清除。
强制退出或未确认失联会阻止此状态目录下的后续实验。恢复沿用
`qwen_executor status/recover`，见`PRODUCTION_EXECUTOR.md`，但必须确认的是
**整轮实验的所有请求均已终止**，不是其中一个请求。恢复不会重发任何测试请求。

`status: running`的报告只是最后一次检查点，不能据此认为尚未发送请求。输出文件
已存在时拒绝自动续跑或覆盖。此整轮保护用于离线实验，不代表生产执行器已实现
多请求调度、逐请求恢复或30人课堂并发。
