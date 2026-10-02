#!/bin/bash
# Linux CI: real Docker bind permissions, SQLite and release files. Only EC2/systemd/network
# services are stubbed. Two local digest aliases use the already-built image (no registry).
set -euo pipefail
if [ "$(uname -s)" != Linux ] || [ "$(id -u)" != 0 ]; then
    echo 'run this Linux-only check with sudo and WORKBENCH_IMAGE set' >&2
    exit 1
fi
repo=$(cd "$(dirname "$0")/../.." && pwd)
image=${WORKBENCH_IMAGE:?give the image built from this checkout}
export REAL_DOCKER LOCAL_IMAGE
REAL_DOCKER=$(command -v docker)
LOCAL_IMAGE=$("$REAL_DOCKER" image inspect -f '{{.Id}}' "$image")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export TEST_WORK=$work TEST_REPO=$repo
export WORKBENCH_ENV=$work/env WORKBENCH_RELEASE_ENV=$work/release
export WORKBENCH_HOME=$work/home WORKBENCH_STATE=$work/state SYSTEMD_DIR=$work/units
export HEALTH_WAIT_ATTEMPTS=1 HEALTH_WAIT_SECONDS=0
export WORKBENCH_UID=10001 WORKBENCH_GID=10001
export IMAGE_A IMAGE_B
IMAGE_A=synthetic.local/workbench@sha256:$(printf 'a%.0s' {1..64})
IMAGE_B=synthetic.local/workbench@sha256:$(printf 'b%.0s' {1..64})
mkdir -p "$work/bin"
cat > "$work/stub.py" <<'PY'
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

name, *args = sys.argv[1:]
work = Path(os.environ['TEST_WORK'])
live = work / 'volume/data'
state = work / 'unit-state'

def record(event):
    with (work / 'calls.jsonl').open('a') as out:
        out.write(json.dumps(event) + '\n')

record({'command': name, 'args': args})
if name == 'docker':
    # Do not prune the CI daemon's unrelated images. Every data operation still runs in Docker.
    if args[:2] == ['image', 'ls']:
        sys.exit(0)
    assert args[:2] != ['image', 'rm'], args
    mounts = {}
    for i, arg in enumerate(args):
        if arg == '-v':
            source, target, *mode = args[i + 1].split(':')
            mounts[target] = (Path(source), mode)
            if Path(source) == live:
                assert mode == ['ro'], 'the live bind must be read-only'
    operation = None
    if 'v2_backup.py' in args:
        operation = args[args.index('v2_backup.py') + 1]
        if operation in ('create', 'verify') and args[args.index('--data') + 1] == '/data':
            source, mode = mounts['/data']
            assert source != live and not mode, 'SQLite must use a writable disposable copy'
            assert state.read_text().strip() == 'inactive', 'copied before shutdown'
            assert source.parent == work / 'state', 'the copy must live on the root disk'
            assert source.stat().st_mode & 0o777 == 0o700
            allowed = {'v2.db', 'v2.db-wal', 'v2.db-journal', 'deleted-accounts.jsonl'}
            # A previous verifier may already have rebuilt this same copy's disposable index.
            if operation == 'verify':
                allowed.add('v2.db-shm')
            assert {p.name for p in source.iterdir()} <= allowed
        assert any(source == live and mode == ['ro'] for source, mode in mounts.values()), mounts
    aliases = (os.environ['IMAGE_A'], os.environ['IMAGE_B'])
    actual = [os.environ['LOCAL_IMAGE'] if arg in aliases else arg for arg in args]
    if operation == 'verify':
        result = subprocess.run([os.environ['REAL_DOCKER'], *actual], stdout=subprocess.PIPE)
        sys.stdout.buffer.write(result.stdout)
        report = json.loads(result.stdout)
        assert report['problems'] == [], report
        assert report['counts']['accounts'] == int(os.environ['EXPECTED_ACCOUNTS']), report
        record({'verified_accounts': report['counts']['accounts'], 'args': args})
        sys.exit(result.returncode)
    sys.exit(subprocess.call([os.environ['REAL_DOCKER'], *actual]))
if name == 'systemctl':
    if args[0] == 'is-active':
        value = state.read_text().strip()
        print(value)
        sys.exit(0 if value == 'active' else 3)
    if args == ['stop', 'workbench.service']:
        state.write_text('inactive\n')
    elif args[0] in ('start', 'restart') and args[1] == 'workbench.service':
        state.write_text('active\n')
    elif args == ['start', 'workbench-backup.service']:
        assert not (work / 'state/last-backup').exists(), 'first backup was not needed'
        subprocess.run([str(work / 'home/current/deploy/aws/host/backup.sh')], check=True)
    elif args[0] not in ('daemon-reload', 'enable', 'start'):
        raise AssertionError(args)
elif name == 'aws':
    assert args[:2] == ['s3', 'cp'] and args[3].startswith('s3://synthetic/'), args
    shutil.copyfile(args[2], work / 'uploaded.tar.gz')
elif name == 'curl':
    if args[-1].endswith('/api/me'):
        print('401', end='')
    elif args[-1].endswith('/'):
        print('303 http://127.0.0.1:8765/login?return_to=/', end='')
    else:
        assert args[-1].endswith('/healthz'), args
elif name == 'mount-data':
    assert (live / '.workbench-data').is_file()
else:
    raise AssertionError(name)
PY
cat > "$work/bin/stub" <<'STUB'
#!/bin/bash
exec python3 "$TEST_WORK/stub.py" "$(basename "$0")" "$@"
STUB
chmod +x "$work/bin/stub"
for command in aws systemctl curl mount-data docker; do ln -s stub "$work/bin/$command"; done
export PATH="$work/bin:$PATH" MOUNT_DATA=$work/bin/mount-data

for wal in clean uncheckpointed; do
    rm -rf "$work/volume" "$work/state" "${work:?}/home" "$work/units" "$work/release" "$work/uploaded.tar.gz"
    mkdir -p "$work/volume/data" "$work/state" "$work/home" "$work/units" "$work/probe"
    chown 10001:10001 "$work/volume/data" "$work/probe"
    : > "$work/calls.jsonl"
    printf 'inactive\n' > "$work/unit-state"
    export EXPECTED_ACCOUNTS=1
    [ "$wal" = clean ] || export EXPECTED_ACCOUNTS=2
    cat > "$WORKBENCH_ENV" <<ENV
DATA_DEVICE=/dev/synthetic
DATA_MOUNT=$work/volume
WORKBENCH_DATA_DIR=$work/volume/data
BACKUP_BUCKET=synthetic
AWS_REGION=us-east-1
WORKBENCH_MODE=v2
WORKBENCH_OIDC_ISSUER=https://issuer.invalid/pool
WORKBENCH_OIDC_CLIENT_ID=synthetic
WORKBENCH_OIDC_DOMAIN=https://login.invalid
WORKBENCH_OIDC_USER_POOL_ID=synthetic
WORKBENCH_OIDC_SECRET_FILE=$work/unused-synthetic-secret
WORKBENCH_PUBLIC_ORIGIN=https://pilot.invalid
WORKBENCH_PUBLIC_HOST=pilot.invalid
ENV
    # Closing the last SQLite connection removes its sidecars. os._exit instead leaves the
    # second committed account only in WAL; reading the main file alone must miss it.
    "$REAL_DOCKER" run --rm -i --network none --read-only --tmpfs /tmp \
        -v "$work/volume/data:/data" "$image" python - "$wal" <<'PY'
import os
from pathlib import Path
import sqlite3
import sys
import tenant_store

data = Path('/data')
db = data / 'v2.db'
tenant_store.initialize_store(db)
tenant_store.provision_user(db, issuer='https://synthetic.invalid/pool', subject='baseline')
tenant_store.ensure_ledger(db, data / 'deleted-accounts.jsonl')
(data / '.workbench-data').touch()
(data / '.workbench-format').write_text('2\n')
(data / 'unrelated-private.txt').write_text('synthetic: exclude from the disposable copy')
assert not (data / 'v2.db-wal').exists()
assert not (data / 'v2.db-shm').exists()
if sys.argv[1] == 'uncheckpointed':
    connection = sqlite3.connect(db)
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute('PRAGMA journal_mode=WAL')
    tenant_store.provision_user(db, issuer='https://synthetic.invalid/pool', subject='wal-only')
    connection.execute("UPDATE settings SET value='7' WHERE key='queue_limit'")
    connection.commit()
    assert (data / 'v2.db-wal').stat().st_size > 0
    main = sqlite3.connect(f'{db.as_uri()}?immutable=1', uri=True)
    assert main.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1
    main.close()
    os._exit(0)
PY
    python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path
work = Path(os.environ['TEST_WORK'])
source = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (work / 'volume/data').iterdir()}
(work / 'source-before.json').write_text(json.dumps(source))
PY
    if [ "$wal" = clean ]; then
        # Prove this real bind reproduces the old bug. Root on the host does not bypass :ro.
        if "$REAL_DOCKER" run --rm --network none --read-only --tmpfs /tmp \
            -v "$work/volume/data:/data:ro" -v "$work/probe:/backups" "$image" \
            python v2_backup.py create --data /data --out /backups/old-path.tar.gz > "$work/old-path.log" 2>&1; then
            echo 'FAIL: the old read-only WAL path unexpectedly succeeded' >&2; exit 1
        fi
        grep -qE 'sqlite3[.]OperationalError: (attempt to write a readonly database|unable to open database file)' \
            "$work/old-path.log"
    fi
    # A first install on existing synthetic V2 data must verify and trigger its first backup.
    # The second digest exercises both the last-good and new-release verifiers on that data.
    "$repo/deploy/aws/host/install.sh" "$IMAGE_A"
    test -e "$work/state/last-backup"
    test -s "$work/uploaded.tar.gz"
    "$repo/deploy/aws/host/install.sh" "$IMAGE_B"
    test "$(head -1 "$work/state/good-releases")" = "$IMAGE_B"
    test "$(cat "$work/unit-state")" = active
    python3 - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tarfile
work = Path(os.environ['TEST_WORK'])
live = work / 'volume/data'
after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in live.iterdir()}
assert after == json.loads((work / 'source-before.json').read_text()), 'live source changed'
with tarfile.open(work / 'uploaded.tar.gz') as archive:
    (work / 'archived.db').write_bytes(archive.extractfile('data/v2.db').read())
with sqlite3.connect(work / 'archived.db') as connection:
    subjects = [row[0] for row in connection.execute('SELECT subject FROM users ORDER BY subject')]
    expected = ['baseline'] if os.environ['EXPECTED_ACCOUNTS'] == '1' else ['baseline', 'wal-only']
    assert subjects == expected, subjects
    limit = connection.execute("SELECT value FROM settings WHERE key='queue_limit'").fetchone()[0]
    assert limit == ('20' if len(expected) == 1 else '7'), 'committed WAL data lost'
events = [json.loads(line) for line in (work / 'calls.jsonl').read_text().splitlines()]
verifiers = [event for event in events if 'verified_accounts' in event]
for image, count in ((os.environ['IMAGE_A'], 2), (os.environ['IMAGE_B'], 1)):
    # The first backup also verifies its restored copy; count only install's --data /data.
    actual = sum(image in event['args'] and event['args'][-2:] == ['--data', '/data'] for event in verifiers)
    assert actual == count, (image, actual, verifiers)
assert sum(event.get('command') == 'aws' for event in events) == 1, 'first backup missing or repeated'
assert not list((work / 'state').glob('backup-source-*')), 'source copy left behind'
assert not list((work / 'state').glob('backup-check-*')), 'restore check left behind'
assert not list((work / 'state').glob('install-check-*')), 'install copy left behind'
PY
    echo "ok   Docker V2 $wal: first backup and upgrade verify; WAL preserved, live source unchanged"
done
