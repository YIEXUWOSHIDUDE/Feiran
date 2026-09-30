# Feiran · 斐然

> What oft was thought, but ne’er so well express’d.

English | [简体中文](README.zh-CN.md)

An AI-powered workspace that helps you turn real experience into clearer, job-specific resumes. Understand what a role asks for, refine how you present your experience, and review every change before exporting.

![Feiran — real experience, clearer stories, brighter next steps](web/feiran-brand.png)

## Real experience. Clearer stories. Brighter next steps.

- **Understand the role** — identify requirements in the job description and review anything missed or misclassified.
- **Present relevant experience** — use your confirmed facts to prepare a CV for each job.
- **Refine the wording and structure** — review AI suggestions for phrasing, ordering and what to leave out.
- **Stay in control** — inspect changes, undo them individually, and approve the exact version you want.
- **Export your CV** — download the PDF after reviewing and approving it.

*Same you. A stronger story.*

## Use the cloud workspace

**Already have cloud access? Open your Feiran HTTPS address and sign in. You do not need to install Python, download the code or configure a local API key.** The current instance is restricted to its owner; registration is not open. Continue with “Using Feiran” below after signing in.

Prefer running it on your computer? Complete [local setup instructions](#local-deployment-optional) remain on this page. Local deployment is optional and is not required to use the cloud workspace.

A single-owner Feiran workspace is now deployed on AWS with a public HTTPS address and private login. **There is no public demo or open registration.** The owner can open the workspace in a browser; local operation remains optional. Login protection, synthetic English/Chinese PDF export, persistence across test containers, and backup restoration have been verified on AWS. The cloud DeepSeek secret is connected, and a bounded live API check passed. Full resume quality still requires your review. See [AWS public deployment](docs/aws-public.md) and the [acceptance record](docs/aws-acceptance.md) for the tested scope and remaining checks.

## Using Feiran

Once you have access to a running workspace:

1. **Bring your experience.** Choose Chinese or English when uploading a PDF; both CVs are kept separately. Check and confirm the extracted facts. Editing a fact saves a new version that needs confirmation.
2. **Choose a job.** In My jobs → New job, enter any public posting URL and choose Read and generate. Greenhouse, Lever, Ashby and Tencent links use their public APIs; other sites are read as webpages, preferring structured JobPosting data. Feiran reads the title and description, then prepares a CV draft for review. Check the extracted text for completeness. Login, CAPTCHA, JavaScript-only pages and incomplete responses may require manual pasting. Repeating a saved URL opens the existing job. You can also browse followed companies.
3. **Review your tailored CV.** Check the job requirements, proposed wording and layout changes. See which requirements your CV supports, which evidence was left out, and where information is missing. Add suggested experience only when it is true for you.
4. **Approve and export.** Read the CV and every change, approve that version, and download the final PDF. Changed content needs a new review.

Switch between **English** and **简体中文** at the top of the page. The interface remembers your choice; changing the interface language does not translate your job descriptions, experience statements or CV. CV languages depend on the information in your profile.

Choose an uploaded Chinese or English CV for each job. Uploading again replaces only that language and keeps the previous profile in history. After related facts or profile content change, the old draft remains viewable but must be regenerated and reviewed before final export. PDFs need extractable text; scanned documents are not supported.

All regions share one listings database; Mainland China, other regions and unknown-location filters use explicit workplace locations. You can also add Tencent's public board at `https://careers.tencent.com/zh-cn/search.html`. Its list contains responsibility summaries; full requirements are fetched on selection. BOSS, ByteDance and Alibaba automatic refresh adapters are not yet included; paste their full descriptions instead. Cached postings reflect the time they were fetched; check the official page before applying.

## Your information and your decisions

- Your experience and saved materials stay in the private data directory of your Feiran instance. In a cloud workspace, this is on the cloud host; local operation stores it on your computer.
- AI features send relevant job and experience text to DeepSeek. The application filters known identifying details and uses temporary references for experience statements. Filtering does not guarantee anonymity; experience text can still be identifying.
- Suggestions are not new facts. You decide whether an addition is true and which wording to approve.
- Approval belongs to the version you reviewed. Changing the content or relevant facts can require you to review and approve again.

## Current limits

AI can misunderstand a requirement or change the meaning of a sentence. Automated checks catch some unsupported changes, but they cannot verify your real experience or replace your review.

Job ordering is based on skill-word overlap, not eligibility or the probability of an offer. Missing evidence does not mean you lack a qualification, and a saved job description does not prove a role is still open.

Feiran prepares and reviews materials. It does not submit applications, track applications or predict hiring outcomes. Optional semantic review remains an unvalidated experiment and is not part of the normal web workflow.

## Local deployment (optional)

Follow these steps only if you want to run your own copy of Feiran. Cloud users can skip this section. Local and cloud data are separate and do not sync automatically.

### Windows (PowerShell)

Install [Git for Windows](https://git-scm.com/install/windows), the [Python Install Manager](https://www.python.org/downloads/windows/), and [Google Chrome](https://www.google.com/chrome/), then open a new PowerShell window. These steps use Python 3.14; skip `pymanager install 3.14` if it is already installed. See the [official Python Windows guide](https://docs.python.org/3.14/using/windows.html) for installation details.

1. Download the code and install dependencies:

   ```powershell
   pymanager install 3.14
   git clone https://github.com/YIEXUWOSHIDUDE/Feiran.git
   cd Feiran
   py -3.14 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   These commands call the virtual environment's Python directly, so you do not need to activate a script or change PowerShell's execution policy.

2. For AI features, paste your own DeepSeek API key into the hidden prompt and press Enter to save it, then configure the file path:

   ```powershell
   New-Item -ItemType Directory -Force .local | Out-Null
   .\.venv\Scripts\python.exe -c "from getpass import getpass; from pathlib import Path; Path('.local/deepseek_api_key').write_text(getpass('DeepSeek API key: ').strip(), encoding='utf-8')"
   $env:DEEPSEEK_API_KEY_FILE = (Resolve-Path .\.local\deepseek_api_key).Path
   ```

   Input is hidden and stays out of command history; the key is saved in a local file ignored by Git. Do not share `.local`. An existing `DEEPSEEK_API_KEY` environment variable takes precedence over the file.

3. Configure Chrome for PDF export. These commands check three common installation locations:

   ```powershell
   $chromeCandidates = @(
       "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
       "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
       "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
   )
   $env:CHROME_PATH = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
   if (-not $env:CHROME_PATH) { throw 'Chrome not found. Set CHROME_PATH to the full path of chrome.exe.' }
   ```

   For a custom installation, set `$env:CHROME_PATH` to the actual full path of `chrome.exe`. Chinese PDF export also requires a font with Chinese character support on your system.

4. Start Feiran in the same PowerShell window:

   ```powershell
   .\.venv\Scripts\python.exe web.py
   ```

   Open <http://127.0.0.1:8765/>; press `Ctrl+C` to stop. On later starts, enter the `Feiran` directory, set `$env:DEEPSEEK_API_KEY_FILE` and `$env:CHROME_PATH` again as above, and run the startup command. You do not need to reinstall dependencies or save the key again.

These instructions have been checked against the code and official installation documentation. End-to-end operation and PDF export have not yet been verified on Windows.

### macOS / Linux

The commands below are for macOS / Linux. Install Python 3.14 (the version used by CI and the container) and Google Chrome or Chromium for PDF export.

1. Download the code and install dependencies:

   ```sh
   git clone https://github.com/YIEXUWOSHIDUDE/Feiran.git
   cd Feiran
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

2. For AI features, configure your own DeepSeek API key. Create `.local/`, then use a text editor to save only the key in `.local/deepseek_api_key`. Point the app to that file:

   ```sh
   mkdir -p .local
   # After saving the key file:
   chmod 600 .local/deepseek_api_key
   export DEEPSEEK_API_KEY_FILE="$PWD/.local/deepseek_api_key"
   ```

   Git ignores `.local/`. Do not commit keys or resume data. AI features also send relevant text to DeepSeek when running locally. An existing `DEEPSEEK_API_KEY` environment variable or configured macOS Keychain entry can be used instead.

3. Start the workspace in the same terminal:

   ```sh
   .venv/bin/python web.py
   ```

   Open <http://127.0.0.1:8765/> in your browser and follow the workflow above. Data is stored in `.local/` by default. Press `Ctrl+C` to stop the server. If Chrome / Chromium is not detected, set `CHROME_PATH` to the browser executable's full path. Chinese PDF export requires a font with Chinese character support on your system.

For your own AWS deployment, see [AWS public deployment](docs/aws-public.md).

## Development

Offline tests live in `tests/`. See [local setup, test commands and development notes](docs/development.md#本地开发入口); deployment files are in `deploy/` and deployment guides in `docs/`.
