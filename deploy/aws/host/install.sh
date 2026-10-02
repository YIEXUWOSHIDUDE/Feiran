#!/bin/bash
# Install a release of the workbench on the EC2 host: its image, by digest, and the host files
# that came in it (compose files, seccomp profile, these scripts, the systemd units), so the host
# always runs what was tested with that image. A release's files, once installed, are never
# changed: installing it again reuses them. Installs, backups and restores run one at a time, and
# an install looks at the data only once the data volume itself is mounted.
#
# A release must be able to read the data: its data format (workspace.DATA_FORMAT) may not be
# older than the one the data folder records (.workbench-format, which each start of the app
# writes, so it goes with the data and its backups; data from before the record existed is in
# format 1). A release that raises the format needs a backup from the last hour, and is never
# undone automatically: once it has started, the data may be in a format the release before
# cannot read.
#
# Before switching, with the workbench stopped so the data cannot change in between, the new
# release's own read-only check of the data (backup.py verify) may find no problem that the last
# good release's check does not; otherwise nothing is switched and the workbench starts again as
# it was, also if the install fails or is stopped meanwhile. After switching, the release must
# start and answer within two minutes; otherwise the last good release (the last one that passed,
# never one an install was cut short on) is installed again. The first install also makes the
# first backup, so the backup alarm starts from a real one.
#
#   install.sh <registry/repository@sha256:...>     (workbench-release pulls the image first)
set -euo pipefail
set -a
# shellcheck source=/dev/null
. "${WORKBENCH_ENV:-/etc/workbench/env}"
set +a

image=${1:?usage: install.sh <image@sha256:...>}
[[ "$image" =~ ^[^@[:space:]]+@sha256:[0-9a-f]{64}$ ]] \
    || { echo "refused: give the image by its digest (...@sha256:...), not a tag" >&2; exit 1; }
home=${WORKBENCH_HOME:-/opt/workbench}
release_env=${WORKBENCH_RELEASE_ENV:-/etc/workbench/release}
units=${SYSTEMD_DIR:-/etc/systemd/system}
state=${WORKBENCH_STATE:-/var/lib/workbench}
rolling_back=${WORKBENCH_ROLLING_BACK:-0}
digest=${image##*@sha256:}
release=$home/releases/${digest:0:12}

# What to do if this install ends early: before the workbench is stopped, nothing; once it is
# stopped for the checks, start it again as it was; once switched, say what to do.
phase=preparing container="" was_running=0
start_as_it_was() { if [ "$was_running" = 1 ]; then systemctl start workbench.service || true; fi; }
on_exit() {
    if [ -n "$container" ]; then docker rm -f "$container" > /dev/null 2>&1 || true; fi
    case "$phase" in
        stopped) start_as_it_was ;;
        switched)
            if [ "${WORKBENCH_MODE:-v1}" = v2 ]; then
                systemctl stop workbench.service || echo "could not stop the unverified V2 release" >&2
            fi
            echo "interrupted after switching to $image, before it was seen to work: install it again," \
            "or install the last good release (${good:-none yet})" >&2 ;;
    esac
}
trap on_exit EXIT
trap 'exit 1' HUP INT TERM

# Going back to the last good release runs inside the install that failed, which already holds
# the lock through this same open file; any other install opens the file and waits for the lock.
[ "$rolling_back" = 1 ] || exec 9> "$state/data.lock"
flock -w "${LOCK_WAIT_SECONDS:-900}" 9 || { echo "refused: a backup, a restore or another install is still running" >&2; exit 1; }
# The last release that passed all of this, then the good one before it: one file, written whole.
good=$(sed -n 1p "$state/good-releases" 2> /dev/null || true)
previous=$good
[ "$previous" != "$image" ] || previous=""  # the good release again: nothing to go back to

# A V2 host must never roll back into the single-owner application in the same image.
case "${WORKBENCH_MODE:-v1}" in
    v1) format_query='import workspace; print(workspace.DATA_FORMAT)'; backup_tool=backup.py ;;
    v2)
        format_query='import web_v2, identity; print(web_v2.V2_DATA_FORMAT)'
        backup_tool=v2_backup.py
        for setting in WORKBENCH_OIDC_ISSUER WORKBENCH_OIDC_CLIENT_ID WORKBENCH_OIDC_DOMAIN \
                WORKBENCH_OIDC_USER_POOL_ID WORKBENCH_OIDC_SECRET_FILE WORKBENCH_PUBLIC_ORIGIN WORKBENCH_PUBLIC_HOST; do
            [ -n "${!setting:-}" ] || { echo "refused: V2 is missing $setting" >&2; exit 1; }
        done
        docker run --rm --network none --entrypoint python "$image" -c 'import web_v2, identity, v2_backup' \
            || { echo "refused: this release has no V2 support" >&2; exit 1; } ;;
    *) echo "refused: unknown WORKBENCH_MODE" >&2; exit 1 ;;
esac
# V1 public mode may never silently roll back to an image without its login gate.
if [ "${WORKBENCH_MODE:-v1}" = v1 ] && [ -n "${WORKBENCH_PUBLIC_HOST:-}" ]; then
    docker run --rm --network none --entrypoint python "$image" -c 'from public_access import OwnerAccess' \
        || { echo "refused: this release has no owner login support" >&2; exit 1; }
fi

revision=$(docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image")
if [ ! -d "$release" ]; then
    container=$(docker create "$image")
    rm -rf "$release.new"
    mkdir -p "$release.new"
    docker cp "$container:/app/compose.yaml" "$release.new/compose.yaml"
    docker cp "$container:/app/deploy" "$release.new/deploy"
    # The image goes with its files: switching current switches both at once.
    printf 'WORKBENCH_IMAGE=%s\nWORKBENCH_REVISION=%s\n' "$image" "$revision" > "$release.new/release.env"
    mv -T "$release.new" "$release"
fi
"${MOUNT_DATA:-$release/deploy/aws/host/mount-data.sh}" "$DATA_DEVICE" "$DATA_MOUNT" > /dev/null

format=$(docker run --rm --network none --entrypoint python "$image" -c "$format_query")
if [ -e "$WORKBENCH_DATA_DIR/.workbench-format" ]; then
    recorded=$(cat "$WORKBENCH_DATA_DIR/.workbench-format")
elif [ -e "$WORKBENCH_DATA_DIR/v2.db" ]; then
    recorded=2
elif [ -e "$WORKBENCH_DATA_DIR/workbench.db" ] || [ -e "$WORKBENCH_DATA_DIR/cv-profile.json" ] \
        || [ -d "$WORKBENCH_DATA_DIR/jobs" ]; then
    recorded=1  # data from before the app recorded its format is in the first one
else
    recorded=0  # no data yet
fi
[[ "$format" =~ ^[0-9]+$ ]] || { echo "refused: $image does not say its data format" >&2; exit 1; }
[[ "$recorded" =~ ^[0-9]+$ ]] || { echo "refused: the data's record of its format cannot be read" >&2; exit 1; }
if [ "$format" -lt "$recorded" ]; then
    echo "refused: $image stores data in format $format, but the data is in format $recorded;" \
        "to go back before that change, restore a backup made before it" >&2
    exit 1
fi
if [ "${WORKBENCH_MODE:-v1}" = v2 ] && [ "$recorded" = 1 ]; then
    echo "refused: migrate V1 data explicitly before installing in V2 mode" >&2
    exit 1
fi
# First V2 startup is allowed only on a genuinely empty, prepared volume. The normal verifier
# must still reject a missing database on a previously used V2 volume.
fresh_v2=0
if [ "${WORKBENCH_MODE:-v1}" = v2 ] && [ "$recorded" = 0 ]; then
    [ -z "$(find "$WORKBENCH_DATA_DIR" -mindepth 1 -maxdepth 1 ! -name .workbench-data -print -quit)" ] \
        || { echo "refused: the uninitialized V2 data folder is not empty" >&2; exit 1; }
    fresh_v2=1
fi
raises_format=0
if [ "$format" -gt "$recorded" ] && [ "$recorded" -gt 0 ]; then
    raises_format=1
    if [ -z "$(find "$state/last-backup" -mmin -60 2> /dev/null)" ]; then
        echo "refused: $image changes the data's format from $recorded to $format. Make a backup first" \
            "(systemctl start workbench-backup) and install it within the hour; going back means restoring that backup" >&2
        exit 1
    fi
fi

problems_found_by() {  # what release $1 finds wrong with the data (read-only), one per line, sorted
    local report
    report=$(docker run --rm --network none --read-only --tmpfs /tmp --security-opt no-new-privileges:true \
        -v "$WORKBENCH_DATA_DIR:/data:ro" "$1" python "$backup_tool" verify --data /data) || [ $? = 1 ]
    printf '%s' "$report" | python3 -c 'import json, sys; print("\n".join(sorted(json.load(sys.stdin)["problems"])))'
}
refuse_and_go_back() {  # once switched: install the last good release again, when that is safe
    phase=handled
    if [ "${WORKBENCH_MODE:-v1}" = v2 ]; then systemctl stop workbench.service; fi
    echo "refused: $image $1" >&2
    if [ "$rolling_back" = 0 ] && [ "$raises_format" = 1 ]; then
        echo "not going back automatically: $image may already hold the data in format $format, which the" \
            "release before cannot read. Stop the workbench (systemctl stop workbench), restore the backup made" \
            "before this install ($home/current/deploy/aws/host/restore.sh backups/<its name>), then install" \
            "${previous:-the release before}." >&2
    elif [ "$rolling_back" = 0 ] && [ -n "$previous" ]; then
        echo "installing the last good release again: $previous" >&2
        WORKBENCH_ROLLING_BACK=1 "$0" "$previous" >&2 || echo "the release before could not be installed either" >&2
    fi
    exit 1
}

if [ "$rolling_back" = 0 ]; then
    case "$(systemctl is-active workbench.service || true)" in
        active | activating | reloading) was_running=1 ;;
    esac
    phase=stopped
    systemctl stop workbench.service 2> /dev/null || true  # there is no unit yet before the first install
    case "$(systemctl is-active workbench.service || true)" in
        active | activating | deactivating | reloading)
            echo "refused: the workbench did not stop, so the data could change during the checks" >&2
            exit 1 ;;
    esac
    # Both releases read the same, unchanging data; the last good one's findings are the baseline.
    known=""
    if [ -n "$good" ]; then known=$(problems_found_by "$good" 2> /dev/null || true); fi
    found=""
    if [ "$fresh_v2" = 0 ]; then
        found=$(problems_found_by "$image") || { echo "refused: $image could not check the data; nothing was changed" >&2; exit 1; }
    fi
    new=$(LC_ALL=C comm -13 <(printf '%s\n' "$known" | sed '/^$/d') <(printf '%s\n' "$found" | sed '/^$/d'))
    if [ -n "$new" ]; then
        echo "refused: $image finds problems in the data that the last good release does not: $new; nothing was changed" >&2
        exit 1
    fi
fi

install -m 0644 "$release"/deploy/aws/host/*.service "$release"/deploy/aws/host/*.timer "$units/"
ln -sfn "$home/current/release.env" "$release_env.new"  # the running release's, whichever it is
mv -T "$release_env.new" "$release_env"
ln -sfn "$release" "$home/current.new"
mv -T "$home/current.new" "$home/current"
phase=switched

{ systemctl daemon-reload && systemctl enable workbench.service workbench-backup.timer workbench-health.timer \
    && systemctl restart workbench.service; } || refuse_and_go_back "could not be started; see: journalctl -u workbench"
healthy=0
for _ in $(seq "${HEALTH_WAIT_ATTEMPTS:-60}"); do
    if curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8765/healthz; then
        if [ "${WORKBENCH_MODE:-v1}" = v2 ]; then
            page=$(curl -s --max-time 5 -o /dev/null -w '%{http_code} %{redirect_url}' http://127.0.0.1:8765/ || true)
            api=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/api/me || true)
            if [ "$page" = '303 http://127.0.0.1:8765/login?return_to=/' ] && [ "$api" = 401 ]; then healthy=1; break; fi
        else
            healthy=1; break
        fi
    fi
    sleep "${HEALTH_WAIT_SECONDS:-2}"
done
[ "$healthy" = 1 ] || refuse_and_go_back "did not become healthy; see: journalctl -u workbench"
phase=passed

if [ "$image" != "$good" ]; then  # it passed: now the last good release, and the one before it kept
    printf '%s\n%s\n' "$image" "$good" > "$state/good-releases.new"
    mv -f "$state/good-releases.new" "$state/good-releases"
fi
if [ "$rolling_back" = 0 ]; then
    # This release's image and the good one before it stay (to go back to); older ones are in the registry.
    kept_before=$(sed -n 2p "$state/good-releases" 2> /dev/null || true)
    docker image ls --digests --format '{{.Repository}}@{{.Digest}}' | while read -r stored; do
        case "$stored" in "" | "$image" | "$kept_before" | *"<none>"*) ;; *) docker image rm "$stored" > /dev/null || true ;; esac
    done
fi
systemctl start workbench-health.timer workbench-backup.timer
flock -u 9  # the first backup takes the lock itself
[ -e "$state/last-backup" ] || systemctl start workbench-backup.service
echo "installed $image (revision $revision)"
