"""Offline fixtures for the public Tencent adapter; no account or personal data."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from job_search import SearchError, fetch_board, fetch_selected, parse_board_link
from listings import add_source, refresh_source, list_sources, ListingsError


def raw(job_id='123'):
    return {'PostId':job_id,'RecruitPostName':'后端开发工程师','CountryName':'中国',
            'LocationName':'北京','Responsibility':'开发 Python 后端服务',
            'LastUpdateTime':'2026年09月30日','IsValid':True,'RequireWorkYearsName':'两年以上工作经验'}


def page(count, posts):
    return {'Code':200,'Data':{'Count':count,'Posts':posts}}


class TencentTests(unittest.TestCase):
    @patch('job_search._fetch_json')
    def test_all_pages_and_update_date_is_not_publication_date(self, read):
        read.side_effect=[page(2,[raw()]),page(2,[raw('456')])]
        jobs=fetch_board('tencent','tencent')
        self.assertEqual(len(jobs),2)
        self.assertIsNone(jobs[0]['posted_at'])
        self.assertIn('列表摘要',jobs[0]['text'])
        self.assertEqual(jobs[0]['location'],'中国; 北京')
        self.assertIn('pageIndex=2',read.call_args.args[0])
        self.assertEqual(parse_board_link('https://careers.tencent.com/zh-cn/search.html'),('tencent','tencent'))

    @patch('job_search._fetch_json')
    def test_selected_post_requires_complete_detail_and_matching_id(self, read):
        read.return_value={'Code':200,'Data':{**raw(),'Requirement':'熟悉 Python，具备后端项目经验'}}
        selected=fetch_selected('tencent','123','tencent')
        self.assertIn('岗位要求\n熟悉 Python',selected['jd']['text'])
        self.assertIn('ByPostId?postId=123',read.call_args.args[0])
        for data in ({**raw(),'Requirement':''},{**raw(),'PostId':'999','Requirement':'Python'},
                     {**raw(),'IsValid':False,'Requirement':'Python'}):
            read.return_value={'Code':200,'Data':data}
            with self.assertRaises(SearchError):fetch_selected('tencent','123','tencent')

    @patch('job_search._fetch_json')
    def test_partial_duplicate_changed_or_invalid_response_preserves_cache(self, read):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'listings.db'
            read.return_value=page(1,[raw()])
            add_source(db,'tencent','tencent')
            for responses in ([page(2,[raw()]),page(2,[])],
                              [page(2,[raw()]),page(2,[raw()])],
                              [page(2,[raw()]),page(3,[raw('456')])],
                              [{'Code':500,'Data':{}}]):
                read.side_effect=copy.deepcopy(responses)
                with self.assertRaises(ListingsError):refresh_source(db,'tencent','tencent')
                self.assertEqual(list_sources(db)[0]['open_count'],1)
