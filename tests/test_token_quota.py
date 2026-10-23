"""Offline transport and store checks for per-call token accounting."""
import sys, unittest, tempfile, json, threading, concurrent.futures
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import URLError
from feiran import tenant_store as s
from feiran import deepseek_client as d

class Meter:

    def __init__(self, db, u, t):
        self.db, self.u, self.t = (db, u, t)

    def begin(self, p, o):
        return s.begin_call(self.db, self.u, self.t['task_id'], self.t['token'], prompt_tokens=p, max_output=o)

    def settle(self, c, u):
        return s.settle_call(self.db, self.u, c, u)

    def fail(self, c, r):
        return s.fail_call(self.db, self.u, c, r)

class Reply:

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self, *a):
        return json.dumps({'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 120, 'completion_tokens': 30}}).encode()

class TokenQuotaTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'v2.db'
        s.initialize_store(self.db)
        s.configure_quota(self.db, 'tokens', user_daily_tokens=200000, site_daily_tokens=400000)
        self.ids = [s.provision_user(self.db, issuer='https://synthetic.invalid', subject=str(i)) for i in range(2)]
        self.users = [s.UserWorkspace(self.db, u) for u in self.ids]

    def start(self, i=0, key='a'):
        task = self.users[i].submit_task('model_work', key=key, request={'key': key}, units=1, heavy='model')
        return next((t for t in s.claim_tasks(self.db, total=2, pdf=0, deadline=timedelta(minutes=10)) if t['task_id'] == task['task_id']))

    def meter(self, i=0, key='a'):
        t = self.start(i, key)
        return Meter(self.db, self.ids[i], t)

    def total(self, i=0):
        return self.users[i].usage_today()['tokens']

    def test_duplicate_cache_and_bad_optional_report(self):
        m = self.meter()
        c, _ = m.begin(200, 2000)
        m.settle(c, {'prompt_tokens': 120, 'completion_tokens': 30, 'prompt_cache_hit_tokens': 80, 'prompt_cache_miss_tokens': 40})
        m.settle(c, {'prompt_tokens': 120, 'completion_tokens': 30})
        self.assertEqual(self.total()['total'], 150)
        self.assertEqual(self.total()['calls'], 1)
        c2, _ = m.begin(200, 2000)
        m.settle(c2, {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_cache_hit_tokens': 999})
        self.assertEqual(self.total()['total'], 270)

    def test_transport_retries_retained_and_invalid_answer_counted(self):
        m = self.meter()
        with patch.object(d.urllib.request, 'urlopen', side_effect=[URLError('synthetic timeout'), Reply()]), patch.object(d.time, 'sleep'):
            d.chat_json([{'role': 'user', 'content': 'json'}], api_key='synthetic', meter=m)
        v = self.total()
        self.assertEqual((v['calls'], v['unknown_calls'], v['total']), (2, 1, 150))
        self.assertGreater(v['reserved'], 8000)
        response = {'choices': [{'message': {'content': 'INVALID'}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 70, 'completion_tokens': 10}}
        with self.assertRaises(d.DeepSeekError):
            d.chat_json([{'role': 'user', 'content': 'json'}], api_key='synthetic', post=lambda p, k: response, meter=m)
        self.assertEqual(self.total()['total'], 230)

    def test_no_local_key_no_charge(self):
        m = self.meter()
        with patch.dict(d.os.environ, {}, clear=True), patch.object(d.sys, 'platform', 'linux'), patch.object(d.urllib.request, 'urlopen') as transport:
            with self.assertRaises(d.DeepSeekError):
                d.chat_json([{'role': 'user', 'content': 'json'}], meter=m)
            with self.assertRaises(d.DeepSeekError):
                d.chat_json([{'role': 'user', 'content': 'json'}], api_key='bad key', meter=m)
            transport.assert_not_called()
        self.assertEqual(self.total()['calls'], 0)

    def test_atomic_site_reserve(self):
        s.configure_quota(self.db, 'tokens', user_daily_tokens=3000, site_daily_tokens=3000)
        ms = [self.meter(0), self.meter(1)]
        barrier = threading.Barrier(2)

        def go(m):
            barrier.wait()
            try:
                m.begin(100, 2000)
                return 'ok'
            except s.Refused:
                return 'refused'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            result = list(pool.map(go, ms))
        self.assertEqual(sorted(result), ['ok', 'refused'])
        self.assertEqual(s.usage_on(self.db)['tokens']['site']['charged'], 2100)

    def test_deleted_account_cannot_replenish_site(self):
        m = self.meter()
        c, _ = m.begin(200, 2000)
        m.settle(c, {'prompt_tokens': 100, 'completion_tokens': 50})
        s.delete_account(self.db, self.ids[0], Path(self.tmp.name) / 'deleted.jsonl')
        v = s.usage_on(self.db)['tokens']
        self.assertEqual(v['site']['charged'], 150)
        self.assertEqual(v['deleted_accounts_charged'], 150)

    def test_unlimited_still_respects_kill_switch(self):
        s.configure_quota(self.db, 'tokens', user_daily_tokens=1, site_daily_tokens=1)
        s.set_test_unlimited(self.db, self.ids[0], enabled=True)
        m = self.meter()
        c, _ = m.begin(200, 2000)
        m.settle(c, {'prompt_tokens': 100, 'completion_tokens': 50})
        self.assertTrue(self.users[0].usage_today()['test_unlimited'])
        self.assertEqual(self.total()['total'], 150)
        s.set_setting(self.db, 'tasks_enabled', '0')
        with self.assertRaises(s.Refused):
            m.begin(200, 2000)

    def test_late_report_after_retry_stays_on_original_day(self):
        d1 = datetime(2026, 10, 19, 23, 59, tzinfo=timezone.utc)
        d2 = d1 + timedelta(minutes=2)
        with patch.object(s, '_clock', return_value=d1):
            old = self.meter()
            a, _ = old.begin(200, 2000)
            self.users[0].cancel_task(old.t['task_id'])
        with patch.object(s, '_clock', return_value=d2):
            new = self.meter()
            b, _ = new.begin(300, 2000)
            old.settle(a, {'prompt_tokens': 100, 'completion_tokens': 50})
            self.assertEqual(self.total()['reserved'], 2300)
            self.assertEqual(s.usage_on(self.db, '2026-10-19')['tokens']['site']['charged'], 150)
            new.settle(b, {'prompt_tokens': 200, 'completion_tokens': 70})
            self.assertEqual(self.total()['total'], 270)

    def test_mode_switch_requires_drain(self):
        s.set_setting(self.db, 'user_daily_units', '1')
        s.set_setting(self.db, 'site_daily_units', '1')
        self.start()
        with self.assertRaises(s.StoreError):
            s.configure_quota(self.db, 'legacy')
if __name__ == '__main__':
    unittest.main(verbosity=2)

class LedgerLifecycleTests(unittest.TestCase):
    setUp = TokenQuotaTests.setUp
    start = TokenQuotaTests.start
    meter = TokenQuotaTests.meter
    total = TokenQuotaTests.total
    def test_unknown_partial_overage_and_recovery_keep_the_charge(self):
        meter = self.meter()
        call, _ = meter.begin(100, 2000)
        meter.settle(call, {'prompt_tokens': 5000, 'completion_tokens': False})
        self.assertEqual(self.total()['charged'], 5000)
        self.assertEqual(self.total()['total'], 0)
        self.assertEqual(self.total()['unknown_calls'], 1)
        call, _ = meter.begin(100, 2000)
        s.recover_tasks(self.db)
        self.assertEqual(self.total()['charged'], 7100)
        self.assertEqual(self.total()['pending_calls'], 0)
        self.assertEqual(self.total()['unknown_calls'], 2)
        with self.assertRaises(s.LateResult):
            meter.begin(100, 2000)

    def test_upgrade_is_additive_idempotent_and_backup_checks_ledger(self):
        from feiran.v2_backup import verify_database
        with s.transaction(self.db, write=True) as connection:
            for table in s.METER_TABLES:
                connection.execute('DROP TABLE ' + table)
            connection.execute("DELETE FROM settings WHERE key IN ('quota_mode','user_daily_tokens','site_daily_tokens')")
        self.assertEqual(set(s.upgrade_store(self.db)), set(s.METER_TABLES))
        self.assertEqual(s.upgrade_store(self.db), [])
        self.assertEqual(self.users[0].usage_today()['mode'], 'legacy')
        s.configure_quota(self.db, 'tokens', user_daily_tokens=200000, site_daily_tokens=400000)
        meter = self.meter()
        call, _ = meter.begin(200, 2000)
        meter.settle(call, {'prompt_tokens': 100, 'completion_tokens': 30})
        report = verify_database(self.db)
        self.assertEqual(report['counts']['model_calls'], {'settled': 1})
