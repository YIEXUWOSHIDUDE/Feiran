#!/bin/bash
# Real V2 SQLite archives and deletion replay; AWS, Docker and systemd are local stubs.
# No cloud account, credentials, Docker daemon or root needed. Also runs on macOS.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
host=$repo/deploy/aws/host
work=$(mktemp -d)
trap 'chmod -R u+w "$work"; rm -rf "$work"' EXIT
export PYTHON
PYTHON=$(command -v "${PYTHON:-python3}")
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$repo
export TEST_WORK=$work TEST_REPO=$repo CALLS=$work/calls
export WORKBENCH_ENV=$work/env WORKBENCH_RELEASE_ENV=$work/release WORKBENCH_STATE=$work/state
export WORKBENCH_UID WORKBENCH_GID
WORKBENCH_UID=$(id -u)
WORKBENCH_GID=$(id -g)
fail() { echo "FAIL: $*" >&2; exit 1; }
called() { grep -qF -- "$1" "$CALLS"; }
mkdir -p "$work/bin"
cat > "$work/command.py" <<'PY'
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tenant_store

name, *args = sys.argv[1:]
work = Path(os.environ['TEST_WORK'])
repo = Path(os.environ['TEST_REPO'])
state = work / 'unit-state'
with open(os.environ['CALLS'], 'a') as log:
    log.write(name + ' ' + ' '.join(args) + '\n')

def flag(key):
    return os.environ.get(key) == '1'

if name == 'aws':
    if args[:2] == ['s3api', 'list-objects-v2']:
        print('backups/workbench-20261001T033000Z.tar.gz')
    elif args[:2] == ['s3', 'cp']:
        if flag('FAKE_S3_FAILS'):
            sys.exit(1)
        if args[2].startswith('s3://'):
            shutil.copyfile(work / 'old-backup.tar.gz', args[3])
        else:
            shutil.copyfile(args[2], work / 'uploaded.tar.gz')
    else:
        raise AssertionError(args)
elif name == 'systemctl':
    if args[0] == 'is-active':
        print(state.read_text().strip())
        sys.exit(0 if state.read_text().strip() == 'active' else 3)
    elif args[0] == 'stop':
        if flag('FAKE_STOP_FAILS'):
            sys.exit(1)
        if flag('FAKE_STOP_STILL_RUNNING'):
            sys.exit(0)
        if flag('FAKE_STOP_DEACTIVATING'):
            state.write_text('deactivating\n')
            sys.exit(0)
        if flag('FAKE_DELETE_ON_STOP'):
            # An in-flight deletion finishes while the service is stopping. The restore must
            # read its receipt afterwards, not before this point.
            data = work / 'volume' / 'data'
            with tenant_store.transaction(data / 'v2.db') as connection:
                user_id = connection.execute("SELECT user_id FROM users WHERE subject = 'A'").fetchone()[0]
            tenant_store.delete_account(data / 'v2.db', user_id, data / 'deleted-accounts.jsonl')
        state.write_text('inactive\n')
    elif args[0] == 'start':
        state.write_text('active\n')
    else:
        raise AssertionError(args)
elif name == 'docker':
    assert args[0] == 'run'
    mounts = {}
    for index, value in enumerate(args):
        if value == '-v':
            source, target, *mode = args[index + 1].split(':')
            mounts[target] = (source, mode)
    position = args.index('python')
    script, operation, *values = args[position + 1:]
    assert script == 'v2_backup.py', script
    if operation == 'create':
        assert state.read_text().strip() == 'inactive', 'copied the source before the service stopped'
        assert mounts['/live'][1] == ['ro'], 'live data must stay read-only'
        source = Path(mounts['/data'][0])
        assert source.parent == work / 'state', 'temporary source must be on the root disk'
        assert source.stat().st_mode & 0o777 == 0o700
        assert {p.name for p in source.iterdir()} <= {'v2.db', 'v2.db-wal', 'v2.db-journal', 'deleted-accounts.jsonl'}
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in source.iterdir())
    if flag('FAKE_' + operation.upper() + '_FAILS'):
        sys.exit(1)
    if operation == 'restore':
        assert '--ledger' in values and '--without-live-ledger' not in values
        assert mounts['/data'][1] == ['ro'], 'live data must be a read-only mount'
        if values[values.index('--into') + 1].startswith('/restore/'):
            assert state.read_text().strip() == 'inactive', 'live restore read the ledger before stopping'
    def host_path(value):
        for target, (source, _) in sorted(mounts.items(), key=lambda item: -len(item[0])):
            if value == target or value.startswith(target + '/'):
                return source + value[len(target):]
        return value
    sys.exit(subprocess.run([sys.executable, str(repo / script), operation,
                             *[host_path(value) for value in values]], cwd=repo).returncode)
elif name == 'mount-data':
    sys.exit(1 if flag('FAKE_UNMOUNTED') else 0)
elif name == 'df':
    print('Filesystem 1024-blocks Used Available Capacity Mounted on')
    print('/dev/fake 100000000 0 ' + os.environ.get('FAKE_FREE_KB', '99999999') + ' 0% /')
elif name == 'flock':
    # The calling shell holds the descriptor too, so the lock survives this child process.
    try:
        fcntl.flock(int(args[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
elif name == 'sync':
    pass  # Durable volume writes are covered by the Linux mount-device test, not this stub.
elif name == 'mv':
    assert args[0] == '-T'
    source, target = args[1:]
    if flag('FAKE_SWAP_FAILS') and Path(source).parent.name.startswith('restore-'):
        sys.exit(1)
    os.rename(source, target)
else:
    raise AssertionError(name)
PY
cat > "$work/bin/stub" <<'STUB'
#!/bin/bash
exec "$PYTHON" "$TEST_WORK/command.py" "$(basename "$0")" "$@"
STUB
chmod +x "$work/bin/stub"
for command in aws systemctl docker mount-data df flock sync mv; do
    ln -s stub "$work/bin/$command"
done
export PATH="$work/bin:$PATH" MOUNT_DATA=$work/bin/mount-data
cat > "$work/fixture.py" <<'PY'
import os
from pathlib import Path
import shutil
import tenant_store
import v2_backup
work = Path(os.environ['TEST_WORK'])
for name in ('volume', 'state'):
    shutil.rmtree(work / name, ignore_errors=True)
    (work / name).mkdir()
(work / 'uploaded.tar.gz').unlink(missing_ok=True)
data = work / 'volume' / 'data'
data.mkdir()
database = data / 'v2.db'
tenant_store.initialize_store(database)
a = tenant_store.provision_user(database, issuer='https://synthetic.invalid/pool', subject='A')
b = tenant_store.provision_user(database, issuer='https://synthetic.invalid/pool', subject='B')
tenant_store.ensure_ledger(database, data / 'deleted-accounts.jsonl')
(data / '.workbench-format').write_text('2\n')
(data / '.workbench-data').touch()
v2_backup.create_backup(data, work / 'old-backup.tar.gz')
tenant_store.delete_account(database, b, data / 'deleted-accounts.jsonl')
tenant_store.provision_user(database, issuer='https://synthetic.invalid/pool', subject='current-only')
PY
reset_case() {
    if [ -d "$work/volume/data" ]; then chmod -R u+w "$work/volume/data"; fi
    "$PYTHON" "$work/fixture.py"
    printf '%s\n' "${1:-active}" > "$work/unit-state"
    printf 'DATA_DEVICE=/dev/fake\nDATA_MOUNT=%s/volume\nWORKBENCH_DATA_DIR=%s/volume/data\nBACKUP_BUCKET=synthetic\nAWS_REGION=us-east-1\n' \
        "$work" "$work" > "$WORKBENCH_ENV"
    printf 'WORKBENCH_IMAGE=synthetic-image\nWORKBENCH_REVISION=synthetic-revision\n' > "$WORKBENCH_RELEASE_ENV"
    : > "$CALLS"
}
assert_original_data() {
    "$PYTHON" - <<'PY'
import os
from pathlib import Path
import tenant_store
with tenant_store.transaction(Path(os.environ['TEST_WORK']) / 'volume/data/v2.db') as connection:
    rows = connection.execute('SELECT subject FROM users WHERE deleted_at IS NULL ORDER BY subject').fetchall()
    assert [row[0] for row in rows] == ['A', 'current-only'], rows
PY
}
assert_clean() {
    [ -z "$(find "$work/volume" -maxdepth 1 -name 'restore-*')" ] || fail "temporary restore state left behind"
}

reset_case
"$host/backup.sh" > /dev/null
called 'python v2_backup.py create' || fail "V2 data used the V1 backup tool"
called 'python v2_backup.py restore' || fail "V2 archive was not restored for verification"
called 'python v2_backup.py verify' || fail "V2 restored data was not verified"
called '--ledger /data/deleted-accounts.jsonl' || fail "backup check ignored the live deletion ledger"
[ -e "$work/state/last-backup" ] || fail "successful backup was not recorded"
[ -f "$work/uploaded.tar.gz" ] || fail "verified archive was not uploaded"
[ "$(cat "$work/unit-state")" = active ] || fail "backup left the service stopped"
assert_original_data
echo 'ok   V2 backup: real archive, live read-only ledger, restore and verify before success'

reset_case
FAKE_DELETE_ON_STOP=1 "$host/restore.sh" latest > /dev/null
"$PYTHON" - <<'PY'
import os
from pathlib import Path
import tenant_store
import v2_backup
work = Path(os.environ['TEST_WORK'])
data = work / 'volume/data'
with tenant_store.transaction(data / 'v2.db') as connection:
    assert connection.execute('SELECT COUNT(*) FROM users WHERE deleted_at IS NULL').fetchone()[0] == 0
    assert connection.execute('SELECT COUNT(*) FROM users WHERE deleted_at IS NOT NULL').fetchone()[0] == 2
assert not v2_backup.verify_data(data)['problems']
assert (data / '.workbench-data').is_file()
assert (data / '.workbench-format').read_text().strip() == '2'
kept = list((work / 'volume').glob('data.before-*'))
assert len(kept) == 1 and (kept[0] / 'v2.db').is_file()
calls = (work / 'calls').read_text()
assert calls.index('systemctl stop ') < calls.index('python v2_backup.py restore ')
assert calls.index('python v2_backup.py verify ') < calls.index('systemctl start ')
assert calls.count('systemctl stop ') == 1
PY
[ "$(cat "$work/unit-state")" = active ] || fail "restore left the service stopped"
assert_clean
echo 'ok   V2 restore: deletion during shutdown is replayed, old data kept, service restarted'

reset_case inactive
"$host/restore.sh" latest > /dev/null
called 'systemctl start' && fail "restored data started a service that was previously inactive"
assert_clean
for failure in missing_ledger malformed_ledger FAKE_RESTORE_FAILS FAKE_VERIFY_FAILS FAKE_SWAP_FAILS FAKE_STOP_FAILS FAKE_STOP_STILL_RUNNING FAKE_STOP_DEACTIVATING; do
    reset_case
    case "$failure" in
        missing_ledger) rm "$work/volume/data/deleted-accounts.jsonl" ;;
        malformed_ledger) printf 'not a receipt\n' > "$work/volume/data/deleted-accounts.jsonl" ;;
    esac
    if env "$failure=1" "$host/restore.sh" latest > /dev/null 2>&1; then fail "$failure: restore claimed success"; fi
    assert_original_data
    if [ "$failure" = FAKE_STOP_DEACTIVATING ]; then
        called 'python v2_backup.py restore' && fail "restore read its ledger while shutdown was in progress"
    fi
    [ "$(cat "$work/unit-state")" = active ] || fail "$failure: original service not restarted"
    assert_clean
done
echo 'ok   V2 restore failures: missing/broken ledger, bad archive, failed checks/stop/swap keep original data and service'

for failure in FAKE_CREATE_FAILS FAKE_RESTORE_FAILS FAKE_VERIFY_FAILS FAKE_S3_FAILS FAKE_UNMOUNTED FAKE_FREE_KB FAKE_STOP_STILL_RUNNING FAKE_STOP_DEACTIVATING missing_ledger; do
    reset_case
    if [ "$failure" = missing_ledger ]; then rm "$work/volume/data/deleted-accounts.jsonl"; fi
    if env "$failure=1" "$host/backup.sh" > /dev/null 2>&1; then fail "$failure: backup claimed success"; fi
    [ ! -e "$work/state/last-backup" ] || fail "$failure: failed backup recorded as successful"
    [ "$(cat "$work/unit-state")" = active ] || fail "$failure: backup left service stopped"
    assert_original_data
    [ -z "$(find "$work/state" -maxdepth 1 -name 'backup-source-*')" ] || fail "$failure: private source copy left behind"
    case "$failure" in
        FAKE_FREE_KB | FAKE_STOP_STILL_RUNNING | FAKE_STOP_DEACTIVATING)
            called 'docker' && fail "$failure: read/copied data without space or a stopped service" ;;
    esac
done
reset_case
printf 'unknown\n' > "$work/volume/data/.workbench-format"
if "$host/backup.sh" > /dev/null 2>&1; then fail "unknown data format was backed up"; fi
if "$host/restore.sh" latest > /dev/null 2>&1; then fail "unknown data format was restored"; fi
called 'docker' && fail "unknown data format reached a backup tool"
called 'systemctl stop' && fail "unknown data format stopped the service"
echo 'ok   V2 backup failures: no false success; unknown data formats refuse before stopping'

# A replacement volume has only its mount marker. The operator supplies the current ledger
# independently of the archive; explicit V2 mode must not silently choose the V1 backup tool.
for ledger in present missing; do
    reset_case inactive
    cp "$work/volume/data/deleted-accounts.jsonl" "$work/current-ledger"
    rm -rf "$work/volume/data"
    mkdir "$work/volume/data"
    touch "$work/volume/data/.workbench-data"
    printf 'WORKBENCH_MODE=v2\n' >> "$WORKBENCH_ENV"
    if [ "$ledger" = present ]; then
        cp "$work/current-ledger" "$work/volume/data/deleted-accounts.jsonl"
        "$host/restore.sh" latest > /dev/null
        "$PYTHON" - <<'PYTEST'
import os
from pathlib import Path
import tenant_store
import v2_backup
data = Path(os.environ['TEST_WORK']) / 'volume/data'
with tenant_store.transaction(data / 'v2.db') as connection:
    assert connection.execute("SELECT deleted_at FROM users WHERE subject = 'B'").fetchone()[0]
    assert connection.execute('SELECT COUNT(*) FROM users WHERE deleted_at IS NULL').fetchone()[0] == 1
assert not v2_backup.verify_data(data)['problems']
assert (data / '.workbench-format').read_text().strip() == '2'
PYTEST
    else
        if "$host/restore.sh" latest > /dev/null 2>&1; then fail "an empty volume restored without a current ledger"; fi
        [ ! -e "$work/volume/data/v2.db" ] || fail "a refused restore populated the empty volume"
    fi
    called 'python v2_backup.py restore' || fail "an empty V2 volume selected the V1 restore tool"
    called 'systemctl start' && fail "a new volume unexpectedly started the service"
    assert_clean
done
reset_case
rm "$work/volume/data/.workbench-format"
printf 'WORKBENCH_MODE=v2\n' >> "$WORKBENCH_ENV"
"$host/backup.sh" > /dev/null
called 'python v2_backup.py create' || fail "explicit V2 mode did not select its backup tool without a marker"
for mismatch in 'v1 2' 'v2 1' 'unknown 2'; do
    reset_case
    mode=${mismatch% *}
    format=${mismatch#* }
    printf 'WORKBENCH_MODE=%s\n' "$mode" >> "$WORKBENCH_ENV"
    printf '%s\n' "$format" > "$work/volume/data/.workbench-format"
    if "$host/backup.sh" > /dev/null 2>&1; then fail "$mismatch: backup accepted conflicting mode/format"; fi
    if "$host/restore.sh" latest > /dev/null 2>&1; then fail "$mismatch: restore accepted conflicting mode/format"; fi
    called 'docker' && fail "conflicting mode/format reached a backup tool"
    called 'systemctl stop' && fail "conflicting mode/format stopped the service"
done
echo 'ok   replacement volumes: explicit V2 mode, current ledger required, conflicting mode/format refused'


# Docker stubs above do not enforce bind-mount permissions. Remove source write permission
# for real here: a closed WAL DB has no sidecars, but reading it still needs to create them.
for wal in clean uncheckpointed; do
    reset_case
    export WAL_CASE=$wal
    "$PYTHON" - <<'PYREADONLY'
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
work = Path(os.environ['TEST_WORK'])
data = work / 'volume/data'
assert os.geteuid() != 0, 'run the real read-only permission regression as a non-root user'
assert not (data / 'v2.db-wal').exists()
if os.environ['WAL_CASE'] == 'uncheckpointed':
    subprocess.run([sys.executable, '-c', """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute("UPDATE settings SET value = '7' WHERE key = 'queue_limit'")
connection.commit()
os._exit(0)
""", str(data / 'v2.db')], check=True)
    assert (data / 'v2.db-wal').stat().st_size > 0
# An unrelated private file must not be copied just because it shares the data directory.
(data / 'not-part-of-v2.txt').write_text('synthetic private file outside the V2 backup')
for path in data.iterdir():
    path.chmod(0o444)
data.chmod(0o555)
state = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in data.iterdir()}
(work / 'source-before.json').write_text(json.dumps(state))
PYREADONLY
    "$host/backup.sh" > /dev/null
    "$PYTHON" - <<'PYREADONLY'
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tarfile
work = Path(os.environ['TEST_WORK'])
data = work / 'volume/data'
after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in data.iterdir()}
assert after == json.loads((work / 'source-before.json').read_text()), 'live source changed'
assert data.stat().st_mode & 0o777 == 0o555
assert all(p.stat().st_mode & 0o777 == 0o444 for p in data.iterdir())
with tarfile.open(work / 'uploaded.tar.gz') as archive:
    extracted = work / 'copied.db'
    extracted.write_bytes(archive.extractfile('data/v2.db').read())
with sqlite3.connect(extracted) as connection:
    limit = connection.execute("SELECT value FROM settings WHERE key = 'queue_limit'").fetchone()[0]
    assert limit == ('7' if os.environ['WAL_CASE'] == 'uncheckpointed' else '20'), 'committed WAL data lost'
assert not list((work / 'state').glob('backup-source-*')), 'temporary source copy left behind'
PYREADONLY
    [ "$(cat "$work/unit-state")" = active ] || fail "$wal: service did not restart"
done
unset WAL_CASE
echo 'ok   read-only live WAL databases: clean close and uncheckpointed commits both back up without changing the source'
