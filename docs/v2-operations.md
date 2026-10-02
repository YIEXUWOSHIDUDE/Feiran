# Feiran V2 运维与云端演练清单

> 本文只列可审阅的操作步骤，不记录进度；结果、未验证项和阻塞只写在 [v2-plan.md](v2-plan.md)。截至 2026-09-30，**下面没有任何一步在 AWS 上执行过**，`deploy/aws/cognito.yaml` 与 `deploy/aws/compose.v2.yaml` 均未部署、未启用。凡新建或修改云资源、改变运行中服务、发信、迁移真实资料、调用收费模型、提交/推送/发布，都要先取得用户对**该步骤**的明确授权；一步的授权不延伸到下一步。不要把本文或旧部署文档当成操作授权。

## 1. 执行前要拿到的授权与费用决定

| 编号 | 操作 | 费用或外部影响 | 需要谁决定什么 |
|---|---|---|---|
| G1 | 创建 Cognito 栈（用户池、托管登录域、应用客户端、主机角色读取客户端密钥的 IAM 策略） | 用户池按月活计费（模板默认 Lite 层）；用户池设了删除保护并在删栈时保留 | Region、`DomainPrefix`、层级与价格（以创建当日官方价格页为准） |
| G2 | 邀请用户（`AdminCreateUser` 发邀请邮件） | 默认邮件发送每个 AWS 账户每天 **50 封**、不可调整、每天 09:00 UTC 重置（[Cognito 配额](https://docs.aws.amazon.com/cognito/latest/developerguide/quotas.html)）；邀请和找回密码共用 | 100 人分至少两天邀请，或另行授权配置 SES（验证发信域名、申请出沙箱、SES 计费） |
| G3 | 隔离演练环境 | 第二套 `workbench.yaml` + `public.yaml` 栈（EC2、EBS、S3、CloudFront）按时计费 | 新建隔离栈，或在约定窗口占用现有主机（会中断当前所有者服务） |
| G4 | 在主机上安装 V2 发行版 | 改变运行中服务 | 发布授权；需先提交、推送并由 CI 构建镜像（G7） |
| G5 | 迁移所有者真实资料 | 真实个人资料 | 所有者的 Cognito `sub`、停机窗口、迁移前 V1 备份保留期 |
| G6 | 真实 DeepSeek 演练与 B6 有限评估 | 付费模型调用 | 预算与调用上限（`eval_v2.py live --max-calls`） |
| G7 | 提交、推送、PR、CI、合并 | 远端仓库 | 用户 / Codex 决定 |

## 2. 现有主机文件还需要的改动（尚未修改）

`deploy/aws/host/*.sh` 是当前单用户线上部署及其 CI 测试（`deploy/aws/test-host-scripts.sh`）的一部分。这一轮没有改它们，以免下一次 V1 发布路径悄悄变化。V2 上主机前需要另做一个可审阅增量：

1. **`fetch-secret.sh`**：V2 模式（设置了 `WORKBENCH_OIDC_CLIENT_ID`）下，用主机角色读取应用客户端密钥写入 `/run/workbench/oidc_client_secret`（0400，uid 10001，沿用现有临时文件 + `mv` 写法，不打印、不进日志、不进命令行参数）：
   `aws cognito-idp describe-user-pool-client --region "$AWS_REGION" --user-pool-id "$WORKBENCH_OIDC_USER_POOL_ID" --client-id "$WORKBENCH_OIDC_CLIENT_ID" --query UserPoolClient.ClientSecret --output text`
   CloudFormation 不能输出客户端密钥（`GetAtt` 只有 `ClientId`），所以由主机在启动时读取。V2 模式跳过所有者 Basic 登录密钥：V2 绝不同时接受旧所有者口令。
2. **`/etc/workbench/env`**（V2 模式）：
   - `WORKBENCH_OIDC_ISSUER` / `WORKBENCH_OIDC_CLIENT_ID` / `WORKBENCH_OIDC_DOMAIN`：Cognito 栈输出 `Issuer`、`ClientId`、`ManagedLoginDomain`；另加 `WORKBENCH_OIDC_USER_POOL_ID`（输出 `UserPoolId`）。
   - `WORKBENCH_PUBLIC_ORIGIN`：用户打开的 CloudFront 地址，`https://…`，无结尾斜杠，必须与 Cognito 回调/退出地址的前缀完全一致。
   - `WORKBENCH_PUBLIC_HOST`：**源站私有 DNS**（`public.yaml` 输出 `OriginHost`）。`public.yaml` 用 `AllViewerExceptHostHeader`，源站收到的 Host 是私有 DNS；应用只接受这个 Host 和 localhost。CSRF 的 Origin 检查用的是 `WORKBENCH_PUBLIC_ORIGIN`。
   - `WORKBENCH_OIDC_SECRET_FILE=/run/workbench/oidc_client_secret`；删除 `WORKBENCH_LOGIN_*`。
3. **`workbench.service`**：V2 模式的 `ExecStart`/`ExecStop` 多加 `-f deploy/aws/compose.v2.yaml`（drop-in 或在 env 中给出 compose 文件列表）。V2 仍是一个容器、一个 Uvicorn worker，任务执行器在进程内。
4. **`backup.sh` / `restore.sh`**：数据目录的 `.workbench-format` 为 2 时改用 V2 工具：
   - 备份：`python v2_backup.py create --data /data --out /backups/NAME.tar.gz`（SQLite 在线备份，一致性不要求停服务；沿用停服务的现有流程也可以）。
   - 校验：`python v2_backup.py restore --archive … --into /check/data --ledger /data/deleted-accounts.jsonl`，再 `python v2_backup.py verify --data /check/data`。
   - 恢复：`--ledger` 必须指向**现行**删除账本。V2 数据目录始终有 `deleted-accounts.jsonl`：应用启动、迁移和恢复时都会确保它存在（从未删除过账户时是空文件；文件丢失而数据库还在时，按数据库里的墓碑重建）。因此给出的账本不存在、不是文件或读不出时，恢复会拒绝，且不写入目标目录——通常是路径写错或数据卷已丢失。只有确认现行账本确实丢失时才改用 `--without-live-ledger`，此时最后一次备份之后删除的账户会回来，需要按账户删除记录（见 §7）补删。
   - 建议另加：每次删除账户后把 `deleted-accounts.jsonl` 也复制到 S3（几 KB），避免数据卷整体丢失时只能用旧备份里的账本。这是待评审的建议，尚未实现。
5. **`enable-public.sh` 的 V2 版本**：先确认镜像含 V2（`python -c 'import web_v2, identity'`），写入上面的 env，取到密钥后重启；只有同时满足 `GET /healthz` = 200、`GET /` = 303 跳到 `/login`、`GET /api/me` = 401 才算接通，否则停止服务（与现有“401 才算接通”同理）。
6. **`test-host-scripts.sh`**：为以上 V2 分支补测试。

`compose.v2.yaml` 已准备好（不在使用中）：`command: python web_v2.py`，`WORKBENCH_OIDC_*` 环境变量，客户端密钥以 compose secret 挂载。V2 启动时会拒绝：缺 Cognito 设置、数据卷未挂载（`WORKBENCH_REQUIRE_DATA=1` 且无 `.workbench-data`）、未迁移的 V1 数据目录、非 https 的公开地址（localhost 除外）。

## 3. Cognito 栈（G1）

```sh
aws cloudformation create-change-set --region "$AWS_REGION" --stack-name feiran-v2-auth \
  --change-set-name create --change-set-type CREATE --capabilities CAPABILITY_IAM \
  --template-body file://deploy/aws/cognito.yaml \
  --parameters ParameterKey=PublicOrigin,ParameterValue=https://dXXXX.cloudfront.net \
               ParameterKey=DomainPrefix,ParameterValue=feiran-XXXX \
               ParameterKey=HostRoleName,ParameterValue=<基础栈的主机角色名>
aws cloudformation describe-change-set --region "$AWS_REGION" --stack-name feiran-v2-auth --change-set-name create
# 审阅后才执行：
aws cloudformation execute-change-set --region "$AWS_REGION" --stack-name feiran-v2-auth --change-set-name create
aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name feiran-v2-auth --query 'Stacks[0].Outputs'
```

模板要点：只允许管理员建用户（无自助注册），邮箱为用户名且大小写不敏感，授权码流程 + `openid email`，回调 `${PublicOrigin}/auth/callback`、退出 `${PublicOrigin}/signed-out`，ID/访问令牌 15 分钟（应用自己的服务端会话最长 12 小时、空闲 2 小时）。应用另有注册开关与账户上限，所以即便以后放开 Cognito 自助注册，未开启应用注册时新身份也进不来。模板本地已过 `cfn-lint`，未在 AWS 上创建过。

## 4. 启动与试点设置（G3/G4，先只用合成账号）

在主机上对运行中的容器执行管理命令（输出只有内部 ID、Cognito `sub`、状态和数字，没有简历内容）：

```sh
v2() { docker compose -f compose.yaml -f deploy/aws/compose.aws.yaml -f deploy/aws/compose.v2.yaml exec -T workbench python v2_admin.py --data /data "$@"; }
v2 settings
v2 set user_daily_units 20      # 数字待预算决定，见下
v2 set site_daily_units 600
v2 set queue_limit 20
v2 set max_accounts 100
v2 set registration_open 1      # 没有有限的每日额度和账户上限时会被拒绝
v2 usage                        # 当天（UTC）全站和各账户已预留的额度单位
v2 set tasks_enabled 0          # 紧急开关：立即拒绝新任务；已在进行的模型调用仍可能计费
v2 accounts --subject <sub>     # 由 Cognito sub 找内部账户
v2 disable <user_id>            # 结束其会话、取消其排队/执行中的任务
v2 delete <user_id> --confirm <user_id>   # 先写删除账本，再删数据并留下墓碑
```

每类操作的额度单位（`v2_flow.UNITS`）：上传简历 1、导入岗位/从 URL/开始岗位列表 3、准备简历 2、改写 1、版面 1、缺口检查 2、导出 PDF 1。提交时为用户和全站原子预留；确定未产生费用（例如在调用模型前失败）才退回，费用未知的不退回、也不自动无限重试。用演练里每个任务记录的 token 用量估算“每单位成本”，再由预算推出两个每日上限。AWS 预算告警不是硬上限，这两个设置和 `tasks_enabled` 才是。

邀请（G2，会发信）：`aws cognito-idp admin-create-user --user-pool-id … --username <email> --user-attributes Name=email,Value=<email> Name=email_verified,Value=true --desired-delivery-mediums EMAIL`。用户首次登录时，应用在注册开启且未满上限时为其 `(issuer, sub)` 建空白工作区；每人第一次用到“上传解析”和“按岗位处理”时分别看到数据去向并同意，未同意不调用模型。

## 5. B4 云端演练（隔离环境、合成账号）

| 编号 | 演练 | 操作 | 必须观察到 |
|---|---|---|---|
| #3 | 模型不可用 | 不配置 DeepSeek 密钥（或临时撤销），提交“准备简历”和“缺口检查” | 任务显示真实失败或阶段降级原因；原文草稿可用；不出现成功假象 |
| #4 | 旧页批准 | 两个标签页；一页改事实或改写，另一页点批准 | 409，批准不写入；刷新后按新版本重新审核 |
| #5 | 生成中断 | 任务 running 时 `sudo systemctl restart workbench` | 重启后为 interrupted；同一按钮再提交复用原任务（attempts 增加），只有一条材料链，不重复发布 |
| #7 | 发布回退 | 对 V2 数据安装上一 V1 发行版 | V1 因数据格式 2 拒绝启动；按 §6 的回退步骤恢复 |
| 恢复 | 跨环境恢复 | `v2_backup create` → 新的空数据卷 `restore --ledger <现行账本副本>` → `verify` → 启动 | 归属与批准不变；旧会话全部失效需重新登录；执行中的任务变 interrupted |
| 删除 | 删除后恢复旧备份 | 合成用户 C 自助删除账户 → 用删除前的备份恢复 | C 登录得到 account_deleted，数据不存在 |
| 隔离 | 两个 Cognito 合成账号 | 把 A 的岗位/任务/上传/预览/下载 ID 给 B | 一律与“不存在”相同的 404 |
| 会话 | 浏览器开发者工具 | 检查 `__Host-` Cookie（Secure、HttpOnly、SameSite=Lax、Path=/）；去掉 `X-Workbench-Token` 或换 Origin 的写请求 | 写请求 403；退出后旧页面 API 401，并经 Cognito 退出页回到 `/signed-out` |
| 停用 | `v2 disable` | 对已登录用户执行 | 其下一次请求 401；排队任务 cancelled |
| PDF | Linux 容器内 Chromium | 中英文各导出一次 | 页数、文字、链接与渲染图逐项检查 |
| 负载 | 目标机型上运行 `python -m tests.acceptance_v2 --out /tmp/v2-acceptance --chrome` | 与本机结果对比 | 用于决定机型，不是性能承诺 |

## 6. 所有者资料迁移（G5）与回退

1. 在 Cognito 邀请所有者并取得其 `sub`：`aws cognito-idp admin-get-user --user-pool-id … --username <所有者邮箱> --query "UserAttributes[?Name=='sub'].Value" --output text`。
2. 停止 V1，执行现有 `backup.sh`，确认备份已校验，保留到 V2 验收结束。
3. 预演（只写临时副本）：`python v2_migrate.py run --v1-data /data --v2-db /data/v2.db --owner-issuer <Issuer> --owner-subject <sub> --dry-run`，审阅报告里的计数、未迁移项（未保存的上传、未完成操作的提示、谈话要点匹配、各岗位历史目录）和“哪些批准未能保留”。
4. 正式迁移：同一命令去掉 `--dry-run`，再 `python v2_backup.py verify --data /data`。V1 文件只读打开、保持原样。所有者已存在、日志未清（`-wal`/`-journal`）或格式不对时会拒绝并删除半成品。
5. 启动 V2，所有者登录核对：事实与双语 profile 版本、岗位、材料链；只有仍完全成立的批准与其最终 PDF 被带过来，其余回到待审核。迁移从不补造批准或确认。
6. 回退：`python v2_migrate.py rollback --data /data --backup-out /backups/v2-before-rollback.tar.gz`（先备份 V2 并移开，数据目录回到 V1 格式），再安装上一 V1 发行版。V2 期间新产生的资料保存在该备份里，不会被静默丢弃。

## 7. 日常运维

- 备份：每晚 `v2_backup create`，S3 保留期沿用基础栈（默认 35 天）。删除账户后，旧备份在保留期内仍含其资料，到期删除；隐私说明需写明这一点，不承诺即时擦除。
- 删除账户记录：数据库内墓碑 + `deleted-accounts.jsonl`。恢复时两者合并后重新删除，同一 Cognito 身份不会因登录或恢复而重新开放。
- 日志：每行一个 JSON，记录路由、状态、耗时、任务事件和错误类型与位置；不记录简历内容、令牌或请求路径中的 ID。
- 任务：同一时刻全站最多 2 个重任务、其中 PDF 最多 1 个；每人最多 1 个执行中 + 1 个排队；排队超过 15 分钟失效并退回额度；单任务时限 10 分钟；同一请求最多尝试 3 次。重启时遗留的 running 任务标为 interrupted，不会自行重跑。
