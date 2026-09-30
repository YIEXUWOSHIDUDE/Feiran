# Running the workbench on AWS

A private, single-user beta: the same app, on one EC2 host, reached only through a Session Manager
port forward from your computer. It is not a public service, not highly available, and not for more
than one user. Nothing here has been deployed yet (see [What is not verified](#what-is-not-verified)).

## What the stack creates

`deploy/aws/workbench.yaml` (CloudFormation) creates everything in one stack:

| Part | What it is | Kept when the stack is deleted? |
|---|---|---|
| Network | A small VPC, one public subnet, an internet gateway; the host has a public IPv4 address for its outgoing traffic | No |
| Security group | No inbound rule at all; outgoing HTTPS (443) only | No |
| Host | One EC2 instance (t3.small by default), Amazon Linux 2023, an encrypted 30 GiB root disk, IMDSv2 with a hop limit of 1 (the container cannot get the host's credentials), no SSH key | No |
| Data volume | An encrypted gp3 EBS volume (20 GiB by default) holding the facts, CV profile, jobs and their history | **Yes** |
| Registry | An ECR repository for release images (tags immutable, scanned on push; tagged releases stay until you delete them, untagged leftovers expire after 7 days) | No (images deleted) |
| Backup bucket | A private, encrypted, versioned S3 bucket, TLS only; backups expire after 35 days by default | **Yes** |
| Logs | A CloudWatch log group (30 days by default) | No |
| Alarms | Emailed through SNS: the app stops answering or reporting, no backup for 26 hours, an error was logged, EC2 status checks fail | No |
| Releases | A release role that only this repository's release workflow, running in the GitHub environment `aws`, can take on; and a release command (an SSM document) that installs a release of this registry by digest and runs nothing else. The account's OIDC provider for GitHub is made too, unless the account has one | No |
| Budget | A monthly cost budget for the account ($40 by default) that emails you at 80% spent and when the month's forecast passes it | No |

The host's role can be managed through Session Manager, pull from this registry, write to this log
group, write and read backups (never delete them), report health metrics, and read the one DeepSeek
secret you name. The app itself runs in the container with none of these permissions. The release
role is a separate one: it can push images to this registry and send the host the release command,
and nothing else; it cannot read the data, the backups or the key. Whoever can release can still put
code next to your data, so only you should be able to run the release workflow (step 3).

### What it costs

Rough monthly figures at on-demand list prices in us-east-1 as last known; check the AWS pricing
pages for your region before relying on them:

| Item | About |
|---|---|
| t3.small, running all month (standard CPU credits, so bursts are never billed extra) | $15 |
| Public IPv4 address, while the host runs | $3.65 |
| EBS: 30 GiB root + 20 GiB data (gp3) | $4 |
| Secrets Manager, one secret (only if you store the DeepSeek key) | $0.40 |
| CloudWatch: 3 custom metrics, 4 alarms, a little log data | $1.50 |
| ECR images ($0.10 per GB a month; each release you keep is one image) and S3 backups | about $1 |
| **Total** | **about $25** |

A NAT gateway (about $32 a month) or VPC endpoints (about $7 each, and several would be needed) were
rejected: the host needs the internet anyway for DeepSeek and the job boards.

**Charges that continue after you stop the instance:** both EBS volumes, the backups in S3, the
images in ECR, the secret, and the CloudWatch metrics and alarms. The public address is released
while the instance is stopped. **After you delete the stack**, the data volume and the backup bucket
are kept on purpose and keep costing a little until you delete them yourself (see
[Stopping and cleaning up](#stopping-and-cleaning-up)).

**The budget only notifies.** It emails you (with `AlertEmail` set) when 80% of `MonthlyBudgetUsd`
has been spent in a month and when the month's forecast passes it. Nothing is stopped and spending
is not capped; AWS's figures can also lag by several hours.

## Before you start

- On your computer: the AWS CLI v2 and the Session Manager plugin for it.
- AWS credentials in your terminal (for example `aws configure sso`). Never paste keys into a chat.
- Permission to create the stack's resources, including IAM roles (`CAPABILITY_IAM`), the account's
  OIDC provider for GitHub (unless it has one) and a budget.
- Admin rights on the GitHub repository, to set up the `aws` environment, and the `gh` CLI.
- Nothing to build on your computer: the release workflow builds, tests and pushes each release on
  GitHub.

Set these once in your terminal (the region is your choice):

```sh
export AWS_REGION=us-east-1
export STACK=job-fit-workbench
```

## 1. Store the DeepSeek key (optional)

Skip this for the first run: the synthetic run below never calls DeepSeek, and without a key the
workbench works with its heading rules and says on the page that the key is missing.

When you want DeepSeek, copy the key from your Keychain straight into Secrets Manager, without it
ever being on a command line or in a file:

```sh
security find-generic-password -s deepseek-api-key -w \
  | aws secretsmanager create-secret --name job-fit-workbench/deepseek-api-key --secret-string file:///dev/stdin \
      --query ARN --output text
```

Give the ARN it prints (not the key) to the stack as `DeepSeekSecretArn`. The host puts the key in a
file under `/run` (memory) that only the container's user can read.

## 2. Create the stack

Pick an Amazon Linux 2023 image and an availability zone. The image is given by ID so a later stack
update never replaces the host by surprise:

```sh
IMAGE_ID=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)
ZONE=${AWS_REGION}a
aws cloudformation deploy --template-file deploy/aws/workbench.yaml --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides AvailabilityZone="$ZONE" ImageId="$IMAGE_ID" AlertEmail=you@example.com
aws cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].Outputs' --output table
```

Confirm the subscription email AWS sends, or alarms will not reach you. At first boot the host
installs Docker, the Compose plugin (checked against its published SHA-256) and the ECR credential
helper; it runs nothing until a release is installed.

An account can have only one OIDC provider for GitHub. If `aws iam list-open-id-connect-providers`
already lists `token.actions.githubusercontent.com`, add `GitHubOidcProviderArn=<its ARN>` to the
parameters, or the stack fails to create. `GitHubRepository` (this repository) and
`GitHubEnvironment` (`aws`) name the only workflow jobs the release role trusts; `MonthlyBudgetUsd`
sets the budget.

## 3. Set up releases from GitHub

Releases come from the **Release to AWS** workflow (`.github/workflows/release.yml`), run by hand;
a merge never releases. It runs in the GitHub environment `aws` and signs in to AWS with the job's
OIDC token, so no AWS key is stored anywhere. Create the environment and give it the stack's
outputs as variables (none of them is secret):

```sh
REPO=YIEXUWOSHIDUDE/job-fit-materials-workbench
output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
gh api -X PUT "repos/$REPO/environments/aws" > /dev/null
gh variable set AWS_REGION --repo "$REPO" --env aws --body "$AWS_REGION"
gh variable set AWS_RELEASE_ROLE_ARN --repo "$REPO" --env aws --body "$(output ReleaseRoleArn)"
gh variable set AWS_REPOSITORY_URI --repo "$REPO" --env aws --body "$(output RepositoryUri)"
gh variable set AWS_HOST_ID --repo "$REPO" --env aws --body "$(output HostId)"
gh variable set AWS_RELEASE_DOCUMENT --repo "$REPO" --env aws --body "$(output ReleaseDocumentName)"
```

Then, on GitHub, under Settings → Environments → `aws`: add yourself as a **required reviewer**, and
under deployment branches allow **`main` only**. Every release then waits for your approval, and a
job from another branch cannot take on the release role.

The repository is public, so the release runs' logs are public too. They show the AWS account ID
(it is part of the registry's address), the host's ID and what the install printed: the release,
its revision, and the names of any files the data check flags. They never show data, keys or
tokens; the workflow masks the credentials it gets.

## 4. Push the first release, format the data volume, install

Push the first release **without installing it**: in the Actions tab, run Release to AWS with
"Install it on the host" turned off, or

```sh
gh workflow run release.yml --repo "$REPO" --ref main -f install=false
```

and approve it when GitHub asks. The run's "Sign in" step prints the token's claims: `sub` must read
`repo:YIEXUWOSHIDUDE/job-fit-materials-workbench:environment:aws`, which is what the release role
trusts. When the run has finished, its summary names the image; or look it up:

```sh
git fetch origin main
COMMIT=$(git rev-parse origin/main)  # the commit the run built
REPOSITORY=$(output RepositoryUri)
RELEASE="$REPOSITORY@$(aws ecr describe-images --repository-name "${REPOSITORY#*/}" \
  --image-ids imageTag="$COMMIT" --query 'imageDetails[0].imageDigest' --output text)"
echo "$RELEASE"
```

Commands run on the host through Session Manager. This helper waits until a command has ended,
prints what it printed, and fails when it failed (works in bash and zsh):

```sh
HOST=$(output HostId)
run_on_host() {
  local id state
  id=$(aws ssm send-command --instance-ids "$HOST" --document-name AWS-RunShellScript \
    --parameters "$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]]}))' "$1")" \
    --query Command.CommandId --output text) || return
  state=Pending
  while :; do
    case $state in Pending|InProgress|Delayed|Cancelling) sleep 5 ;; *) break ;; esac
    if ! state=$(aws ssm get-command-invocation --command-id "$id" --instance-id "$HOST" \
        --query Status --output text 2>&1); then
      # Right after sending, the command may not be found yet; any other error ends the wait.
      case $state in *InvocationDoesNotExist*) state=Pending ;; *) echo "$state (command $id)" >&2; return 1 ;; esac
    fi
  done
  aws ssm get-command-invocation --command-id "$id" --instance-id "$HOST" --query StandardOutputContent --output text
  aws ssm get-command-invocation --command-id "$id" --instance-id "$HOST" --query StandardErrorContent --output text >&2
  [ "$state" = Success ] || { echo "on the host: $state" >&2; return 1; }
}
```

**The first time only**, format the new data volume; nothing formats it by itself. This takes
`format-data.sh` from the release and runs it. It formats only the volume whose ID you give, and only
if that is this host's data volume, the stack made it, it is not mounted, and it reads as blank from
end to end (reading it all takes a few minutes). Skip this if you gave the stack a `DataVolumeId`:
that volume already holds your data, and the script refuses it.

```sh
VOLUME=$(output DataVolumeId)
run_on_host "set -o pipefail; docker pull $RELEASE && \
  docker run --rm --entrypoint cat $RELEASE /app/deploy/aws/host/format-data.sh | bash -s $VOLUME"
```

Then install the release. `workbench-release` pulls the image, takes `install.sh` from that same
image, and runs it. The host's compose files, seccomp profile, scripts and systemd units come from
the image, so the host runs the files that were tested with it. The first install also makes the
first backup.

```sh
run_on_host "/usr/local/sbin/workbench-release $RELEASE"
```

Every later release goes through the workflow; see [Releasing and rolling back](#releasing-and-rolling-back).

## 5. Open the workbench

```sh
aws ssm start-session --target "$HOST" --document-name AWS-StartPortForwardingSession \
  --parameters portNumber=8765,localPortNumber=8765
```

Leave it running and open <http://127.0.0.1:8765/>. Nothing on the host listens beyond its loopback,
and the security group lets nothing in.

## 6. A first run with synthetic data

Before any real data goes to AWS, check persistence and recovery with the synthetic run
(`deploy/smoke.py`: a synthetic CV, facts, a job, an approved CV, English and Chinese PDFs printed by
Chromium in the container). It refuses a folder with anything real in it.

```sh
SMOKE="cd /opt/workbench/current && set -a && . /etc/workbench/env && . /etc/workbench/release && set +a && \
  docker compose -f compose.yaml -f deploy/aws/compose.aws.yaml run --rm --no-deps workbench python deploy/smoke.py"
run_on_host "systemctl stop workbench && { $SMOKE create --data /data --out /data/smoke; ran=\$?; systemctl start workbench; exit \$ran; }"
```

Then check each of these, and write down what happened:

| Check | How |
|---|---|
| The data is there after the container is recreated | `run_on_host "systemctl restart workbench"`, then `run_on_host "$SMOKE verify --data /data"` |
| The data volume is mounted before the app starts after a reboot | `run_on_host "reboot"`, wait, then `run_on_host "findmnt /srv/workbench && systemctl is-active workbench && $SMOKE verify --data /data"` |
| A backup reaches S3, and restores | `run_on_host "systemctl start workbench-backup && stat -c %y /var/lib/workbench/last-backup"` (the time is recorded only after the host restored the archive and verified it) and `aws s3 ls s3://BUCKET/backups/` |
| It restores and the workbench can use it | `run_on_host "/opt/workbench/current/deploy/aws/host/restore.sh latest && $SMOKE verify --data /data"` |
| The logs hold no token or CV text | In CloudWatch Logs Insights on `/$STACK/app`: `fields @message \| filter @message like /token=/ or @message like /Example Corp/` finds nothing |
| Nothing is reachable from the internet | `aws ec2 describe-security-groups --filters Name=vpc-id,Values=VPC --query 'SecurityGroups[].IpPermissions'` shows no inbound rule; a connection to the host's public address on 8765 or 22 times out |

## 7. Move your real data

Only after every check in step 6 passes. On your Mac, with the workbench stopped:

```sh
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
.venv/bin/python backup.py create --data .local --out "$HOME/workbench-$STAMP.tar.gz" --revision "$(git rev-parse HEAD)"
BUCKET=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='BackupBucketName'].OutputValue" --output text)
aws s3 cp "$HOME/workbench-$STAMP.tar.gz" "s3://$BUCKET/backups/workbench-$STAMP.tar.gz"
run_on_host "/opt/workbench/current/deploy/aws/host/restore.sh backups/workbench-$STAMP.tar.gz" \
  && rm "$HOME/workbench-$STAMP.tar.gz"
```

The restore checks the archive against its manifest and the workbench's own checks (whole
databases, approvals that still match their CVs and the confirmed facts) before it replaces anything;
the synthetic data it replaces stays on the volume as `data.before-<time>` until you delete it. The
copy on your Mac is deleted only if the restore succeeded.

## Releasing and rolling back

For every later release, run Release to AWS (install on), or
`gh workflow run release.yml --repo "$REPO" --ref main`, and approve it. The workflow builds the
commit for linux/amd64, runs the tests inside the image, pushes it tagged with the commit (a tag is
never pushed twice), and sends the host the release command with the image's digest. The install
then:

1. refuses a release whose data format is older than the data's, before anything is stopped (see
   the data format below);
2. stops the workbench, so the data cannot change in between, and runs two read-only checks of the
   data (`backup.py verify`): the new release's and the last good release's. If the new one finds a
   problem the last good one does not, nothing is switched, the workbench starts again as it was,
   and the run fails;
3. switches to the new release and waits up to two minutes for it to start and answer. If it does
   not, the last good release is installed again and the run fails; its log says why.

The last good release is the last one that passed all of this; an install cut short never becomes
it. The host keeps the running release's image and the good release before it; every tagged release
stays in the registry until you delete it. The install itself never replaces or rewrites the data
folder.

To go back by hand to any earlier release, install its digest; it goes through the same checks:

```sh
aws ecr describe-images --repository-name "${REPOSITORY#*/}" \
  --query 'sort_by(imageDetails, &imagePushedAt)[-5:].[imagePushedAt, imageTags[0], imageDigest]' --output table
run_on_host "/usr/local/sbin/workbench-release $REPOSITORY@sha256:<the digest>"
```

**Data format.** `workspace.DATA_FORMAT` says how the data folder stores things as a whole; every
release so far uses format 1, so any of them can read the data. Each start of the workbench records
its format in the data folder (`.workbench-format`), so the record goes with the data, its backups
and a replaced host; a release refuses to start on data a newer one has recorded, and the install
refuses it before anything stops. A release that stores data so an older one could not read it must
raise the number. Installing such a release:

- needs a backup from the last hour (`run_on_host "systemctl start workbench-backup"` first);
- is never undone automatically: once it has started, the data may be in the new format. If it
  fails, the run says so, and going back is by hand: stop the workbench
  (`run_on_host "systemctl stop workbench"`), restore that backup with `restore.sh`, then install the
  release before.

Backups record their format, and no release restores a backup newer than itself.

## Backups and restoring

- Every night at 03:30 UTC (give or take 15 minutes) the host stops the workbench for the moment its
  data is packed, starts it again, and uploads the archive to `s3://BUCKET/backups/`. Then it
  restores the archive into a scratch folder on the root disk (if it has room for another copy of
  the data) and runs the workbench's checks on it; only a backup that passed counts as the last
  backup (and resets the 26-hour alarm). The archive's
  manifest names the release and every file's SHA-256. Unsaved uploads (they hold contact details and
  expire within a day) and temporary files are left out. By hand: `systemctl start workbench-backup`.
- To restore: `restore.sh latest`, or `restore.sh backups/workbench-<time>.tar.gz`. The backup is
  restored into a new folder on the volume and checked; only then is the workbench stopped and the
  folders swapped. The data it replaced stays as `/srv/workbench/data.before-<time>`. If the host
  stops between the two renames of the swap, the next start of the workbench puts the old data back.
- Backups, restores and installs wait for one another: a backup or restore that finds another one
  running is refused, and an install waits up to 15 minutes. Each first checks that the data volume
  is the one mounted.
- Backups expire after `BackupRetentionDays` (35 by default). An overwritten backup stays for 30 more
  days as an earlier version. The host cannot delete backups.

## Stopping and cleaning up

- **Stop the host** (`aws ec2 stop-instances --instance-ids "$HOST"`): the app stops; the volumes,
  backups, images and alarms keep costing (the not-answering alarm will fire). Start it again with
  `aws ec2 start-instances`; the workbench starts once the data volume is mounted.
- **Delete the stack** (`aws cloudformation delete-stack --stack-name "$STACK"`): removes the host,
  network, registry and its images, log group, alarms, budget, and the release role and command.
  **The data volume and the backup bucket are kept**, and so is the OIDC provider for GitHub, which
  other workflows in the account may use (it costs nothing; a new stack needs its ARN as
  `GitHubOidcProviderArn`). To carry on with the same data, create the stack again with
  `DataVolumeId=<the DataVolumeId output>` in the same availability zone; the new stack gets a new
  bucket, so copy any backups you still want into it. Then delete the `aws` environment on GitHub,
  whose variables name what is gone.
- **Delete the data for good**: `aws ec2 delete-volume --volume-id <vol-…>` and, for the bucket, empty
  it (all versions; the console's "Empty" button does this) and delete it. This cannot be undone.

## Replacing the host

Changing the instance type stops and starts the same host. Changing the image replaces the host, and
CloudFormation would try to attach the data volume to the new host while the old one still has it,
so the update fails and rolls back. To move to a new image: back up, delete the stack (the volume and
bucket stay), and create it again with `DataVolumeId` and the new `ImageId`.

## What is not verified

No AWS account was used while building this, so none of it has run on AWS yet. What was checked:
the template passes cfn-lint and `deploy/aws/check-template.py` (nothing inbound, IMDSv2 with one hop,
kept and encrypted data and backups, no delete for the host, no tagged release expired, a first-boot
script that parses); the host scripts' logic runs in CI with `aws`, `docker`, `systemctl` and `curl`
stubbed, and the format and mount scripts on real ext4 loop devices, on Ubuntu; the container, the
smoke run and a backup restored into an empty folder pass in CI. The stubs stand in for systemd, so
how systemd really orders a restart against a backup, and whether a swap survives a real power cut,
were reasoned about, not observed. Not yet verified: the first boot on Amazon Linux 2023 (the package
`amazon-ecr-credential-helper`, the Compose plugin, the NVMe device name of the data volume),
Chromium's sandbox under that kernel, the awslogs driver and the alarms, Session Manager port
forwarding and Run Command (the `run_on_host` helper above), and every step of the checklist above. The release workflow has not run: the OIDC sign-in, the
environment's protection rules, the release command's pattern as SSM applies it, and the budget are
unverified; actionlint checks the workflow's syntax and shell, and `check-template.py` the release
role's trust and the command's pattern (with Python's regular expressions). The acceptance checks
and their results so far are in [aws-acceptance.md](aws-acceptance.md).

The host role gives the Session Manager agent only the actions AWS documents for it, not AWS's
managed policy (which would also let the host read every Parameter Store parameter); if the host
never shows up in Session Manager, that is the first thing to check, and attaching
`AmazonSSMManagedInstanceCore` to the role is the fallback.
