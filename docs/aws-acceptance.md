# AWS acceptance

The beta is done when this holds: **the existing application runs on AWS, keeps its review and
approval guarantees, survives routine restarts and updates, and can be restored from a tested
backup.** This page lists the checks that show it, what each has shown so far, and a place to
record every run on AWS.

**Status: owner workspace deployed; initial checks passed, full matrix incomplete.** On
2026-09-30 UTC, release `613f902` was installed behind CloudFront HTTPS with owner authentication.
The owner supplied the cloud secret; one bounded live DeepSeek connectivity call passed. Host reboot and recovery passed; interruption
and rollback drills below are not claimed as completed. Earlier CI used GitHub's Ubuntu runners, Docker and real
Chromium, with AWS and systemd stubbed for host-script tests. Each result is one of:

- **Passed**: it ran, and the result was the one required.
- **Failed**: it ran, and the result was not the one required.
- **Not performed**: it has not run there.

Commands below use `run_on_host`, `$SMOKE` and the other names set up in
[aws-deployment.md](aws-deployment.md) (steps 3, 4 and 6). Run the checks while the host holds only
the synthetic data (that guide's step 6), before any real data goes up: several restart the app,
restore over its data, or install a release made to fail.

## The matrix

| # | Scenario | Required result | Without AWS (tests, CI) | On AWS |
|---|---|---|---|---|
| 1 | Recreate the container | The data is still there | Passed: CI runs the smoke check in a new container after the one that made the data is gone | Passed on isolated synthetic data on the EBS volume; new test containers preserved facts, profile, job, approval and PDF |
| 2 | Restart the EC2 host | The right data volume is mounted before the app starts | Passed (logic): the mount script on real ext4 loop devices; the app refuses a folder without the volume marker | Passed: host rebooted, correct EBS mount and service active, synthetic data verified, health 200 and owner gate 401 |
| 3 | The model provider is unavailable | An explicit failure or a fallback; never a made-up success | Passed: unit tests with DeepSeek failing | Not performed |
| 4 | Approve from a stale browser tab | A conflict; nothing the page did not show is approved | Passed: unit tests | Not performed |
| 5 | Interrupt generation | The restart notices, and what was finished stays usable | Passed: unit tests that kill the app mid-step | Not performed |
| 6 | Restore into an empty environment | Facts, profile, jobs and approvals agree | Passed: unit tests; CI restores a backup into an empty folder and runs the smoke check on it | Passed: synthetic archive restored into an empty folder; first production backup uploaded to private S3 after restore verification. Cross-stack restore not performed |
| 7 | Roll back a release | The release before runs against the same data | Passed (logic): host scripts with stubs | Not performed |
| 8 | Inspect the cloud logs | No key, token or CV text | Passed: unit tests; CI checks the container's log | Passed: 35 CloudWatch records scanned, no tested token/key-prefix/synthetic-CV patterns found; this is a bounded check |
| 9 | Check public reachability | The optional HTTPS entry requires owner login; direct host ports remain inaccessible | Passed: base deployment has no inbound rule; public-mode tests enforce login | Passed: HTTPS 200 health, 401 without/wrong login, authenticated page/API 200, API without CSRF token 403; S3 public-access blocks enabled; HTTP 403 and direct ports 22/8765 timed out |

## Each check

### 1. Recreate the container

- **Evidence so far.** CI's container job: "The whole workflow…" makes synthetic data; "A new
  container finds everything the last one left" checks it from a new container; "Served on the
  loopback only…; recreated, the data stays" starts the app again on it.
- **On AWS.** `systemctl restart` removes the container and makes a new one:

  ```sh
  run_on_host "systemctl restart workbench"
  run_on_host "$SMOKE verify --data /data"
  ```

  Passes if every line of the verify reads `ok`.

### 2. Restart the EC2 host

- **Evidence so far.** `deploy/aws/test-mount-data.sh` (CI, real loop devices):
  - only the device named is mounted, and only if it holds `data/.workbench-data`;
  - another device, a file system without the marker, and data without a file system are all
    refused, byte for byte unchanged;
  - a restore cut short is undone after a reboot.

  `workbench.service` runs this check before every start (`ExecStartPre`). CI's "Refuse to start on
  a folder that is not the data volume" shows the app's own second guard.
- **On AWS.**

  ```sh
  aws ec2 reboot-instances --instance-ids "$HOST"   # then wait two or three minutes
  run_on_host "findmnt -n -o SOURCE,TARGET /srv/workbench && systemctl is-active workbench && \
    journalctl -b -u workbench -o cat | grep -m1 'the data volume is mounted'"
  run_on_host "$SMOKE verify --data /data"
  ```

  Passes if the volume's device is mounted at `/srv/workbench`, the workbench is active, the mount
  line comes before its start in the journal, and the verify passes.

### 3. The model provider is unavailable

- **Evidence so far.** These unit tests pass:
  - `test_requirement_flow`: heading rules take over when DeepSeek fails or finds nothing;
  - `test_matching`: word matching takes over when DeepSeek fails;
  - `test_cv_plan`: a failed or malformed answer leaves the CV unplanned;
  - `test_deepseek_client`: each failure says what kind it is, so the page can explain it;
  - `test_web`: each stage is logged with its outcome (`fallback`) and reason, never its text.
- **On AWS.** With no `DeepSeekSecretArn` (the first run), add a job and prepare its CV. Passes if:
  - the page says the key is missing;
  - requirements come from the heading rules;
  - no stage reports a success it did not have (in Logs Insights: `filter event = "stage"`).

  This needs no paid model call.

### 4. Approve from a stale browser tab

- **Evidence so far.** These unit tests pass:
  - `test_web`: "approving approves only the CV the page showed" (409, nothing approved; an approval
    that names no CV is refused with 422);
  - `test_web`: "the preview shows only the CV the page holds";
  - `test_gaps`: "accepting never confirms a version the user has not seen".
- **On AWS.** Open one job in two tabs. Change the CV in the second tab, then approve in the first.
  Passes if the first tab is told the CV changed, nothing is approved, and after reloading it
  approves the CV it now shows.

### 5. Interrupt generation

- **Evidence so far.** These unit tests pass:
  - `test_web`: "a restart during generation shows only what was finished";
  - `test_web`: "an approval cut short by a crash is no approval after the restart";
  - `test_web`: "a crash while the CV is prepared again keeps the notice until it is";
  - `test_workspace`: the crash tests (a step cut short is undone at the next start; a half-written
    file leaves the old one; a journal that cannot be read stops nothing).
- **On AWS.** Start preparing a CV, and while it runs:

  ```sh
  run_on_host "systemctl restart workbench"
  ```

  Passes if, after reloading, the job shows the last finished step, any notice about the cut-short
  change is shown, and preparing again works.

### 6. Restore into an empty environment

- **Evidence so far.** `test_backup`:
  - a backup restored into an empty folder is the same workbench;
  - restores refuse a changed, truncated or escaping archive;
  - a backup of an older facts layout restores.

  CI restores a backup into an empty folder and runs the smoke check on it.
- **On AWS.** Every nightly backup is restored into an empty folder on the host and checked before
  it counts. By hand, with a restore over the live data:

  ```sh
  run_on_host "systemctl start workbench-backup && stat -c %y /var/lib/workbench/last-backup"
  run_on_host "/opt/workbench/current/deploy/aws/host/restore.sh latest && $SMOKE verify --data /data"
  ```

  Passes if the backup's time is recorded, the restore finishes, and the verify passes. The
  strongest version is a second stack, with no `DataVolumeId`: format its new volume, copy one backup
  into its bucket, `restore.sh` it, and verify. This creates billable resources for as long as it
  runs.

### 7. Roll back a release

- **Evidence so far.** `deploy/aws/test-host-scripts.sh` (CI, with stubs):
  - a release that does not answer, or does not start, is replaced by the last good one, which runs
    again, also after an install that was cut short;
  - one whose data check finds new problems is refused before anything is switched;
  - a release older than the data's format is refused before anything stops;
  - one that raises the format needs a fresh backup and is never undone automatically.
- **On AWS.**
  - **By hand:** install the release before by its digest, verify, then install the newest again:

    ```sh
    run_on_host "/usr/local/sbin/workbench-release $REPOSITORY@sha256:<the release before>"
    run_on_host "$SMOKE verify --data /data"
    ```

  - **Automatic:** needs a release that passes the tests but fails on the host. A failing `/healthz`
    would not do: the tests catch it, and nothing would reach the host. Instead, on a branch, change
    the workbench's command in `compose.yaml` to `["sleep", "infinity"]`: the tests pass, the image
    installs, and it never answers. Allow that branch in the `aws` environment for this run only,
    and run the workflow on it. Passes if:
    - the run fails, and its install step shows the candidate refused (`did not become healthy`),
      then `installing the last good release again` and `installed <that release>`;
    - afterwards `run_on_host "cat /etc/workbench/release"` names the last good release;
    - `$SMOKE verify --data /data` passes.

    Then remove the branch from the environment.

### 8. Inspect the cloud logs

- **Evidence so far.**
  - `test_run_log` and the logging tests in `test_web`: routes, never paths; made-up methods;
    library wording; errors by type only; the 500's id; stage lines.
  - Mutation checks: removing each guard fails a test.
  - CI checks the container's log: every line is JSON, request lines carry routes and never a path,
    and the page token is absent.
- **On AWS.** After the synthetic run (step 6 of the deployment guide), in CloudWatch Logs Insights
  on `/$STACK/app`:

  ```text
  fields @timestamp, @message
  | filter @message like /token=|X-Workbench-Token|sk-|Alex Example|Example Corp|REST APIs|示例候选人|000-000-0000/
  ```

  Passes if this finds nothing. The patterns cover a token in a query string, the token header,
  a key's prefix, and the synthetic CV's own words (its name, employer, a line, the Chinese name and
  the phone number).

### 9. Check public reachability

The commands below describe the original private-only deployment. For the optional
[public owner mode](aws-public.md), the listener is deliberately available inside the VPC,
with ingress restricted to CloudFront and owner authentication on all private content.
Use that guide's HTTPS/401/CSRF checks instead of requiring an empty security group.

- **Evidence so far.**
  - `check-template.py`: the security group has no inbound rule.
  - CI: Docker publishes the app on `127.0.0.1` only, nothing else listens on its port, and requests
    with another host name or no token are refused.
- **On AWS.** From your computer:

  ```sh
  SG=$(aws ec2 describe-instances --instance-ids "$HOST" \
    --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)
  PUBLIC_IP=$(aws ec2 describe-instances --instance-ids "$HOST" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  aws ec2 describe-security-groups --group-ids "$SG" --query 'SecurityGroups[].IpPermissions'
  nc -vz -w 5 "$PUBLIC_IP" 8765; nc -vz -w 5 "$PUBLIC_IP" 22
  run_on_host "ss -ltnH"
  ```

  Passes if:
  - the security group lists no inbound permission (`[[]]`);
  - both connections time out;
  - on the host, port 8765 is bound only to `127.0.0.1`.

## Record of runs on AWS

Add one row per check each time it runs: the date, the release's commit, the commands as run, what
came back, and the result. Keep failed runs.

| Date | Release (commit) | Check | Commands | What came back | Result |
|---|---|---|---|---|---|
| 2026-09-30 UTC | 613f902 | New data volume | `format-data.sh` | Refused nonzero raw blocks before changing anything | Failed safely |
| 2026-09-30 UTC | 613f902 | New volume recovery | Completed safety snapshot; `ListSnapshotBlocks`; guarded zero initialization; original formatter | Exact new, unmounted volume; zero snapshot blocks and no continuation token; formatter then passed | Passed |
| 2026-09-30 UTC | 613f902 | Install and owner login | `workbench-release`; `enable-public.sh` | Installed pinned image, first S3 backup restored/verified, owner authentication active | Passed |
| 2026-09-30 UTC | 613f902 | Public access | HTTPS requests with/without owner credentials and page token | Health 200; unauthenticated page/API/preview/download 401; bad login 401; authenticated page/API 200; missing page token 403 | Passed |
| 2026-09-30 UTC | 613f902 | Synthetic export, first harness | Isolated `docker run ... deploy/smoke.py create` | Harness omitted writable HOME used by Compose; Chromium exited -5. Production configuration unchanged | Failed |
| 2026-09-30 UTC | 613f902 | Synthetic workflow and restore | Same image/security profile, `HOME=/tmp`, EBS test folder; smoke create/verify; backup create/restore; smoke verify | English and Chinese PDFs each one page with expected text/fonts; facts and approval survived new containers and archive restoration | Passed |
| 2026-09-30 UTC | 613f902 | Storage and logs | S3 public-access configuration; CloudWatch pattern scan | All four S3 public-access blocks enabled; 35 log records, zero tested sensitive-pattern matches | Passed |

The synthetic workflow used a scripted model and real Chromium. It proves rendering and
application state handling, not live DeepSeek output quality. Test data remained in separate
EBS folders, outside the owner's production data directory. No personal CV was migrated.

The public Tencent board import was attempted through the authenticated cloud API. It returned
HTTP 400 because the upstream pagination repeated a posting; the adapter refused to save an
incomplete batch. Live bulk import remains unverified. Individual posting URLs and manual JD
entry are separate paths.

Host reboot completed at 2026-09-30 09:04:23 UTC. After boot, the data device was mounted at
`/srv/workbench`, the production service was active, and the isolated synthetic facts, profile,
job, approval and PDF still passed `deploy/smoke.py verify`. Local and public health returned
200, and unauthenticated private content still returned 401.

DeepSeek connection was completed on 2026-09-30 UTC through the base stack's existing secret
parameter and the host environment. The first test invocation used the wrong Compose project
name for the symlinked release and made no model request; selecting the sole running workbench
container corrected the test harness. From that production container, `chat_json` returned the
expected synthetic JSON using `deepseek-flash`: 1 request, 52 input tokens, 5 output tokens,
with a 128-output-token cap and HTTP retries disabled. No personal input or secret value was
printed. The owner gate remained 401 without login and public HTTPS health returned 200.
This confirms live connectivity, not a full real-model resume quality evaluation.
