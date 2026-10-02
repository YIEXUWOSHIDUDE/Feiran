#!/bin/bash
# Called by systemd with the host and release environment already loaded.
set -euo pipefail
files=(-f compose.yaml -f deploy/aws/compose.aws.yaml)
case "${WORKBENCH_MODE:-v1}" in
    v1) ;;
    v2) files+=(-f deploy/aws/compose.v2.yaml) ;;
    *) echo "refused: unknown WORKBENCH_MODE" >&2; exit 1 ;;
esac
exec docker compose "${files[@]}" "$@"
