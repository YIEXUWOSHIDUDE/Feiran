# Guest workspaces (V2)

A guest workspace lets a visitor try Feiran without an account. One click on the public welcome
page makes a workspace of their own, holding a **synthetic** sample CV profile, an example job and
a CV for that job. The guest can find the job's requirements again, prepare, reword and adjust
the CV with DeepSeek, check it against the requirements and adopt suggestions, edit lines, undo
and redo, fit the CV to one page, approve it and download the PDF — through the same app, store,
task runner and model meter a signed-in user uses.

- **AI within an allowance.** Each workspace gets a **one-time** allowance of model tokens (input
  plus output; default 100,000). It never refills. All guest workspaces together also share a
  **daily** allowance (default 1,000,000 tokens per UTC day). When either is used up, AI work is
  refused with a clear message; editing, fitting, approving and downloading keep working, and
  nothing is deleted because the allowance ran out.
- **No time limit.** A workspace has no end time and its session has none either. It stays until
  the guest ends it (**End guest session**, signing out, or signing in), or the operator ends it.
- **The sample itself is not AI output.** It was written by hand and put in without a model; the
  page says so.

It is **off by default**. Nothing below changes for signed-in users while it is off, and signing
in with Cognito works the same while it is on.

## Switching it on and off

Settings live in the V2 database and take effect at once (no restart). On the host, with the
`v2` helper from [v2-operations.md](v2-operations.md) §4:

```sh
v2 set guest_enabled 1                 # /welcome offers a guest workspace; signed-out visitors to / go there
v2 set guest_lifetime_tokens 100000    # each workspace's one-time AI allowance
v2 set guest_daily_tokens 1000000      # all guests together per UTC day
v2 guests                              # settings, all guests' AI use today, each workspace (no content)
v2 set guest_enabled 0                 # no new workspace starts; existing ones keep working
v2 guests end <user_id>                # remove one now
v2 guests end-all                      # remove every guest workspace now
v2 guests expire                       # remove those that cannot be used (a start that failed halfway)
v2 set tasks_enabled 0                 # the site's kill switch stops guests' AI too
```

`v2 set` needs an existing store: start the app once on the data folder first.

## Limits

Each setting is a whole number inside fixed bounds. `v2 set` refuses a value outside them, and a
value written into the database some other way is still read clamped to them, so no setting can
make guests unlimited.

| Setting | Default | Bounds | Meaning |
|---|---|---|---|
| `guest_enabled` | `0` (unset) | `0` / `1` | Whether new guest workspaces can start |
| `guest_lifetime_tokens` | 100,000 | 1,000–1,000,000 | One workspace's AI allowance, given once, never refilled |
| `guest_daily_tokens` | 1,000,000 | 10,000–100,000,000 | All guest workspaces' AI use per UTC day, removed ones included |
| `guest_max_active` | 100 | 1–1000 | Guest workspaces **stored** at once. None expires, so this is a storage cap; none is removed to make room. When full, new starts are refused (`guest_full`) until one is ended |
| `guest_starts_per_hour` | 20 | 1–200 | Workspaces started in the last hour, across the site (ending one early does not give its start back) |
| `guest_max_tasks` | 20 | 1–100 | **Fit and PDF runs** one workspace may make. AI tasks are counted apart, so using AI never uses up printing |

`guest_lifetime_minutes` from the previous release is gone: `v2 set` refuses it, and a stored
value is ignored.

Fixed in code (`feiran/tenant_store.py`):

| Constant | Value | Meaning |
|---|---|---|
| `GUEST_MAX_VERSIONS` | 150 | Versions one workspace may keep in each of: CV versions (each edit, undo, AI stage or fit is one), requirement versions, check versions |
| `GUEST_MAX_MODEL_TASKS` | 200 | AI tasks one workspace may make (the token allowance usually binds first) |
| `GUEST_QUEUED` | 2 | Guest tasks waiting at once on the whole site, and never half or more of `queue_limit` (with `queue_limit` below 2 no guest task can queue) |
| `GUEST_RUNNING` | 1 | Guest tasks (AI or print) running at once on the whole site |
| `GUEST_SESSIONS` | 4 | Sessions one workspace keeps (a double click or a resend can add one); the oldest go first |
| `GUEST_SEED_WAIT` | 5 min | A workspace whose sample was never put in (a crash while starting) is removed after this |
| `GUEST_START_KEEP` (`feiran/web_v2.py`) | 30 min | How long a welcome page's start token can be used |
| `GUEST_COOKIE_KEEP` (`feiran/web_v2.py`) | 400 days | How long the browser keeps a guest's session cookie; renewed whenever the app or welcome page is opened |

The counting limits are kept in the database, so a restart forgets nothing.

## How the AI allowance is metered

Guests use the existing per-call meter (`model_calls`, `begin_call` / `settle_call` / `fail_call`):

- **Before each transport call** (every retry included), `begin_call` reserves the prompt
  estimate plus an output budget, in one write transaction. The budget is cut to what is left of
  the workspace's allowance, of all guests' day, and (in tokens mode) of the site's day. A call
  that cannot get at least `MIN_OUTPUT_TOKENS` is refused, and nothing is sent. Two calls can
  never both spend the last tokens.
- **When the answer comes**, the call is charged what DeepSeek reported (input plus output), even
  past its reservation. The overage is charged, shown, and stops the next call. A call with no or
  unusable usage stays charged at its reservation ("usage unknown"). A duplicate report changes
  nothing, and a late answer (after a cancel or timeout) settles its own call.
- **The workspace's use** is everything its calls charged, over every day. Retries, resends of the
  same request, restarts and new UTC days never give any back.
- **All guests' day** counts open workspaces' calls plus `guest_model_usage`. Ending a workspace
  adds its calls there, and to the site's `deleted_model_usage` totals, before they are deleted.
  So ending a workspace (or starting a fresh one) never gives back all guests' day or the
  site's.
- **Guest caps apply whatever `quota_mode` is.** A guest is never a test account: `v2
  test-unlimited` refuses one, and an exemption row is ignored for a guest. The site's own limits
  still apply: in tokens mode `site_daily_tokens`; in legacy mode each AI task reserves its
  `site_daily_units` units. A guest is not held to `user_daily_*`, which are signed-in accounts'
  daily limits. Signed-in accounts' quotas and exemptions are unchanged.
- **What the page shows**: the workspace's tokens used and left of its one-time allowance (calls
  under way or of unknown use counted at their reservation), plus its print-run and version use.
  It never shows other guests' use. Refusal codes: `guest_ai_exhausted` (403, the workspace's
  allowance) and `guest_ai_busy` (429, all guests' day, until 00:00 UTC); in tokens mode the
  site's day gives `quota_exhausted` (scope `site`).
- **Consent**: a guest sees the same data-flow notice ("job processing") before its first AI task
  and must agree to it; the model is sent the sample job and CV only.

## What a guest can and cannot do

The server enforces this in three places, not only by hiding buttons:

1. **Routes** (`web_v2.GUEST_ROUTES`). A guest's session reaches:
   - the page and `/api/me`; reading its facts, CV languages, jobs and one job; its tasks (and
     cancelling one); dismissing notices; `/api/consent`;
   - on its sample job: finding requirements again, adding or deciding requirements; preparing,
     rewording and adjusting the CV; checking it, and asking for, adopting, writing or declining
     a suggestion (on this CV only);
   - editing a CV line, undoing or redoing an edit or a change, fitting to one page, approving,
     creating the final PDF, preview, download;
   - `/guest/end` and `/logout`.

   Every other route answers **403 `guest_not_allowed`** before it runs. That covers uploads, new
   jobs (pasted or from a URL), followed boards and listings, the sponsor check, saving lines to
   the profile, editing or confirming facts, account deletion, and any route added later.
2. **Tasks** (`UserWorkspace.submit_task`). A guest may queue `fit_cv` and `export_pdf` (on the
   PDF queue, no model call), and `prepare_job` (on its existing job only), `prepare_cv`,
   `tailor_cv`, `plan_cv`, `check_gaps` and `suggest_gap`. None of these reads a page or a board.
   `claim_tasks` ends any other guest task it finds as failed (`guest_not_allowed`) without
   starting it.
3. **Model calls** (`begin_call`). Allowed only for those model tasks and only within the
   allowances above.

The usual per-user rules still apply: one task running and one waiting per workspace, the CSRF
token on every write, the Origin check, task deadlines, retries and idempotency.

**Download flow.** As for a signed-in user: tick the review box, choose **Approve this CV**, then
**Create final PDF**; **Download final PDF** appears once it is printed. Any later edit or fit
needs a new approval.

**The sample.** `examples/guest_sample.json` is put in by `feiran/guest_sample.py` through the
store's own entry points; the person, school, employers, company and posting are made up.

- The job's requirements come from the heading rules (no model).
- The CV is the saved profile as written, built for those requirements, like a preparation's
  first stage.
- Its rewording and job-adjustment stages are recorded as "AI rewording not run yet: the sample
  CV was prepared without AI…" / "尚未进行 AI 润色…", never as model output.

## Starting, resuming and ending

- `GET /welcome` (public) offers sign-in and the guest workspace, and sets a random start token
  in an `HttpOnly`, `SameSite=Strict` cookie and in the page. Opening it starts nothing. A
  signed-in user is sent to `/`; a browser with a guest workspace is offered to continue it, and
  its cookie is renewed.
- `POST /guest/start` needs `Origin` equal to `WORKBENCH_PUBLIC_ORIGIN` and the start token in both
  the cookie and `X-Guest-Start`. The same start sent again (a double click or a resend) opens the
  one workspace it made. A browser already in a guest workspace is sent back to it, and a
  signed-in user's session is never replaced (409 `signed_in`).
- **Resuming.** The session cookie (`HttpOnly`, `SameSite=Lax`) is persistent: the browser keeps
  it for 400 days (the most current browsers allow), renewed each time the guest opens the app or
  the welcome page. The server keeps the session with no end.
  - Clearing the browser's cookies or site data, a private window, another browser or device, or
    400 days without a visit all lose the way back. The workspace then stays on the server,
    counted in `guest_max_active`, until the operator ends it.
  - There is no way to recover it from an e-mail or a code.
- Refusals: `guest_disabled` 404; `guest_full` 503 (the stored-workspace cap); `guest_rate_limited`
  429 (both with `Retry-After: 60`); `guest_start_refused` 403; `guest_task_limit` (print runs),
  `guest_model_task_limit` and `guest_version_limit` 403; `guest_ai_exhausted` 403;
  `guest_ai_busy` 429.
- **Identity.** A guest identity (`urn:feiran:guest`) can never sign in or be provisioned, and
  Cognito issuers must be https.
  - Guests are not counted in `max_accounts`, not listed by `v2 accounts`, and `v2 delete`
    refuses one.
  - A guest is never turned into an account: signing in from a guest workspace ends it, and the
    account opens without the sample.
  - The server marks a guest's page explicitly (`workbench-guest` = `guest`), apart from any
    time; a signed-in page never has the mark.
- **End.** By **End guest session**, by signing out or signing in, or by the operator.
  - From that moment its session and every private route answer 401, and a running task's result
    is refused (`LateResult`).
  - The workspace is removed whole: queued and running work cancelled, every row of the account
    and its PDFs deleted, with **no tombstone and no deletion-ledger line**. Its model use stays
    in the day totals, without its ID.
  - Only accounts with the guest issuer and a `guest_workspaces` row are ever removed this way.
  - Nothing ends a workspace for its age or for having used its AI allowance.

## Upgrading, backups and rollback

**Upgrading from the release with fixed guest ends** (2-hour workspaces, no AI). At the first
start, `upgrade_store` changes each existing guest workspace and session to have no end. That
includes one whose old end had just passed but which was not yet removed. Its facts, profile, job,
CV versions, PDFs and tasks are kept. The sample's preparation notes in guests' workspaces stop
saying a guest makes no AI requests; only those exact sentences are replaced. Signed-in accounts
and their sessions (with their own expiry) are untouched.

A sample CV made by that release was not built for the job's requirements, so rewording or
adjusting it fails at once, sends nothing, and asks the guest to choose **Start over** first;
starting over prepares it with AI.

**Backups.** `v2_backup create` leaves guest workspaces and their sessions out of the copy, and
`restore` removes any it finds, so a restore never brings guest access or content back. Guests'
model use at the time of the copy stays in the copy's day totals (the site's and all guests'),
without IDs, as when a workspace ends. Use after the copy was made is not in it, and no separate
spend journal is kept. Signed-in accounts' backup, deletion ledger and restore behave as before.

**Rolling back:**
- **To the release with fixed guest ends** (#38). It reads the guest tables. Workspaces keep
  working there and are never removed by age: they carry an end date far in the future. That
  release's page would show it as an end date, and it refuses AI.
  - Its `guest_max_active` counts these workspaces against its own default of 10, so new starts
    may be refused there until some are ended.
  - It ignores `guest_model_usage`. A workspace it ends has its model calls folded only into the
    site's totals, not all guests' day.
  - To leave nothing behind, run `v2 guests end-all` first.
- **To a release without guest workspaces.** Run `v2 guests end-all` first: such a release does
  not read the guest tables (so no guest session works there), but it would see the guest
  accounts' rows as ordinary accounts.

## Known limits

- **Site-wide start limits.** The hourly start limit and the stored-workspace cap apply to the
  whole site, not per visitor, because the app does not trust `X-Forwarded-For`. One visitor can
  use up the guest starts for everyone until the hour passes, and abandoned workspaces fill
  `guest_max_active` until the operator ends them (`v2 guests` shows each one's start and use).
  Signed-in users are not affected. Put per-client limits in front of `/guest/start` at the load
  balancer or WAF if that matters.
- **Guests share one runner slot.** Guest tasks start after signed-in users' queued tasks and
  one at a time across all guests. With many guests, AI tasks wait or are refused with "Guest
  workspaces are busy". A guest task that is already running holds its runner slot until it
  finishes (there is no pre-emption), so a signed-in user's print can wait up to one guest
  print.
- **Retries cost tokens.** The client retries a failed HTTP call up to `HTTP_ATTEMPTS` times. Each
  attempt reserves, and is charged at least its reservation when no usage comes back, so failed
  calls use up a guest's allowance.
- **Previews are not counted.** Opening the preview renders HTML on each request, as for
  signed-in users.
- **Not tested here with a real model or printer.** Offline tests use a scripted model and a
  stand-in printer; real DeepSeek and real Chrome checks are done separately.

## Trying it locally

No Cognito pool is needed to exercise the guest flow; the placeholder sign-in settings below make
sign-in itself fail, which is expected. Without `DEEPSEEK_API_KEY`, AI tasks fail with a clear
reason and charge nothing (no call is made without a key). Use a test key for the real model.

```sh
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
export WORKBENCH_OIDC_ISSUER=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_PlaceholderOnly \
       WORKBENCH_OIDC_CLIENT_ID=placeholder WORKBENCH_OIDC_DOMAIN=https://placeholder.invalid \
       WORKBENCH_PUBLIC_ORIGIN=http://localhost:8765
.venv/bin/python -m feiran.web_v2 --data /tmp/feiran-guest-demo --port 8765 &   # first start makes the store
.venv/bin/python -m feiran.v2_admin --data /tmp/feiran-guest-demo set guest_enabled 1
.venv/bin/python -m feiran.v2_admin --data /tmp/feiran-guest-demo set site_daily_units 1000   # legacy mode's site limit
# open http://localhost:8765/ and choose "Try a guest workspace"
```

Fit to one page and the final PDF need Chrome or Chromium (found as in `feiran/cv.py`, or set
`CHROME_PATH`). Tests: `.venv/bin/python -m unittest tests.test_guest_workspace tests.test_guest_ai -v`.
