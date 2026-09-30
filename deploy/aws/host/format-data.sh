#!/bin/bash
# Format the workbench's data volume, once, when it is new. This erases the volume, so it runs
# only on purpose, only on the volume this host was given (DATA_DEVICE, named by its EBS volume
# ID, which must be typed to confirm), only if the stack made that volume (DATA_VOLUME_NEW=1; one
# given as DataVolumeId held data before), only when it is not mounted, and only when it has no
# signature of any kind and reads as zeros from end to end (reading it all takes a few minutes).
# Fresh EBS blocks can also read as pseudorandom data; that case is deliberately refused here.
# See docs/aws-deployment.md for the snapshot-backed empty-volume check before initialization.
# It makes an ext4 file system with data/.workbench-data, owned by the
# container's user.
#
#   format-data.sh <volume ID>      (the stack's DataVolumeId output, e.g. vol-0123456789abcdef0)
set -euo pipefail
set -a
# shellcheck source=/dev/null
. "${WORKBENCH_ENV:-/etc/workbench/env}"
set +a

volume=${1:?usage: format-data.sh <volume ID, e.g. vol-0123456789abcdef0>}
refuse() { echo "refused: $*" >&2; exit 1; }
[[ "$volume" =~ ^vol-[0-9a-f]{8,17}$ ]] || refuse "$volume is not an EBS volume ID"
[[ "$DATA_DEVICE" == */nvme-Amazon_Elastic_Block_Store_"${volume//-/}" ]] \
    || refuse "$volume is not the data volume this host was given ($DATA_DEVICE)"
[ "${DATA_VOLUME_NEW:-0}" = 1 ] \
    || refuse "$volume was given to the stack (DataVolumeId), so it held data before; it is never formatted here"
[ -b "$DATA_DEVICE" ] || refuse "no block device at $DATA_DEVICE"
device=$(readlink -f "$DATA_DEVICE")
[ -z "$(findmnt -n -S "$device" || true)" ] || refuse "$volume is mounted; this is not a new volume"
set +e
blkid -p "$device" > /dev/null 2>&1
probe=$?  # 0: a signature was found; 2: none at all; anything else: the device could not be read
set -e
[ "$probe" = 2 ] || refuse "$volume already holds something (blkid exit $probe); nothing was changed"
echo "reading all of $volume to be sure it is blank..."
cmp -s -n "$(blockdev --getsize64 "$device")" "$device" /dev/zero || refuse "$volume is not blank; nothing was changed"

mkfs.ext4 -q -L workbench-data "$device"
mkdir -p "$DATA_MOUNT"
mount -o nodev,nosuid,noexec "$device" "$DATA_MOUNT"
mkdir "$DATA_MOUNT/data"
touch "$DATA_MOUNT/data/.workbench-data"
chown "${WORKBENCH_UID:-10001}:${WORKBENCH_GID:-10001}" "$DATA_MOUNT" "$DATA_MOUNT/data" "$DATA_MOUNT/data/.workbench-data"
sync -f "$DATA_MOUNT"
umount "$DATA_MOUNT"
echo "formatted $volume: an empty data folder, ready for the workbench"
