#!/bin/bash
# The data volume is formatted only on purpose (format-data.sh, the volume named, blank, not
# mounted) and only mounted after that; a volume that is refused is left exactly as it was; a
# restore that stopped between its two renames is undone. Runs in CI on loop devices; needs sudo,
# losetup, mkfs.ext4 and blkid.
set -euo pipefail

host="$(cd "$(dirname "$0")" && pwd)/host"
work=$(mktemp -d)
devices=()
cleanup() {
    mountpoint -q "$work/mnt" && sudo umount "$work/mnt"
    for device in "${devices[@]}"; do sudo losetup -d "$device" || true; done
    sudo rm -rf "$work"
}
trap cleanup EXIT
mkdir -p "$work/by-id" "$work/mnt"
new_device() {  # a loop device, and in $by_id the name EBS would give volume vol-<$1>
    truncate -s 64M "$work/$1.img"
    local device
    device=$(sudo losetup --find --show "$work/$1.img")
    devices+=("$device")
    by_id=$work/by-id/nvme-Amazon_Elastic_Block_Store_vol$1
    ln -sfn "$device" "$by_id"
}
host_env() {  # the device; whether the stack made the volume (1, the default) or was given it (0)
    printf 'DATA_DEVICE=%s\nDATA_MOUNT=%s\nDATA_VOLUME_NEW=%s\n' "$1" "$work/mnt" "${2:-1}" > "$work/env"
}
format() { sudo env WORKBENCH_ENV="$work/env" "$host/format-data.sh" "$@"; }
mount_data() { sudo env MOUNT_WAIT_SECONDS=2 "$host/mount-data.sh" "$1" "$work/mnt"; }
checksum() { sudo sha256sum "$(readlink -f "$1")" | cut -d' ' -f1; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# A new volume: never formatted by mounting; formatted once, on purpose.
new_device 0123456789abcdef0
blank=$by_id
before=$(checksum "$blank")
if mount_data "$blank" 2> /dev/null; then fail "a blank volume was mounted"; fi
[ "$(checksum "$blank")" = "$before" ] || fail "mounting changed a blank volume (formatted it?)"
host_env "$blank" 0
if format vol-0123456789abcdef0 2> /dev/null; then fail "a volume given to the stack (DataVolumeId) was formatted"; fi
host_env "$blank"
if format vol-0fedcba9876543210 2> /dev/null; then fail "a volume other than the one named was formatted"; fi
[ "$(checksum "$blank")" = "$before" ] || fail "a refused format changed the volume"
format vol-0123456789abcdef0 > /dev/null
mount_data "$blank" > /dev/null
[ -f "$work/mnt/data/.workbench-data" ] || fail "the formatted volume has no data folder with the marker"
[ "$(stat -c %u:%g "$work/mnt" "$work/mnt/data" "$work/mnt/data/.workbench-data" | sort -u)" = 10001:10001 ] \
    || fail "not owned by the app's user"
[ "$(sudo ls -A "$work/mnt/data")" = .workbench-data ] || fail "the data folder holds more than the marker"
if format vol-0123456789abcdef0 2> /dev/null; then fail "a mounted volume was formatted"; fi
echo "a fact" | sudo tee "$work/mnt/data/facts.txt" > /dev/null
mount_data "$blank" > /dev/null || fail "a restart of the workbench was refused"
sudo umount "$work/mnt"
if format vol-0123456789abcdef0 2> /dev/null; then fail "a volume holding a file system was formatted again"; fi
mount_data "$blank" > /dev/null
grep -q "a fact" "$work/mnt/data/facts.txt" || fail "the data did not survive"
echo "ok   a new volume is formatted only on purpose, once; after that it is only mounted"

# A restore that stopped between its two renames is undone at the next start: the workbench's
# own restart, or the host's after a reboot (the volume not yet mounted, data/ still aside).
for when in "at a restart" "after a reboot"; do
    sudo mv -T "$work/mnt/data" "$work/mnt/data.before-20261001T033000Z-0a1b2c3d"
    printf 'kept=data.before-20261001T033000Z-0a1b2c3d\n' | sudo tee "$work/mnt/restore-in-progress" > /dev/null
    [ "$when" = "at a restart" ] || sudo umount "$work/mnt"
    mount_data "$blank" > /dev/null 2>&1 || fail "$when: a restore cut short stopped the next start"
    grep -q "a fact" "$work/mnt/data/facts.txt" || fail "$when: the data a cut-short restore moved aside was not put back"
    [ ! -e "$work/mnt/restore-in-progress" ] || fail "$when: the restore's record was left behind"
done
sudo umount "$work/mnt"
echo "ok   a restore cut short between its renames is undone, at a restart or after a reboot"

# Something else is never mounted read-write, formatted or changed.
new_device 00000000000000001
other=$by_id
sudo mount "$(readlink -f "$blank")" "$work/mnt"
if mount_data "$other" 2> /dev/null; then fail "accepted a mount point holding another device"; fi
sudo umount "$work/mnt"
sudo mkfs.ext4 -q "$(readlink -f "$other")"
sudo mount "$(readlink -f "$other")" "$work/mnt"
sudo touch "$work/mnt/.workbench-data"  # a marker, but not in a data folder
sudo umount "$work/mnt"
before=$(checksum "$other")
if mount_data "$other" 2> /dev/null; then fail "a file system without data/.workbench-data was mounted"; fi
mountpoint -q "$work/mnt" && fail "left mounted after refusing"
[ "$(checksum "$other")" = "$before" ] || fail "a refused file system was changed"
host_env "$other"
if format vol-00000000000000001 2> /dev/null; then fail "a volume holding a file system was formatted"; fi
new_device 00000000000000002
dirty=$by_id
sudo dd if=/dev/urandom of="$(readlink -f "$dirty")" bs=1M count=1 status=none
before=$(checksum "$dirty")
host_env "$dirty"
if format vol-00000000000000002 2> /dev/null; then fail "a volume holding data but no file system was formatted"; fi
if mount_data "$dirty" 2> /dev/null; then fail "a volume holding data but no file system was mounted"; fi
[ "$(checksum "$dirty")" = "$before" ] || fail "a volume holding data was changed"
new_device 00000000000000003  # blank at the start, data further in (a damaged old volume, say)
deep=$by_id
sudo dd if=/dev/urandom of="$(readlink -f "$deep")" bs=1M count=1 seek=32 status=none
before=$(checksum "$deep")
host_env "$deep"
if format vol-00000000000000003 2> /dev/null; then fail "a volume holding data past its first MiB was formatted"; fi
[ "$(checksum "$deep")" = "$before" ] || fail "a volume holding data past its first MiB was changed"
echo "ok   another file system, or data without one, is refused and left exactly as it was"

if mount_data "$work/by-id/no-such-device" 2> /dev/null; then fail "a missing device was accepted"; fi
echo "ok   a missing device is refused"
echo "mount-data: all checks passed"
