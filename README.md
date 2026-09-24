# 岗位匹配与申请材料审核工作台

当前命令行流程在本机 SQLite 中保存带版本、分类和标签的候选人事实；从 Greenhouse 搜索并获取 JD；确认 JD 要求后按需检索少量事实候选；最后由用户绑定双侧原文并生成待审核建议。仓库不包含真实候选人资料。

架构、业务不变量和当前限制见 [development.md](development.md)。

## 环境

需要 Python 3.10 或更新版本，不需要安装第三方依赖。TypeSafe/Jev 仅用于可选的 JD 候选语义判断；普通离线流程不需要 API key。

## 完整流程

### 1. 建立并确认候选人事实

每个事实必须选择一个粗分类；`--tag` 可以重复，记录用于检索的技能、领域和资格词。

```sh
python3 facts.py add \
  --type project \
  --tag Python \
  --tag FastAPI \
  --tag backend \
  --text "使用 Python 和 FastAPI 完成后端课程项目。"

python3 facts.py list
python3 facts.py confirm fact-xxxxxxxxxxxx@1
```

支持的分类：`project`、`experience`、`skill`、`education`、`eligibility`、`availability`、`achievement`、`other`。

事实较多时可从一个 JSON 文件批量导入，格式见 [examples/synthetic_facts_import.json](examples/synthetic_facts_import.json)：每条必须有 `type` 和 `text`，`tags` 和 `id` 可选，其他字段会被拒绝。

`id` 是自定义的可读 ID（如 `fact-usc-coursework`，只能用 `fact-` 开头的字母、数字和连字符），简历 profile 用它引用事实，见 [examples/synthetic_cv_facts.json](examples/synthetic_cv_facts.json)。带 `id` 的条目：ID 不存在则新建；内容未变则不改动；内容改变则为该事实新建一个 `pending` 版本，需要重新确认。因此可以直接修改 JSON 文件后重新导入。新 ID 的内容若与已有事实完全相同会被拒绝，以免同一事实出现两份。

```sh
python3 facts.py import .local/my-facts.json
python3 facts.py confirm fact-aaaaaaaaaaaa@1 fact-bbbbbbbbbbbb@1
```

导入会先校验全部条目，任何一条无效则整批不写入；全部新事实都是 `pending`，重复导入同一内容不会产生重复事实。导入输出的 `next_step` 会列出可确认的 `FACT_ID@VERSION`，请逐条核对后只保留确实无误的 ID。一次确认多个版本时，任何一个不是当前版本就全部不确认。不带 `id` 的条目不会修改已有事实；修改请用 `revise` 或带 `id` 重新导入。个人事实文件请放在 `.local/` 下，不要提交。

修改文本、分类或标签会创建新的 `pending` 版本；没有提供的字段会沿用当前版本：

```sh
python3 facts.py revise fact-xxxxxxxxxxxx --text "更新后的事实原文"
python3 facts.py revise fact-xxxxxxxxxxxx --type experience --tag Python --tag backend
python3 facts.py revise fact-xxxxxxxxxxxx --clear-tags
python3 facts.py list --history fact-xxxxxxxxxxxx
python3 facts.py confirm fact-xxxxxxxxxxxx@2
```

单个事实仍可使用旧写法 `confirm fact-xxxxxxxxxxxx --version 2`。

创建 v2 后，已确认的 v1 会保留为历史，但不能再用于新匹配。文本、分类和标签作为一个完整版本共同确认。

### 2. 获取岗位 JD

任何来源的岗位（LinkedIn、公司官网、国内招聘网站等）都可以把 JD 纯文本粘贴进来：

```sh
python3 job_search.py paste \
  --file .local/acme-jd.txt \
  --title "Software Engineer Intern" \
  --company "Acme" \
  --url "https://jobs.example.com/123" \
  --output .local/acme-input.json

# macOS 也可以直接读取剪贴板
pbpaste | python3 job_search.py paste --file - --title "后端开发实习生" --output .local/xx-input.json
```

不提供 `--url` 时来源记为“未知”。`captured_at` 是粘贴时间，不是官方发布时间；粘贴内容也不能证明岗位仍开放。`--output` 与 `select` 生成的审核输入格式相同。

Greenhouse 招聘板可以直接搜索。搜索可以使用事实库中当前已确认版本的标签进行本地词面排序；不会把候选人事实发送给 Greenhouse。排序按命中的不同标签数量，同一标签被多条事实使用只算一次。排序时先只读标签和事实 ID，最终只为展示中命中的事实读取原文；每个岗位最多展示 20 条证据。接口超时、连接失败、429 或 5xx 会自动重试一次；404 通常表示招聘板标识或岗位 ID 有误。

```sh
python3 job_search.py search \
  --board duolingounirecruitment \
  --facts-db .local/workbench.db \
  --title Intern

python3 job_search.py select \
  --board duolingounirecruitment \
  --job-id 实际岗位ID \
  --output .local/review-input.json
```

`select` 会再次读取选中岗位，并只生成 JD 快照。它不会提前复制全部事实；岗位已消失或接口失败时也不会使用旧内容冒充最新。

### 3. 提取并确认 JD 要求

```sh
python3 requirement_flow.py propose \
  .local/review-input.json \
  --output .local/review-candidates.json

python3 requirement_flow.py decide \
  .local/review-candidates.json \
  --confirm req-xxxxxxxxxx \
  --exclude req-yyyyyyyyyy \
  --output .local/review-decided.json
```

`propose` 从已知要求章节复制 JD 原文，所有候选从 `pending` 开始。可识别常见英文标题（`Requirements`、`Minimum Requirements`、`You have`、`Nice to have` 等）和中文标题（`岗位要求`、`任职要求`、`职位要求`、`加分项` 等），也能处理 `**…**`、`【…】`、`一、`、`1.` 等装饰或编号，以及 `任职要求：熟悉 Python` 这种同一行写内容的情况。`decide` 只确认或排除要求，不再承担事实关联；候选 ID 根据原文稳定生成。

规则漏掉的要求可以手动补充，但必须是 JD 中的确切原文（可以是一行中的一部分）；补充的候选同样从 `pending` 开始，需要再用 `decide` 确认：

```sh
python3 requirement_flow.py add \
  .local/review-candidates.json \
  --text "Comfortable with SQL and Linux" \
  --output .local/review-candidates-2.json
```

不在 JD 中的文字、已存在的候选以及已进入事实匹配阶段的文件都会被拒绝。

若要使用 Jev 补充“是否是申请人要求、类别、required/preferred 强度”判断，可在 zsh 中隐藏输入 API key：

```sh
read -s "TYPESAFE_API_KEY?TypeSafe API key: "
export TYPESAFE_API_KEY
python3 requirement_flow.py propose \
  .local/review-input.json \
  --typesafe \
  --output .local/review-candidates.json
unset TYPESAFE_API_KEY
```

该请求只发送 JD 和要求候选，不发送候选人事实。Jev 的输出仍是待核对信号，不能自动确认要求。不要把 key 写进命令、JSON 或仓库文件。

### 4. 检索事实候选并人工绑定

```sh
python3 matching.py propose \
  .local/review-decided.json \
  --facts-db .local/workbench.db \
  --limit 10 \
  --output .local/review-matches.json

python3 matching.py decide \
  .local/review-matches.json \
  --facts-db .local/workbench.db \
  --link req-xxxxxxxxxx=fact-xxxxxxxxxxxx \
  --no-match req-yyyyyyyyyy \
  --output .local/review-linked.json

python3 review.py .local/review-linked.json
```

检索先使用要求的类别信号缩小事实类型，再使用版本化标签寻找相关候选。英文标签只以英文字母、数字为边界，因此 `熟悉Python` 也能命中 `Python`；中文标签使用子串匹配。只有一两个字母/数字的短标签（如 `Go`、`R`、`C`、`AI`）区分大小写且不能紧挨 `&`，单字母标签也不能紧挨 `-`，以避免 `go to market`、`R&D`、`C-suite` 误命中；句首的 `Go` 等仍可能误命中，可改用 `Golang` 这类更具体的标签。输出只复制用户最终选择的事实，不复制整个事实库。若事实从候选生成后被修改、失去确认或不再是当前版本，`decide` 会拒绝旧候选并要求重新检索。

`--no-match` 是明确的“当前没有支持事实”，审核结果会保留未知。检索顺序和候选数量不是匹配分数、资格结论或录取概率。

### 5. 生成简历 PDF

简历由两部分组成：`.local/cv-profile.json` 保存姓名、联系方式、学校/公司/职位/日期等版面信息和各条目引用的事实 ID（格式见 [examples/synthetic_cv_profile.json](examples/synthetic_cv_profile.json)）；每一行正文逐字来自事实库中当前已确认的事实版本。文字字段可以是普通字符串（各语言相同），也可以是 `{"en": ..., "zh": ...}`；中文留空时回退英文，并在输出的 `language_fallbacks` 中列出。

```sh
python3 cv.py draft --profile .local/cv-profile.json --language en --output .local/cv-draft-en.json
python3 cv.py pdf .local/cv-draft-en.json --output .local/cv-en.pdf

python3 cv.py draft --profile .local/cv-profile.json --language zh --output .local/cv-draft-zh.json
python3 cv.py pdf .local/cv-draft-zh.json --output .local/cv-zh.pdf
```

- 英文默认 US Letter，中文默认 A4，可用 `--paper` 修改。`--job .local/review-linked.json` 会把与该岗位要求关联的事实排在各条目前面，不增删内容。
- 引用了未确认事实时 `draft` 拒绝执行，并列出需要确认的 `FACT_ID@VERSION`。`pdf` 会再次核对：事实被修改、草稿被手工改写都会被拒绝，需要重新生成草稿。
- PDF 由本机 Google Chrome 无界面打印（找不到时可设置 `CHROME_PATH`），HTML 中所有文字都经过转义并禁止任何网络加载。超过一页会给出提示。
- 目前所有 PDF 都带“DRAFT / 草稿”水印；批准流程和 DeepSeek 按岗位改写尚未实现。
- 输出文件都是新建，不会覆盖已有文件。

## 数据与限制

- `.local/workbench.db`、运行 JSON、`.env` 和个人数据均被 Git 忽略。
- 所有生成命令只新建 JSON 文件，不覆盖已有文件。
- SQLite schema 当前为 v2；程序会保留数据并自动把 v1 迁移到 v2，旧事实分类为 `other`、标签为空。
- `search --profile` 仍保留给合成样例，其 `confirmed` 字段只是输入声明；实际使用推荐 `--facts-db`。
- 岗位来源目前只有 Greenhouse 搜索和手动粘贴；Lever、Ashby 等尚未接入。要求提取仍是固定标题规则，未知标题需用 `add` 手动补充。
- TypeSafe 当前只判断 JD 要求；把候选人事实发送给外部模型尚未授权或实现。
- 当前没有网页界面、材料批准记录、AI 改写、自动投递或录取概率预测；简历 PDF 只有草稿版本。

运行全部测试：

```sh
python3 -m unittest -v
```
