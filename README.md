# Feiran · 斐然

Prepare a job-specific CV from your profile and a job description. Generate complete suggestions, edit individual lines, and fit the result to one page with reversible content selection.

> What oft was thought, but ne’er so well express’d.

English | [简体中文](README.zh-CN.md)

An open-source CV workspace with Chinese and English profiles, job-source and sponsorship context, local editing, and PDF export. Use an existing cloud account or run your own copy locally.

![Feiran — relevant content, clearer stories, brighter next steps](web/feiran-brand.png)

## Relevant content. Clearer stories. Brighter next steps.

- **Understand the role** — identify requirements in the job description and review anything missed or misclassified.
- **Present relevant experience** — use your profile and AI additions to prepare a CV for each job.
- **Refine the wording and structure** — review AI suggestions for phrasing, ordering and what to leave out.
- **Edit locally** — adopt, edit, undo and redo individual entries without regenerating the whole CV; save a line to your shared profile separately.
- **Fit to one page** — leave out less relevant content using actual PDF page counts, with reversible changes and unchanged font sizes.
- **Export your CV** — download the PDF after reviewing and approving it.

*Same you. A stronger story.*

## Use the cloud workspace

**Already have cloud access? Open your Feiran HTTPS address and sign in. You do not need to install Python, download the code or configure a local API key.** The current V2 instance is restricted to configured accounts; self-registration is not open. Continue with “Using Feiran” below after signing in.

Prefer running it on your computer? Complete [local setup instructions](#local-deployment-optional) remain on this page. Local deployment is optional and is not required to use the cloud workspace.

The V2 workspace is deployed on AWS with HTTPS and Cognito sign-in. The four-round workflow improvements and one-page fitting are included in [release `4c595a7`](https://github.com/YIEXUWOSHIDUDE/Feiran/actions/runs/40027427699), deployed on October 23, 2026. Live English acceptance covered fitting an actual PDF from two pages to one, undo/redo, persistence after refresh, and browser downloads. Font sizes were unchanged, with no clipping or overlap in the inspected PDF. Chinese one-page fitting has not yet been checked in the live browser. See [development evidence and limits](docs/development.md) for details; older deployment records describe earlier releases.

## Using Feiran

The steps below describe the deployed V2 flow. The optional local V1 mode differs where noted.

For the full feature inventory, walkthrough and remaining work, see the [project plan and usage guide (Chinese)](docs/project-plan.md).

Once you have access to a running workspace:

1. **Bring your experience.** Choose Chinese or English when uploading a PDF; both CVs are kept separately. Save the extracted content to use it immediately. Editing saves a new usable version and preserves earlier text in history.
2. **Choose a job.** In V2, paste a public job URL or description, or browse followed companies, to review the job first. Greenhouse, Lever, Ashby and Tencent use public APIs; other URLs use page content, preferring JobPosting data. Login, CAPTCHA, JavaScript-only or incomplete pages may require pasting the description. Review the sponsor label, original evidence, source and capture time, plus skill words paired with saved experience. Unknown sponsor policies remain visible by default. Click **Prepare CV** when ready to extract requirements and generate a draft. Reading a job uses no model calls or generation allowance. Saved URLs reopen the existing job. V1 local mode keeps its previous read-and-prepare flow.
3. **Edit your tailored CV.** Use **Check now** when you want to compare the current CV with the requirements. **AI suggested entry** generates complete sentences or skills. Adopt or write an entry directly into this job's CV, then use **Edit this CV** to adjust individual lines. Use **Save to my profile** separately to reuse a line for other jobs.
4. **Fit it to one page if needed.** Choose **Fit to one page** to leave out less relevant content. Inspect the listed changes and undo any you want back. If protected content prevents a one-page result, the CV stays unchanged and the page explains why.
5. **Approve and export.** The current version requires **Approve this CV**, then **Create final PDF** and **Download final PDF**. Editing or fitting changes the version and requires approval again. Making this approval optional is planned, not implemented.

Switch between **English** and **简体中文** at the top of the page. The interface remembers your choice; changing the interface language does not translate your job descriptions, experience statements or CV. CV languages depend on the information in your profile.

Choose an uploaded Chinese or English CV for each job. Uploading again replaces only that language and keeps the previous profile in history. After related facts or profile content change, the old draft remains viewable but must be regenerated and reviewed before final export. PDFs need extractable text; scanned documents are not supported.

All regions share one listings database; Mainland China, other regions and unknown-location filters use explicit workplace locations. You can also add Tencent's public board at `https://careers.tencent.com/zh-cn/search.html`. Its list contains responsibility summaries; full requirements are fetched on selection. BOSS, ByteDance and Alibaba automatic refresh adapters are not yet included; paste their full descriptions instead. Cached postings reflect the time they were fetched; check the official page before applying.

### Local editing and one-page fitting

Adopting or writing an entry changes only this job’s CV, word for word, without preparing the whole CV again. Use **Edit this CV** to change individual lines, undo or redo local changes, and **Save to my profile** only when you want to reuse a line across jobs. Changed CVs need approval again.

Unsaved words survive task polling and navigation within the same tab. Reloading or closing the tab clears unsaved drafts. A conflicting save keeps your text; copy it or use **Discard unsaved changes** to load the latest line. Missing original lines keep recoverable unsaved text. Drafts are held in memory, not browser persistent storage.

One-page fitting selects content to leave out; it does not shorten sentences or shrink the font. It protects adopted/edited lines, restored content, essential education details, and minimum experience and skills content. Original profile data remains intact. Fitting uses actual Chromium printing, makes no DeepSeek calls, and leaves the CV unchanged if it cannot fit. Undoing a cut or editing afterward can change the page count; fit or export again to check it. Some whole-section cuts are restored as one change.

Local editing was tested with real DeepSeek calls and a local V2 browser. The changes are deployed, and the October 23 live English check confirmed saved local content, one-page fitting, undo/redo and successful PDF downloads. The earlier client-blocked download observation no longer describes that tested path. V1 and cross-language adoption still need complete browser coverage.

## Your information and your decisions

- Your experience and saved materials stay in the private data directory of your Feiran instance. In a cloud workspace, this is on the cloud host; local operation stores it on your computer.
- AI features send relevant job and experience text to DeepSeek. The application filters known identifying details and uses temporary references for experience statements. Filtering does not guarantee anonymity; experience text can still be identifying.
- Suggestions can extend beyond the saved profile. You choose which content to adopt and which version to approve.
- Approval belongs to the version you reviewed. Changing the content or relevant facts can require you to review and approve again.

## Current limits

AI can misunderstand a requirement or change the meaning of a sentence. Content review remains available before final export; generation does not require per-line authenticity confirmation.

Sponsor labels use finite English/Chinese text rules; unrecognized wording stays unknown. In V2, each description is checked once, when a job is saved or a followed company is read, and the result is stored with its check time and rules version. Opening pages reuses it; an unchanged re-read updates only the capture time, and changed wording gets a new check while saved jobs keep theirs. **Wording not checked yet** means no result exists under the current rules (saved before results were kept, a failed check, or new rules): use the job's **Check the sponsor wording** button or refresh the company. V1 local listings also persist answers by text digest and rules version, reusing them across browsing and filtering; the first read of an old database fills the cache. Saved V1 job views reuse an in-process answer for unchanged wording. In both modes, opening a job never starts a paid coverage check; use Check now or Check again when needed. Employer sponsorship history does not establish the policy of this role, and these labels do not decide a candidate's visa eligibility.

Job ordering and pre-generation hints are based on skill-word overlap, not eligibility or the probability of an offer. Missing evidence does not mean you lack a qualification, and a saved job description does not prove a role is still open.

Feiran prepares and reviews materials. It does not submit applications, track applications or predict hiring outcomes.

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
   .\.venv\Scripts\python.exe -m feiran.web
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
   .venv/bin/python -m feiran.web
   ```

   Open <http://127.0.0.1:8765/> in your browser and follow the workflow above. Data is stored in `.local/` by default. Press `Ctrl+C` to stop the server. If Chrome / Chromium is not detected, set `CHROME_PATH` to the browser executable's full path. Chinese PDF export requires a font with Chinese character support on your system.

For your own AWS deployment, see [AWS public deployment](docs/aws-public.md).

## Development

Application code and the starter company list live in [`feiran/`](feiran/); browser assets in [`web/`](web/); tests in [`tests/`](tests/); deployment tools in [`deploy/`](deploy/); guides in [`docs/`](docs/); and synthetic examples in [`examples/`](examples/).

V2 quota metering and per-requirement AI entry suggestions are documented in [the V2 implementation notes](docs/v2-plan.md). Existing deployments keep their current quota mode until an operator changes it.

Offline tests live in `tests/`. See [local setup, test commands and development notes](docs/development.md#本地开发入口); deployment files are in `deploy/` and deployment guides in `docs/`.

## License

This project is licensed under the [MIT License](LICENSE).
