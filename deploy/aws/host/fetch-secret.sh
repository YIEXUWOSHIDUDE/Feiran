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
