#!/bin/bash
# Put a backup from the private S3 bucket back as the workbench's data. Nothing happens unless
# the data volume itself is mounted. The backup is restored into a new folder on the volume and
# checked there first. V2 stops before reading the live deletion ledger, so an account deleted
# during restoration cannot return; V1 stops after verification. The folders are swapped by
# two renames on the same volume. Stopping also cancels a pending restart. The swap is recorded
# first: if it stops between the renames, the next start of the workbench puts the old data back
# (mount-data.sh). The data it replaces is kept beside it as data.before-<time>, never deleted;
# the workbench is started again if it was running or starting. A backup made on a laptop
# (backup.py create --data .local) restores the same way. Backups, restores and installs run one
# at a time.
#
#   restore.sh latest
#   restore.sh backups/workbench-20261001T033000Z.tar.gz
set -euo pipefail
set -a
# shellcheck source=/dev/null
. "${WORKBENCH_ENV:-/etc/workbench/env}"
# shellcheck source=/dev/null
. "${WORKBENCH_RELEASE_ENV:-/etc/workbench/release}"
set +a

key=${1:?usage: restore.sh latest | backups/workbench-<time>.tar.gz}
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
if [ "$key" = latest ]; then
    key=$(aws s3api list-objects-v2 --region "$AWS_REGION" --bucket "$BACKUP_BUCKET" --prefix backups/ \
        --query 'sort_by(Contents, &LastModified)[-1].Key' --output text)
fi
[[ "$key" =~ ^backups/workbench-[0-9]{8}T[0-9]{6}Z\.tar\.gz$ ]] \
    || { echo "refused: $key is not a backup this workbench made" >&2; exit 1; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
work=restore-$stamp  # on the volume, beside data/, so the swap is a rename
kept=data.before-$stamp
install -d -m 0700 -o "${WORKBENCH_UID:-10001}" -g "${WORKBENCH_GID:-10001}" "$DATA_MOUNT/$work"
running=0
stopped=0
finish() {
    result=$?
    trap - EXIT
    # Cleanup failure must not skip restarting a service this restore stopped.
    rm -rf "${DATA_MOUNT:?}/$work" || result=$?
    if [ "$running" = 1 ]; then systemctl start workbench.service || result=$?; fi
    exit "$result"
}
stop_workbench() {
    [ "$stopped" = 0 ] || return 0
    case "$(systemctl is-active workbench.service || true)" in
        active | activating | reloading) running=1 ;;
        *) running=0 ;;
    esac
    systemctl stop workbench.service
    case "$(systemctl is-active workbench.service || true)" in
        active | activating | reloading | deactivating)
            echo "refused: the workbench did not stop" >&2; exit 1 ;;
    esac
    stopped=1
}
trap finish EXIT
aws s3 cp "s3://$BACKUP_BUCKET/$key" "$DATA_MOUNT/$work/backup.tar.gz" --region "$AWS_REGION" --only-show-errors
chown "${WORKBENCH_UID:-10001}:${WORKBENCH_GID:-10001}" "$DATA_MOUNT/$work/backup.tar.gz"
in_image() {
    if [ "$format" = 2 ]; then
        docker run --rm --network none --read-only --tmpfs /tmp --security-opt no-new-privileges:true \
            -v "$DATA_MOUNT/$work:/restore" -v "$WORKBENCH_DATA_DIR:/data:ro" \
            "$WORKBENCH_IMAGE" python "$backup_tool" "$@"
    else
        docker run --rm --network none --read-only --tmpfs /tmp --security-opt no-new-privileges:true \
            -v "$DATA_MOUNT:/volume" "$WORKBENCH_IMAGE" python "$backup_tool" "$@"
    fi
}
image_work=/volume/$work
if [ "$format" = 2 ]; then
    # Lock out app writes before the ledger is read, and keep them out through the swap.
    # The data.lock above serializes host maintenance, not account deletion inside the app.
    stop_workbench
    image_work=/restore
fi
restore_args=(restore --archive "$image_work/backup.tar.gz" --into "$image_work/data")
if [ "$format" = 2 ]; then restore_args+=(--ledger /data/deleted-accounts.jsonl); fi
in_image "${restore_args[@]}"
in_image verify --data "$image_work/data"  # refuses (and nothing is swapped) if anything is wrong
# A backup made on a laptop has no volume marker; this one is going onto the volume.
if [ ! -e "$DATA_MOUNT/$work/data/.workbench-data" ]; then
    install -m 0644 -o "${WORKBENCH_UID:-10001}" -g "${WORKBENCH_GID:-10001}" /dev/null "$DATA_MOUNT/$work/data/.workbench-data"
fi

stop_workbench
printf 'kept=%s\n' "$kept" > "$DATA_MOUNT/restore-in-progress"
sync -f "$DATA_MOUNT"
mv -T "$DATA_MOUNT/data" "$DATA_MOUNT/$kept"
if ! mv -T "$DATA_MOUNT/$work/data" "$DATA_MOUNT/data"; then
    mv -T "$DATA_MOUNT/$kept" "$DATA_MOUNT/data"
    rm -f "$DATA_MOUNT/restore-in-progress"
    echo "refused: the restored data could not be put in place; the data is as it was" >&2
    exit 1
fi
sync -f "$DATA_MOUNT"
rm -f "$DATA_MOUNT/restore-in-progress"
sync -f "$DATA_MOUNT"
echo "restored $key; the data it replaced is in $DATA_MOUNT/$kept"
