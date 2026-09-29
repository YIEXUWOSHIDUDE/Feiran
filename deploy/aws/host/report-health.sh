#!/bin/bash
# Every five minutes (workbench-health.timer): tell CloudWatch whether the workbench answers and
# how many hours ago the last backup reached S3. The alarms also fire when these reports stop
# coming, so a host that is down is noticed too.
set -euo pipefail
set -a
# shellcheck source=/dev/null
. "${WORKBENCH_ENV:-/etc/workbench/env}"
set +a

state=${WORKBENCH_STATE:-/var/lib/workbench}
healthy=0
if curl -fsS --max-time 10 -o /dev/null http://127.0.0.1:8765/healthz; then healthy=1; fi
if [ -e "$state/last-backup" ]; then
    hours=$(( ($(date +%s) - $(stat -c %Y "$state/last-backup")) / 3600 ))
else
    hours=9999  # never backed up
fi
aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace Workbench --metric-data \
    "MetricName=Healthy,Value=$healthy,Unit=Count" "MetricName=BackupAgeHours,Value=$hours,Unit=Count"
echo "healthy=$healthy backup_age_hours=$hours"
