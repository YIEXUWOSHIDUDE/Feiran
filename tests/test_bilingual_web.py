"""Bilingual uploads and edits use synthetic input and offline model responses only."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from cv import build_draft, approve_draft, content_fingerprint
from facts import import_facts, confirm_facts, list_facts
from job_search import prepare_pasted_jd, prepare_review_input
from tests.test_web import FakeDeepSeek, TOKEN
from tests.test_cv import FakePrinter
from web import create_app


class BilingualWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = self.root / 'workbench.db'
        self.profile = self.root / 'cv-profile.json'
        self.en = {'profile_version': 1, 'name': 'Alex Example', 'contact': {},
                   'sections': [{'kind': 'skills', 'entries': [{'facts': ['fact-en']}]}]}
        self.profile.write_text(json.dumps(self.en))
        import_facts(self.db, [{'id': 'fact-en', 'text': 'Languages: Python', 'type': 'skill', 'tags': ['Python']}])
        confirm_facts(self.db, [('fact-en', 1)])
        self.chat = FakeDeepSeek()
        self.chat.cv_structure = {'sections': [{'kind': 'skills', 'heading': 2,
            'entries': [{'facts': [{'lines': [3], 'tags': ['Python']}]}]}]}
        self.app = create_app(self.db, self.root/'jobs', profile_path=self.profile, token=TOKEN,
                              chat=self.chat, printer=FakePrinter(), starter=[])
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765', headers={'X-Workbench-Token': TOKEN})
        self.job = self.app.state.workspace.create_job(prepare_review_input(
            prepare_pasted_jd('Requirements:\n- Python', 'Engineer', 'Example', None, 'Shanghai')))

    def tearDown(self):
        self.client.close()
        self.directory.cleanup()

    def upload(self, language='zh', line='编程语言：Python', name='Alex Example'):
        # Deliberately a Latin name on a Chinese document: explicit language must win.
        with patch('web.read_pdf', return_value={'lines': [[name], ['专业技能'], [line]], 'links': []}):
            response = self.client.post('/api/cv/upload?language='+language, content=b'%PDF-synthetic')
        self.assertEqual(response.status_code, 200, response.text)
        result = self.client.post('/api/cv/uploads/'+response.json()['upload_id']+'/save', json={'name': name})
        self.assertEqual(result.status_code, 200, result.text)
        return json.loads((self.root/'cv-profile.zh.json').read_text())

    def test_chinese_upload_keeps_english_and_reupload_keeps_history(self):
        original = self.profile.read_bytes()
        zh = self.upload()
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertEqual(zh['name'], {'zh': 'Alex Example'})
        self.assertEqual(self.client.get('/api/cv/languages').json()['languages'], ['en', 'zh'])
        pending = [f for f in list_facts(self.db) if f['status'] == 'pending']
        self.assertEqual(len(pending), 1)
        confirm_facts(self.db, [(pending[0]['id'], pending[0]['version'])])
        self.assertIn('编程语言', build_draft(zh, self.db, 'zh')['sections'][0]['entries'][0]['lines'][0]['text'])
        self.assertEqual(build_draft(self.en, self.db, 'en')['header']['name'], 'Alex Example')
        self.upload(line='编程语言：Python 和 Java')
        self.assertEqual(self.profile.read_bytes(), original)
        self.assertTrue(list((self.root/'profile-history').glob('cv-profile.zh-*.json')))
        restored = create_app(self.db, self.root/'jobs', profile_path=self.profile, token=TOKEN, chat=self.chat, starter=[])
        with TestClient(restored, base_url='http://localhost', headers={'X-Workbench-Token': TOKEN}) as client:
            self.assertEqual(client.get('/api/cv/languages').json()['languages'], ['en', 'zh'])

    def test_language_choice_is_saved_and_absent_language_is_refused(self):
        url = f'/api/jobs/{self.job}/language'
        self.assertEqual(self.client.post(url, json={'language':'zh'}).status_code, 400)
        self.upload()
        result = self.client.post(url, json={'language':'zh'})
        self.assertEqual(result.json()['language'], 'zh')
        self.assertEqual(self.client.get('/api/jobs/'+self.job).json()['language'], 'zh')
        self.assertEqual(self.client.post(url, json={'language':'../../secret'}).status_code, 400)

    def test_edit_creates_pending_version_and_stale_browser_cannot_overwrite(self):
        draft = build_draft(self.en, self.db, 'en')
        approved = approve_draft(draft, self.db)
        workspace = self.app.state.workspace
        workspace.write(self.job, 'cv-draft-en', draft)
        workspace.write(self.job, 'cv-approved-en', approved)
        workspace.write_bytes(self.job, 'cv-final-en', b'%PDF-synthetic')
        body = {'text':'Languages: Python, Java', 'tags':['Python','Java'], 'expected_version':1}
        saved = self.client.post('/api/facts/fact-en/edit', json=body)
        self.assertEqual(saved.json()['fact']['status'], 'pending')
        self.assertEqual(saved.json()['fact']['version'], 2)
        self.assertEqual(self.client.post('/api/facts/fact-en/edit', json=body).status_code, 400)
        self.assertTrue(self.client.get('/api/jobs/'+self.job).json()['cv']['en']['stale'])
        self.assertEqual(self.client.get(f'/download/{self.job}/en.pdf?token={TOKEN}').status_code, 400)
        self.assertEqual(self.client.post(f'/api/jobs/{self.job}/cv/en/approve',
            json={'expected_content_sha256':content_fingerprint(approved)}).status_code, 400)

    def test_profile_replacement_blocks_old_approval_and_export(self):
        draft = build_draft(self.en, self.db, 'en')
        self.app.state.workspace.write(self.job, 'cv-draft-en', draft)
        self.app.state.workspace.write(self.job, 'cv-approved-en', approve_draft(draft, self.db))
        updated = copy.deepcopy(self.en); updated['name'] = 'Alex Revised'
        self.profile.write_text(json.dumps(updated))
        self.assertTrue(self.client.get('/api/jobs/'+self.job).json()['cv']['en']['stale'])
        self.assertEqual(self.client.post(f'/api/jobs/{self.job}/cv/en/export').status_code, 400)

    def test_language_profiles_and_job_choice_survive_backup_restore(self):
        from backup import create_backup, restore_backup
        self.upload()
        self.client.post(f'/api/jobs/{self.job}/language',json={'language':'zh'})
        # Archives go outside the data tree, as in the deployment backup workflow.
        with tempfile.TemporaryDirectory() as folder:
            archive=Path(folder)/'backup.tar.gz'; restored=Path(folder)/'restored'
            result=create_backup(self.root,archive)
            self.assertEqual(result.get('problems',[]),[])
            restore_backup(archive,restored)
            self.assertEqual((restored/'cv-profile.zh.json').read_bytes(),(self.root/'cv-profile.zh.json').read_bytes())
            restored_app=create_app(restored/'workbench.db',restored/'jobs',profile_path=restored/'cv-profile.json',token=TOKEN,chat=self.chat,starter=[])
            with TestClient(restored_app,base_url='http://localhost',headers={'X-Workbench-Token':TOKEN}) as client:
                self.assertEqual(client.get('/api/cv/languages').json()['languages'],['en','zh'])
                self.assertEqual(client.get('/api/jobs/'+self.job).json()['language'],'zh')

    def test_other_language_identity_is_masked_when_preparing_english(self):
        self.upload(name='示例私密姓名')
        self.client.post('/api/facts/fact-en/edit',json={'text':'Languages: Python for 示例私密姓名',
            'tags':['Python'],'expected_version':1})
        confirm_facts(self.db,[('fact-en',2)])
        self.chat.sent.clear()
        created=self.client.post('/api/jobs',json={'title':'Engineer','text':'Requirements:\n- Python'})
        self.assertEqual(created.status_code,200,created.text)
        job_id=created.json()['job_id']
        sent='\n'.join(self.chat.sent)
        self.assertTrue(sent)
        self.assertNotIn('示例私密姓名',sent)
        # An unreadable second-language profile must stop requests, not drop its redactions.
        (self.root/'cv-profile.zh.json').write_text('{')
        self.chat.sent.clear()
        self.client.post(f'/api/jobs/{job_id}/cv/en/prepare')
        self.assertEqual(self.chat.sent,[])
