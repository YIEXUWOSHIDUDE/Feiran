# Job Fit Workbench

English | [简体中文](README.zh-CN.md)

Purpose: for each job, help the candidate present the best CV they can make from their own confirmed, real experience: what to put first, what to cut, and how to word it in the job's language. It does not judge whether the candidate qualifies. Nothing beyond the candidate's facts is ever added, and the candidate reviews and approves the final CV.

Candidate facts are stored locally in SQLite with versions, a type and tags. Jobs come from public Greenhouse, Lever and Ashby job boards, or from a pasted job description (JD). The web page is the main entry point; the command line is for development and testing. The repository contains no real candidate data.

Architecture, business invariants and current limits: [development.md](development.md) (in Chinese).

## Requirements

Python 3.10 or newer. The command-line tools need no third-party packages. TypeSafe/Jev is only used for optional semantic checks of JD requirements; the normal offline flow needs no API key. The web page needs a small virtual environment (see [Web page](#web-page)).

## Command-line flow

### 1. Build and confirm candidate facts

Every fact needs a coarse type; `--tag` can be repeated and records the skills, domains and qualifications used for search.

```sh
python3 facts.py add \
  --type project \
  --tag Python \
  --tag FastAPI \
  --tag backend \
  --text "Built a backend course project with Python and FastAPI."

python3 facts.py list
python3 facts.py confirm fact-xxxxxxxxxxxx@1
```

Supported types: `project`, `experience`, `skill`, `education`, `eligibility`, `availability`, `achievement`, `other`.

Many facts can be imported from one JSON file; see [examples/synthetic_facts_import.json](examples/synthetic_facts_import.json). Each item needs `type` and `text`; `tags` and `id` are optional; any other field is rejected.

`id` is a readable ID of your choice (for example `fact-uni-coursework`; letters, digits and hyphens after `fact-`). The CV profile uses it to refer to facts; see [examples/synthetic_cv_facts.json](examples/synthetic_cv_facts.json). For an item with an `id`: a new ID creates the fact; unchanged content changes nothing; changed content creates a new `pending` version of that fact, which needs confirming again. So you can edit the JSON file and import it again. A new ID whose content exactly matches an existing fact is rejected, so one fact never exists twice.

```sh
python3 facts.py import .local/my-facts.json
python3 facts.py confirm fact-aaaaaaaaaaaa@1 fact-bbbbbbbbbbbb@1
```

Import validates every item first; if any item is invalid, nothing is written. All new facts start as `pending`, and importing the same content again creates no duplicates. The import output's `next_step` lists the `FACT_ID@VERSION` refs you can confirm: check each one and keep only the ones that are right. When confirming several versions at once, if any one is not the current version, none is confirmed. Items without an `id` never modify existing facts; to change a fact use `revise` or re-import with its `id`. Keep personal fact files under `.local/` and never commit them.

Changing the text, type or tags creates a new `pending` version; fields you do not give are carried over from the current version:

```sh
python3 facts.py revise fact-xxxxxxxxxxxx --text "Updated fact text"
python3 facts.py revise fact-xxxxxxxxxxxx --type experience --tag Python --tag backend
python3 facts.py revise fact-xxxxxxxxxxxx --clear-tags
python3 facts.py list --history fact-xxxxxxxxxxxx
python3 facts.py confirm fact-xxxxxxxxxxxx@2
```

A single fact can still be confirmed the old way: `confirm fact-xxxxxxxxxxxx --version 2`.

Once v2 exists, the confirmed v1 is kept as history but is no longer used for new matches. Text, type and tags are confirmed together as one complete version.

### 2. Get the job description

A job from any source (LinkedIn, a company site, a Chinese job site, …) can be pasted as plain text:

```sh
python3 job_search.py paste \
  --file .local/acme-jd.txt \
  --title "Software Engineer Intern" \
  --company "Acme" \
  --url "https://jobs.example.com/123" \
  --output .local/acme-input.json

# On macOS you can read the clipboard directly
pbpaste | python3 job_search.py paste --file - --title "Backend Developer Intern" --output .local/xx-input.json
```

Without `--url` the source is recorded as unknown. `captured_at` is when you pasted it, not the official posting date, and pasted text does not prove the job is still open. `--output` writes the same review-input format as `select`.

Greenhouse, Lever and Ashby boards can be searched directly (`--provider` defaults to `greenhouse`). Search can rank locally by the tags of the current confirmed fact versions; candidate facts are never sent to the job boards. Jobs are ranked by the number of distinct tags they mention; a tag used by several facts counts once. Ranking reads only tags and fact IDs; fact text is read only for the evidence shown, at most 20 items per job. Timeouts, connection failures, 429 and 5xx responses are retried once; a 404 usually means the board name or job ID is wrong.

```sh
python3 job_search.py search \
  --board duolingounirecruitment \
  --facts-db .local/workbench.db \
  --title Intern

python3 job_search.py select \
  --board duolingounirecruitment \
  --job-id <job id> \
  --output .local/review-input.json
```

For Lever use `--provider lever --board palantir`; for Ashby `--provider ashby --board openai`. The board name is the part after `jobs.lever.co/`, `jobs.ashbyhq.com/` or `boards.greenhouse.io/`.

`select` reads the chosen job again and writes only a JD snapshot. It does not copy any facts yet, and if the job is gone or the API fails, it never passes off old content as current.

### 3. Extract and confirm requirements

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

`propose` copies JD lines word for word from known requirement sections; every candidate starts as `pending`. It recognizes common English headings (`Requirements`, `Minimum Requirements`, `You have`, `Nice to have`, `What we look for`, …) and Chinese headings (`岗位要求`, `任职要求`, `职位要求`, `加分项`, …), handles decorations and numbering such as `**…**`, `【…】`, `一、` and `1.`, and headings with content on the same line (`任职要求：熟悉 Python`). `decide` only confirms or excludes requirements; it no longer links facts. Candidate IDs are derived from the text, so they are stable.

Requirements the rules miss can be added by hand, but only as exact JD text (part of a line is fine). Added candidates also start as `pending` and need `decide`:

```sh
python3 requirement_flow.py add \
  .local/review-candidates.json \
  --text "Comfortable with SQL and Linux" \
  --output .local/review-candidates-2.json
```

Text not in the JD, an existing candidate, or a file already at the fact-matching stage is rejected.

To add Jev's judgments (is it an applicant requirement, its category, required or preferred), enter the API key without echo in zsh:

```sh
read -s "TYPESAFE_API_KEY?TypeSafe API key: "
export TYPESAFE_API_KEY
python3 requirement_flow.py propose \
  .local/review-input.json \
  --typesafe \
  --output .local/review-candidates.json
unset TYPESAFE_API_KEY
```

This request sends only the JD and the requirement candidates, never candidate facts. Jev's output is a signal to check; it never confirms a requirement by itself. Never put the key in a command, a JSON file or the repository.

### 4. Find supporting facts and link them

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

Search first narrows fact types by the requirement's category signal, then looks for candidates by versioned tags. English tags only use ASCII letters and digits as word boundaries, so `熟悉Python` still matches `Python`; Chinese tags match as substrings. Short tags of one or two letters/digits (such as `Go`, `R`, `C`, `AI`) are case-sensitive and may not touch `&`, and single letters may not touch `-`, so `go to market`, `R&D` and `C-suite` do not match; a sentence starting with `Go` can still match, so prefer a more specific tag such as `Golang`. The output copies only the facts the user chose, never the whole fact store. If a fact was changed, lost its confirmation or is no longer the current version after the candidates were made, `decide` rejects the old candidate and asks for a new search.

`--no-match` records "no supporting fact for now", and the report keeps it as unknown. Search order and candidate counts are not a match score, an eligibility verdict or a chance of an offer.

### 5. Build the CV PDF

A CV has two parts. `.local/cv-profile.json` holds the name, contact details, school/company/title/dates layout and the fact IDs each entry uses (format: [examples/synthetic_cv_profile.json](examples/synthetic_cv_profile.json)). Every line of body text is copied word for word from the current confirmed fact version. Text fields can be a plain string (same in every language) or `{"en": ..., "zh": ...}`; a blank Chinese value falls back to English and is listed in the output's `language_fallbacks`.

```sh
python3 cv.py draft --profile .local/cv-profile.json --language en --output .local/cv-draft-en.json
python3 cv.py pdf .local/cv-draft-en.json --output .local/cv-en.pdf

python3 cv.py draft --profile .local/cv-profile.json --language zh --output .local/cv-draft-zh.json
python3 cv.py pdf .local/cv-draft-zh.json --output .local/cv-zh.pdf
```

- English defaults to US Letter and Chinese to A4; change it with `--paper`. `--job .local/review-linked.json` puts the facts linked to that job's requirements first in each entry, adding or removing nothing.
- `draft` refuses to use unconfirmed facts and lists the `FACT_ID@VERSION` refs to confirm. `pdf` checks again: a changed fact or a hand-edited draft is refused, and the draft must be built again.
- The PDF is printed by the local Google Chrome in headless mode (set `CHROME_PATH` if it is not found). All text in the HTML is escaped and no network loading is allowed. A CV longer than one page is reported.
- Any PDF from an unapproved draft carries a "DRAFT / 草稿" watermark; approval is described at the end of this section.
- Output files are always new; existing files are never overwritten.

Rewording or translating for a job (DeepSeek):

```sh
python3 cv.py tailor .local/cv-draft-zh.json \
  --job .local/review-linked.json \
  --output .local/cv-tailored-zh.json
python3 cv.py pdf .local/cv-tailored-zh.json --output .local/cv-tailored-zh.pdf
```

- The API key is read from the `DEEPSEEK_API_KEY` environment variable, then from the macOS Keychain (service `deepseek-api-key`); it is never written to a file. To store or replace it: `security add-generic-password -U -a "$USER" -s deepseek-api-key -w` (it prompts without echo).
- Only the text of education details, experience and project bullets and skills lines is sent, plus the job title and its confirmed requirements; never the name, contact details, school or company names, or publications.
- Each line is one fact. A rewrite is rejected if it adds a number, technology or skill the fact does not have, a stronger claim ("led", "managed", 主导, 负责, …), a link, or has the wrong shape; that line keeps the fact's own words, and the reasons are in the command output and the draft. Translations may use the other language's word for a term the line already has (前端 for "frontend", 接口 for "API"). These checks are word-level and cannot prove the meaning is identical, so read every line before exporting.
- `--job` is optional; without it the draft is only translated and polished. The default model is `deepseek-flash` with `--effort low`; use `--model deepseek-v4-pro` or `--effort high` to change it.

Review, approval and the final PDF:

```sh
python3 cv.py approve .local/cv-tailored-zh.json --output .local/cv-approved-zh.json
python3 cv.py pdf .local/cv-approved-zh.json --output .local/cv-final-zh.pdf
```

- Check every line in the watermarked PDF first. `approve` checks all facts again, lists every line DeepSeek changed (original → rewrite), and records the approval time and a content fingerprint in a new file.
- Only an approved file whose content has not changed at all since approval exports as a final PDF without the watermark. If anything changes after approval (including the name or dates), or a fact it uses is revised, the final export is refused; build a new draft and approve again.
- An approved file cannot be reworded again or approved twice.

## Web page

The web page needs a virtual environment inside the project (install once; nothing is installed globally):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Start it, open http://127.0.0.1:8765/ in the browser, and press Ctrl+C to stop:

```sh
.venv/bin/python web.py
```

- **Facts**: see all facts and confirm several pending versions at once. Once every fact is confirmed, the page moves to Find jobs.
- **Find jobs**: all open jobs of the companies you follow, ranked by how many of your confirmed skill tags they mention, most first; ties go to the newest posting. One role posted in several cities is one row. Filter by title and location, or tick "Hide senior roles" (hides Senior, Staff, Principal, Lead, Manager, Director and similar titles; "Member of Technical Staff" stays). **Start** reads the job again and adds it to My jobs; clicking it again opens the same job instead of making a second one.
- **My jobs**: the jobs you started; you can also paste a JD from any source to create a job.
- **Each job** (done automatically after Start or pasting; about 5 seconds in 2026-09):
  1. **What this job asks for**: DeepSeek picks the requirement lines of the JD by line number and the program copies them word for word, so no requirement is invented; all of them count. You can mark a line "Not a requirement", add a missed line, or let DeepSeek look again; saving prepares the CV again.
  2. **Your CV for this job**: CVs come only in the language(s) your own resume is written in, judged by the language of the name in your profile. With only an English name there is only an English CV, even for a Chinese JD. The draft is built automatically, reworded for the job (every line passes the fact check), and DeepSeek then proposes structure changes: section order, entry and bullet order, and cutting what does not help this job. Every change is listed with its reason and can be undone or redone on its own, without calling DeepSeek again. Tick "I read the CV and every change", approve, then create and download the final PDF. If your resume has a second language, that CV can be prepared with one click.
  3. **Not on your CV yet**: only the requirements none of your confirmed facts covers (covered ones are hidden). For each gap DeepSeek suggests at most one addition: a tool for one of your skills lines, or a new line under one of your jobs or projects. **It is added only when you click "True for me"**: a skill becomes a new confirmed version of that skills line; a new line becomes a new confirmed fact and is written into your profile (the old profile is first backed up to `profile-history/`); then the CV is prepared again. "Not true" keeps it as a gap and it never reaches the CV. Requirements about years, seniority, degrees or personal traits get no suggestion; suggestions with numbers, leadership words or content already on the CV are dropped.

Notes:

- Followed companies and downloaded jobs are stored in `.local/listings.db`, separate from the fact store; deleting it only loses the company list and downloaded jobs. It starts with the 30 companies in `starter_boards.json`; a company you remove does not come back on the next start. Add a company under Companies by pasting a `boards.greenhouse.io/…`, `jobs.lever.co/…` or `jobs.ashbyhq.com/…` link.
- When Find jobs opens, companies not updated in the last 24 hours are downloaded again (4 at a time; 30 companies took about 6 seconds in 2026-09). There is no background schedule. Only GET requests without any personal data go to the public job boards; ranking happens entirely on your computer.
- The ranking number is a word count, not a match score or a chance of an offer. "AI" appears in 79% of jobs, so the number is only good for ordering. Tag matches are saved in `listings.db` and recomputed only when a job's text or your confirmed skills change (about 5 seconds the first time for about 9,000 jobs, then about 0.05 seconds).
- Each job is stored in `.local/jobs/<job id>/`, one file per step. Redoing a step moves that step and every later one into the job's `history/`; nothing is overwritten or deleted. The CV depends only on the requirements, so talking points and gap checks never change it.
- Structure changes are limited: education is always kept, in its place; jobs stay in date order and keep at least one line each; a change can only reorder or leave out existing lines, never add one or move it to another entry. The CV file keeps every line, so undoing a change only changes what is shown.
- Every DeepSeek request uses temperature 0. Even so, borderline requirements (such as "architect distributed systems") can count as covered in one check and as a gap in the next; each job's gap list is saved and only changes when you click "Check again".
- DeepSeek only receives JD lines, CV line texts (confirmed facts or their rewrites) and the job's requirements; never the name, contact details, schools, companies or project names. If DeepSeek is unavailable, requirements come from the heading rules and the CV stops at the last step that worked, with a prompt to retry.
- Only requests to 127.0.0.1/localhost are accepted, and every API call needs a random token created when the page starts (preview and download links carry the same token in the URL). Other websites cannot read your facts or trigger DeepSeek calls.
- Use `--facts-db`, `--jobs`, `--profile` and `--port` to change paths and the port; `listings.db` lives next to the fact store.
- Web tests need the virtual environment: `.venv/bin/python -m unittest`. With plain `python3` the web tests are skipped.

## Data and limits

- `.local/workbench.db`, run files, `.env` and all personal data are ignored by Git.
- Every command that writes creates a new JSON file; existing files are never overwritten.
- The SQLite schema is v2; v1 data is kept and migrated automatically, with old facts typed `other` and no tags.
- `search --profile` is kept for the synthetic examples, and its `confirmed` field is only a claim in the input file; use `--facts-db` for real use.
- Jobs come from public Greenhouse, Lever and Ashby boards and from pasted JDs. Sites that need a login, such as LinkedIn, are not scraped, and there is no whole-web job discovery. Paste jobs from Chinese company sites.
- The web page finds requirements with DeepSeek first and falls back to the heading rules; the command-line `propose` uses only the heading rules (`section-lines-v3`). Across 8,787 jobs downloaded in 2026-09, the heading rules found no requirement line for 9% of Greenhouse, 7% of Ashby and 1% of Lever jobs; for those, add lines by hand with `add` or the page's "Add missed requirement".
- TypeSafe only judges JD requirements. With the user's consent, DeepSeek receives the text of confirmed facts (for rewording, structure changes, matching and gap suggestions), but never the name, contact details, schools, companies or project names.
- There is no application tracking, no automatic applying and no prediction of offers. Fact text cannot yet be edited on the web page (use the command line). The location filter only matches text; there is no country filter such as "US only".

Run all tests:

```sh
python3 -m unittest -v
```
