# 请求执行模式的备份与隔离恢复

## 完成范围

新增`qwen18-backup-v2`，保存请求执行模式所需的运行档案、账本和全部历史恢复审计。
`qwen_dev_ops.py backup-request`只在应用停止后执行，持有应用状态锁和请求账本锁
直至备份完成；命令本身不会停止应用、重启模型或发送推理请求。

原`backup`保留串行v1行为，并继续拒绝把请求执行模式当成串行备份。旧v1归档仍可
验证和隔离恢复。`verify-copy`和`verify-restore`现在同时识别v1/v2。

## 归档内容

每份v2备份包含：

| 文件 | 内容 |
| --- | --- |
| `database.dump` | PostgreSQL一致性快照，含账号、凭据、提交、成绩、教学管理及outbox |
| `instance.json` | 原实例标记，保留原合同绑定 |
| `source.tar.gz` | 按部署源码清单逐项验证的原发布文件 |
| `evaluation-assets.tar.gz` | 冻结样本清单、语料、分词器文件、模型启动证据 |
| `request-execution.tar.gz` | 完整运行档案、备份路径配置、请求账本、全部恢复审计、已有旧未知请求标记 |
| `manifest.json` | 全部文件/归档成员摘要、表指纹、请求状态摘要、格式版本 |

不复制锁文件的原inode；隔离恢复时在新的私有目录中建立独立验证锁。模型权重按
已固定的模型修订引用，不随备份重复复制；模型进程环境仍由维护窗口单独保存。

只有全部归档、原语料/合同/运行档案绑定和请求状态通过验证后，才发布完成清单。
导出中断会留下没有有效完成清单的部分目录，不覆盖旧备份，不因此清除请求状态。

## 一致性与未知请求

- 备份全程排斥正常应用启动、请求执行器和恢复工具对同一状态目录的操作。
- 数据库使用`REPEATABLE READ`导出快照，`pg_dump --snapshot`与表指纹来自同一快照。
- 原运行档案必须与原注册合同匹配；账本绑定必须与该档案、实例和容量一致。
  数据库里有其他运行档案的未完成作业时拒绝生成完成备份。
- 检查最新及全部历史恢复审计，保存原待确认请求集合及阻断位。待确认请求不妨碍
  保存故障证据，但备份会明确标记`requires_reconciliation`。
- 恢复验证不调用`recover_all`、不伪造终止证明、不更改旧租约，也不重放推理请求。
  未确认状态恢复后仍须被原账本安全检查阻断；旧未知请求标记同样保留。

## 操作顺序

离线准备脚本现在同时生成受保护的`request-backup-settings.json`。它需要指向实际
发布目录、数据、运行档案和启动证据，不可使用尚含`RELEASE_ID`的占位路径。

在确认维护窗口并停止应用后，使用新发布版本的工具与模块路径运行：

```text
python NEW_RELEASE/scripts/qwen_dev_ops.py backup-request \
  --project-root PROJECT_ROOT --instance-dir INSTANCE_HOME \
  --request-backup-settings PRIVATE_SETTINGS
```

命令返回新备份目录及完成清单的SHA-256。复制到另一台机器后，用独立保存的摘要验证：

```text
python scripts/qwen_dev_ops.py verify-copy \
  --backup-dir COPIED_BACKUP --manifest-sha256 EXPECTED_SHA256
```

学校服务器上的隔离数据库恢复演练：

```text
python NEW_RELEASE/scripts/qwen_dev_ops.py verify-restore \
  --project-root PROJECT_ROOT --instance-dir INSTANCE_HOME \
  --backup-dir BACKUP_DIR --manifest-sha256 EXPECTED_SHA256
```

应确保`PYTHONPATH`或安装环境使用对应新发布版本；旧运维脚本不认识v2格式。

恢复演练创建随机命名的独立数据库，执行真实`pg_restore`，核对所有表、账号凭据、
本人历史及提示词；使用归档里的实际执行合同重建内存队列。来源数据库和请求状态
不被替换。清理临时数据库前核验所有者、唯一标记和创建时记录的数据库OID，不使用
强制删除或CASCADE。归档中的Python文件只作为数据展开，绝不导入执行。

恢复成功表示数据与安全状态可以还原，**不表示已恢复线上服务**。验证报告始终
标明`automatic_start_allowed=false`，真正恢复到运行环境仍需维护流程和启动核验。

## 离机与跨平台检查

Linux生成的备份可在Windows做离线完整性验证；归档内原Linux路径作为历史元数据
处理，不要求它们在Windows存在。展开使用全新私有临时目录，拒绝路径穿越、链接
成员、重复成员、丢失的审计和内容摘要不匹配。

Windows不能依赖POSIX权限位。本轮离机测试发现Python创建的子目录会带`OWNER RIGHTS`
条目，不能通过现有受保护文件检查。已仅对新建恢复临时目录设置当前用户/System/
Administrators的明确访问控制，并让子目录继承；没有放宽读取检查或修改项目、系统
其他目录的权限。修正后两份学校生成的实际备份均在Windows通过完整性验证。

## 验证证据

学校隔离用例使用真实PostgreSQL、`pg_dump`和`pg_restore`，语料与成绩为明确测试数据。
每组两个凭据账号、两份由确定性替身计算的完整50样本成绩，另外保留排队状态；
阻断组再保留一份运行中提交及一个模拟未确认请求。没有真实Qwen调用。

| 用例 | 本人提交记录 | 已有恢复审计 | 待确认请求 | 隔离恢复结果 |
| --- | ---: | ---: | ---: | --- |
| 正常停止 | 3 | 1 | 0 | 表、凭据、提示词、排队恢复一致；账本清洁 |
| 未确认请求 | 4 | 1 | 1 | 表和状态一致；恢复后继续阻断，未自动重发 |

两组源测试数据库均保持不变，源账本及审计文件摘要未变；源测试数据库和隔离恢复
数据库全部确认清理。这里的“源数据库”指独立测试数据库，不是学校在线学生库。

证据目录：学校`artifacts/request-backup-services-20260923-v1`，本机
`runtime/request-backup-services-results-20260923`。

- Linux验收源码包：`62c9326897354e9adab130a5c8c3dcbc9c537f13ba531d377e5ecec7cfbba71a`。
- 正常组报告：`ecf3f05b8a350825f2c0607ea8d0dd6ad7256db4b0cb020fd43a3f5ba7cb3c0f`。
- 阻断组报告：`296ebcea55acd0adddfccd8fb6a1779a436a562f1fc33ae97b169b8cf4c4754f`。
- 正常组备份清单：`29acb0408c2c4492c92f8b8175cc5ea8b10e4d36efb5493535e876529aee1e26`。
- 阻断组备份清单：`aa5f0b3a837fea09c2d58b56a531544e832ddb48ec99b5e3bf3298c64b346f18`。

Linux源码验收后补充了上述Windows临时目录权限适配；离机检查使用修正后的源码。
自动检查新增必需的真实数据库备份/恢复组，使用PostgreSQL服务容器内相同版本的
客户端工具，避免客户端版本落后于服务器而导致的假失败或跳过。

本地完整回归835通过、45环境相关跳过（344.23秒）；补充Windows权限适配及真实
受保护文件读取回归后，备份、运维与开发入口相关30项通过，静态检查通过。
新的70合同切换材料位于`runtime/request-development-preparation-20260923-v3`，
现在包含`request-backup-settings.json`，不再将备份格式实现列为未完成项。

当前备份格式适配缺口已补齐。下一步是新的受控维护窗口：备份当前串行实例、核验
恢复，进行真实模型混合负载及新入口验收，再按确认的方案切换开发站。评分一致性
配置按用户要求维持现状，不在这次部署中开启批次无关执行。

准备阶段只读核对：学校用户服务管理器处于`running`，`Linger=no`。服务监督模板
可以继续按用户服务方式准备，但无人登录时的持续运行/开机启动设置仍须在部署时
确认；本轮没有修改该设置或启停任何在线服务。
