# Feiran V2 运维与云端演练清单

> 本文只列可审阅的操作步骤，不记录进度；结果、未验证项和阻塞只写在 [v2-plan.md](v2-plan.md)。2026-10-02 已按用户授权创建隔离试运行的基础、HTTPS 和 Cognito 栈；应用启动及登录验收结果见 v2-plan.md §18。用户将用途收窄为求职作品展示，初始只开放管理员创建的测试身份；邀请邮件、真实资料迁移和收费模型调用仍未授权。本文本身不扩大用户授权。

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

## 2. V2 主机流程

主机通过 `/etc/workbench/env` 中的 `WORKBENCH_MODE=v2` 明确选择 V2；省略时仍为 V1。`workbench.service` 调用 `host/compose.sh`，只有 V2 模式才叠加 `compose.v2.yaml`。V2 绝不接受旧所有者口令。

### 隔离试运行的第一次安装

1. 新建独立基础栈、CloudFront 栈和 Cognito 栈。基础栈的 `GitHubEnvironment` 使用 `aws-v2-pilot`，复用账户已有 GitHub OIDC provider；不覆盖现有 `aws` 环境或生产栈。
2. 在 GitHub 新环境 `aws-v2-pilot` 配置它自己的 Region、release role、repository、host 和 SSM document；发布工作流选择该环境，第一次令 `install=false`，只构建和推送经过测试的镜像。
3. 按既有数据卷流程初始化**新隔离卷**，挂载后只有 `.workbench-data`。不要先运行 V1：它会建立旧格式数据库，V2 将拒绝直接接管。
4. 从选定镜像取出随镜像发布的 `deploy` 文件，以同一版本的脚本配置尚未启动的主机：

   ```sh
   sudo deploy/aws/host/configure-v2.sh \
     <CloudFront栈的OriginHost> <https://CloudFront域名> \
     <Cognito的UserPoolId> <ClientId> <https://登录域名>
   ```

   脚本检查卷已挂载、服务已停止、数据为空或已是 V2，写入 V2 模式和 OIDC 设置，删除 V1 登录设置，设置 `WORKBENCH_BIND_ADDRESS=0.0.0.0`。`WORKBENCH_PUBLIC_HOST` 是源站私有 DNS；浏览器 CSRF 的 Origin 是 CloudFront HTTPS 地址。主机的入站仍由独立 CloudFront 栈限制。
5. `fetch-secret.sh` 用主机角色向 Cognito 读取该应用客户端密钥，保存到 `/run/workbench/oidc_client_secret`（0400，容器 uid 10001）。不打印密钥、不把密钥作为参数，V2 不读取所有者 Basic 登录密钥；取不到密钥则配置失败。
6. 使用现有 `workbench-release` 按镜像 digest 安装。安装器在 V2 模式读取 `web_v2.V2_DATA_FORMAT`，检查 V2 模块和配置。只有真正空的初始数据卷可跳过数据库完整性检查；已使用的 V2 卷必须通过 `v2_backup.py verify`。V1 数据必须先显式迁移。
7. 安装成功需要同时满足：`GET /healthz` = 200；未登录 `GET /` = 303，跳往 `/login?return_to=/`；`GET /api/me` = 401。鉴权检查不通过则停止 V2，只有能安全读取当前数据的上一个发行版才可恢复；不会回到 V1 登录。
8. 再按 §4 配置**最多两个**合成试运行账号，完成 §5 的真实登录及隔离检查后才扩大人数。初始不配置 DeepSeek secret，不迁移真实资料。

### 备份与恢复

- `.workbench-format=2` 时，主机 `backup.sh` / `restore.sh` 使用 `v2_backup.py`。新空卷以显式 V2 模式选择 V2 恢复；已有格式与显式模式不一致时拒绝。
- 备份沿用短暂停服流程，归档后恢复原服务状态；只有临时恢复副本和校验均通过才更新“备份成功”时间。现行数据和删除账本只读挂载到检查容器。
- V2 恢复在读取现行删除账本**之前**停止服务，覆盖停服过程中完成的删除，并一直保持停止直到目录交换结束；失败时保留原数据并恢复原服务状态。主机维护锁不能替代这个停服步骤，因为应用内删除账号不持有主机锁。
- 恢复始终要求 `/data/deleted-accounts.jsonl`，不会自动使用 `--without-live-ledger`。新隔离恢复环境必须先从可信现行来源取得账本副本；缺失或损坏时停止恢复。
- 每次删除后把账本另存 S3 尚未实现；数据卷整体丢失且拿不到备份后的现行账本时，仍需要人工对照删除记录，不能承诺自动避免账号恢复。

`test-host-scripts.sh` 保留 V1 回归；`test-v2-host.sh` 检查 V2 配置、密钥、安装和鉴权失败；`test-v2-backup-host.sh` 用真实合成 SQLite 归档验证恢复，包括停服期间的删除与各类失败。CI 另外在 Linux 容器运行 V2 负载、中断、迁移和真实 Chromium 中英文 PDF 检查。这些证据不替代 AWS 上的演练。

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

模板要点：只允许管理员建用户（无自助注册），邮箱为用户名且大小写不敏感，授权码流程 + `openid email`，回调 `${PublicOrigin}/auth/callback`、退出 `${PublicOrigin}/signed-out`，ID/访问令牌 15 分钟（应用自己的服务端会话最长 12 小时、空闲 2 小时）。应用另有注册开关与账户上限，所以即便以后放开 Cognito 自助注册，未开启应用注册时新身份也进不来。模板已通过 `cfn-lint`，并在隔离试运行栈中创建成功；创建成功不等于真实登录验收通过。

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
