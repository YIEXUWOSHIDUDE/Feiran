#!/bin/bash
# Install a release of the workbench on the EC2 host: its image, by digest, and the host files
# that came in it (compose files, seccomp profile, these scripts, the systemd units), so the host
# always runs what was tested with that image. The data volume is never touched. A release's
# files, once installed, are never changed: installing it again reuses them. Installs, backups
# and restores run one at a time. The first install also makes the first backup, so the backup
# alarm starts from a real one.
#
#   install.sh <registry/repository@sha256:...>     (workbench-release pulls the image first)
set -euo pipefail

image=${1:?usage: install.sh <image@sha256:...>}
[[ "$image" =~ ^[^@[:space:]]+@sha256:[0-9a-f]{64}$ ]] \
    || { echo "refused: give the image by its digest (...@sha256:...), not a tag" >&2; exit 1; }
home=${WORKBENCH_HOME:-/opt/workbench}
release_env=${WORKBENCH_RELEASE_ENV:-/etc/workbench/release}
units=${SYSTEMD_DIR:-/etc/systemd/system}
state=${WORKBENCH_STATE:-/var/lib/workbench}
digest=${image##*@sha256:}
release=$home/releases/${digest:0:12}
exec 9> "$state/data.lock"
flock -w "${LOCK_WAIT_SECONDS:-900}" 9 || { echo "refused: a backup, a restore or another install is still running" >&2; exit 1; }

revision=$(docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")
if [ ! -d "$release" ]; then
    container=$(docker create "$image")
    trap 'docker rm -f "$container" > /dev/null' EXIT
    rm -rf "$release.new"
    mkdir -p "$release.new"
    docker cp "$container:/app/compose.yaml" "$release.new/compose.yaml"
    docker cp "$container:/app/deploy" "$release.new/deploy"
    # The image goes with its files: switching current switches both at once.
    printf 'WORKBENCH_IMAGE=%s\nWORKBENCH_REVISION=%s\n' "$image" "$revision" > "$release.new/release.env"
    mv -T "$release.new" "$release"
fi

install -m 0644 "$release"/deploy/aws/host/*.service "$release"/deploy/aws/host/*.timer "$units/"
ln -sfn "$home/current/release.env" "$release_env.new"  # the running release's, whichever it is
mv -T "$release_env.new" "$release_env"
ln -sfn "$release" "$home/current.new"
mv -T "$home/current.new" "$home/current"

systemctl daemon-reload
systemctl enable workbench.service workbench-backup.timer workbench-health.timer
systemctl restart workbench.service
healthy=0
for _ in $(seq "${HEALTH_WAIT_ATTEMPTS:-60}"); do
    if curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8765/healthz; then healthy=1; break; fi
    sleep "${HEALTH_WAIT_SECONDS:-2}"
done
[ "$healthy" = 1 ] || { echo "refused: $image did not become healthy; see: journalctl -u workbench" >&2; exit 1; }
systemctl start workbench-health.timer workbench-backup.timer
flock -u 9  # the first backup takes the lock itself
[ -e "$state/last-backup" ] || systemctl start workbench-backup.service
echo "installed $image (revision $revision)"
