# 正式部署配置与离线预检

本工具可以在域名尚未确认时准备配置草稿。它读取本机公开任务与评测合同，生成
认证配置、反向代理和应用进程模板，并列出待补信息。**生成成功不表示可以上线。**

## 1. 生成准备文件

在主工程目录、已安装项目依赖的环境运行：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_public_deployment.py --settings config/production.example.json --output runtime/production-preparation-20260913-v1
```

示例输出目录已在本机生成。再次运行应选择新的目录名，工具拒绝覆盖已有目录。
输出必须在项目`runtime/`下，且父目录已存在；文件不进入版本库。
Linux上使用安装了应用的Python解释器运行同一个脚本。

| 生成文件 | 用途 |
| --- | --- |
| `preflight.json` | 待补字段、目录/合同数量、满足现有公开受理条件的任务清单 |
| `auth.json.template` | 正式同源地址、可信代理与真实邮件配置；只引用密码文件路径 |
| `nginx.conf.template` | 同机Nginx反向代理草稿，只转发至回环地址上的应用 |
| `loj-api.service.template` | 系统级systemd应用进程草稿，以指定非root账号运行 |
| `api-command.json` | 对应应用命令的参数数组，方便核对，不自动执行 |
| `executor.json.template` | 与应用共用连接配置/命名空间的串行执行配置，模型及数据路径待补 |
| `loj-executor.service.template` | 串行执行进程草稿，遇未确认请求或配置错误停止自动重启 |

域名未知时保留`public_origin: null`，模板显示`__PUBLIC_HOST__`。
脚本不解析域名、不连接数据库/队列/邮件/模型，也不安装或启动服务。
Linux输出目录权限为700，文件为600；Windows上的这些草稿不含服务密码，正式
私有文件仍需通过目标系统实际权限检查。

## 2. 参数含义

将`config/production.example.json`作为配置起点，实际填写版放在私有运行目录。

- `public_origin`：实际浏览器访问的根地址，例如`https://judge.school.edu`。
  该示例不是现有网址。必须是规范的小写HTTPS地址，不含路径、尾斜杠、用户信息
  或显式默认端口`:443`。当前代理模板使用域名；域名待确认时填写`null`。
- `release_root`、`python`、`shared_dir`：目标Linux机器上的明确发布目录、应用
  解释器、生产私有配置目录。模板使用无空格、无路径回退的绝对路径；
  `RELEASE_ID`须替换为实际审核发布标识。默认`/srv/...`只是可调整的部署布局。
- `service_user`：具有对应目录读取权限的非root服务账号；尚未落实时为`null`。
- `api_port`：同机应用端口，默认8100；避开现有8000模型、8080/8090开发端口。
- `registry`：发布目录内的注册表相对路径。脚本验证描述与合同对应、静态模型
  配置和共享请求体上限，不改写合同或任务状态。
- `trusted_proxies`：实际直接连接应用的代理地址，逐个填写精确地址。
  当前同机模板要求包含`127.0.0.1`；学校网关若位于另一机器，需根据实际链路
  调整代理配置与监听防护，再验证地址可信性。
- `tls_certificate`、`tls_certificate_key`：实际证书链和私钥路径。
- `smtp`：真实邮件服务器、端口、发送地址、登录账号及`starttls`或`ssl`模式。
  SMTP（Simple Mail Transfer Protocol，简单邮件传输协议）的密码通过独立文件读取。
- `runtime_available_challenges`：经生产执行器验证可处理的任务标识清单。
  不因存在70份合同就自动填入70项；清单只声明能力，不会创建执行器。

当前XPOS注册表的离线结果为**74项目录、70份合同、0项满足现有正式公开受理条件**。
这与学校开发实例能执行70项并不冲突；其开发覆盖设置不能用于正式开放。
需要完成来源/权利记录与公开激活确认，按版本化流程生成新的正式发布资料，保留
旧合同及历史成绩。教学讲解“发布”也不等于评测合同“激活”。

自动化调用可加`--require-complete`：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_public_deployment.py --settings runtime/production.json --output runtime/production-preparation-next --require-complete
```

该命令要求先准备实际填写的`runtime/production.json`。尚有待补项时，工具仍保存
草稿和报告，但以状态码2退出。不加该参数时，状态码0仅代表准备文件生成成功。
即使报告为`static_configuration_valid`，也只代表静态配置通过；
`live_dependencies_checked`与`production_started`始终为`false`。

## 3. 生产私有文件与连接保护

由部署者在实际`shared_dir`内配置：

```text
auth.json        ← 填全后的认证配置
smtp-password    ← 单行邮件密码
postgres.url     ← 单行PostgreSQL连接地址
redis.url        ← 单行Redis连接地址
```

Linux文件须为私有普通文件，建议权限600，由应用账号拥有并可读取；不使用符号链接。
密码不进入命令参数或版本库。应用与执行器均支持
`--postgres-database-url-file`和`--redis-url-file`，与对应内联地址互斥。
生产命令拒绝直接携带密码的内联连接地址。

连接校验要求明确目标，拒绝重复参数、查询参数覆盖目标、隐式`PG*`连接环境设置
及控制字符。`POSTGRES_TEST_*`测试变量不属于`PG*`。
PostgreSQL支持明确回环地址和绝对Unix套接字目录；非回环连接须使用安全`sslmode`
（建议部署时选择`verify-full`）。Redis非回环连接须使用`rediss`。
Redis启动检查要求6.2以上，并执行无键、无写入的Lua能力探针；仅能PING不算队列就绪。

## 4. 安装与运行检查顺序

1. 固定审核提交和构件，在目标机器建立独立应用环境与正式配置/数据绑定。
   保留开发数据库及模型环境；生产初始化、账号安排和迁移另行执行并记录。
2. 填全配置，重新运行严格离线预检；由目标应用账号验证私有文件可读及保护权限。
3. 在实际代理环境合并模板，再运行`nginx -t`。模板要求支持
   `ssl_reject_handshake`的Nginx（1.19.4以上）。已有默认站点时先处理默认监听冲突，
   不将整份草稿直接覆盖学校网关配置。证书和外网路由需在该机器实际验证。
4. 核对systemd单元中的解释器、发布路径和用户，运行
   `systemd-analyze verify /实际路径/loj-api.service`后安装。此模板是系统级服务；
   当前学校账号的用户服务权限和`Linger=no`并未因生成模板而改变。
5. 使用统一串行入口`qwen_executor`及新增的执行器模板，补齐模型与数据路径，
   初始化独立持久状态后验证。详见[生产执行器](PRODUCTION_EXECUTOR.md)。
   新入口包含请求前落盘、进程互斥和有记录的恢复；目标机器的安装与实际模型验收
   仍须完成。单任务`qwen_worker`不能直接扩展为70个并发消费者。
6. 启动后的`/health/live`只证明应用存活；`/health/ready`检查应用依赖，不能证明
   已声明的生产执行器实际健康。需要补真实邮件收发、执行器与模型验证，再完成
   `PUBLIC_DEPLOYMENT.md`中的校外完整业务验收。

代理覆盖`X-LOJ-Client-IP`，清除不受信任的转发链；未匹配的入口拒绝访问。
请求体上限从合同读取，不额外改变提交规则。应用进程使用现有脱敏请求日志；
生成的HTTPS站点关闭原始访问地址日志，其他网关日志策略需按实际部署核对。

模型请求因网络错误或超时失联后，即使本地请求线程已退出，也保留进程内活动标记，
阻止同一提供器再次发送请求。新`qwen_executor`进一步在发送前建立持久记录，
提供跨进程阻断与恢复审计，已进行隔离故障测试；学校真实运行环境尚未切换该入口。

## 5. 本轮验证范围

本机验证：Ruff通过；620项Python通过、32项依赖实际PostgreSQL/Redis等环境的
检查跳过；8项前端契约、32组学生和16组管理员浏览器检查通过。
浏览器使用独立SQLite/模拟模型应用。测试确认生成参数可被真实命令解析器及认证
配置加载器接受、错误配置被拒绝、已有输出不被覆盖、失联请求不再启动第二次推理。

本机已生成`runtime/production-preparation-20260913-v1/preflight.json`，状态为
`pending_configuration`。未执行目标Linux机器的Nginx/systemd检查、真实邮件、
公网访问或生产故障演练。新代码的远端自动检查结果应以对应提交的实际运行记录为准。
