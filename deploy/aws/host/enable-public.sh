#!/bin/bash
# Run through SSM after the public stack exists and the authenticated release is installed.
# Only non-secret identifiers are arguments. The actual password stays in Secrets Manager.
set -euo pipefail
origin=${1:?usage: enable-public.sh <origin-private-dns> <login-secret-arn>}
secret_arn=${2:?usage: enable-public.sh <origin-private-dns> <login-secret-arn>}
[[ "$origin" =~ ^[a-z0-9.-]+$ ]] || { echo 'refused: invalid origin hostname' >&2; exit 1; }
[[ "$secret_arn" =~ ^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$ ]] \
    || { echo 'refused: invalid login secret ARN' >&2; exit 1; }
env_file=${WORKBENCH_ENV:-/etc/workbench/env}
# Never make an older app that ignores the login settings publicly reachable.
docker run --rm --network none --entrypoint python \
    "$(sed -n 's/^WORKBENCH_IMAGE=//p' /etc/workbench/release)" -c 'from public_access import OwnerAccess'
temporary=$(mktemp "${env_file}.XXXXXX")
trap 'rm -f "$temporary"' EXIT
sed '/^WORKBENCH_PUBLIC_HOST=/d; /^WORKBENCH_LOGIN_SECRET_ARN=/d; /^WORKBENCH_LOGIN_FILE=/d; /^WORKBENCH_BIND_ADDRESS=/d' \
    "$env_file" > "$temporary"
printf 'WORKBENCH_PUBLIC_HOST=%s\nWORKBENCH_LOGIN_SECRET_ARN=%s\nWORKBENCH_LOGIN_FILE=/run/workbench/owner_login\nWORKBENCH_BIND_ADDRESS=0.0.0.0\n' \
    "$origin" "$secret_arn" >> "$temporary"
# Validate/fetch before replacing settings or exposing the port.
WORKBENCH_ENV="$temporary" /opt/workbench/current/deploy/aws/host/fetch-secret.sh
chmod 0600 "$temporary"
mv -f "$temporary" "$env_file"
trap - EXIT
systemctl restart workbench.service
for _ in $(seq 30); do
    code=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/ || true)
    if [ "$code" = 401 ]; then echo 'owner authentication is active'; exit 0; fi
    sleep 2
done
echo 'public authentication did not pass: stopping the service' >&2
systemctl stop workbench.service
exit 1
