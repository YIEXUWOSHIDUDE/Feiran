#!/bin/bash
# What the host scripts do, with aws, docker, systemctl, curl and the mount check replaced by
# stubs that record how they were called. Runs in CI on Linux; needs no AWS account, no Docker and
# no root. (deploy/aws/test-mount-data.sh tests the mount and format scripts on real devices.)
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
host=$repo/deploy/aws/host
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
calls=$work/calls
fail() { echo "FAIL: $*" >&2; exit 1; }
called() { grep -qF -- "$1" "$calls"; }
order() { grep -o "$1" "$calls" | tr '\n' '|'; }

mkdir -p "$work/bin" "$work/etc" "$work/run" "$work/volume/data" "$work/state" "$work/units" "$work/home"
cat > "$work/bin/aws" <<'EOF'
#!/bin/bash
echo "aws $*" >> "$CALLS"
case "$1 $2" in
    "secretsmanager get-secret-value") printf '%s\n' "${FAKE_SECRET-}" ;;
    "s3 cp")
        [ "${FAKE_S3_FAILS:-0}" = 0 ] || exit 1
        case "$3" in s3://*) echo "an archive" > "$4" ;; esac ;;
    "s3api list-objects-v2") echo "${FAKE_LATEST:-backups/workbench-20261001T033000Z.tar.gz}" ;;
esac
EOF
cat > "$work/bin/systemctl" <<'EOF'
#!/bin/bash
# Keeps the workbench unit's state in a file, as systemd would.
echo "systemctl $*" >> "$CALLS"
case "$1" in
    is-active) cat "$UNIT_STATE"; [ "$(cat "$UNIT_STATE")" = active ] ;;
    stop) [ "$2" != workbench.service ] || echo inactive > "$UNIT_STATE" ;;
    start | restart) [ "$2" != workbench.service ] || echo active > "$UNIT_STATE" ;;
esac
EOF
cat > "$work/bin/curl" <<'EOF'
#!/bin/bash
echo "curl $*" >> "$CALLS"
[ "${FAKE_HEALTHY:-1}" = 1 ] || exit 7
EOF
cat > "$work/bin/df" <<'EOF'
#!/bin/bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/fake 100000000 0 ${FAKE_FREE_KB:-99999999} 0% /"
EOF
cat > "$work/bin/mount-data" <<'EOF'
#!/bin/bash
echo "mount-data $*" >> "$CALLS"
[ "${FAKE_UNMOUNTED:-0}" = 0 ] || { echo "refused: not mounted" >&2; exit 1; }
EOF
cat > "$work/bin/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
case "$1" in
    run)
        spool=$(printf '%s\n' "$@" | sed -n 's|^\(.*\):/backups$|\1|p')
        volume=$(printf '%s\n' "$@" | sed -n 's|^\(.*\):/volume$|\1|p')
        check=$(printf '%s\n' "$@" | sed -n 's|^\(.*\):/check$|\1|p')
        case " $* " in
            *" backup.py create "*)
                [ "${FAKE_CREATE_FAILS:-0}" = 0 ] || exit 1
                touch "$spool/$(basename "${!#}")"
                [ "${FAKE_DATA_PROBLEMS:-0}" = 0 ] || exit 1 ;;
            *" backup.py restore "*)
                into=${!#}
                case "$into" in /check/*) into=$check/${into#/check/} ;; *) into=$volume/${into#/volume/} ;; esac
                mkdir -p "$into" && echo restored > "$into/facts" ;;
            *" backup.py verify "*) [ "${FAKE_VERIFY_FAILS:-0}" = 0 ] || exit 1 ;;
        esac ;;
    create) echo container-1 ;;
    cp) cp -R "$REPO/${2#container-1:/app/}" "$3" ;;
    inspect) echo rev-abc123 ;;
esac
EOF
chmod +x "$work/bin/"*
export PATH="$work/bin:$PATH" CALLS=$calls REPO=$repo UNIT_STATE=$work/unit-state MOUNT_DATA=$work/bin/mount-data
export WORKBENCH_ENV=$work/etc/env WORKBENCH_RELEASE_ENV=$work/etc/release WORKBENCH_STATE=$work/state
export WORKBENCH_HOME=$work/home SYSTEMD_DIR=$work/units WORKBENCH_UID WORKBENCH_GID
WORKBENCH_UID=$(id -u)
WORKBENCH_GID=$(id -g)
image="123456789012.dkr.ecr.us-east-1.amazonaws.com/job-fit-workbench@sha256:$(printf 'a%.0s' {1..64})"
host_env() {  # the host's settings; the workbench's state (active, activating, inactive)
    printf 'AWS_REGION=us-east-1\nBACKUP_BUCKET=test-bucket\nLOG_GROUP=test-logs\nDEEPSEEK_KEY_FILE=%s\nDATA_DEVICE=/dev/fake-data\nDATA_MOUNT=%s\nWORKBENCH_DATA_DIR=%s\nDEEPSEEK_SECRET_ARN=%s\n' \
        "$work/run/workbench/key" "$work/volume" "$work/volume/data" "${2-}" > "$WORKBENCH_ENV"
    rm -f "$WORKBENCH_RELEASE_ENV"  # after an install, a link into the release; never written through
    printf 'WORKBENCH_IMAGE=%s\nWORKBENCH_REVISION=rev-abc123\n' "$image" > "$WORKBENCH_RELEASE_ENV"
    echo "${1:-inactive}" > "$UNIT_STATE"
    : > "$calls"
}
unit_state() { cat "$UNIT_STATE"; }

# fetch-secret.sh
host_env inactive ""
"$host/fetch-secret.sh" | grep -q "without DeepSeek" || fail "no secret named was not said"
if [ ! -f "$work/run/workbench/key" ] || [ -s "$work/run/workbench/key" ]; then fail "no secret named left no empty key file"; fi
[ "$(stat -c %a "$work/run/workbench/key")" = 400 ] || fail "the key file is readable by others"
called "aws" && fail "asked AWS for a secret nobody named"
host_env inactive "arn:aws:secretsmanager:us-east-1:123456789012:secret:deepseek-AbCdEf"
out=$(FAKE_SECRET=sk-test-key-123 "$host/fetch-secret.sh" 2>&1)
[ "$(cat "$work/run/workbench/key")" = sk-test-key-123 ] || fail "the key was not put in place"
case "$out $(cat "$calls")" in *sk-test-key*) fail "the key was printed or passed on a command line" ;; esac
called "--secret-id arn:aws:secretsmanager:us-east-1:123456789012:secret:deepseek-AbCdEf" || fail "the named secret was not asked for"
if FAKE_SECRET="   " "$host/fetch-secret.sh" 2>/dev/null; then fail "an empty secret was accepted"; fi
[ "$(cat "$work/run/workbench/key")" = sk-test-key-123 ] || fail "a refused secret replaced the key"
[ -z "$(find "$work/run/workbench" -name '.key.*')" ] || fail "a temporary key file was left behind"
echo "ok   fetch-secret: the named secret, or none; never printed; an empty one refused"

# backup.sh
host_env active
"$host/backup.sh" > /dev/null
[ "$(order 'mount-data\|systemctl stop workbench.service\|backup.py create\|systemctl start workbench.service\|backup.py restore\|backup.py verify\|aws s3 cp')" \
    = "mount-data|systemctl stop workbench.service|backup.py create|systemctl start workbench.service|backup.py restore|backup.py verify|aws s3 cp|" ] \
    || fail "not: mount checked, stopped, packed, started again, restored and verified, uploaded"
called "s3://test-bucket/backups/workbench-" || fail "not uploaded under backups/"
[ -e "$work/state/last-backup" ] || fail "a backup that worked was not recorded"
[ -z "$(ls "$work/state/backups")" ] || fail "the local archive was kept"
[ -z "$(find "$work/state" "$work/volume" -maxdepth 1 -name 'backup-check-*')" ] || fail "the restored copy that was checked was left behind"
called "-v $work/state/backup-check-" || fail "the backup was not checked on the root disk"
[ "$(unit_state)" = active ] || fail "the workbench was not started again"
host_env activating
"$host/backup.sh" > /dev/null
called "systemctl stop workbench.service" || fail "a workbench that was starting was not stopped first"
[ "$(unit_state)" = active ] || fail "a workbench that was starting was not started again"
host_env inactive
"$host/backup.sh" > /dev/null
called "systemctl start workbench.service" && fail "started a workbench that had not been running"
host_env active
rm -f "$work/state/last-backup"
if FAKE_UNMOUNTED=1 "$host/backup.sh" > /dev/null 2>&1; then fail "a backup ran without the data volume mounted"; fi
if called "systemctl stop" || called "docker run"; then fail "stopped or copied before finding the volume unmounted"; fi
for broken in FAKE_CREATE_FAILS FAKE_DATA_PROBLEMS FAKE_VERIFY_FAILS FAKE_S3_FAILS FAKE_FREE_KB; do
    host_env active
    if env "$broken=1" "$host/backup.sh" > /dev/null 2>&1; then fail "$broken: reported as a backup that worked"; fi
    [ -e "$work/state/last-backup" ] && fail "$broken: recorded as a backup that worked"
    [ "$(unit_state)" = active ] || fail "$broken: the workbench stayed stopped"
    [ -z "$(find "$work/state" "$work/volume" -maxdepth 1 -name 'backup-check-*')" ] || fail "$broken: the checked copy was left behind"
done
host_env active
FAKE_FREE_KB=1 "$host/backup.sh" > /dev/null 2>&1 || true
called "backup.py restore" && fail "restored a backup to check it on a disk without room for it"
called "aws s3 cp" || fail "a backup that could not be checked was not uploaded (it is still the best copy)"
host_env active
FAKE_CREATE_FAILS=1 "$host/backup.sh" > /dev/null 2>&1 || true
called "aws s3" && fail "uploaded when no archive was made"
host_env active
FAKE_VERIFY_FAILS=1 "$host/backup.sh" > /dev/null 2>&1 || true
called "aws s3 cp" || fail "an archive that did not verify was not uploaded (it is still the best copy)"
if (flock -n 9 && "$host/backup.sh" > /dev/null 2>&1) 9> "$work/state/data.lock"; then fail "a backup ran beside a restore or an install"; fi
echo "ok   backup: only on the volume; stopped even while starting, started again only if it ran; counted only when it restores and verifies"

# restore.sh
echo current > "$work/volume/data/facts"
host_env active
"$host/restore.sh" latest > /dev/null
[ "$(order 'mount-data\|aws s3api list-objects-v2\|aws s3 cp\|backup.py restore\|backup.py verify\|systemctl stop workbench.service\|systemctl start workbench.service')" \
    = "mount-data|aws s3api list-objects-v2|aws s3 cp|backup.py restore|backup.py verify|systemctl stop workbench.service|systemctl start workbench.service|" ] \
    || fail "not: mount checked, downloaded, restored, checked, then swapped with the workbench stopped"
called "s3://test-bucket/backups/workbench-20261001T033000Z.tar.gz" || fail "the latest backup was not the one restored"
[ "$(cat "$work/volume/data/facts")" = restored ] || fail "the restored data is not in place"
[ -e "$work/volume/data/.workbench-data" ] || fail "the restored data was not marked as the volume's (a laptop backup has no marker)"
kept=$(find "$work/volume" -maxdepth 1 -name 'data.before-*')
if [ -z "$kept" ] || [ "$(cat "$kept/facts")" != current ]; then fail "the data it replaced was not kept"; fi
[ -z "$(find "$work/volume" -maxdepth 1 -name 'restore-*')" ] || fail "the download or the swap's record was left behind"
host_env active
if FAKE_VERIFY_FAILS=1 "$host/restore.sh" latest > /dev/null 2>&1; then fail "a backup that failed its check was put in place"; fi
called "systemctl stop" && fail "the workbench was stopped for a backup that failed its check"
[ "$(cat "$work/volume/data/facts")" = restored ] || fail "a backup that failed its check replaced the data"
[ -z "$(find "$work/volume" -maxdepth 1 -name 'restore-*')" ] || fail "a refused restore left its download behind"
host_env active
for key in "backups/../cv-profile.json" "backups/other.tar.gz" "s3://elsewhere/workbench.tar.gz"; do
    if "$host/restore.sh" "$key" > /dev/null 2>&1; then fail "restored $key"; fi
done
if FAKE_LATEST=None "$host/restore.sh" latest > /dev/null 2>&1; then fail "restored when there was no backup"; fi
if FAKE_UNMOUNTED=1 "$host/restore.sh" latest > /dev/null 2>&1; then fail "restored without the data volume mounted"; fi
called "aws s3 cp" && fail "downloaded something that is not a backup, or onto a disk that is not the volume"
if (flock -n 9 && "$host/restore.sh" latest > /dev/null 2>&1) 9> "$work/state/data.lock"; then fail "a restore ran beside a backup or an install"; fi
echo "ok   restore: only onto the volume; checked before it replaces anything; the old data kept; one at a time"

# report-health.sh
host_env active
rm -f "$work/state/last-backup"
FAKE_HEALTHY=1 "$host/report-health.sh" > /dev/null
called "MetricName=Healthy,Value=1,Unit=Count MetricName=BackupAgeHours,Value=9999,Unit=Count" \
    || fail "healthy and never backed up was not reported as such"
host_env active
touch "$work/state/last-backup"
FAKE_HEALTHY=0 "$host/report-health.sh" > /dev/null
called "MetricName=Healthy,Value=0,Unit=Count MetricName=BackupAgeHours,Value=0,Unit=Count" \
    || fail "down with a fresh backup was not reported as such"
echo "ok   report-health: whether it answers, and the backup's age"

# install.sh
host_env inactive
if "$host/install.sh" "123456789012.dkr.ecr.us-east-1.amazonaws.com/job-fit-workbench:latest" 2>/dev/null; then
    fail "a tag was installed"
fi
rm -f "$work/state/last-backup"
: > "$calls"
"$host/install.sh" "$image" > /dev/null
release=$(readlink -f "$work/home/current")
[ "$release" = "$work/home/releases/aaaaaaaaaaaa" ] || fail "current does not point at the release"
if [ ! -f "$release/compose.yaml" ] || [ ! -x "$release/deploy/aws/host/mount-data.sh" ]; then fail "the host files were not taken from the image"; fi
if [ ! -f "$work/units/workbench.service" ] || [ ! -f "$work/units/workbench-health.timer" ]; then fail "the units were not installed"; fi
grep -qx "WORKBENCH_IMAGE=$image" "$WORKBENCH_RELEASE_ENV" || fail "the release's image was not recorded"
grep -qx "WORKBENCH_REVISION=rev-abc123" "$WORKBENCH_RELEASE_ENV" || fail "the release's revision was not recorded"
[ "$(readlink -f "$WORKBENCH_RELEASE_ENV")" = "$release/release.env" ] \
    || fail "the release's image is not switched together with its files"
called "systemctl restart workbench.service" || fail "the workbench was not restarted"
called "systemctl start workbench-backup.service" || fail "the first install made no first backup"
called "docker rm -f container-1" || fail "the container the files came from was left behind"
touch "$work/state/last-backup"
: > "$calls"
"$host/install.sh" "$image" > /dev/null
called "systemctl start workbench-backup.service" && fail "a later install made a backup"
called "docker create" && fail "the files of a release already installed were taken again"
[ -f "$work/home/releases/aaaaaaaaaaaa/compose.yaml" ] || fail "installing a release again removed its files"
if FAKE_HEALTHY=0 HEALTH_WAIT_ATTEMPTS=2 HEALTH_WAIT_SECONDS=0 "$host/install.sh" "$image" > /dev/null 2>&1; then
    fail "a release that never became healthy was reported installed"
fi
if (flock -n 9 && LOCK_WAIT_SECONDS=0 "$host/install.sh" "$image" > /dev/null 2>&1) 9> "$work/state/data.lock"; then
    fail "an install ran beside a backup or a restore"
fi
echo "ok   install: by digest only; host files from the image, never changed once installed; one at a time; the first install makes the first backup"
echo "host scripts: all checks passed"
