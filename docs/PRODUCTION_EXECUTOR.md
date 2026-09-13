# 生产串行执行器与故障恢复

入口：`python -m linguistic_oj.qwen_executor`。
这是独立的生产执行进程：轮流检查已配置的任务队列，每次执行一份完整评测。
沿用原有数据库认领、期限、评分和失败处理，不创建账号、不发送测试邮件、不开放draft。

## 配置与启动

配置起点是`config/executor.example.json`，或者部署准备工具生成的
`executor.json.template`。实际配置放在私有目录，Linux建议权限600。
执行配置允许最多1 MiB以容纳多任务路径；认证和连接私有文件保留原有较小上限。

| 字段 | 含义 |
| --- | --- |
| `version` | 固定`qwen-serial-executor-v1` |
| `root`、`registry` | 明确的源码发布绝对目录及目录内的注册表相对路径 |
| `state_dir` | 本实例长期保留的私有状态目录，不能位于临时目录或随发布替换 |
| `postgres_database_url_file`、`redis_url_file` | 与生产应用对应的私有连接地址文件 |
| `namespace` | 与生产应用一致的队列命名空间；默认模板为`loj-public` |
| `vllm_base_url` | 本组合固定使用同机`http://127.0.0.1:8000/v1` |
| `tokenizer_snapshot`、`launch_evidence` | 固定模型的本机分词器快照和启动证据绝对路径 |
| `artifacts` | 任务标识到公开描述、私有清单、数据文件绝对路径的映射 |

`artifacts`中的每项形如：

```json
{
  "实际任务标识": {
    "public_challenge": "/实际发布目录/challenges/public/实际任务标识.json",
    "private_challenge": "/实际私有数据目录/实际任务标识.json",
    "dataset": "/实际数据目录/对应语言.jsonl"
  }
}
```

这些是占位示意，须使用经核对的真实路径。选中任务必须满足现有公开激活条件，
公开描述必须与注册表一致，私有清单与数据必须通过已有完整性校验；所有任务须绑定
同一Qwen3.5-9B模型身份。模型相关环境不得设置隐式HTTP代理。

配置中的任务标识应与应用的`runtime_available_challenges`一致。
当前70份开发合同尚未满足正式激活条件，不能将其状态就地改成active来测试新入口。
自动测试使用新建的手写合成数据和测试合同，原合同及成绩保持原来的身份。

以下命令由实际服务账号执行，路径均替换为部署者配置的值：

```bash
# 离线校验配置及数据，不连接数据库、队列或模型。
python -m linguistic_oj.qwen_executor check --config /private/production/executor.json

# 由部署者预先创建一个空的、服务账号拥有的700权限本地目录，再显式初始化。
python -m linguistic_oj.qwen_executor init --config /private/production/executor.json

# 前台运行；启动时才检查数据库、队列、分词器快照、启动证据和实际模型身份。
python -m linguistic_oj.qwen_executor run --config /private/production/executor.json
```

`check`返回`model_attested: false`和`production_started: false`，不能替代启动验收。
`run --once`只轮询所有配置队列一轮，可能处理多份作业，但始终串行。
`run`不会自动初始化缺失状态，也不会在绑定不符时重新生成状态。

运行绑定摘要包括合同指纹、数据库/队列目标、连接用户名、命名空间及模型入口。
密码轮换不改变绑定；切换数据库、用户、命名空间或合同会导致绑定不符，须先完成
离线迁移安排，不能靠删除原状态目录解决。日志与状态文件不保存密码或学生提示词。

## 跨进程保护怎样工作

1. 执行进程在整个生命周期持有该状态目录的排他文件锁，第二个进程不能同时启动。
2. 每次模型请求发送前，先写入唯一操作编号、任务标识、开始时间，刷新文件到磁盘，
   原子替换`state.json`，再刷新父目录。任何写入失败都会阻止本次发送。
3. 收到完整模型响应，或者按现有提供器协议确认请求已终止时，才清除待确认记录。
   失联、未确认超时或意外异常均保留记录。
4. 某一任务出现待确认请求后，整个串行循环停止，不继续认领其他队列。
5. 进程被强制结束后，文件锁会释放，持久记录仍保留；新进程在构建执行器、连接
   队列或认领任务前检查记录并拒绝继续执行。

保护按每次模型请求写入，空队列轮询不反复写恢复文件。
请求前就发生的进程退出由已有数据库租约处理；已有运行任务租约过期后记录为失败，
不会因此自动再跑一遍。恢复工具也不修改提交状态、清空队列或重新投递原任务。

这是**同一主机、同一持久状态目录下的协作进程互斥**。它不阻止其他程序绕过执行器
直接调用模型，也不是多机分布式锁。实际接入前须协调现有开发执行循环的模型使用；
不要在另一目录再初始化一个实例，或在本模型上另开单任务`qwen_worker`并发运行。
状态应位于支持文件锁和原子替换的本地持久文件系统，并纳入对应生产实例的备份。
恢复备份或迁移主机前同样需要确认旧模型请求已经结束，不能用旧的空闲状态副本
覆盖未确认记录后直接运行。

## 状态与停止

```bash
python -m linguistic_oj.qwen_executor status --state-dir /private/production/executor-state
```

输出包含`binding_sha256`、`pending`、`last_recovery`和`process_lock_held`。
这是一份瞬时观察，不是完整健康证明：

- 锁被持有、`pending`非空，可能是正在进行正常推理。
- 进程已退出、`pending`非空，表示需要核实上次请求并执行恢复。
- 进程已退出、`pending`为空，只表示没有未确认请求，不表示服务正在运行。

正常收到SIGTERM或SIGINT停止信号后，当前已认领的整份评测继续完成，然后退出，
不再认领下一份。生产systemd模板为`loj-executor.service.template`：

- `Restart=on-failure`：一般运行异常可以重新启动，但新进程仍检查持久记录。
- `RestartPreventExitStatus=75 78`：待确认请求、重复实例及配置错误不进入自动重启循环。
- `TimeoutStopSec=infinity`：等待当前评测结束，不因通用停止时限自动强杀执行进程。
- 退出码75表示恢复阻断或状态占用，78表示配置/状态存储问题，70表示其他运行异常。

部署者先在目标Linux环境检查实际单元，再安装运行；模板生成不代表服务已安装。
当前应用`/health/ready`仍只检查其既有依赖，执行器状态尚未接入实时准入开关。
正式开放前还需验证这一联动及课堂负载，不能仅凭应用就绪或进程锁判断模型链路健康。

## 有记录的人工恢复

先停止该执行服务，核实旧模型请求确已终止。应用健康接口可访问、队列租约到期、
本地进程退出或等待了足够时间，都不能单独作为终止证明。
`operation_id`是本机恢复标识，不是模型服务返回的请求编号。

由有权限的运维人员保存私有确认文件，例如：

```json
{
  "operation_id": "从状态报告复制的32位操作编号",
  "binding_sha256": "从状态报告复制的64位绑定摘要",
  "prior_request_terminated": true,
  "confirmed_by": "实际确认人员",
  "evidence_reference": "实际终止核查记录或运维记录位置"
}
```

```bash
python -m linguistic_oj.qwen_executor recover --state-dir /private/production/executor-state --expected-operation-id 实际操作编号 --evidence-file /private/termination-confirmation.json
```

恢复命令需要独占状态锁，严格匹配操作编号、绑定和确认字段。工具记录的是运维
人员的终止确认，不能自行证明该确认真实，也不会自动停止或重启学校模型。

先持久保存`recoveries/<操作编号>.json`，再清除待确认状态。若在这两步之间退出，
只能用相同证据续作；不会覆盖先前记录。错编号、否定确认和恢复后的再次重放均被拒绝。
确认恢复后再显式启动服务。不要手动删除`state.json`、恢复记录或`.executor.lock`。

## 验证证据与边界

本轮本地完整回归：636项Python通过、33项跳过；其中新增Linux停止信号测试在
Windows上跳过，其余32项为原有依赖实际服务的测试。Linux结果以对应提交的远端
自动检查为准。

新增隔离测试覆盖：真实子进程强制退出、实际本地HTTP请求、落盘先于网络发送、
重复启动、写入失败、全队列阻断、错证据/重复恢复、审计写入后的中断恢复、配置绑定
和任务数据校验。模型服务为可控的本机测试服务，没有调用学校GPU或中断学校模型。
这证明保护路径的进程与通信行为；学校真实模型、生产服务安装、服务器重启/掉电、
容量以及外网访问仍须在对应环境验收。
