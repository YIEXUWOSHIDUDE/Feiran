"""Independent acceptance probes; synthetic data, no external service calls."""
import json
import tempfile
from pathlib import Path
from datetime import timedelta

import tenant_store as ts
import v2_backup
from cv import build_draft_from_facts
from tests.test_v2_store import StoreCase, run_task
from tests.test_web_v2 import V2Case
from web_v2 import MAX_BODY


def restore_missing_ledger():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / 'data'
        db = data / ts.DATABASE
        ts.initialize_store(db)
        owner = ts.provision_user(db, issuer='https://example.invalid/pool', subject='synthetic')
        user = ts.UserWorkspace(db, owner)
        user.import_facts([{'id': 'fact-private', 'type': 'experience', 'text': 'Synthetic private fact', 'tags': []}])
        archive = root / 'before-deletion.tar.gz'
        v2_backup.create_backup(data, archive)
        ts.delete_account(db, owner, data / ts.DELETION_LEDGER)
        restored = root / 'restored'
        result = v2_backup.restore_backup(archive, restored, live_ledger=root / 'typo-missing-ledger.jsonl')
        return {'problems': result['problems'], 'deleted_again': result['deleted_again'],
                'restored_account': ts.list_accounts(restored / ts.DATABASE)[0]['state'],
                'restored_private_facts': len(ts.UserWorkspace(restored / ts.DATABASE, owner).list_facts())}


def store_probes():
    case = StoreCase()
    case.setUp()
    try:
        job = case.ready(case.a)
        inputs = case.a.cv_inputs(job, 'en')
        draft = build_draft_from_facts(inputs['profile']['profile'],
                                      inputs['facts'].current([f['id'] for f in inputs['facts'].listed()]),
                                      'en', job=inputs['requirements']['decided'])
        task, token = run_task(case.database, case.a_id, 'prepare_cv', job_id=job)
        original = case.a.list_facts()[0]
        case.a.revise_fact(original['id'], expected_version=1, text=original['text']+' Changed.', tags=original['tags'])
        output = case.a.publish_cv(task_id=task, token=token, job_id=job, language='en', base_head=None,
                                  documents=[('draft', draft)], profile_version=1, requirements_version=1, stages=[])
        stale = {'task_status': case.a.get_task(task)['status'],
                 'inputs_current': case.a.get_material(output['materials'][0])['inputs_current'] if output.get('materials') else None}
        task, token = run_task(case.database, case.b_id, 'cost-probe')
        ts.mark_spending(case.database, case.b_id, task, token, 'first-call')
        # First call raises without usage, then a later stage succeeds and reports its usage.
        ts.mark_spending(case.database, case.b_id, task, token, 'second-call')
        # record_usage now names the execution it reports for (its token), so late answers stay apart.
        ts.record_usage(case.database, case.b_id, task, token, {'prompt_tokens': 10, 'completion_tokens': 2})
        cost = case.b.get_task(task)['cost']
        ts.finish_task(case.database, case.b_id, task, token, 'succeeded')
        task, token = run_task(case.database, case.b_id, 'deadline-probe')
        case.clock.advance(minutes=6)
        try:
            ts.finish_task(case.database, case.b_id, task, token, 'succeeded')
        except ts.LateResult:
            pass
        deadline = case.b.get_task(task)['status']
        return {'stale_publication': stale, 'unknown_then_known_call_cost': cost,
                'finish_after_deadline_before_sweeper': deadline}
    finally:
        case.doCleanups()


def web_probes():
    case = V2Case()
    case.setUp()
    try:
        client, headers = case.browser('body-probe')
        body = json.dumps({'stage': 'job_processing', 'version': 1, 'ignored': 'x' * (MAX_BODY + 1)}).encode()
        chunked = client.post('/api/consent', headers={**headers, 'Content-Type': 'application/json'},
                              content=iter([body[:1000], body[1000:]]))
        normal = client.post('/api/consent', headers={**headers, 'Content-Type': 'application/json'}, content=body)
        large = {'bytes': len(body), 'without_content_length': chunked.status_code,
                 'with_content_length': normal.status_code,
                 'sent_transfer_encoding': chunked.request.headers.get('transfer-encoding')}
        client, headers = case.ready_user('idempotency-probe')
        job = case.new_job(client, headers)
        keyed = {**headers, 'Idempotency-Key': 'stable-request'}
        first = client.post(f'/api/jobs/{job}/cv/en/prepare', headers=keyed)
        settled = case.settle(client, headers, first)
        retry = client.post(f'/api/jobs/{job}/cv/en/prepare', headers=keyed)
        retry_result = {'first': first.status_code, 'finished': settled['status'], 'retry': retry.status_code,
                        'retry_body': retry.json(), 'first_task_id': first.json()['task']['task_id']}
        # A second tab replays its earlier selection after the first tab has excluded a requirement.
        before = client.get(f'/api/jobs/{job}').json()
        rid = before['candidates'][0]['id']
        # The page now sends the requirements version it showed (a required field); this tab is current.
        first_decision = client.post(f'/api/jobs/{job}/requirements/decide', headers=headers,
                                     json={'confirm': [], 'exclude': [rid],
                                           'expected_version': before['requirements_version']})
        if first_decision.status_code == 202:
            case.settle(client, headers, first_decision)
        second_decision = client.post(f'/api/jobs/{job}/requirements/decide', headers=headers,
                                      json={'confirm': [rid], 'exclude': [],
                                            'expected_version': before['requirements_version']})
        after = client.get(f'/api/jobs/{job}').json()
        decision = {'old_version': before['requirements_version'], 'final_version': after['requirements_version'],
                    'stale_page_status': second_decision.status_code,
                    'final_requirement_status': next(i['status'] for i in after['candidates'] if i['id'] == rid)}
        case.app.state.runner.drain(20)
        return {'body_limit': large, 'idempotency_after_success': retry_result, 'stale_decision': decision}
    finally:
        case.doCleanups()


import unittest


class IndependentAcceptanceRegressions(unittest.TestCase):
    """Failures reproduced by Codex after the 483-test baseline passed.

    These checks express the intended contracts. Fix production behavior, not the
    assertions; synthetic fixtures and provider doubles stay local to these probes.
    """

    @classmethod
    def setUpClass(cls):
        cls.store = store_probes()
        cls.web = web_probes()

    def test_restore_requires_the_explicit_live_ledger_to_exist(self):
        with self.assertRaises(v2_backup.BackupError):
            restore_missing_ledger()

    def test_chunked_json_obeys_the_same_body_limit(self):
        self.assertEqual(self.web['body_limit']['with_content_length'], 413)
        self.assertEqual(self.web['body_limit']['without_content_length'], 413)

    def test_identical_idempotency_key_returns_completed_task(self):
        self.assertEqual(self.web['idempotency_after_success']['finished'], 'succeeded')
        self.assertEqual(self.web['idempotency_after_success']['retry'], 202)
        self.assertEqual(self.web['idempotency_after_success']['retry_body']['task']['task_id'],
                         self.web['idempotency_after_success']['first_task_id'])

    def test_stale_requirement_decision_cannot_overwrite_newer_choice(self):
        self.assertEqual(self.web['stale_decision']['stale_page_status'], 409)
        self.assertEqual(self.web['stale_decision']['final_requirement_status'], 'excluded')

    def test_changed_input_cannot_publish_a_successful_current_result(self):
        self.assertEqual(self.store['stale_publication']['task_status'], 'superseded')

    def test_later_call_usage_does_not_erase_unknown_cost(self):
        self.assertEqual(self.store['unknown_then_known_call_cost'], 'unknown')

    def test_deadline_is_enforced_when_publishing_not_only_by_sweeper(self):
        self.assertNotEqual(self.store['finish_after_deadline_before_sweeper'], 'succeeded')


    def test_unknown_cost_survives_a_later_successful_attempt(self):
        case = StoreCase()
        case.setUp()
        try:
            task, token = run_task(case.database, case.a_id, 'retry-cost', key='lost', units=2)
            ts.mark_spending(case.database, case.a_id, task, token, 'model')
            ts.finish_task(case.database, case.a_id, task, token, 'failed', error_code='response_lost')
            self.assertEqual(case.a.get_task(task)['cost'], 'unknown')
            task, token = run_task(case.database, case.a_id, 'retry-cost', key='lost', units=2)
            ts.mark_spending(case.database, case.a_id, task, token, 'model')
            ts.record_usage(case.database, case.a_id, task, token, {'prompt_tokens': 5, 'completion_tokens': 2})
            ts.finish_task(case.database, case.a_id, task, token, 'succeeded')
            self.assertEqual(case.a.usage_today()['used'], 4)
            self.assertEqual(case.a.get_task(task)['cost'], 'unknown',
                             'First attempt never reported usage; task-wide cost is still unknown')
        finally:
            case.doCleanups()

    def test_pasted_job_response_loss_reuses_the_original_job(self):
        from tests.test_web_v2 import JD_TEXT
        case = V2Case()
        case.setUp()
        try:
            client, headers = case.ready_user('paste-retry')
            headers = {**headers, 'Idempotency-Key': 'paste-once'}
            body = {'title': 'Example', 'text': JD_TEXT}
            first = client.post('/api/jobs', json=body, headers=headers)
            case.settle(client, headers, first)
            second = client.post('/api/jobs', json=body, headers=headers)
            self.assertIn(second.status_code, (200, 202), second.text)
            self.assertEqual(second.json()['job_id'], first.json()['job_id'])
            self.assertEqual(len(client.get('/api/jobs').json()['jobs']), 1)
        finally:
            case.doCleanups()


    def test_paste_key_is_bound_even_when_it_returns_an_existing_job(self):
        from tests.test_web_v2 import JD_TEXT
        case = V2Case()
        case.setUp()
        try:
            client, headers = case.ready_user('existing-paste-key')
            body = {'title': 'Original job', 'text': JD_TEXT}
            first = client.post('/api/jobs', headers={**headers, 'Idempotency-Key': 'original'}, json=body)
            case.settle(client, headers, first)
            alias = {**headers, 'Idempotency-Key': 'key-that-returned-existing'}
            existing = client.post('/api/jobs', headers=alias, json=body)
            self.assertEqual(existing.status_code, 200)
            self.assertEqual(existing.json()['job_id'], first.json()['job_id'])
            with ts.transaction(case.database) as db:
                before_tasks = db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
            different = client.post('/api/jobs', headers=alias,
                                    json={**body, 'title': 'Another job', 'text': JD_TEXT + '\nSQL experience required.'})
            case.app.state.runner.drain(20)
            self.assertEqual(different.status_code, 409, different.text)
            self.assertEqual(len(client.get('/api/jobs').json()['jobs']), 1)
            with ts.transaction(case.database) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], before_tasks)
        finally:
            case.doCleanups()


if __name__ == '__main__':
    unittest.main()
