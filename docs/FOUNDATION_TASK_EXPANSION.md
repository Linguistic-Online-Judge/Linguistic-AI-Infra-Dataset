# 18语言基础任务扩充：已部署与验收

## 当前覆盖

学校8090已从26项目录/22个执行配置扩充到**60项目录/56个执行配置**。
新增17项分词、17项依存句法；原有18项通用词性、中文分词与转写、德语专用词性与
依存句法全部保留。每种语言现在均具备分词、通用词性、依存句法三项基础任务。
每项仍为50个固定样本。

| 语言 | 分词树库 | 通用词性树库 | 依存树库 |
| --- | --- | --- | --- |
| 阿拉伯语 | PUD | PUD | PUD |
| 中文 | GSDSimp（原有） | Beginner | Beginner |
| 丹麦语 | DDT | DDT | DDT |
| 荷兰语 | LassySmall | LassySmall | LassySmall |
| 英语 | CHILDES | CHILDES | CHILDES |
| 法语 | FQB | FQB | FQB |
| 德语 | HDT | HDT | HDT（原有） |
| 希伯来语 | HTB | HTB | HTB |
| 印地语 | HDTB | HDTB | HDTB |
| 匈牙利语 | Szeged | Szeged | Szeged |
| 意大利语 | KIParlaForest | KIParlaForest | KIParlaForest |
| 日语 | PUD | PUD | PUD |
| 韩语 | Kaist | Kaist | Kaist |
| 葡萄牙语 | CINTIL | CINTIL | CINTIL |
| 俄语 | SynTagRus | SynTagRus | SynTagRus |
| 西班牙语 | AnCora | AnCora | PUD |
| 瑞典语 | Talbanken | Talbanken | Talbanken |
| 泰语 | PUD | PUD | PUD |

专用词性与转写的进一步扩充仍按`TASK_COVERAGE_PLAN.md`另批推进。本机8080继续使用
5项模拟任务；实际扩充后的目录在学校8090。

## 数据、说明和长度预算

- 新注册表：`config/challenge_contract_registry_foundation_v1.json`，是在旧v1注册表
  上追加34项的新文件。旧注册表和旧合同文件没有改写。
- 新合同位于`config/evaluation_contracts/foundation-v1/`，任务ID以`foundation-v1`结尾。
- 构建工具：`scripts/build_foundation_catalog.py`。固定50样本、种子2026，使用完整
  候选池按既有选择算法生成，不通过更换种子筛出模型表现更好的样本。
- 各任务使用当前固定Qwen tokenizer检查全部样本的标准输出及零/少样本模板输入。
  输出预算以标准紧凑JSON长度加格式余量估计；字符串输出仍不是严格封闭的理论上界。
  自定义提示词仍需通过提交后的实际输入长度检查。
- 西班牙语AnCora的初始依存样本最大模板输入2006词元、标准输出2364词元，二者已超过
  4096上下文。该项改用PUD，测得输入1319、标准输出1127、配置输出预算1536，能够容纳。
  原西班牙语UPOS继续使用AnCora。此变更仅涉及新增任务的数据源选择。
- PUD来源参考：[UD Spanish PUD r2.18 README](https://raw.githubusercontent.com/UniversalDependencies/UD_Spanish-PUD/r2.18/README.md)，
  其中标注CC BY-SA 3.0及原文权利说明。此开发扩充没有替代正式来源权利审核，所有任务
  仍为私有开发模式的draft配置，不宣称完成对外发布许可。
- `web/assets/language-lessons.js`提供18语言各两条手写格式示例，分词与依存共用对应
  词元并各自产生规定的输入输出结构。这些例子不是从评测样本提取的答案。
- 分词说明明确为“树库词元拼接后的边界恢复”，不冒充保留原始空格/缩合形式的自然
  文本分词。每个语言带有边界处理提示；依存示例覆盖主语、限定词、助词和助动词等。
  手写教学例子仍适合由课程教师审校，不将自动结构检查称为语言专家审定。

原始长度报告：学校`artifacts/foundation-20260913-v1/build-final/build-report.json`，
本机副本在`runtime/foundation-build-final/build-report.json`。更早的`build`和
`build-checked`为未采用的预检产物，不作为当前验收证据。

## 受控升级与数据保留

- 当前源码：`/mnt/local/babylm26_g2/projects/linguistic-oj/artifacts/foundation-20260913-v1/source`。
- 当前数据目录：同级`data/`；标准数据只读指向原候选数据，私有清单包含旧22项与新34项。
- 继续使用原`qwen-development-18-v1/state`、原数据库和原Redis实例命名空间。
- `--registry config/challenge_contract_registry_foundation_v1.json`指定扩展注册表。
- `scripts/extend_qwen_registry.py`要求应用已停止并取得实例锁，核对旧标记摘要、数据库
  归属、无未完成作业、旧记录合同指纹和所有新数据绑定后，才执行只增不减的更新。
- `registry_extension.py`拒绝删除/替换旧合同，原子更新标记并写入升级前后文本和摘要日志。
- 更新前后11张表的内容摘要完全一致，原有6个账号和20条成功评测均保留。
  此过程没有数据库结构迁移，也没有修改模型进程或原有正式`app/current`指针。

部署记录：`runtime/foundation-deployment-20260913.json`；服务器同级`deployment.json`。
源码包`runtime/foundation-source-20260913.tar.gz`共有186个归档条目，SHA-256为
`59a953f140d00f4b148917348d710ba9fc03ebd6697d3f1660d21116312e234b`。
启动时PID为1246388，仅作本次记录；排查应查询当前进程。
日志位于`artifacts/foundation-20260913-v1/server.stdout.log`、`server.stderr.log`，
PID文件为`server.pid`。

## 验收结果

- Python：**554通过、32跳过**；Ruff通过。
- Node契约：8项通过；真实本地浏览器32组学生流程、16组管理员流程通过。
- 学校：新增34项每项实际执行50个样本，共**1700个样本、34条评测，全部succeeded**。
  检查了对应语言示例、模板、结果原提示词及匹配评测身份的排行榜。
- 分词格式有效数47—50/50，新增依存格式有效数33—47/50。实际分数有高有低；
  平台保留格式问题和原始计分，没有修补模型输出或根据成绩修改评测样本/规则。
- 依存任务耗时较长；例如本轮西班牙语约11分钟。多人队列等待和容量仍需独立验收，
  不用顺序运行成功推断课堂并发容量。
- 验收运行曾被工具执行时限中断；已通过原记录接续，无重复提交。最终数据库54条
  成功评测、7个账号，服务ready、无排队/运行任务、无未确认推理标记。

报告：`runtime/browser-tests/foundation-20260913-v1/report.json`，`passed: true`，
`completed_jobs: 34`。其中的账号恢复文件保持在忽略目录，不加入源码包。
截图同目录，包括`catalog-expanded-1440.png`和各任务的`practice-*`、`result-*`。
学校报告副本：`artifacts/foundation-20260913-v1/browser-acceptance.json`。

验收脚本可按批继续读取已有记录：

```powershell
node tests/browser/qwen-foundation.mjs --run-real-qwen --limit 2
```

它仅针对新增34项，已成功的记录会跳过；未知受理状态不会自动重复提交。
不能将这个真实模型脚本加入普通快速CI。

## 扩充后的备份

已生成`backups/qwen-development/20260912T214730Z-78cc303d`，清单SHA-256：
`2105b9e4422c25a2581774b50954bb3ca94f883c72764a0b31a8eeaae96b24f5`。
隔离恢复通过，核对54条本人记录、7份凭据及11张表，验证队列重建，恢复库已清理，
运行库内容摘要未变化。报告为该目录下
`verification-63fca3e09c484612849dc374c571570f.json`。
副本已下载至本机`runtime/backups/20260912T214730Z-78cc303d`，并用上述清单摘要
校验全部备份文件和归档内容通过。扩充前的旧备份继续保留。
