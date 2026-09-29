# 岗位匹配与申请材料审核工作台

[English](README.md) | 简体中文

目的：帮助候选人针对每个岗位，用自己已确认的真实经历拿出最好的一版简历（先放什么、删什么、怎样按岗位的说法改写），而不是评判候选人是否符合岗位。系统不会加入事实以外的内容，最终简历由用户审核批准。

本机 SQLite 保存带版本、分类和标签的候选人事实；岗位来自 Greenhouse、Lever、Ashby 公开招聘板或手动粘贴的 JD。网页是主要入口；命令行用于开发和测试。仓库不包含真实候选人资料。

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

`id` 是自定义的可读 ID（如 `fact-uni-coursework`，只能用 `fact-` 开头的字母、数字和连字符），简历 profile 用它引用事实，见 [examples/synthetic_cv_facts.json](examples/synthetic_cv_facts.json)。带 `id` 的条目：ID 不存在则新建；内容未变则不改动；内容改变则为该事实新建一个 `pending` 版本，需要重新确认。因此可以直接修改 JSON 文件后重新导入。新 ID 的内容若与已有事实完全相同会被拒绝，以免同一事实出现两份。

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

Greenhouse、Lever、Ashby 招聘板可以直接搜索（`--provider` 默认 `greenhouse`）。搜索可以使用事实库中当前已确认版本的标签进行本地词面排序；不会把候选人事实发送给 Greenhouse。排序按命中的不同标签数量，同一标签被多条事实使用只算一次。排序时先只读标签和事实 ID，最终只为展示中命中的事实读取原文；每个岗位最多展示 20 条证据。接口超时、连接失败、429 或 5xx 会自动重试一次；404 通常表示招聘板标识或岗位 ID 有误。

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

Lever 用 `--provider lever --board palantir`，Ashby 用 `--provider ashby --board openai`；招聘板标识就是 `jobs.lever.co/<标识>`、`jobs.ashbyhq.com/<标识>` 或 `boards.greenhouse.io/<标识>` 里的那一段。

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
- 未批准的草稿导出的 PDF 都带“DRAFT / 草稿”水印；批准流程见本节末尾。
- 输出文件都是新建，不会覆盖已有文件。

按岗位改写或翻译（DeepSeek）：

```sh
python3 cv.py tailor .local/cv-draft-zh.json \
  --job .local/review-linked.json \
  --output .local/cv-tailored-zh.json
python3 cv.py pdf .local/cv-tailored-zh.json --output .local/cv-tailored-zh.pdf
```

- API key 依次从环境变量 `DEEPSEEK_API_KEY` 和 macOS 钥匙串（service `deepseek-api-key`）读取，不写入任何文件。存入或更换：`security add-generic-password -U -a "$USER" -s deepseek-api-key -w`（会提示输入且不显示）。
- 只发送教育细节、经历和项目要点、技能行的原文，以及岗位标题和已确认的要求；不发送姓名、联系方式、学校和公司名称、论文。
- 每一行对应一条事实。改写中出现事实里没有的数字、技术或技能词、“主导/负责/led/managed”等更强的表述、链接，或者格式不对，都会被拒绝；该行保留事实原文，拒绝原因写在命令输出和草稿中。翻译可以用另一种语言表达原句已有的词（如 frontend → 前端、API → 接口）。这些检查只看词面，不能证明语义完全一致，导出前仍需逐行核对。
- `--job` 可以省略，此时只翻译和润色。默认模型 `deepseek-flash`、`--effort low`，可用 `--model deepseek-v4-pro` 或 `--effort high` 调整。

审核、批准与最终 PDF：

```sh
python3 cv.py approve .local/cv-tailored-zh.json --output .local/cv-approved-zh.json
python3 cv.py pdf .local/cv-approved-zh.json --output .local/cv-final-zh.pdf
```

- 先用带水印的 PDF 逐行核对。`approve` 会再次核对全部事实，列出 DeepSeek 改动过的每一行（原文 → 改写），并在新文件中记录批准时间和内容指纹。
- 只有已批准、且批准后内容没有任何改动的文件才能导出无水印的最终 PDF。批准后改动任何内容（包括姓名、日期），或引用的事实被修改，最终导出都会被拒绝，需要重新生成草稿并重新批准。
- 已批准的文件不能再改写，也不能重复批准。

## 网页界面

网页版需要项目内的虚拟环境（只需安装一次，不会安装到全局）：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

启动后在浏览器打开 http://127.0.0.1:8765/ ，按 Ctrl+C 停止：

```sh
.venv/bin/python web.py
```

- **Facts**：上传 PDF 简历，核对姓名和联系方式后保存。每一行成为一条待确认事实（与已有事实完全相同的行直接复用），简历的结构成为你的 profile（旧 profile 先备份到 `profile-history/`）。姓名、邮箱、电话和链接只在本机读取，不发给 DeepSeek；DeepSeek 按行号看其余各行，只判断哪些是标题、条目和要点，文字由程序照原文复制。然后勾选并一次确认多个 pending 版本。全部确认后自动转到 Find jobs。
- **Find jobs**：关注公司的全部公开岗位按“提到你多少个已确认技能标签”排序，最多的在前；同分时较新的在前。同一职位在多个城市发布只显示一行。可按标题、地点筛选，可勾选“Hide senior roles”（隐藏 Senior、Staff、Principal、Lead、Manager、Director 等标题；“Member of Technical Staff” 保留）。点 **Start** 会重新读取该岗位并新建到 My jobs；同一岗位再点只会打开已有的那一个。
- **My jobs**：已开始的岗位；也可以粘贴任意来源的 JD 新建岗位。
- **每个岗位**（Start 或粘贴后自动完成，2026-09 实测约 5 秒）：
  1. **What this job asks for**：DeepSeek 按行号挑出 JD 中的要求行，程序逐字复制原文，因此不会编造要求；全部自动计入。每行标明岗位写的是必需、加分还是未说明，以及是自动计入还是经你审核；保存你的审核后整份列表标为已审核。可以把某行改为 “Not a requirement”、补充漏掉的原文或让 DeepSeek 重新找；保存后简历自动重新准备。
  2. **Your CV for this job**：简历只用你的简历本身所用的语言（按 profile 中姓名填写的语言判断；只填英文名就只有英文简历，中文 JD 也用英文简历）。自动生成草稿、按岗位措辞改写（每行通过事实检查：新增数字、技术词或领导类用词，把数字挪到别的对象上，去掉 “not”“prototype”“helped”“in progress” 等否定或限定，或加入 “production”“customers” 等范围词的改写都会被拒绝）、再由 DeepSeek 提出结构调整：栏目顺序、项目和条目顺序、删去对这个岗位没有帮助的条目。每项改动都列出理由，可以单独撤销或恢复，不需要再调用 DeepSeek。勾选“已阅读简历和全部改动”后批准，再生成并下载最终 PDF。批准只针对页面上显示的那一份简历：如果期间另一个标签页或窗口改动了它，什么都不会被批准，页面会显示当前的简历，请重新阅读。另一种语言可一键准备。
  3. **What this CV shows for each requirement**：DeepSeek 把每条要求与你的已确认事实、这份简历实际显示的文字（改写过的行按改写后的文字判断）以及各条目的职位或学位和日期逐一比对；每行与所属条目一起发送，年限只按该条目的日期计算。只是“相关”的行不算证明了这条要求，技能词相同也不算。每条要求得到一个状态，按简历当前显示的内容计算，所以撤销一处删减会立即改变结果：
     - **Shown**：简历上有一行（或几行合起来）说明了它。
     - **Left out**：你的事实能说明它，但这份简历删去了那一行、改写后不再说明它，或根本没有收录。删减或改写可以就地撤销（“Put it back”“Use your own wording”，或两者一起）。
     - **Related only**：有相关的行，但没有完整说明，并写出缺少什么（如 “3+ years”）。
     - **No evidence**：没有任何已确认事实写到它；这不等于你不具备。
     - **Not checked**：DeepSeek 没能回答，在得到回答之前不算作已显示。

     对 Related only 和 No evidence 的要求，DeepSeek 最多建议一处补充：给某行技能加上工具名，或在某段经历/项目下加一行。**只有你点 “True for me” 才会加入**：技能会成为该技能行的新确认版本，新的一行会成为新确认事实并写入 profile（旧 profile 先备份到 `profile-history/`），然后重新准备简历；点 “Not true” 则不会进入简历，之后重新检查时同一建议仍保持为不属实。年限、资深程度、学历、个人特质、质量或规模（如 efficient、real-time、large-scale），或需要单独项目才能说明的经验（如某种模型架构、研究方向）不给建议；DeepSeek 被要求只在已有相关工作的条目下建议新的一行、不改写你已有的行，每条新行旁边会列出该条目已有的行；含数字、领导类用词，或与简历已有行几乎相同（只改了一两个词）的建议会被丢弃。建议只用于生成它时的那段经历或那行技能：之后若已改变（例如上传了新简历），会被拒绝并提示重新检查。也可以自己写（或先修改建议，“Write your own line”）：选一行技能或一段经历/项目，写好后点 “Add to my CV”。它按你写的原样作为已确认事实加入（数字也可以，因为是你自己的话）；只有为空、简历里已经有、或它的位置已改变时才会被拒绝。已确认事实或简历文字变化后会自动重新检查；删减、排序和撤销不需要重新检查。

说明：

- 关注的公司和下载的岗位保存在 `.local/listings.db`（与事实库分开；删除它只会丢掉公司列表和下载的岗位）。第一次创建时预置 `starter_boards.json` 里的 30 家公司，之后删掉的公司不会自动回来。在页面下方 Companies 里粘贴 `boards.greenhouse.io/…`、`jobs.lever.co/…` 或 `jobs.ashbyhq.com/…` 链接即可添加公司。
- 打开 Find jobs 时，超过 24 小时未更新的公司会自动重新下载（每次 4 家并行，2026-09 实测 30 家约 6 秒）；没有后台定时任务。只向公开招聘板发送不含个人信息的 GET 请求；排序完全在本机进行。
- 排序数字是词面计数，不是匹配度或录取概率。实测 “AI” 出现在 79% 的岗位里，所以数字只适合比较先后。技能标签与岗位的匹配结果会保存在 `listings.db`，只有岗位文字或已确认技能变化时才重新计算（约 9 千个岗位首次约 5 秒，之后约 0.05 秒）。
- 每个岗位保存在 `.local/jobs/<岗位编号>/`，每一步一个文件。重做某一步时，这一步和之后的文件会移到该岗位的 `history/`，不会被覆盖或删除。简历只依赖要求（`decided`），与 talking points 无关，所以生成 talking points 不会影响简历。每个文件要么完整写入，要么完全不写。替换某一步时，新文件写好才算完成；如果工作台在那之前停止，下次启动会把旧文件放回原处，并在岗位页面上说明。会写好几步的操作（准备简历：草稿、改写、调整）会保留已经完成的步骤，页面显示它做到了哪一步。如果保存上传的简历、或添加你确认的一行没有完成（工作台停止，或保存到一半出错），Facts 页面或岗位页面会一直说明，直到你用同一份 PDF 或同样的文字重做，或点 Dismiss；重做是安全的，不会重复添加。崩溃是在每个时间点强行终止真实进程来测试的；每次写入都会刷到磁盘，但没有测试过断电。
- 对事实、简历 profile 和岗位的改动一次只进行一个，所以两个标签页不会同时写同一个文件；一个改动如果等另一个超过 90 秒（DeepSeek 可能需要一分钟），会被拒绝并提示“请重试”。关注或刷新公司、上传简历都不需要等它们；读取只在文件正被移动的那一刻稍等。
- 结构调整的限制：教育经历始终保留且位置不变；工作经历保持时间顺序且每段至少保留一行；只能排序和删去已有行，不能新增或跨条目移动。简历文件保存全部行，撤销任何一项改动只是换一种显示方式。
- 所有 DeepSeek 请求都用 temperature 0。即便如此，边界要求（如 “architect distributed systems”）在不同次检查中仍可能一次判为 Related only、一次判为 Shown；检查结果按岗位保存，只有事实或简历文字变化、或点 “Check again” 时才重新判断。
- 发给 DeepSeek 的只有：JD 原文行、简历各行文字（已确认事实或其改写）、各条目的职位或学位及日期（用于要求检查）和岗位要求；不发送姓名、联系方式、学校、公司、项目名称或事实 ID（事实 ID 可能由原文生成，各行改用 L1 这样的临时编号）。DeepSeek 不可用时退回标题规则提取要求，简历停在最后成功的一步并提示可重试。
- 只接受发往 127.0.0.1/localhost 的请求；所有接口都需要页面启动时生成的随机令牌（预览和下载链接把同一令牌放在地址里）。其他网站无法读取你的事实，也无法触发 DeepSeek 调用。
- 可用 `--facts-db`、`--jobs`、`--profile`、`--port` 修改路径和端口；`listings.db` 放在事实库所在目录。
- 网页测试需要虚拟环境：`.venv/bin/python -m unittest`；直接用 `python3` 运行时网页测试会自动跳过。

## 在容器中运行

同一个网页也能在 Linux 容器中运行（linux/amd64，即将来在 AWS 主机上的运行方式；Apple Silicon 的 Mac 上靠模拟运行，会慢一些）。数据绝不会进入镜像，而是保存在宿主机上的一个目录中，挂载到 `/data`：

```sh
mkdir -p ~/workbench-data && touch ~/workbench-data/.workbench-data
WORKBENCH_DATA_DIR=~/workbench-data docker compose up -d --build
```

打开 http://127.0.0.1:8765/；`docker compose down` 会停止它。该目录保存事实、简历 profile 和岗位，新容器会读取它们。`.local/` 的布局相同，也可以直接用作这个目录（先在其中放一个 `.workbench-data` 文件，并停止 `web.py`：同一时间只运行一个服务）。

- 端口仅发布在宿主机的回环地址上。Host 检查和令牌检查与上文相同。
- 容器以 uid 10001 运行，该用户必须能写入该目录（在 Linux 上：`sudo chown -R 10001:10001 <folder>`）。
- 设置 `WORKBENCH_REQUIRE_DATA=1` 时，如果目录中没有 `.workbench-data` 文件，应用会拒绝启动，因此缺失数据卷时绝不会启动一个空工作台。
- DeepSeek 密钥来自宿主机上的文件：`DEEPSEEK_KEY_FILE=<file>`。Compose 将其挂载为 secret；密钥绝不会出现在镜像或环境变量中。没有它时，DeepSeek 相关步骤会失败并给出明确提示。
- Chromium 用 Liberation Sans（字宽与 Arial 相同）和 Noto Sans CJK 打印简历，并保留沙箱：compose 使用 `deploy/seccomp-chromium.json` 运行容器，即 Docker 默认的 seccomp 配置，再允许沙箱创建的用户、PID 和网络命名空间，其他种类一律不允许。
- `GET /healthz` 无需令牌即可返回 `{"status": "ok"}`。访问日志只记录请求方法、路径、状态码和耗时，绝不记录查询字符串（预览和下载链接携带令牌）。
- `deploy/smoke.py` 使用合成数据、脚本化的 DeepSeek 替身和真实 PDF 将整个工作流运行一次；CI 在每个 pull request 中都会在容器里运行它。它会上传简历并确认事实，所以拒绝任何已有数据的目录：只能给它一个空的临时目录，绝不能用你的真实数据目录。

## 数据与限制

- `.local/workbench.db`、运行 JSON、`.env` 和个人数据均被 Git 忽略。
- 所有生成命令只新建 JSON 文件，不覆盖已有文件。
- SQLite schema 当前为 v2；程序会保留数据并自动把 v1 迁移到 v2，旧事实分类为 `other`、标签为空。
- `search --profile` 仍保留给合成样例，其 `confirmed` 字段只是输入声明；实际使用推荐 `--facts-db`。
- 岗位来源是 Greenhouse、Lever、Ashby 的公开招聘板和手动粘贴；LinkedIn 等需要登录的网站不采集，也不做全网岗位发现。中国公司的官网岗位请粘贴。
- 网页先用 DeepSeek 挑选要求行，失败时退回标题规则；命令行 `propose` 只用标题规则（`section-lines-v3`）。按 2026-09 下载的 8,787 个岗位统计，标题规则找不到任何要求行的比例：Greenhouse 9%、Ashby 7%、Lever 1%；这些岗位可用 `add` 或网页的 “Add missed requirement” 手动补充原文。
- TypeSafe 只判断 JD 要求。经用户同意，DeepSeek 会收到已确认事实的文字（用于改写、结构调整、匹配和缺口建议），但不会收到姓名、联系方式、学校、公司、项目名称或事实 ID。
- 当前没有投递记录、自动投递或录取概率预测；网页中还不能编辑事实原文（请用命令行）。地点筛选只是“包含文字”，还没有“只看美国”这类按国家的筛选。

运行全部测试：

```sh
python3 -m unittest -v
```
