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
    "cognito-idp describe-user-pool-client") printf '%s\n' "${FAKE_OIDC_SECRET-}" ;;
    "secretsmanager get-secret-value") printf '%s\n' "${FAKE_SECRET-}" ;;
    "s3 cp")
        [ "${FAKE_S3_FAILS:-0}" = 0 ] || exit 1
        case "$3" in s3://*) echo "an archive" > "$4" ;; esac ;;
    "s3api list-objects-v2") echo "${FAKE_LATEST:-backups/workbench-20261001T033000Z.tar.gz}" ;;
esac
EOF
cat > "$work/bin/systemctl" <<'EOF'
#!/bin/bash
# Keeps the workbench unit's state in a file, as systemd would. Starting it starts the release the
# release file names (a, b or c: its digest's first letter), which records its data format in the
# data folder as the app does: FAKE_FORMAT_b=2. FAKE_RESTART_FAILS=b: release b does not start.
# FAKE_STOP_FAILS=1: the workbench does not stop.
echo "systemctl $*" >> "$CALLS"
case "$1" in
    is-active) cat "$UNIT_STATE"; [ "$(cat "$UNIT_STATE")" = active ] ;;
    stop)
        [ "$2" = workbench.service ] || exit 0
        [ -z "${FAKE_STOP_FAILS:-}" ] || exit 1
        echo inactive > "$UNIT_STATE" ;;
    start | restart)
        [ "$2" = workbench.service ] || exit 0
        running=$(sed -n 's/.*@sha256:\(.\).*/\1/p' "$WORKBENCH_RELEASE_ENV" 2> /dev/null)
        if [ -n "$running" ] && [ "$running" = "${FAKE_RESTART_FAILS:-}" ]; then exit 1; fi
        echo active > "$UNIT_STATE"
        format=FAKE_FORMAT_$running record=${WORKBENCH_DATA_DIR:-/nonexistent}/.workbench-format
        recorded=$(cat "$record" 2> /dev/null || echo 0)
        if [ -n "$running" ] && [ -d "${WORKBENCH_DATA_DIR:-/nonexistent}" ] && [ "${!format:-1}" -gt "$recorded" ]; then
            echo "${!format:-1}" > "$record"
        fi ;;
esac
EOF
cat > "$work/bin/curl" <<'EOF'
#!/bin/bash
# FAKE_UNHEALTHY=b: the release whose digest starts with b never answers. A release never answers
# on data in a newer format than its own either: the app refuses to start on it.
echo "curl $*" >> "$CALLS"
if [ "${FAKE_INTERRUPT_INSTALL:-0}" = 1 ]; then kill -TERM "$PPID"; exit 1; fi
running=$(sed -n 's/.*@sha256:\(.\).*/\1/p' "$WORKBENCH_RELEASE_ENV" 2> /dev/null)
format=FAKE_FORMAT_$running
recorded=$(cat "${WORKBENCH_DATA_DIR:-/nonexistent}/.workbench-format" 2> /dev/null || echo 0)
if [ "${FAKE_HEALTHY:-1}" = 1 ] && [ "$running" != "${FAKE_UNHEALTHY:-}" ] && [ "${!format:-1}" -ge "$recorded" ]; then
    case "${!#}" in
        */api/me) printf '%s' "${FAKE_API_CODE:-401}" ;;
        */) printf '%s' "${FAKE_ROOT_RESULT:-303 http://127.0.0.1:8765/login?return_to=/}" ;;
    esac
    exit 0
fi
exit 7
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
    compose) exit 0 ;;
    run)
        spool=$(printf '%s\n' "$@" | sed -n 's|^\(.*\):/backups$|\1|p')
        volume=$(printf '%s\n' "$@" | sed -n 's|^\(.*\):/volume$|\1|p')
        check=$(printf '%s\n' "$@" | sed -n 's|^\(.*\):/check$|\1|p')
        # Per release (a or b, the digest's first letter): FAKE_FORMAT_b=2, FAKE_PROBLEMS_b='"x"'.
        release=$(printf '%s\n' "$@" | sed -n 's/.*@sha256:\(.\).*/\1/p' | head -1)
        format=FAKE_FORMAT_$release problems=FAKE_PROBLEMS_$release
        case " $* " in
            *" --entrypoint python "*)
                if [[ "$*" == *web_v2* ]] && [ "${FAKE_NO_V2:-0}" = 1 ]; then exit 1; fi
                echo "${!format:-1}" ;;
            *" backup.py verify --data /data "* | *" v2_backup.py verify --data /data "*)
                echo "verify by $release while $(cat "$UNIT_STATE")" >> "$CALLS"
                printf '{"counts": {}, "problems": [%s], "notes": []}\n' "${!problems:-}"
                [ -z "${!problems:-}" ] || exit 1 ;;
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
    image)
        if [ "$2" = ls ] && [ -n "${FAKE_IMAGES:-}" ]; then
            printf '%s\n' "$FAKE_IMAGES"
        fi ;;
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
image_b="123456789012.dkr.ecr.us-east-1.amazonaws.com/job-fit-workbench@sha256:$(printf 'b%.0s' {1..64})"
running() { sed -n 's/^WORKBENCH_IMAGE=//p' "$WORKBENCH_RELEASE_ENV"; }
host_env() {  # the host's settings; the workbench's state (active, activating, inactive)
    printf 'AWS_REGION=us-east-1\nBACKUP_BUCKET=test-bucket\nLOG_GROUP=test-logs\nDEEPSEEK_KEY_FILE=%s\nDATA_DEVICE=/dev/fake-data\nDATA_MOUNT=%s\nWORKBENCH_DATA_DIR=%s\nDEEPSEEK_SECRET_ARN=%s\n' \
        "$work/run/workbench/key" "$work/volume" "$work/volume/data" "${2-}" > "$WORKBENCH_ENV"
    rm -f "$WORKBENCH_RELEASE_ENV"  # after an install, a link into the release; never written through
    printf 'WORKBENCH_IMAGE=%s\nWORKBENCH_REVISION=rev-abc123\n' "$image" > "$WORKBENCH_RELEASE_ENV"
    echo "${1:-inactive}" > "$UNIT_STATE"
    : > "$calls"
}
unit_state() { cat "$UNIT_STATE"; }


# All credentials below are synthetic. AWS, Docker and systemd are command-recording stubs.
v2_env() {
    host_env "${1:-inactive}"
    cat >> "$WORKBENCH_ENV" <<EOF
WORKBENCH_MODE=v2
WORKBENCH_PUBLIC_HOST=ip-10-0-0-1.ec2.internal
WORKBENCH_PUBLIC_ORIGIN=https://pilot.example.test
WORKBENCH_OIDC_USER_POOL_ID=us-east-1_Test
WORKBENCH_OIDC_ISSUER=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Test
WORKBENCH_OIDC_CLIENT_ID=testclient
WORKBENCH_OIDC_DOMAIN=https://test.auth.us-east-1.amazoncognito.com
WORKBENCH_OIDC_SECRET_FILE=$work/run/workbench/oidc_client_secret
EOF
}
export FAKE_OIDC_SECRET=synthetic-cognito-client-secret
v2_env
out=$("$host/fetch-secret.sh")
[ "$(cat "$work/run/workbench/oidc_client_secret")" = "$FAKE_OIDC_SECRET" ] || fail 'missing Cognito secret'
[ "$(stat -c %a "$work/run/workbench/oidc_client_secret")" = 400 ] || fail 'Cognito secret permissions'
called 'cognito-idp describe-user-pool-client' || fail 'did not fetch Cognito client'
called 'secretsmanager' && fail 'fetched a V1 owner secret in V2'
case "$out $(cat "$calls")" in *"$FAKE_OIDC_SECRET"*) fail 'printed a secret' ;; esac
for invalid in '' None 'bad secret'; do
    if FAKE_OIDC_SECRET="$invalid" "$host/fetch-secret.sh" > /dev/null 2>&1; then fail 'accepted invalid client secret'; fi
    [ "$(cat "$work/run/workbench/oidc_client_secret")" = "$FAKE_OIDC_SECRET" ] || fail 'replaced key on failure'
done
[ -z "$(find "$work/run/workbench" -name '.oidc.*')" ] || fail 'left a temporary client secret'
echo 'ok   V2 secret: protected, never logged, no owner password, invalid response preserves previous secret'

: > "$calls"
WORKBENCH_MODE=v2 "$host/compose.sh" up --no-build
called '-f deploy/aws/compose.v2.yaml up --no-build' || fail 'V2 overlay not selected'
: > "$calls"
WORKBENCH_MODE=v1 "$host/compose.sh" down
called 'compose.v2.yaml' && fail 'V1 used V2 overlay'
if WORKBENCH_MODE=typo "$host/compose.sh" up > /dev/null 2>&1; then fail 'unknown mode accepted'; fi

configure() { "$host/configure-v2.sh" ip-10-0-0-1.ec2.internal https://pilot.example.test us-east-1_Test testclient https://test.auth.us-east-1.amazoncognito.com; }
host_env inactive
printf 'WORKBENCH_LOGIN_FILE=/old/owner\nWORKBENCH_LOGIN_SECRET_ARN=old\n' >> "$WORKBENCH_ENV"
configure > /dev/null
grep -qx WORKBENCH_MODE=v2 "$WORKBENCH_ENV" || fail 'configuration omitted V2 mode'
grep -qx WORKBENCH_BIND_ADDRESS=0.0.0.0 "$WORKBENCH_ENV" || fail 'CloudFront cannot reach loopback'
grep -q '^WORKBENCH_LOGIN_' "$WORKBENCH_ENV" && fail 'retained V1 owner settings'
called 'systemctl restart' && fail 'configuration started a release before install'
[ "$(stat -c %a "$WORKBENCH_ENV")" = 600 ] || fail 'configuration permissions'
cp "$WORKBENCH_ENV" "$work/expected-env"
if FAKE_OIDC_SECRET=None configure > /dev/null 2>&1; then fail 'configured without client secret'; fi
cmp "$WORKBENCH_ENV" "$work/expected-env" || fail 'failed fetch changed settings'
echo active > "$UNIT_STATE"
if configure > /dev/null 2>&1; then fail 'configured a running host'; fi
echo inactive > "$UNIT_STATE"
echo 1 > "$work/volume/data/.workbench-format"
if configure > /dev/null 2>&1; then fail 'configured a V1 data volume'; fi
rm "$work/volume/data/.workbench-format"
touch "$work/volume/data/cv-profile.json"
if configure > /dev/null 2>&1; then fail 'accepted unmarked legacy data'; fi
rm "$work/volume/data/cv-profile.json"
echo 'ok   V2 configure: stopped isolated data only; no service starts; failed configuration preserves settings'

export FAKE_FORMAT_a=2 FAKE_FORMAT_b=2
v2_env
"$host/install.sh" "$image" > /dev/null
called 'web_v2.V2_DATA_FORMAT' || fail 'installer checked V1 format'
called 'python v2_backup.py verify' && fail 'fresh V2 volume was treated as corrupt'
called 'http://127.0.0.1:8765/api/me' || fail 'install did not check API auth'
[ "$(running)" = "$image" ] || fail 'fresh V2 was not installed'
[ "$(unit_state)" = active ] || fail 'fresh V2 not running'
touch "$work/state/last-backup" "$work/volume/data/v2.db"
: > "$calls"
"$host/install.sh" "$image" > /dev/null
called 'python v2_backup.py verify --data /data' || fail 'existing V2 data not verified with V2 tool'
called 'python backup.py verify' && fail 'V2 data checked by V1 tool'
: > "$calls"
if FAKE_NO_V2=1 "$host/install.sh" "$image_b" > /dev/null 2>&1; then fail 'image without V2 accepted'; fi
called 'systemctl stop' && fail 'unsupported image stopped existing service'
[ "$(running)" = "$image" ] || fail 'unsupported image changed release'
: > "$calls"
if FAKE_API_CODE=200 HEALTH_WAIT_ATTEMPTS=1 HEALTH_WAIT_SECONDS=0 "$host/install.sh" "$image" > /dev/null 2>&1; then
    fail 'health alone accepted an unauthenticated API'
fi
[ "$(unit_state)" = inactive ] || fail 'failed authentication left the V2 service serving'
: > "$calls"
if FAKE_ROOT_RESULT='303 https://evil.example/login' HEALTH_WAIT_ATTEMPTS=1 HEALTH_WAIT_SECONDS=0 "$host/install.sh" "$image" > /dev/null 2>&1; then
    fail 'accepted an unexpected login redirect'
fi
[ "$(unit_state)" = inactive ] || fail 'bad redirect left service serving'
: > "$calls"
# A signal after startup must not leave an unverified V2 release serving.
if FAKE_INTERRUPT_INSTALL=1 "$host/install.sh" "$image_b" > /dev/null 2>&1; then fail 'interrupted install passed'; fi
[ "$(unit_state)" = inactive ] || fail 'interrupted install left unverified V2 serving'
[ "$(sed -n 1p "$work/state/good-releases")" = "$image" ] || fail 'interrupted release recorded as good'
# A V1-mode install must refuse the same V2 data before stopping anything.
host_env active
: > "$calls"
if FAKE_FORMAT_a=1 "$host/install.sh" "$image" > /dev/null 2>&1; then fail 'V1 image accepted V2 data'; fi
called 'systemctl stop' && fail 'V1 rollback stopped V2 before rejection'
# The first-start exception never applies to a used V2 directory with a missing database.
v2_env
if FAKE_PROBLEMS_b='"database cannot be opened"' "$host/install.sh" "$image_b" > /dev/null 2>&1; then
    fail 'missing database on used V2 volume accepted'
fi
called 'python v2_backup.py verify' || fail 'used V2 directory took fresh-volume shortcut'
echo 'ok   V2 install: fresh/existing data paths, image capabilities, format rejection, health plus login/API gates'
echo 'V2 host scripts: all checks passed'
