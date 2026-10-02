#!/bin/bash
# Prepare a stopped, isolated host for its FIRST V2 install. Arguments are public identifiers,
# never passwords. Configure before install.sh: starting V1 first would create a legacy DB.
set -euo pipefail
[ "$#" = 5 ] || { echo 'usage: configure-v2.sh <origin-private-dns> <https-public-origin> <pool-id> <client-id> <https-login-domain>' >&2; exit 1; }
origin=$1 public_origin=$2 pool=$3 client=$4 domain=$5
env_file=${WORKBENCH_ENV:-/etc/workbench/env}
set -a
# shellcheck source=/dev/null
. "$env_file"
set +a
refuse() { echo "refused: $*" >&2; exit 1; }
[[ "$AWS_REGION" =~ ^[a-z]{2}-[a-z]+-[0-9]+$ ]] || refuse 'invalid AWS region'
[[ "$origin" =~ ^[a-z0-9][a-z0-9.-]+$ ]] || refuse 'invalid origin hostname'
[[ "$public_origin" =~ ^https://[a-z0-9][a-z0-9.-]+$ ]] || refuse 'invalid public origin'
[[ "$pool" =~ ^[a-z]{2}-[a-z]+-[0-9]+_[A-Za-z0-9]+$ ]] || refuse 'invalid user pool ID'
[ "${pool%_*}" = "$AWS_REGION" ] || refuse 'user pool is in another region'
[[ "$client" =~ ^[a-zA-Z0-9]+$ ]] || refuse 'invalid app client ID'
[[ "$domain" =~ ^https://[a-z0-9-]+\.auth\.[a-z0-9-]+\.amazoncognito\.com$ ]] || refuse 'invalid Cognito login domain'
[[ "$domain" == *".auth.$AWS_REGION.amazoncognito.com" ]] || refuse 'login domain is in another region'
here=$(dirname "$(readlink -f "$0")")
state=${WORKBENCH_STATE:-/var/lib/workbench}
exec 9> "$state/data.lock"
flock -n 9 || refuse 'a backup, restore or install is running'
"${MOUNT_DATA:-$here/mount-data.sh}" "$DATA_DEVICE" "$DATA_MOUNT" > /dev/null
case "$(systemctl is-active workbench.service || true)" in
    active | activating | deactivating | reloading) refuse 'stop the isolated host before configuring V2' ;;
esac
# Never turn a populated owner workspace into the pilot by changing its environment.
if [ -e "$WORKBENCH_DATA_DIR/.workbench-format" ]; then
    [ "$(cat "$WORKBENCH_DATA_DIR/.workbench-format")" = 2 ] || refuse 'V1 data requires an explicit migration'
else
    [ -z "$(find "$WORKBENCH_DATA_DIR" -mindepth 1 -maxdepth 1 ! -name .workbench-data -print -quit)" ] \
        || refuse 'the uninitialized pilot data directory is not empty'
fi
umask 077
temporary=$(mktemp "${env_file}.XXXXXX")
trap 'rm -f "$temporary"' EXIT
sed '/^WORKBENCH_MODE=/d; /^WORKBENCH_OIDC_/d; /^WORKBENCH_LOGIN_/d; /^WORKBENCH_PUBLIC_HOST=/d; /^WORKBENCH_PUBLIC_ORIGIN=/d; /^WORKBENCH_BIND_ADDRESS=/d' \
    "$env_file" > "$temporary"
printf 'WORKBENCH_MODE=v2\nWORKBENCH_PUBLIC_HOST=%s\nWORKBENCH_PUBLIC_ORIGIN=%s\nWORKBENCH_BIND_ADDRESS=0.0.0.0\nWORKBENCH_OIDC_USER_POOL_ID=%s\nWORKBENCH_OIDC_ISSUER=https://cognito-idp.%s.amazonaws.com/%s\nWORKBENCH_OIDC_CLIENT_ID=%s\nWORKBENCH_OIDC_DOMAIN=%s\nWORKBENCH_OIDC_SECRET_FILE=%s\n' \
    "$origin" "$public_origin" "$pool" "$AWS_REGION" "$pool" "$client" "$domain" \
    "$(dirname "$DEEPSEEK_KEY_FILE")/oidc_client_secret" >> "$temporary"
# Do not publish settings if IAM access is missing or Cognito returned no secret.
WORKBENCH_ENV="$temporary" "$here/fetch-secret.sh"
chmod 0600 "$temporary"
mv -f "$temporary" "$env_file"
trap - EXIT
echo 'V2 settings prepared; service remains stopped. Install the tested V2 image next.'
