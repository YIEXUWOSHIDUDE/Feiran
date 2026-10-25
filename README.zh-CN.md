# Feiran · 斐然

从个人资料和岗位描述出发，生成岗位简历、逐条修改内容，并通过可撤销的内容取舍整理为一页。

> What oft was thought, but ne’er so well express’d.

[English](README.md) | 简体中文

一个开源简历工作台，支持中英文资料、岗位来源和签证担保提示、局部编辑与 PDF 导出。可以使用已有云端账号，也可以在自己的电脑上运行。

![Feiran 斐然 — 相关内容，清晰表达，更明亮的下一步](web/feiran-brand.png)

## 相关内容，清晰表达，更明亮的下一步

- **理解岗位**：提取岗位描述中的要求，核对遗漏和误判。
- **呈现相关经历**：结合个人资料和 AI 补充内容，为每个岗位准备简历。
- **调整措辞与结构**：查看 AI 对表达、排序和取舍的建议。
- **局部修改**：逐条采用、编辑、撤销和重做，不重新生成整份简历；需要复用时再保存到个人资料。
- **整理为一页**：按实际 PDF 页数省略关联较弱的内容，可撤销，字号不缩小。
- **导出简历**：审核批准后，下载最终 PDF。

*还是你，表达更出彩。*

## 云端使用

**已有云端访问权限：直接打开你的 Feiran HTTPS 地址并登录，无需在电脑上安装 Python、下载代码或配置本地密钥。** 当前 V2 实例仅向已配置账号开放，不开放自助注册。登录后按下方“如何使用”操作即可。

希望在自己的电脑上运行？本页保留完整的[本地部署（可选）](#本地部署可选)说明；这不是使用云端工作台的前置步骤。

V2 工作台已部署到 AWS，使用 HTTPS 和 Cognito 登录。四轮流程改进及一页整理已包含在 2026 年 10 月 23 日部署的[版本 `4c595a7`](https://github.com/YIEXUWOSHIDUDE/Feiran/actions/runs/40027427699) 中。英文线上实测完成实际 PDF 两页变一页、撤销重做、刷新保存和浏览器下载；检查的 PDF 字号不变，无截断或重叠。中文一页整理尚未完成线上页面验收。详见[开发记录与验收范围](docs/development.md)；较早的部署文档记录的是历史版本。

### 访客工作区（默认关闭）

运维可以开启访客工作区（`python -m feiran.v2_admin set guest_enabled 1`）。开启后，公开欢迎页除登录外还提供一键进入的独立工作区，内含合成示例简历和示例岗位。访客可以查找岗位要求，用 DeepSeek 准备、润色和调整简历，对照岗位核验，也可以编辑、撤销和重做、压缩为一页、批准并下载 PDF。AI 用量按每次调用计量，受两项额度限制：每个工作区一次性的额度（默认 100,000 token，不会补充），以及所有访客共用的每日额度（默认每个 UTC 日 1,000,000 token）。额度用完后仍可编辑、压缩和下载。访客工作区没有时间限制：在访客结束它或清除本站 Cookie 之前，同一浏览器都能重新打开；它不会保存在备份中。上传简历、添加新岗位和保存到个人资料需要账号。限制、配置、迁移与回退见[访客工作区](docs/guest-workspaces.md)。

## 如何使用

以下步骤对应已部署的 V2 流程；可选本地 V1 模式的差异单独注明。

完整功能清单、操作说明和后续计划见[项目计划书与功能使用指南](docs/project-plan.md)。

获得可用工作台的访问权限后：

1. **导入经历。** 上传 PDF 时选择中文或英文，两份简历分别保留。保存提取的内容后即可使用；编辑会保存可直接使用的新版本，原文保留在历史中。
2. **选择岗位。** V2 在“我的岗位 → 新增岗位”输入公开岗位 URL 后，先读取并查看岗位，也可以粘贴正文或浏览关注公司的岗位。列表与详情展示签证担保（Sponsor）的支持、不支持、有条件、未知和矛盾状态，展开可核对原文、来源及获取时间；默认保留未知岗位。Greenhouse、Lever、Ashby 和腾讯链接使用公开接口；其他网站读取网页，优先提取 JobPosting 结构化数据。核对标题、正文及与你个人资料对应的技能词；点击“准备简历”才提取要求并生成待审核材料。查看岗位不调用模型，也不占生成额度。V1 本地模式仍保留“读取并生成”。登录、验证码、仅由 JavaScript 加载正文或内容不完整的页面可能需要手动粘贴。重复输入已保存的链接会打开已有岗位。
3. **编辑岗位简历。** 需要对照当前简历与岗位要求时，点击“立即检查”。“AI 建议填写”会生成完整句子或技能项；采用或自行填写后，直接加入当前岗位简历。在“编辑这份简历”中调整单条内容；需要在其他岗位复用时，单独点击“保存到个人资料”。
4. **按需要整理为一页。** 点击“压缩为一页”，查看被省略的内容，可撤销恢复。如果受保护内容导致无法整理为一页，系统会说明原因并保留原稿。
5. **检查页数、批准并导出。** 点击“检查页数”：Feiran 按最终 PDF 的样子（无水印）实际打印当前版本，并显示页数。批准需要先完成此版本的检查。超过一页时，可点击“压缩为一页”，或勾选接受多页。然后依次点击“批准这份简历”“生成最终 PDF”“下载最终 PDF”。任何编辑、压缩或重新准备都会产生新版本，需要重新检查页数并批准。下载文件名包含公司、岗位、语言、岗位短 ID 和版本，例如 `CV-示例公司-后端开发实习生-ZH-1a2b3c4d-v5e6f7a8b.pdf`；保留中文，并提供 ASCII 备用名。将批准改为可选属于后续计划，尚未实现。

在页面顶部切换 **简体中文** 或 **English**，界面会记住你的选择。切换界面语言不会翻译岗位描述、经历事实或简历；可生成的简历语言取决于你的资料。

按链接读取岗位时，如果网页的结构化数据只有简介，Feiran 会改用同一岗位页面上可见的职责和资格正文；页面没有显示这些内容时，会拒绝读取并提示粘贴完整 JD。此前已保存、只有网页简介的岗位，会在任何 AI 步骤之前被拒绝，不会据简介生成内容；请把完整 JD 粘贴为新岗位。这是启发式判断：它只检查是否有职责或资格内容，不能证明每一部分都完整。

上传的 PDF 中的链接会跟随所在文字保留，包括项目或论文标题、副标题和正文条目。同一条目中两行都写着“GitHub”时，各自保留自己的链接；原本没有链接的行不会借用其他行的链接，被省略的行的链接也不会出现在别处。此前保存的资料仍按条目共用链接。AI 改写去掉了带链接的词时，会在同一行末尾补上原标签和链接。

每个岗位可以单独选择已上传的中文或英文简历。重新上传只替换所选语言，旧资料保留在历史中；相关资料或事实更新后，旧稿仍可预览，但需重新准备和审核才能导出。PDF 需有可提取的文字，暂不支持扫描件识字。

中国与其他地区的岗位使用同一个岗位库，可按中国大陆、其他地区或地点未知筛选，地区依据明确的工作地点判断。除上述招聘板外，也支持添加腾讯招聘官网 `https://careers.tencent.com/zh-cn/search.html`：列表保存职责摘要，选择岗位时重新读取完整要求。BOSS、字节、阿里暂未接入自动刷新，可手动粘贴完整描述。缓存仅代表获取时的公开信息，申请前仍需核对官网状态。

## 你的信息，由你决定

- 经历和已保存材料位于 Feiran 实例的私有数据目录。使用云端工作台时，数据保存在云主机；本地运行时，数据保存在自己的电脑。
- AI 功能会向 DeepSeek 发送相关岗位和经历文字。应用过滤已知身份信息，并用临时编号引用经历。过滤不保证匿名，经历文字本身仍可能识别个人。
- 建议可以扩展已保存资料之外的内容。采用哪些内容、批准哪个版本，由你决定。
- 批准绑定你看过的具体版本。内容或相关事实改变后，可能需要重新审核批准。

### 局部编辑与一页整理

采用建议或自行填写只逐字修改当前岗位简历，不再重新准备整份简历。在“编辑这份简历”中可修改单条内容、撤销或重做；需要跨岗位复用时，再点“保存到个人资料”。修改后的简历需要重新批准。

后台轮询、任务完成及当前标签页内切换页面时，未保存文字会保留。刷新或关闭标签页会清除这些草稿。保存冲突时保留输入；可先复制文字，或点“放弃未保存修改”读取最新条目。原条目消失时仍显示可复制的未保存文字。草稿只保存在内存，不写入浏览器持久存储。

一页整理通过省略内容控制页数，不会自动缩短句子或缩小字号。已采用或编辑的条目、用户恢复的内容、必要教育信息，以及经历和技能的最低保留内容受到保护，原始个人资料不被删除。整理使用 Chromium 实际打印，不调用 DeepSeek；无法达到一页时不改动原稿。撤销省略或继续编辑后，页数可能变化，应重新整理或导出检查。部分整栏省略会作为一个改动项恢复。

局部编辑已用真实 DeepSeek 和本地 V2 页面验证；改动已部署，10 月 23 日英文线上实测确认局部内容保存、一页整理、撤销重做和实际 PDF 下载通过。此前客户端阻止下载的观察已不适用于这条实测路径。V1 和跨语言采用仍需补齐完整页面覆盖。

## 当前限制

AI 可能误解岗位要求，也可能改变一句话的含义。最终导出前仍可审核具体内容；生成不再要求逐条真实性确认。

岗位排序与准备前提示依据技能词重合，不代表申请资格或录取概率。Sponsor 使用有限的中英文文字规则，未覆盖的句式保留未知；不使用雇主历史记录推断本岗位政策，也不判断个人签证资格。V2 在保存岗位或读取关注公司时对每份正文检查一次，结果连同检查时间和规则版本一起保存；再次打开页面直接复用。官网重新读取但正文未变时只更新获取时间；正文变化会单独重新检查，已保存岗位保留各自的结果。显示“尚未检查原文”表示当前规则下还没有结果（保存于开始记录结果之前、检查失败或规则已更新），可在岗位页点击“检查担保表述”，或刷新该公司。V1 本地岗位列表也按正文摘要及规则版本保存判断，重复浏览和筛选复用结果；旧库首次读取建立缓存。单个已保存岗位详情会在进程内复用同一正文的判断。两个模式打开岗位都不会自动发起付费匹配检查，需要时点击“立即检查”或“重新检查”。缺少证据不等于你不具备某项能力；保存的岗位描述也不能证明岗位目前仍开放。

Feiran 用于准备和审核材料，不会提交申请、跟踪投递或预测录用结果。

## 本地部署（可选）

仅在希望自行运行 Feiran 时执行以下步骤。云端用户可以跳过本节。本地与云端的数据各自独立，不会自动同步。

### Windows（PowerShell）

先安装 [Git for Windows](https://git-scm.com/install/windows)、[Python Install Manager](https://www.python.org/downloads/windows/) 和 [Google Chrome](https://www.google.com/chrome/)，安装后重新打开 PowerShell。下列步骤使用 Python 3.14；已有该版本时可跳过 `pymanager install 3.14`。Python 安装细节见[官方 Windows 指南](https://docs.python.org/3.14/using/windows.html)。

1. 下载代码并安装依赖：

   ```powershell
   pymanager install 3.14
   git clone https://github.com/YIEXUWOSHIDUDE/Feiran.git
   cd Feiran
   py -3.14 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   这里直接调用虚拟环境中的 Python，无需激活脚本或修改 PowerShell 执行策略。

2. 使用 AI 功能时，在隐藏输入提示中粘贴自己的 DeepSeek API key，按回车保存，再设置文件路径：

   ```powershell
   New-Item -ItemType Directory -Force .local | Out-Null
   .\.venv\Scripts\python.exe -c "from getpass import getpass; from pathlib import Path; Path('.local/deepseek_api_key').write_text(getpass('DeepSeek API key: ').strip(), encoding='utf-8')"
   $env:DEEPSEEK_API_KEY_FILE = (Resolve-Path .\.local\deepseek_api_key).Path
   ```

   输入不会显示，也不会写进命令历史；key 保存在 Git 忽略的本地文件中。不要分享 `.local` 目录。已有 `DEEPSEEK_API_KEY` 环境变量时，应用优先使用该变量。

3. 指定用于 PDF 导出的 Chrome。以下命令检查三个常见安装位置：

   ```powershell
   $chromeCandidates = @(
       "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
       "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
       "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
   )
   $env:CHROME_PATH = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
   if (-not $env:CHROME_PATH) { throw 'Chrome not found. Set CHROME_PATH to the full path of chrome.exe.' }
   ```

   若 Chrome 装在其他位置，手动将 `$env:CHROME_PATH` 设为实际 `chrome.exe` 的完整路径。中文 PDF 导出还需要系统安装支持中文的字体。

4. 在同一个 PowerShell 窗口启动：

   ```powershell
   .\.venv\Scripts\python.exe -m feiran.web
   ```

   打开 <http://127.0.0.1:8765/>；停止服务按 `Ctrl+C`。以后启动时，进入 `Feiran` 目录，重新设置上述 `$env:DEEPSEEK_API_KEY_FILE` 和 `$env:CHROME_PATH`，再运行启动命令；无需重复安装或重新保存 key。

这些说明已对照现有代码和官方安装文档核对，但尚未在 Windows 上完成端到端运行及 PDF 导出验证。

### macOS / Linux

以下命令适用于 macOS / Linux。先安装 Python 3.14（与项目 CI 和容器使用的版本一致），以及用于导出 PDF 的 Google Chrome 或 Chromium。

1. 下载代码并安装依赖：

   ```sh
   git clone https://github.com/YIEXUWOSHIDUDE/Feiran.git
   cd Feiran
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

2. 使用 AI 功能时，配置自己的 DeepSeek API key。创建 `.local/` 目录，用文本编辑器将 key 单独保存到 `.local/deepseek_api_key`（只保存 key），然后设置文件路径：

   ```sh
   mkdir -p .local
   # 保存密钥文件后执行：
   chmod 600 .local/deepseek_api_key
   export DEEPSEEK_API_KEY_FILE="$PWD/.local/deepseek_api_key"
   ```

   `.local/` 已被 Git 忽略。不要提交密钥或简历资料；本地运行的 AI 功能同样会向 DeepSeek 发送相关文字。已有 `DEEPSEEK_API_KEY` 环境变量或已配置 macOS 钥匙串时，也可沿用现有方式。

3. 在同一个终端启动工作台：

   ```sh
   .venv/bin/python -m feiran.web
   ```

   浏览器打开 <http://127.0.0.1:8765/>，按上方流程上传简历并使用。数据默认保存在 `.local/`，停止服务按 `Ctrl+C`。若无法自动找到 Chrome / Chromium，可设置 `CHROME_PATH` 为浏览器可执行文件的完整路径。中文 PDF 导出需要系统安装支持中文的字体。

自行部署到 AWS 的说明见 [AWS 公网部署](docs/aws-public.md)。

## 开发

应用代码和初始公司清单在 [`feiran/`](feiran/)；前端在 [`web/`](web/)；测试在 [`tests/`](tests/)；部署工具在 [`deploy/`](deploy/)；文档在 [`docs/`](docs/)；合成样例在 [`examples/`](examples/)。

离线测试集中在 `tests/`。参见[本地运行、测试命令与开发记录](docs/development.md#本地开发入口)；部署文件位于 `deploy/`，部署说明位于 `docs/`。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
