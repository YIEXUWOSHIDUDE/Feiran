#!/bin/bash
# Make sure the workbench's data volume is mounted, from this device, before anything uses it:
# at every start of the workbench and before every backup or restore. It never formats: a new
# volume is formatted once, on purpose, with format-data.sh.
#
#   mount-data.sh <block device> <mount point>
#
# The workbench's data folder is data/ on the volume, holding .workbench-data. It is a folder of
# its own so that the file system's lost+found stays out of it and a backup can be restored into
# a new folder beside it, then swapped in. A volume not yet mounted is first checked read-only,
# without replaying its journal, and mounted read-write only if it holds data/.workbench-data, so
# a volume that is refused is left exactly as it was. A restore that stopped between its two
# renames (restore.sh records it) is undone here. Nothing here reads or prints the data.
set -euo pipefail

device=${1:?usage: mount-data.sh <block device> <mount point>}
mount_point=${2:?usage: mount-data.sh <block device> <mount point>}
marker=data/.workbench-data
refuse() { echo "refused: $*" >&2; exit 1; }
holds_workbench_data() {  # the volume mounted at $1 holds data/.workbench-data, or held it until
    # a restore moved data/ aside and stopped before putting the restored data in its place
    [ -f "$1/$marker" ] && return 0
    local kept
    kept=$(sed -n 's/^kept=//p' "$1/restore-in-progress" 2> /dev/null || true)
    [[ "$kept" =~ ^data\.before-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$ ]] && [ -f "$1/$kept/.workbench-data" ]
}

wait_seconds=${MOUNT_WAIT_SECONDS:-120}  # an EBS volume can take a moment to appear after boot
for _ in $(seq "$wait_seconds"); do
    [ -b "$device" ] && break
    sleep 1
done
[ -b "$device" ] || refuse "no block device at $device"
real_device=$(readlink -f "$device")

if mountpoint -q "$mount_point"; then
    [ "$(findmnt -n -o SOURCE --mountpoint "$mount_point")" = "$real_device" ] \
        || refuse "something other than $device is mounted at $mount_point"
else
    [ "$(blkid -p -o value -s TYPE "$real_device" 2> /dev/null || true)" = ext4 ] \
        || refuse "$device holds no ext4 file system; if it is a new volume, format it once with format-data.sh"
    mkdir -p "$mount_point"
    mount -o ro,noload "$real_device" "$mount_point"
    if ! holds_workbench_data "$mount_point"; then
        umount "$mount_point"
        refuse "$device has no $marker, so it is not this workbench's data; it was left as it was"
    fi
    umount "$mount_point"
    mount -o nodev,nosuid,noexec "$real_device" "$mount_point"
fi

record=$mount_point/restore-in-progress
if [ -f "$record" ]; then
    kept=$(sed -n 's/^kept=//p' "$record")
    if [[ "$kept" =~ ^data\.before-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$ ]] && [ ! -e "$mount_point/data" ] \
            && [ -d "$mount_point/$kept" ]; then
        mv -T "$mount_point/$kept" "$mount_point/data"
        echo "a restore stopped between its two renames: the data it replaced is back in place" >&2
    fi
    rm -f "$record"
    sync -f "$mount_point"
fi
[ -f "$mount_point/$marker" ] || refuse "$mount_point has no $marker"
echo "the data volume is mounted at $mount_point"
