#!/bin/bash
# Back up the data volume to the private S3 bucket (nightly by workbench-backup.timer, or by
# hand). Nothing is copied unless the data volume itself is mounted. The workbench is stopped
# while its data is packed, so the databases and the job files agree (stopping also cancels a
# restart that was pending), and started again afterwards if it was running or starting,
# whatever happened. The archive is then checked the way a restore would use it: restored into a
# new folder and verified there, on the root disk (a second copy beside the live data could fill
# the data volume), if it has room. It goes to S3 either way, as the best copy there is, but only
# an archive that restores and verifies counts as a backup that worked (the time kept for the
# health report and its alarm). Backups, restores and installs run one at a time.
set -euo pipefail
set -a
# shellcheck source=/dev/null
. "${WORKBENCH_ENV:-/etc/workbench/env}"
# shellcheck source=/dev/null
. "${WORKBENCH_RELEASE_ENV:-/etc/workbench/release}"
set +a

here=$(dirname "$(readlink -f "$0")")
state=${WORKBENCH_STATE:-/var/lib/workbench}
exec 9> "$state/data.lock"
flock -n 9 || { echo "refused: a backup, a restore or an install is running" >&2; exit 1; }
"${MOUNT_DATA:-$here/mount-data.sh}" "$DATA_DEVICE" "$DATA_MOUNT" > /dev/null
# An explicit V2 mode also selects V2 on a replacement volume before it has a format
# marker. Restoring there still needs a trustworthy live deletion ledger placed by the operator.
mode=${WORKBENCH_MODE:-}
case "$mode" in
    '' | v1) format=1 ;;
    v2) format=2 ;;
    *) echo "refused: unknown workbench mode $mode" >&2; exit 1 ;;
esac
if [ -e "$WORKBENCH_DATA_DIR/.workbench-format" ]; then
    format=$(cat "$WORKBENCH_DATA_DIR/.workbench-format")
    if [ -n "$mode" ] && [ "$format" != "${mode#v}" ]; then
        echo "refused: mode $mode cannot use data in format $format" >&2; exit 1
    fi
fi
case "$format" in
    1) backup_tool=backup.py ;;
    2) backup_tool=v2_backup.py ;;
    *) echo "refused: unknown data format $format" >&2; exit 1 ;;
esac
spool=$state/backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
name="workbench-$stamp.tar.gz"
install -d -m 0700 -o "${WORKBENCH_UID:-10001}" -g "${WORKBENCH_GID:-10001}" "$spool"
# Archives an earlier run could not upload; they hold personal data, so they do not stay. One
# with this run's name (a run in this same second whose upload failed) is not this run's archive.
find "$spool" -name 'workbench-*.tar.gz' -mtime +2 -delete
rm -f "${spool:?}/$name"
rm -rf "${state:?}"/backup-check-*  # a check a crash cut short

case "$(systemctl is-active workbench.service || true)" in
    active | activating | reloading) running=1 ;;
    *) running=0 ;;
esac
start_again() { if [ "$running" = 1 ]; then systemctl start workbench.service; fi; }
trap start_again EXIT
systemctl stop workbench.service
[ "$(systemctl is-active workbench.service || true)" != active ] || { echo "refused: the workbench did not stop" >&2; exit 1; }
created=0
docker run --rm --network none --read-only --tmpfs /tmp --security-opt no-new-privileges:true \
    -e WORKBENCH_REVISION -v "$WORKBENCH_DATA_DIR:/data:ro" -v "$spool:/backups" \
    "$WORKBENCH_IMAGE" python "$backup_tool" create --data /data --out "/backups/$name" || created=$?
trap - EXIT
start_again
[ -f "$spool/$name" ] || { echo "refused: no archive was made" >&2; exit 1; }

check=$state/backup-check-$stamp
trap 'rm -rf "$check"' EXIT
in_image() {
    docker run --rm --network none --read-only --tmpfs /tmp --security-opt no-new-privileges:true \
        -v "$spool:/backups:ro" -v "$check:/check" -v "$WORKBENCH_DATA_DIR:/data:ro" \
        "$WORKBENCH_IMAGE" python "$backup_tool" "$@"
}
restore_args=(restore --archive "/backups/$name" --into /check/data)
if [ "$format" = 2 ]; then
    # The real source stays read-only even though a disposable copy is being restored.
    restore_args+=(--ledger /data/deleted-accounts.jsonl)
fi
checked=1
need_kb=$(( $(du -sk "$WORKBENCH_DATA_DIR" | cut -f1) + 1048576 ))  # the data again, and 1 GiB to spare
free_kb=$(df -Pk "$state" | awk 'NR == 2 { print $4 }')
if [ "$free_kb" -lt "$need_kb" ]; then
    echo "not checked: restoring this backup needs about $need_kb KiB on the root disk, which has $free_kb" >&2
else
    install -d -m 0700 -o "${WORKBENCH_UID:-10001}" -g "${WORKBENCH_GID:-10001}" "$check"
    if in_image "${restore_args[@]}" > /dev/null \
            && in_image verify --data /check/data > /dev/null; then
        checked=0
    fi
fi
rm -rf "$check"

aws s3 cp "$spool/$name" "s3://$BACKUP_BUCKET/backups/$name" --region "$AWS_REGION" --only-show-errors
rm -f "$spool/$name"
if [ "$created" != 0 ] || [ "$checked" != 0 ]; then
    echo "uploaded s3://$BACKUP_BUCKET/backups/$name, but it does not count as a backup that worked" \
        "(data problems: $created; did not restore and verify: $checked)" >&2
    exit 1
fi
touch "$state/last-backup"
echo "backed up to s3://$BACKUP_BUCKET/backups/$name (restored and verified)"
