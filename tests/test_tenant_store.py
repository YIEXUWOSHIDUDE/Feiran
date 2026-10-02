"""Synthetic V2 acceptance: real SQLite, existing draft rules, no identity/model network."""
import copy
from contextlib import contextmanager
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cv import CVError, build_draft, build_draft_from_facts, render_html
from facts import FactStoreError, import_facts, initialize_database, confirm_facts, load_current_facts
from tenant_store import (Conflict, NotFound, StoreError, UserWorkspace,
                          initialize_store, provision_user, set_user_active)
from tests.test_cv import FACTS, PROFILE


@contextmanager
def raw(path):
    """A plain SQLite connection for poking at the file directly, committed and then closed."""
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


JD = {'text': 'Requirements:\nPython and unit tests.', 'title': 'Example internship',
      'source': None, 'captured_at': '2026-09-30T10:00:00+00:00'}


def one_fact(fact_id='fact-shared', text='Wrote tests for a course project.'):
    return {'id': fact_id, 'text': text, 'type': 'project', 'tags': ['testing']}


def profile(name='Alex Example', fact_id='fact-shared', language='en'):
    return {'profile_version': 1, 'name': {language: name}, 'contact': {},
            'sections': [{'kind': 'experience', 'entries': [{'title': 'Example Corp', 'facts': [fact_id]}]}]}


class TenantStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / 'v2.db'
        initialize_store(self.database)
        self.a_id = provision_user(self.database, issuer='https://identity.example', subject='A')
        self.b_id = provision_user(self.database, issuer='https://identity.example', subject='B')
        self.a = UserWorkspace(self.database, self.a_id)
        self.b = UserWorkspace(self.database, self.b_id)

    def prepare(self, user, name='Alex Example', text='Wrote tests for a course project.', language='en'):
        user.import_facts([one_fact(text=text)])
        user.confirm_facts([('fact-shared', 1)])
        user.save_profile(language, profile(name, language=language), expected_version=0)
        return user.create_job(JD, request_id='job-request')['job_id']

    def draft(self, user, job_id, request_id='draft-request', language='en', version=1, fact_version=1):
        return user.create_draft(job_id, language, request_id=request_id,
                                 expected_profile_version=version, expected_facts=[('fact-shared', fact_version)])

    def test_two_users_complete_draft_flow_with_same_fact_ids_and_source(self):
        a_job = self.prepare(self.a)
        b_job = self.prepare(self.b, name='Blair Example', text='Wrote tests for another project.')
        a_draft, b_draft = self.draft(self.a, a_job), self.draft(self.b, b_job)
        self.assertEqual(a_draft['draft']['status'], 'draft')
        self.assertEqual(b_draft['draft']['status'], 'draft')
        self.assertEqual(a_draft['draft']['facts'], b_draft['draft']['facts'])
        self.assertNotEqual(a_draft['material_id'], b_draft['material_id'])
        self.assertEqual(a_draft['draft']['job']['source'], None)
        for draft, own, other in [(a_draft, 'Alex Example', 'Blair Example'), (b_draft, 'Blair Example', 'Alex Example')]:
            rendered = render_html(draft['draft'])
            self.assertIn(own, rendered)
            self.assertNotIn(other, rendered)
            self.assertIn('DRAFT', rendered)
        self.assertEqual([x['job_id'] for x in self.a.list_jobs()], [a_job])
        self.assertEqual([x['job_id'] for x in self.b.list_jobs()], [b_job])

    def test_foreign_and_missing_job_material_fact_have_same_refusal(self):
        job = self.prepare(self.a)
        material = self.draft(self.a, job)
        for method, foreign in [(self.b.get_job, job), (self.b.list_materials, job),
                                 (self.b.get_material, material['material_id']), (self.b.get_fact, 'fact-shared')]:
            errors = []
            for value in [foreign, 'unknown-id']:
                with self.assertRaises(NotFound) as error:
                    method(value)
                errors.append(str(error.exception))
            self.assertEqual(*errors)
        with self.assertRaises(NotFound):
            self.b.revise_fact('fact-shared', expected_version=1, text='Injected', fact_type='project', tags=[])
        with self.assertRaises(NotFound):
            self.b.confirm_facts([('fact-shared', 1)])
        self.assertEqual(self.a.get_fact('fact-shared')['text'], one_fact()['text'])

    def test_foreign_job_cannot_be_used_for_own_material(self):
        foreign_job = self.prepare(self.a)
        self.prepare(self.b)
        with self.assertRaises(NotFound):
            self.draft(self.b, foreign_job)
        self.assertEqual(self.b.list_materials(self.b.list_jobs()[0]['job_id']), [])

    def test_profile_cannot_reference_foreign_fact_and_refusal_writes_nothing(self):
        self.a.import_facts([one_fact('fact-a-only')])
        with self.assertRaises(NotFound):
            self.b.save_profile('en', profile(fact_id='fact-a-only'), expected_version=0)
        with self.assertRaises(NotFound):
            self.b.get_profile('en')
        self.assertEqual(self.b.list_facts(), [])

    def test_batch_confirmation_is_atomic_if_one_reference_is_foreign(self):
        self.a.import_facts([one_fact('fact-a-only')])
        self.b.import_facts([one_fact('fact-b-only')])
        with self.assertRaises(NotFound):
            self.b.confirm_facts([('fact-b-only', 1), ('fact-a-only', 1)])
        self.assertEqual(self.b.get_fact('fact-b-only')['status'], 'pending')
        self.assertEqual(self.a.get_fact('fact-a-only')['status'], 'pending')

    def test_import_cannot_self_confirm_or_supply_owner(self):
        for extra in [{'confirmed': True}, {'status': 'confirmed'}, {'user_id': self.b_id}]:
            with self.subTest(extra=extra), self.assertRaises(FactStoreError):
                self.a.import_facts([one_fact(), {**one_fact('fact-injected'), **extra}])
            self.assertEqual(self.a.list_facts(), [])

    def test_batch_import_conflict_rolls_back_and_cannot_revert_corrected_fact(self):
        self.a.import_facts([one_fact()])
        self.a.revise_fact('fact-shared', expected_version=1, text='Corrected experience', fact_type='project', tags=[])
        with self.assertRaises(Conflict):
            self.a.import_facts([one_fact('fact-new'), one_fact()])
        self.assertEqual(len(self.a.list_facts()), 1)
        self.assertEqual(self.a.get_fact('fact-shared')['version'], 2)
        self.assertEqual(self.a.get_fact('fact-shared', version=1)['text'], one_fact()['text'])

    def test_duplicate_import_is_local_and_does_not_remove_confirmation(self):
        item = {key: value for key, value in one_fact().items() if key != 'id'}
        a = self.a.import_facts([item])[0]
        self.a.confirm_facts([(a['id'], 1)])
        again = self.a.import_facts([item])[0]
        b = self.b.import_facts([item])[0]
        self.assertEqual(a['id'], again['id'])
        self.assertEqual(again['status'], 'confirmed')
        self.assertEqual(b['status'], 'pending')
        self.assertNotEqual(a['id'], b['id'])

    def test_fact_edit_invalidates_own_material_only_and_retains_history(self):
        a_job, b_job = self.prepare(self.a), self.prepare(self.b)
        a_draft, b_draft = self.draft(self.a, a_job), self.draft(self.b, b_job)
        self.a.revise_fact('fact-shared', expected_version=1, text='Corrected project scope', fact_type='project', tags=[])
        self.assertEqual(self.a.get_fact('fact-shared')['status'], 'pending')
        self.assertEqual(self.a.get_fact('fact-shared', version=1)['status'], 'confirmed')
        self.assertFalse(self.a.get_material(a_draft['material_id'])['inputs_current'])
        self.assertTrue(self.b.get_material(b_draft['material_id'])['inputs_current'])
        self.assertEqual(self.a.get_material(a_draft['material_id'])['draft'], a_draft['draft'])
        with self.assertRaises(Conflict):
            self.a.confirm_facts([('fact-shared', 1)])
        with self.assertRaises(Conflict):
            self.draft(self.a, a_job, 'stale')
        with self.assertRaises(CVError):
            self.draft(self.a, a_job, 'pending', fact_version=2)
        self.assertEqual(len(self.a.list_materials(a_job)), 1)
        self.a.confirm_facts([('fact-shared', 2)])
        new = self.draft(self.a, a_job, 'new-request', fact_version=2)
        self.assertEqual(new['version'], 2)
        self.assertTrue(new['inputs_current'])
        self.assertEqual(new['draft']['status'], 'draft')

    def test_profile_versions_are_separate_by_language_and_owner(self):
        a_job = self.prepare(self.a)
        a_draft = self.draft(self.a, a_job)
        self.a.save_profile('zh', profile('示例甲', language='zh'), expected_version=0)
        zh = self.draft(self.a, a_job, 'zh-request', language='zh')
        self.a.save_profile('en', profile('Alex Updated'), expected_version=1)
        self.assertEqual(self.a.get_profile('en', version=1)['profile']['name']['en'], 'Alex Example')
        self.assertEqual(self.a.get_profile('zh')['version'], 1)
        self.assertFalse(self.a.get_material(a_draft['material_id'])['inputs_current'])
        self.assertTrue(self.a.get_material(zh['material_id'])['inputs_current'])
        self.assertIn('示例甲', render_html(zh['draft']))
        with self.assertRaises(NotFound):
            self.b.get_profile('en', version=1)
        with self.assertRaises(Conflict):
            self.a.save_profile('en', profile('Wrong'), expected_version=1)
        self.assertEqual(self.a.get_profile('en')['version'], 2)

    def test_profile_can_wait_for_confirmation_but_cannot_generate_until_confirmed(self):
        self.a.import_facts([one_fact()])
        self.a.save_profile('en', profile(), expected_version=0)
        job = self.a.create_job(JD, request_id='j')['job_id']
        with self.assertRaises(CVError):
            self.draft(self.a, job)
        self.assertEqual(self.a.list_materials(job), [])
        self.a.confirm_facts([('fact-shared', 1)])
        self.assertTrue(self.draft(self.a, job)['inputs_current'])

    def test_profile_and_expected_fact_validation_reject_injected_or_missing_refs(self):
        job = self.prepare(self.a)
        for refs in [[], [('fact-shared', 1), ('fact-other', 1)], [('fact-shared', True)],
                     [('fact-shared', 1), ('fact-shared', 1)]]:
            with self.subTest(refs=refs), self.assertRaises(StoreError):
                self.a.create_draft(job, 'en', request_id='bad', expected_profile_version=1, expected_facts=refs)
        for bad in [{**profile(), 'approved': True}, profile(language='zh')]:
            with self.assertRaises((StoreError, CVError)):
                self.a.save_profile('en', bad, expected_version=1)
        self.assertEqual(self.a.list_materials(job), [])

    def test_draft_retry_is_idempotent_even_after_inputs_change(self):
        job = self.prepare(self.a)
        first = self.draft(self.a, job)
        self.assertEqual(self.draft(self.a, job), first)
        self.a.save_profile('en', profile('Alex Updated'), expected_version=1)
        retried = self.draft(self.a, job)
        self.assertEqual(retried['material_id'], first['material_id'])
        self.assertFalse(retried['inputs_current'])
        with self.assertRaises(Conflict):
            self.draft(self.a, job, version=2)
        self.assertEqual(len(self.a.list_materials(job)), 1)
        regenerated = self.draft(self.a, job, 'explicit-new-request', version=2)
        self.assertNotEqual(regenerated['material_id'], first['material_id'])
        self.assertEqual(regenerated['version'], 2)

    def test_job_retry_and_unknown_source_are_preserved(self):
        first = self.a.create_job(JD, request_id='same')
        self.assertEqual(self.a.create_job(JD, request_id='same'), first)
        self.assertEqual(first['jd'], JD)
        self.assertNotIn('posted_at', first['jd'])
        with self.assertRaises(Conflict):
            self.a.create_job({**JD, 'text': 'Different role'}, request_id='same')
        self.assertEqual(len(self.a.list_jobs()), 1)
        for bad in [{**JD, 'captured_at': '2026-09-30'}, {**JD, 'user_id': self.b_id}, {**JD, 'text': ''}]:
            with self.assertRaises((ValueError, StoreError)):
                self.a.create_job(bad, request_id='bad')
        self.assertEqual(len(self.a.list_jobs()), 1)

    def test_reopened_store_preserves_versions_ownership_and_draft(self):
        job = self.prepare(self.a)
        draft = self.draft(self.a, job)
        initialize_store(self.database)
        reopened = UserWorkspace(self.database, self.a_id)
        self.assertEqual(reopened.get_material(draft['material_id']), draft)
        self.assertEqual(reopened.get_fact('fact-shared')['status'], 'confirmed')
        with self.assertRaises(NotFound):
            UserWorkspace(self.database, self.b_id).get_material(draft['material_id'])

    def test_disabled_and_unknown_users_cannot_use_preexisting_workspace_handles(self):
        job = self.prepare(self.a)
        material = self.draft(self.a, job)
        set_user_active(self.database, self.a_id, active=False)
        calls = [self.a.list_facts, self.a.list_jobs, lambda: self.a.get_material(material['material_id']),
                 lambda: self.a.import_facts([one_fact('fact-new')]),
                 lambda: self.a.get_profile('en'), lambda: self.draft(self.a, job)]
        for call in calls:
            with self.assertRaises(NotFound):
                call()
        self.assertEqual(provision_user(self.database, issuer='https://identity.example', subject='A'), self.a_id)
        with self.assertRaises(NotFound):
            self.a.list_facts()
        with self.assertRaises(NotFound):
            UserWorkspace(self.database, 'unknown').list_facts()
        self.assertEqual(self.b.list_facts(), [])

    def test_provider_identity_is_stable_and_includes_issuer(self):
        self.assertEqual(provision_user(self.database, issuer='https://identity.example', subject='A'), self.a_id)
        other = provision_user(self.database, issuer='https://other.example', subject='A')
        self.assertNotEqual(other, self.a_id)
        self.assertEqual(UserWorkspace(self.database, other).list_facts(), [])

    def test_two_concurrent_edits_cannot_lose_a_revision(self):
        self.a.import_facts([one_fact()])
        start = threading.Barrier(2)
        def edit(text):
            start.wait(timeout=5)
            try:
                self.a.revise_fact('fact-shared', expected_version=1, text=text, fact_type='project', tags=[])
                return 'saved'
            except Conflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(edit, ['First correction', 'Second correction']))
        self.assertCountEqual(results, ['saved', 'conflict'])
        self.assertEqual(self.a.get_fact('fact-shared')['version'], 2)
        self.assertEqual(self.a.get_fact('fact-shared', version=1)['text'], one_fact()['text'])

    def test_two_concurrent_same_generation_requests_publish_once(self):
        job = self.prepare(self.a)
        start = threading.Barrier(2)
        def generate(_):
            start.wait(timeout=5)
            return self.draft(self.a, job)['material_id']
        with ThreadPoolExecutor(max_workers=2) as executor:
            ids = list(executor.map(generate, range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(len(self.a.list_materials(job)), 1)

    def test_database_foreign_keys_reject_cross_user_links(self):
        job = self.prepare(self.a)
        material = self.draft(self.a, job)
        self.b.import_facts([one_fact('fact-b-only')])
        with raw(self.database) as connection:
            connection.execute('PRAGMA foreign_keys = ON')
            statements = [
                ('INSERT INTO profile_facts VALUES (?, ?, ?, ?)', (self.a_id, 'en', 1, 'fact-b-only')),
                ('INSERT INTO material_facts VALUES (?, ?, ?, ?)', (self.a_id, material['material_id'], 'fact-b-only', 1)),
                ('INSERT INTO material_facts VALUES (?, ?, ?, ?)', (self.b_id, material['material_id'], 'fact-b-only', 1)),
            ]
            for sql, values in statements:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql, values)
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_sql_failure_rolls_back_material_and_does_not_consume_request(self):
        job = self.prepare(self.a)
        # A trigger fails after the material row is written, while its input refs are inserted.
        with raw(self.database) as connection:
            connection.execute("CREATE TRIGGER refuse_ref BEFORE INSERT ON material_facts BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(StoreError):
            self.draft(self.a, job)
        self.assertEqual(self.a.list_materials(job), [])
        with raw(self.database) as connection:
            connection.execute('DROP TRIGGER refuse_ref')
        self.assertEqual(self.draft(self.a, job)['version'], 1)

    def test_foreign_fact_ids_do_not_resolve_by_guessing_or_sql_input(self):
        self.prepare(self.a)
        for value in ['fact-shared', "' OR 1=1 --", '../v1/workbench.db']:
            with self.assertRaises(NotFound):
                self.b.get_fact(value)
        self.assertEqual(self.b.list_facts(), [])

    def test_legacy_and_v2_stores_cannot_be_mistaken_for_each_other(self):
        legacy = Path(self.temp.name) / 'v1.db'
        initialize_database(legacy)
        import_facts(legacy, [one_fact()])
        before = legacy.read_bytes()
        with self.assertRaises(StoreError):
            initialize_store(legacy)
        self.assertEqual(legacy.read_bytes(), before)
        before = self.database.read_bytes()
        with self.assertRaises(FactStoreError):
            initialize_database(self.database)
        self.assertEqual(self.database.read_bytes(), before)

    def test_missing_store_and_unknown_schema_never_initialize_silently(self):
        missing = Path(self.temp.name) / 'missing.db'
        with self.assertRaises(StoreError):
            UserWorkspace(missing, self.a_id).list_facts()
        self.assertFalse(missing.exists())
        unknown = Path(self.temp.name) / 'unknown.db'
        with raw(unknown) as connection:
            connection.execute('CREATE TABLE unrelated(value TEXT)')
        before = unknown.read_bytes()
        with self.assertRaises(StoreError):
            initialize_store(unknown)
        self.assertEqual(unknown.read_bytes(), before)
        link = Path(self.temp.name) / 'link.db'
        link.symlink_to(self.database)
        with self.assertRaises(StoreError):
            initialize_store(link)
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)

    def test_newer_schema_is_refused_without_changes(self):
        for version in (99, 2):  # 2: the unreleased format without request keys, never migrated
            with self.subTest(version=version):
                with raw(self.database) as connection:
                    connection.execute(f'PRAGMA user_version = {version}')
                before = self.database.read_bytes()
                with self.assertRaises(StoreError):
                    self.a.list_facts()
                with self.assertRaises(StoreError):
                    initialize_store(self.database)
                self.assertEqual(self.database.read_bytes(), before)


class DraftReuseTests(unittest.TestCase):
    def test_legacy_and_scoped_snapshot_use_identical_rules_for_both_languages(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'v1.db'
            import_facts(db, FACTS)
            confirm_facts(db, [(fact['id'], 1) for fact in FACTS])
            scoped = load_current_facts(db, [fact['id'] for fact in FACTS])
            for language in ['en', 'zh']:
                legacy = build_draft(PROFILE, db, language)
                pure = build_draft_from_facts(PROFILE, scoped, language)
                legacy.pop('created_at'); pure.pop('created_at')
                self.assertEqual(legacy, pure)
                self.assertEqual(render_html(legacy), render_html(pure))
            pending = copy.deepcopy(scoped)
            pending[FACTS[0]['id']]['status'] = 'pending'
            with self.assertRaises(CVError):
                build_draft_from_facts(PROFILE, pending)
