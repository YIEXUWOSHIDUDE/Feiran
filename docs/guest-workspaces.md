# Guest workspaces (V2)

A guest workspace lets a visitor try Feiran without an account: one click on the public welcome
page makes a temporary workspace of their own, holding a **synthetic** sample CV profile, an
example job and a CV for that job. The guest edits lines, undoes and redoes, fits the CV to one
page, approves it and downloads the PDF — through the same app, store and task runner a signed-in
user uses. A guest workspace makes **no AI requests**; it ends after a fixed time and everything
in it is then deleted.

It is **off by default**. Nothing below changes for signed-in users while it is off, and signing
in with Cognito works the same while it is on.

## Switching it on and off

Settings live in the V2 database and take effect at once (no restart). On the host, with the
`v2` helper from [v2-operations.md](v2-operations.md) §4:

```sh
v2 set guest_enabled 1            # /welcome offers a guest workspace; signed-out visitors to / go there
v2 guests                         # settings in force, starts in the last hour, each open workspace (no content)
v2 set guest_enabled 0            # no new workspace starts; open ones keep working until they end
v2 guests end-all                 # remove every open guest workspace now
v2 guests end <user_id>           # remove one now
v2 guests expire                  # remove ended ones now (the app also does this by itself)
```

`v2 set` needs an existing store: start the app once on the data folder first.

## Limits

Each setting is a whole number inside fixed bounds. `v2 set` refuses a value outside them, and a
value written into the database some other way is still read clamped to them, so no setting can
make guests unlimited.

| Setting | Default | Bounds | Meaning |
|---|---|---|---|
| `guest_enabled` | `0` (unset) | `0` / `1` | Whether new guest workspaces can start |
| `guest_max_active` | 10 | 1–100 | Guest workspaces open at once, across the site |
| `guest_starts_per_hour` | 20 | 1–200 | Workspaces started in the last hour, across the site (ending one early does not give its start back) |
| `guest_lifetime_minutes` | 120 | 10–240 | Lifetime, fixed when the workspace starts; activity never extends it |
| `guest_max_tasks` | 20 | 1–100 | Fit and PDF runs one workspace may make over its life |

Fixed in code (`feiran/tenant_store.py`):

| Constant | Value | Meaning |
|---|---|---|
| `GUEST_MAX_VERSIONS` | 150 | CV versions one workspace may make (each edit, undo, redo or fit is one) |
| `GUEST_QUEUED` | 2 | Guest tasks waiting at once on the whole site, and never half or more of `queue_limit` (with `queue_limit` below 2 no guest task can queue) |
| `GUEST_RUNNING` | 1 | Guest tasks running at once on the whole site |
| `GUEST_SESSIONS` | 4 | Sessions one workspace keeps (a double click or a resend can add one); the oldest go first |
| `GUEST_SEED_WAIT` | 5 min | A workspace whose sample was never put in (a crash while starting) is removed after this |
| `GUEST_START_KEEP` (`feiran/web_v2.py`) | 30 min | How long a welcome page's start token can be used |

The two counting limits are kept in the database (`guest_workspaces`, `guest_starts`), so a
restart forgets nothing.

## What a guest can and cannot do

The server enforces this in three places, not only by hiding buttons:

1. **Routes** (`web_v2.GUEST_ROUTES`). A guest's session reaches only: the page, `/api/me`, reading
   its facts, CV languages, jobs and one job, its tasks (and cancelling one), dismissing notices,
   choosing a CV language it has, editing a CV line, undoing/redoing an edit or a layout change,
   fitting to one page, approving, creating the final PDF, preview, download, `/guest/end` and
   `/logout`. Every other route — uploads, job pages and URLs, followed boards and listings,
   requirement finding, prepare/tailor/plan, gap checks and suggestions, saving lines to the
   profile, consent, account deletion, and any route added later — answers **403
   `guest_not_allowed`** before it runs.
2. **Tasks** (`UserWorkspace.submit_task`). A guest may queue only `fit_cv` and `export_pdf`, on
   the PDF queue, charged 0 units of the daily model allowance. `claim_tasks` ends any other
   guest task it finds as failed (`guest_not_allowed`) without starting it.
3. **Model calls** (`begin_call`, `mark_spending`). Refused for any guest task: nothing is sent.

So a guest makes no DeepSeek request, no upload, and no outbound read of a job board or page.
The usual per-user rules still apply too (one task running and one waiting per workspace,
CSRF token on every write, the Origin check).

**Download flow.** As for a signed-in user: tick the review box, choose **Approve this CV**, then
**Create final PDF**; **Download final PDF** appears once it is printed. Any later edit or fit
needs a new approval.

**The sample.** `examples/guest_sample.json`, put in by `guest_sample.seed` through the store's
own entry points. The person, school, employers, company and posting are made up. The job's
requirements come from the heading rules (no model); the CV is the saved profile as written. Its
rewording and job-adjustment stages are recorded as skipped ("AI rewording not used" /
"未使用 AI 润色"), never as model output. The page labels the guest workspace, its synthetic data
and its end time and remaining runs.

## Starting, resuming and ending

- `GET /welcome` (public) offers sign-in and the guest workspace, and sets a random start token
  in an `HttpOnly`, `SameSite=Strict` cookie and in the page. Opening it starts nothing; a
  signed-in user is sent to `/`.
- `POST /guest/start` needs `Origin` equal to `WORKBENCH_PUBLIC_ORIGIN` and the start token in both
  the cookie and `X-Guest-Start`. The same start sent again (a double click or a resend) opens the
  one workspace it made. A browser already in a guest workspace is sent back to it; a signed-in
  user's session is never replaced (409 `signed_in`).
- Refusals: `guest_disabled` 404, `guest_full` 503, `guest_rate_limited` 429 (both with
  `Retry-After: 60`), `guest_start_refused` 403, `guest_task_limit` / `guest_version_limit` 403.
- A guest identity (`urn:feiran:guest`) can never sign in or be provisioned, and Cognito issuers
  must be https. Guests are not counted in `max_accounts`, not listed by `v2 accounts`, and `v2
  delete` refuses one. A guest is never turned into an account: signing in from a guest workspace
  ends it, and the account opens without the sample.
- **End.** At its fixed end, by **End guest session**, by signing out or signing in, or by the
  operator. From that moment its session and every private route answer 401, the store refuses
  its ID (even before cleanup), and a running task's result is refused (`LateResult`). The task
  runner's loop and each app start then remove it whole: queued and running work cancelled, every
  row of the account and its PDFs deleted, with **no tombstone and no deletion-ledger line**.
  Only accounts with the guest issuer and a `guest_workspaces` row are ever removed this way.

## Backups and rollback

`v2_backup create` leaves guest workspaces (and their sessions) out of the copy; `restore`
removes any it finds, so a restore never brings guest access back. Signed-in accounts' backup,
deletion ledger and restore behave as before.

Before installing a release without guest workspaces, run `v2 guests end-all`: an older release
does not read the guest tables (so no guest session works there), but it would see the guest
accounts' rows as ordinary accounts.

## Known limits

- The hourly and open-workspace limits are site-wide, not per visitor: the app does not trust
  `X-Forwarded-For`, so one visitor can use up the guest starts for everyone until the hour
  passes (signed-in users are not affected). Put per-client limits in front of `/guest/start` at
  the load balancer or WAF if that matters.
- Guest tasks start after signed-in users' queued tasks and one at a time, but a guest print that
  is already running holds the runner's PDF slot until it finishes (there is no pre-emption), so a
  signed-in user's print can wait up to one guest print.
- Opening the preview renders HTML on each request, as for signed-in users; it is not counted.
- Offline tests use a stand-in printer. Real Chrome 2→1 page fitting and the browser download
  were checked separately (independent QA on macOS Chrome), not by the offline suite.

## Trying it locally

No Cognito pool or model key is needed to exercise the guest flow; the placeholder sign-in
settings below make sign-in itself fail, which is expected.

```sh
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
export WORKBENCH_OIDC_ISSUER=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_PlaceholderOnly \
       WORKBENCH_OIDC_CLIENT_ID=placeholder WORKBENCH_OIDC_DOMAIN=https://placeholder.invalid \
       WORKBENCH_PUBLIC_ORIGIN=http://localhost:8765
.venv/bin/python -m feiran.web_v2 --data /tmp/feiran-guest-demo --port 8765 &   # first start makes the store
.venv/bin/python -m feiran.v2_admin --data /tmp/feiran-guest-demo set guest_enabled 1
# open http://localhost:8765/ and choose "Try a guest workspace"
```

Fit to one page and the final PDF need Chrome or Chromium (found as in `feiran/cv.py`, or set
`CHROME_PATH`). Tests: `.venv/bin/python -m unittest tests.test_guest_workspace -v`.
