import tempfile
import unittest
from pathlib import Path
from listings import location_regions, ranked_listings, add_source, ListingsError
from test_listings import posting, FakeBoards

class RegionTests(unittest.TestCase):
    def test_place_evidence_only(self):
        for place in ('China - Shanghai', '北京; Remote', 'Shanghai; Beijing; Shenzhen; Remote'):
            self.assertEqual(location_regions(place), {'cn'})
        for place in ('Hong Kong, China', 'Taipei, Taiwan', 'London', 'San Francisco, USA'):
            self.assertEqual(location_regions(place), {'other'})
        for place in (None, 'Remote', 'Global', 'A location we do not recognize'):
            self.assertEqual(location_regions(place), {'unknown'})
        self.assertEqual(location_regions('Shanghai; London'), {'cn', 'other'})

    def test_region_filters_before_grouping_and_keeps_unknown_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            db=Path(folder)/'listings.db'; facts=Path(folder)/'facts.db'
            add_source(db,'lever','example',fetch=FakeBoards([
                posting('1','Engineer','Python',location='Shanghai'),
                posting('2','Engineer','Python',location='London'),
                posting('3','中文岗位','Python',location=None),
            ]))
            cn=ranked_listings(db,facts,region='cn')
            self.assertEqual(cn['total'],1)
            self.assertEqual([p['location'] for p in cn['listings'][0]['postings']],['Shanghai'])
            self.assertIn('last_seen_at',cn['listings'][0]['postings'][0])
            self.assertEqual(ranked_listings(db,facts,region='unknown')['listings'][0]['title'],'中文岗位')
            with self.assertRaises(ListingsError):ranked_listings(db,facts,region='invalid')
