import copy,json,unittest
from pathlib import Path
from wafer_fusion import process

class WaferTests(unittest.TestCase):
    def setUp(self):self.d=json.loads(Path('fixtures/input.json').read_text())
    def test_registration_and_pick_mask(self):
        r=process(self.d);self.assertEqual(r['pick_coordinates'],[[0,0]])
        self.assertEqual([d['status'] for d in r['dies']],['pass','fail','incomplete','incomplete'])
        self.assertEqual(r['dies'][0]['observations'][1]['source_at'],[0,1])
    def test_fail_dominates_missing(self):
        self.d['runs'][1]['records']=[];r=process(self.d)
        self.assertEqual(r['dies'][1]['status'],'fail');self.assertEqual(r['pick_coordinates'],[])
    def test_skip_is_not_missing(self):
        r=process(self.d);self.assertEqual(r['dies'][3]['blocked_by'],[{'run':'probe','reason':'skip'}])
        self.assertEqual(r['dies'][2]['blocked_by'],[{'run':'optical','reason':'missing'}])
    def test_order_deterministic(self):
        r=process(self.d);self.d['sites'].reverse()
        for run in self.d['runs']:run['records'].reverse()
        self.assertEqual(process(self.d),r)
    def test_bad_transform_and_outside(self):
        for mutate in (lambda d:d['runs'][0].update(matrix=[[1,0],[1,0]]),lambda d:d['runs'][0]['records'][0].update(at=[9,9])):
            d=copy.deepcopy(self.d);mutate(d)
            with self.assertRaises(ValueError):process(d)
    def test_duplicate_and_unknown_bin(self):
        for mutate in (lambda d:d['sites'].append([0,0]),lambda d:d['runs'][0]['records'].append(d['runs'][0]['records'][0]),lambda d:d['runs'][0]['records'][0].update(bin='NO')):
            d=copy.deepcopy(self.d);mutate(d)
            with self.assertRaises(ValueError):process(d)
