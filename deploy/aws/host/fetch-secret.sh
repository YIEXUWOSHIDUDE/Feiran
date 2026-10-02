#!/bin/bash
# Put the DeepSeek key where compose hands it to the container (DEEPSEEK_KEY_FILE, under /run:
# memory, gone at shutdown), readable only by the container's user. The key comes from the
# Secrets Manager secret DEEPSEEK_SECRET_ARN names; with none named, the file is empty and the
# workbench runs without DeepSeek, saying on the page that the key is missing. The key is never
# printed, logged, passed on a command line or written to a disk.
set -euo pipefail
set -a
# shellcheck source=/dev/null
. "${WORKBENCH_ENV:-/etc/workbench/env}"
set +a

target=${DEEPSEEK_KEY_FILE:?DEEPSEEK_KEY_FILE is not set}
owner=${WORKBENCH_UID:-10001}:${WORKBENCH_GID:-10001}
umask 077
install -d -m 0700 "$(dirname "$target")"
temporary=$(mktemp "$(dirname "$target")/.key.XXXXXX")
trap 'rm -f "$temporary"' EXIT
if [ -n "${DEEPSEEK_SECRET_ARN:-}" ]; then
    aws secretsmanager get-secret-value --region "$AWS_REGION" --secret-id "$DEEPSEEK_SECRET_ARN" \
        --query SecretString --output text > "$temporary"
    grep -q '[^[:space:]]' "$temporary" || { echo "refused: the DeepSeek secret is empty" >&2; exit 1; }
    done_message="the DeepSeek key is in place for the container"
else
    done_message="no DeepSeek secret is named: the workbench runs without DeepSeek"
fi
chown "$owner" "$temporary"
chmod 0400 "$temporary"
mv -f "$temporary" "$target"
trap - EXIT
echo "$done_message"

# V2 has its own identity provider and never fetches the V1 owner password.
case "${WORKBENCH_MODE:-v1}" in
v2)
    target=${WORKBENCH_OIDC_SECRET_FILE:?WORKBENCH_OIDC_SECRET_FILE is not set}
    install -d -m 0700 "$(dirname "$target")"
    temporary=$(mktemp "$(dirname "$target")/.oidc.XXXXXX")
    trap 'rm -f "$temporary"' EXIT
    aws cognito-idp describe-user-pool-client --region "$AWS_REGION" \
        --user-pool-id "${WORKBENCH_OIDC_USER_POOL_ID:?WORKBENCH_OIDC_USER_POOL_ID is not set}" \
        --client-id "${WORKBENCH_OIDC_CLIENT_ID:?WORKBENCH_OIDC_CLIENT_ID is not set}" \
        --query UserPoolClient.ClientSecret --output text > "$temporary"
    python3 - "$temporary" <<'PYSECRET'
from pathlib import Path
import sys
value = Path(sys.argv[1]).read_text().strip()
if not 16 <= len(value) <= 4096 or not value.isascii() or any(c.isspace() for c in value):
    sys.exit("refused: invalid Cognito client secret")
PYSECRET
    chown "$owner" "$temporary"
    chmod 0400 "$temporary"
    mv -f "$temporary" "$target"
    trap - EXIT
    echo "the Cognito client secret is in place for the container"
    exit 0 ;;
v1) ;;
*) echo "refused: unknown WORKBENCH_MODE" >&2; exit 1 ;;
esac

# In V1 public mode a missing or unreadable login secret stops startup.
if [ -n "${WORKBENCH_PUBLIC_HOST:-}" ]; then
    target=${WORKBENCH_LOGIN_FILE:?WORKBENCH_LOGIN_FILE is not set}
    temporary=$(mktemp "$(dirname "$target")/.login.XXXXXX")
    trap 'rm -f "$temporary"' EXIT
    aws secretsmanager get-secret-value --region "$AWS_REGION" \
        --secret-id "${WORKBENCH_LOGIN_SECRET_ARN:?WORKBENCH_LOGIN_SECRET_ARN is not set}" \
        --query SecretString --output text > "$temporary"
    python3 - "$temporary" <<'PYLOGIN'
import json, sys
try:
    value = json.load(open(sys.argv[1]))
    assert value["username"] == "feiran" and isinstance(value["password"], str) and len(value["password"]) >= 32
except Exception:
    sys.exit("refused: invalid owner login secret")
PYLOGIN
    chown "$owner" "$temporary"
    chmod 0400 "$temporary"
    mv -f "$temporary" "$target"
    trap - EXIT
    echo "the owner login is in place for the container"
fi
