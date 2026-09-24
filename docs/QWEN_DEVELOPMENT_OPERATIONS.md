# 当前Qwen开发实例的备份、恢复与诊断

## 已安装命令

学校项目根目录下使用：

```sh
operations/bin/qwen-development status
systemctl --user stop linguistic-oj-request-development.service
operations/bin/qwen-development backup-request --request-backup-settings artifacts/request-workbench-rollout-20260924-v3/request-backup-settings.json
operations/bin/qwen-development verify-restore --backup-dir <本工具输出的备份目录>
systemctl --user start linguistic-oj-request-development.service
```

当前`operations/bin/qwen-development`包装脚本已指向
`artifacts/request-workbench-rollout-20260924-v3/source/scripts/qwen_dev_ops.py`及匹配模块。
请求模式必须使用v2备份，应用需先排空并停止，模型不必为备份而重启。发生未知请求
或完整性错误时应先按恢复证据处理，不能盲目执行最后一条启动命令。
旧启动器`qwen_dev_ops_launcher.py`及`operations/config/qwen-development.json`作为历史
部署材料保留，不应据其旧源码路径判断当前运行版本。

工具读取当前私有`state/instance.json`，核对实例格式、数据库所有者与归属注释；
仅操作该实例的`loj_dev18_`数据库。旧`backup-services`等脚本继续只代表旧实例，
当前8090以本页命令为准。

## 备份内容

当前v2还保存运行档案、请求账本及全部恢复审计，支持保留未确认请求的阻断状态。
详情及校验命令见[请求模式备份与恢复](REQUEST_BACKUP_RESTORE.md)。原串行v1备份仍可验证。

- 使用PostgreSQL导出的同一事务快照生成数据库备份和11张表的内容摘要。
- 源码及公开配置归档，逐文件核对原发布清单。
- 18语言标准数据、当前任务私有清单、tokenizer文件和模型启动证据归档。
- 私有实例标记、文件摘要与数据表数量。模型大权重不重复复制，以固定版本引用。
- Redis作为投递层，恢复策略以数据库outbox为依据，在新的实例命名空间重建。
  对恢复时仍在执行状态的作业，必须先确认旧模型请求终止，不能盲目恢复推理。

备份目录和文件仅向所属操作员开放，不能作为网站静态资源或提交到公开仓库。

## 隔离恢复检查

工具创建随机命名的`loj_restore_`数据库并写入归属注释，恢复数据库后：

1. 检查结构v4、11张表的数量和内容摘要。
2. 检查每条记录的本人历史、结果和原提示词，核对提示词哈希与跨所有者拒绝。
3. 在恢复库中新增一个合成队列探针，以内存队列验证已投递任务能从outbox重新建立。
4. 核对恢复库归属后删除恢复库；保留核对报告和恢复日志。
5. 对比运行库演练前后的摘要。本轮无并发用户写入，因此其结果完全一致。

不会启动模型、运行评测或写入真实Redis队列。教学表即使为空也会核对；
空表恢复通过不能声称已完成非空教学数据的全部恢复场景。

## 2026-09-12实测记录

后续实际32槽位切换的备份与恢复已完成：`20260924T035747Z-9379dd2f`包含79条记录、
13份凭据，全部11张表和请求状态核对通过，Windows离机副本也已验证；详见
[部署结果](REQUEST_WORKBENCH_DEPLOYMENT.md)。下面保留早期恢复点的历史记录。

最新XPOS版本备份为`20260913T084818Z-8a6421c3`，68条记录、7份凭据的隔离恢复通过，
详见`XPOS_TASK_EXPANSION.md`。下述备份为此前恢复点。

后续基础任务扩充后的新备份为`20260912T214730Z-78cc303d`，已通过54条记录、7份凭据的
隔离恢复。当前源码与合同注册表已变更，详见`FOUNDATION_TASK_EXPANSION.md`。
下述20条记录的备份作为扩充前恢复点保留。

- 备份：`backups/qwen-development/20260912T180549Z-9d2ebb81`。
- 清单SHA-256：`a060e16390ed16bbbc0832abc85e5842ceb9d94ad137a85bf876d23447f4b9b2`。
- 恢复核对成功：6份账号凭据、20条评测及原提示词、结果均可读取。
- 11张表一致，隔离队列重建通过，恢复数据库已清理，运行库摘要未变化。
- 成功报告：`verification-6a67f77a05de42ca8225eaaa4900d6f2.json`，位于该备份目录。
- 已复制一份到本机`runtime/backups/20260912T180549Z-9d2ebb81`，并以固定清单摘要
  校验全部文件及归档条目。这是本次异机副本，不代表已配置定时备份或长期保留策略。
- 新诊断命令实际返回当前数据库、结构v4、当前源码目录、应用就绪、20条成功记录，
  未确认推理标记不存在。

本机复查副本：

```powershell
.\.venv\Scripts\python.exe scripts/qwen_dev_ops.py verify-copy --backup-dir runtime/backups/20260912T180549Z-9d2ebb81 --manifest-sha256 a060e16390ed16bbbc0832abc85e5842ceb9d94ad137a85bf876d23447f4b9b2
```

## 自动检查与版本交付进展

- 工作流已增加管理员PostgreSQL测试变量、Node/浏览器检查、安装构件后的启动验收。
- `check_ci_service_coverage.py`使必跑数据库/队列测试缺失或跳过时失败。
- `check_installed_package.py`在源码目录外启动真正安装的包，验证资产、登录、模拟
  提交、结果和本人提示词；已在新的Windows/Python3.14虚拟环境通过。
- 本次构件：`runtime/wheel-check/linguistic_online_judge-0.1.0-py3-none-any.whl`，
  SHA-256 `07adc68f02e9729ffa532c7bdac26cb1b23e3dde489a87697ef6c601e0a1fc82`。
- `requirements/application-win-py314.txt`记录本次实际验证的应用依赖版本，
  不代表学校模型环境或Linux依赖锁定已完成。
- CI配置的远程执行、完整Git源码提交/推送、Linux依赖锁定、定时备份、进程监督和
  并发容量仍需后续完成。工作流写入文件不等于GitHub已运行成功。
