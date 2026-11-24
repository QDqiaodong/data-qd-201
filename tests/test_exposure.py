"""Acceptance tests for compile_exposure_map; all data is synthetic.

Run with the package on the path, e.g.::

    PYTHONPATH=src python -I -m unittest tests.test_exposure -v
"""
import copy
import json
import unittest

from wafer_fusion import compile_exposure_map, process_calibrated

IDENTITY = [[1, 0], [0, 1]]
ZERO = [0, 0]


def payload(wafer='W', radius=5, width=4, height=2, origin=(0, 0),
            pitch=(2, 2), layers=None):
    if layers is None:
        layers = [{'id': 'L1', 'shots': [
            {'id': 's-all', 'bounds': [-100, -100, 100, 100],
             'state': 'ready'}]}]
    return {'wafer': wafer, 'radius': radius,
            'die': {'width': width, 'height': height},
            'lattice': {'origin': list(origin), 'pitch': list(pitch)},
            'layers': copy.deepcopy(layers)}


def by_at(result):
    return {tuple(die['at']): die for die in result['dies']}


def naive_reference(data):
    """Independent brute-force reference using plain integer loops."""
    r = data['radius']
    w, h = data['die']['width'], data['die']['height']
    ox, oy = data['lattice']['origin']
    px, py = data['lattice']['pitch']

    def ceil_div(a, b):
        return -((-a) // b)

    points = []
    for i in range(ceil_div(-r - ox, px), (r - ox) // px + 1):
        x = ox + i * px
        for j in range(ceil_div(-r - oy, py), (r - oy) // py + 1):
            y = oy + j * py
            if x * x + y * y <= r * r:
                points.append((x, y))
    points = sorted(set(points), key=lambda p: (p[1], p[0]))
    layers = sorted(data['layers'], key=lambda layer: layer['id'])
    dies = []
    sites = []
    for x, y in points:
        left, right = 2 * x - w, 2 * x + w
        bottom, top = 2 * y - h, 2 * y + h
        edge = any(cx * cx + cy * cy > 4 * r * r
                   for cx in (left, right) for cy in (bottom, top))
        exposures = []
        reasons = ['wafer_edge'] if edge else []
        probeable = not edge
        for layer in layers:
            ids = []
            for shot in layer['shots']:
                mnx, mny, mxx, mxy = shot['bounds']
                if (2 * mnx <= left and right <= 2 * mxx
                        and 2 * mny <= bottom and top <= 2 * mxy):
                    ids.append(shot['id'])
            ids.sort()
            exposures.append({'layer': layer['id'], 'shot_ids': ids})
            if not ids:
                reasons.append({'layer': layer['id'], 'reason': 'uncovered'})
                probeable = False
            elif len(ids) > 1:
                reasons.append({'layer': layer['id'], 'reason': 'ambiguous'})
                probeable = False
            else:
                shot = next(s for s in layer['shots'] if s['id'] == ids[0])
                if shot['state'] == 'hold':
                    reasons.append({'layer': layer['id'], 'reason': 'held'})
                    probeable = False
        dies.append({'at': [x, y],
                     'status': 'probeable' if probeable else 'excluded',
                     'exposures': exposures, 'reasons': reasons})
        if probeable:
            sites.append([x, y])
    return {'schema': 'wafer-exposure-map-v1', 'wafer': data['wafer'],
            'sites': sites, 'dies': dies}


class WaferGeometryTests(unittest.TestCase):
    """Acceptance group 1: circle membership, edge truncation, exact halves."""

    def test_radius5_grid21_candidates_and_edge_exclusion(self):
        result = compile_exposure_map(payload())
        self.assertEqual(len(result['dies']), 21)
        got = by_at(result)
        # Center (4,0) is in-circle (16 <= 25) but its 4x2 die crosses the
        # circle: corner (6,1) -> 37 > 25 in doubled coordinates.
        self.assertEqual(got[(4, 0)]['status'], 'excluded')
        self.assertEqual(got[(4, 0)]['reasons'], ['wafer_edge'])
        self.assertNotIn([4, 0], result['sites'])
        # The origin die (corners at +/-2, +/-1) fits completely.
        self.assertEqual(got[(0, 0)]['status'], 'probeable')
        self.assertEqual(got[(0, 0)]['reasons'], [])
        self.assertEqual(result['sites'], [
            die['at'] for die in result['dies']
            if die['status'] == 'probeable'])
        # y-then-x ordering of both dies and sites.
        self.assertEqual([d['at'] for d in result['dies']],
                         sorted((d['at'] for d in result['dies']),
                                key=lambda p: (p[1], p[0])))
        self.assertEqual(result['sites'],
                         sorted(result['sites'], key=lambda p: (p[1], p[0])))

    def test_corner_exactly_on_circle_is_inside(self):
        # 6x8 die at the origin: corners (3,4) lie exactly on radius 5.
        result = compile_exposure_map(payload(radius=5, width=6, height=8,
                                              pitch=(2, 2)))
        got = by_at(result)
        self.assertEqual(got[(0, 0)]['status'], 'probeable')
        # A die one grid step out crosses the same circle.
        self.assertEqual(got[(2, 0)]['status'], 'excluded')
        self.assertEqual(got[(2, 0)]['reasons'], ['wafer_edge'])

    def test_odd_dimensions_use_exact_half_units(self):
        # 3x1 die on a unit-pitch grid: half-width 1.5, half-height 0.5 in
        # doubled units, so no floating-point edge decision is possible.
        result = compile_exposure_map(payload(radius=4, width=3, height=1,
                                              pitch=(1, 1)))
        got = by_at(result)
        # Center (2,0): far corner (3.5,0.5), 12.5 <= 16 -> fully inside.
        self.assertEqual(got[(2, 0)]['status'], 'probeable')
        # Center (3,0): far corner (4.5,0.5), 20.5 > 16 -> edge excluded.
        self.assertEqual(got[(3, 0)]['status'], 'excluded')
        self.assertEqual(got[(3, 0)]['reasons'], ['wafer_edge'])
        # The center itself is in-circle (9 <= 16), so it must stay in dies.
        self.assertIn((3, 0), got)

    def test_edge_dies_keep_full_exposure_attribution(self):
        layers = [{'id': 'L1', 'shots': [
            {'id': 's-all', 'bounds': [-100, -100, 100, 100],
             'state': 'ready'}]}]
        result = compile_exposure_map(payload(layers=layers))
        edge_die = by_at(result)[(4, 0)]
        self.assertEqual(edge_die['exposures'],
                         [{'layer': 'L1', 'shot_ids': ['s-all']}])

    def test_matches_naive_reference_on_mixed_fixture(self):
        layers = [
            {'id': 'L1', 'shots': [
                {'id': 'a', 'bounds': [-6, -6, 1, 6], 'state': 'ready'},
                {'id': 'b', 'bounds': [0, -6, 6, 6], 'state': 'hold'}]},
            {'id': 'L2', 'shots': [
                {'id': 'c', 'bounds': [-6, -6, 6, 0], 'state': 'ready'},
                {'id': 'd', 'bounds': [-2, -2, 2, 2], 'state': 'ready'}]},
        ]
        data = payload(radius=6, width=3, height=3, pitch=(2, 2),
                       layers=layers)
        expected = naive_reference(copy.deepcopy(data))
        self.assertEqual(compile_exposure_map(data), expected)


class LayerExposureTests(unittest.TestCase):
    """Acceptance group 2: uncovered, ambiguous, held and ordering."""

    def _fixture(self):
        layers = [
            {'id': 'L1', 'shots': [
                {'id': 's1', 'bounds': [-10, -10, 10, 10], 'state': 'ready'}]},
            {'id': 'L2', 'shots': [
                # Covers centers <= -4 (doubled right edge <= 0) and centers
                # >= 2 respectively; the -2 and 0 columns stay uncovered,
                # while the (0,0) ambiguity case below uses a separate map.
                {'id': 's2', 'bounds': [-10, -10, -2, 10], 'state': 'ready'},
                {'id': 's3', 'bounds': [0, -10, 10, 10], 'state': 'ready'}]},
        ]
        return payload(layers=layers)

    def _ambiguity_fixture(self):
        layers = [
            {'id': 'L1', 'shots': [
                {'id': 's1', 'bounds': [-10, -10, 10, 10], 'state': 'ready'}]},
            {'id': 'L2', 'shots': [
                {'id': 's2', 'bounds': [-10, -10, 2, 10], 'state': 'ready'},
                {'id': 's3', 'bounds': [-2, -10, 10, 10], 'state': 'ready'}]},
        ]
        return payload(layers=layers)

    def test_uncovered_and_edge_attribution(self):
        result = compile_exposure_map(self._fixture())
        got = by_at(result)
        # Interior (-2,0): L1 covers, neither L2 shot reaches it.
        center = got[(-2, 0)]
        self.assertEqual(center['status'], 'excluded')
        self.assertEqual(center['reasons'],
                         [{'layer': 'L2', 'reason': 'uncovered'}])
        self.assertEqual(center['exposures'], [
            {'layer': 'L1', 'shot_ids': ['s1']},
            {'layer': 'L2', 'shot_ids': []}])
        self.assertNotIn([-2, 0], result['sites'])
        # Edge die (-4,0) keeps its wafer_edge reason plus the unique L2
        # cover; the exposure relation is reported even when incomplete.
        edge = got[(-4, 0)]
        self.assertEqual(edge['reasons'], ['wafer_edge'])
        self.assertEqual(edge['exposures'], [
            {'layer': 'L1', 'shot_ids': ['s1']},
            {'layer': 'L2', 'shot_ids': ['s2']}])

    def test_ambiguous_lists_every_covering_shot(self):
        result = compile_exposure_map(self._ambiguity_fixture())
        center = by_at(result)[(0, 0)]
        self.assertEqual(center['status'], 'excluded')
        self.assertEqual(center['reasons'],
                         [{'layer': 'L2', 'reason': 'ambiguous'}])
        self.assertEqual(center['exposures'], [
            {'layer': 'L1', 'shot_ids': ['s1']},
            {'layer': 'L2', 'shot_ids': ['s2', 's3']}])
        self.assertNotIn([0, 0], result['sites'])

    def test_unique_ready_covers_are_probeable(self):
        result = compile_exposure_map(payload())
        got = by_at(result)
        self.assertEqual(got[(-2, 0)]['status'], 'probeable')
        self.assertEqual(got[(0, 0)]['status'], 'probeable')
        self.assertTrue(result['sites'])

    def test_reordering_layers_and_shots_is_invariant(self):
        data = self._ambiguity_fixture()
        expected = compile_exposure_map(data)
        shuffled = copy.deepcopy(data)
        shuffled['layers'].reverse()
        for layer in shuffled['layers']:
            layer['shots'].reverse()
        self.assertEqual(compile_exposure_map(shuffled), expected)

    def test_hold_only_excludes_covered_candidates(self):
        # Baseline: every interior die has one unique ready cover per layer.
        data = payload()
        ready = compile_exposure_map(data)
        held_input = copy.deepcopy(data)
        held_input['layers'][0]['shots'][0]['state'] = 'hold'
        held = compile_exposure_map(held_input)
        self.assertEqual([d['at'] for d in held['dies']],
                         [d['at'] for d in ready['dies']])
        for die_ready, die_held in zip(ready['dies'], held['dies']):
            self.assertEqual(die_held['at'], die_ready['at'])
            self.assertEqual(die_held['exposures'], die_ready['exposures'])
        held_map = by_at(held)
        ready_map = by_at(ready)
        # Attribution is unchanged; the held shot id is still recorded.
        self.assertEqual(held_map[(0, 0)]['exposures'],
                         ready_map[(0, 0)]['exposures'])
        # Interior dies uniquely covered by the held shot are excluded.
        self.assertEqual(held_map[(0, 0)]['reasons'],
                         [{'layer': 'L1', 'reason': 'held'}])
        self.assertEqual(held_map[(-2, 0)]['status'], 'excluded')
        # Edge dies keep wafer_edge; the held reason is added alongside.
        self.assertEqual(held_map[(4, 0)]['reasons'],
                         ['wafer_edge',
                          {'layer': 'L1', 'reason': 'held'}])
        self.assertEqual(held['sites'], [])
        # The ready fixture still probes the interior dies.
        self.assertTrue(ready['sites'])

    def test_hold_does_not_poison_dies_outside_its_shot(self):
        layers = [
            {'id': 'L1', 'shots': [
                {'id': 'west', 'bounds': [-10, -10, 0, 10], 'state': 'hold'},
                {'id': 'east', 'bounds': [0, -10, 10, 10], 'state': 'ready'}]}]
        result = compile_exposure_map(payload(layers=layers))
        got = by_at(result)
        # (2,0) is covered only by the ready east shot; (-2,0) only by west.
        self.assertEqual(got[(2, 0)]['status'], 'probeable')
        self.assertEqual(got[(2, 0)]['exposures'],
                         [{'layer': 'L1', 'shot_ids': ['east']}])
        self.assertEqual(got[(-2, 0)]['status'], 'excluded')
        self.assertEqual(got[(-2, 0)]['reasons'],
                         [{'layer': 'L1', 'reason': 'held'}])


class CombinedReasonTests(unittest.TestCase):
    """Acceptance group 3: every applicable reason survives, ordered."""

    def test_edge_uncovered_and_held_together(self):
        layers = [
            {'id': 'L1', 'shots': [
                {'id': 'h1', 'bounds': [-10, -10, 10, 10], 'state': 'hold'}]},
            {'id': 'L2', 'shots': [
                {'id': 'far', 'bounds': [-20, -20, -10, -10],
                 'state': 'ready'}]},
        ]
        result = compile_exposure_map(payload(layers=layers))
        die = by_at(result)[(4, 0)]
        self.assertEqual(die['status'], 'excluded')
        self.assertEqual(die['reasons'], [
            'wafer_edge',
            {'layer': 'L1', 'reason': 'held'},
            {'layer': 'L2', 'reason': 'uncovered'}])
        self.assertEqual(die['exposures'], [
            {'layer': 'L1', 'shot_ids': ['h1']},
            {'layer': 'L2', 'shot_ids': []}])
        self.assertEqual(result['sites'], [])


class ValidationTests(unittest.TestCase):
    """Illegal structures, types, ids, ranges and duplicates raise ValueError."""

    def setUp(self):
        self.base = payload()

    def assertRejected(self, mutate):
        data = copy.deepcopy(self.base)
        mutate(data)
        snapshot = copy.deepcopy(data)
        with self.assertRaises(ValueError):
            compile_exposure_map(data)
        self.assertEqual(data, snapshot, 'input must not be mutated')

    def test_top_level_shape(self):
        self.assertRejected(lambda d: d.pop('radius'))
        self.assertRejected(lambda d: d.update(extra=1))
        self.assertRejected(lambda d: d.update(wafer='bad id!'))
        self.assertRejected(lambda d: d.__setitem__('wafer', 7))

    def test_radius_and_die_ranges(self):
        self.assertRejected(lambda d: d.__setitem__('radius', 0))
        self.assertRejected(lambda d: d.__setitem__('radius', 10001))
        self.assertRejected(lambda d: d.__setitem__('radius', True))
        self.assertRejected(lambda d: d['die'].__setitem__('width', 0))
        self.assertRejected(lambda d: d['die'].__setitem__('height', 20001))
        self.assertRejected(lambda d: d['die'].__setitem__('width', False))
        self.assertRejected(lambda d: d['die'].update(extra=1))

    def test_lattice_rules(self):
        self.assertRejected(lambda d: d['lattice'].__setitem__('pitch', [0, 2]))
        self.assertRejected(lambda d: d['lattice'].__setitem__('pitch', [2, -1]))
        self.assertRejected(
            lambda d: d['lattice'].__setitem__('origin', [10001, 0]))
        self.assertRejected(
            lambda d: d['lattice'].__setitem__('origin', [1, True]))
        self.assertRejected(
            lambda d: d['lattice'].__setitem__('pitch', [2]))
        self.assertRejected(lambda d: d['lattice'].update(extra=1))

    def test_layer_and_shot_shape(self):
        self.assertRejected(lambda d: d['layers'].append(
            {'id': 'L1', 'shots': [
                {'id': 'x', 'bounds': [0, 0, 1, 1], 'state': 'ready'}]}))
        self.assertRejected(lambda d: d['layers'][0].update(extra=1))
        self.assertRejected(lambda d: d['layers'][0]['shots'][0].pop('id'))
        self.assertRejected(
            lambda d: d['layers'][0]['shots'][0].update(extra=1))
        self.assertRejected(
            lambda d: d['layers'][0]['shots'][0].__setitem__('state', 'done'))
        self.assertRejected(
            lambda d: d.__setitem__('layers', []))
        self.assertRejected(
            lambda d: d['layers'][0].__setitem__('id', 'bad/layer'))

    def test_bounds_rules(self):
        shot = lambda bounds: {'id': 's', 'bounds': bounds, 'state': 'ready'}
        self.assertRejected(lambda d: d['layers'][0].__setitem__(
            'shots', [shot([1, 0, 1, 2])]))       # zero width
        self.assertRejected(lambda d: d['layers'][0].__setitem__(
            'shots', [shot([1, 2, 1, 0])]))       # reversed
        self.assertRejected(lambda d: d['layers'][0].__setitem__(
            'shots', [shot([0, 0, 10001, 1])]))   # out of bound
        self.assertRejected(lambda d: d['layers'][0].__setitem__(
            'shots', [shot([0, 0, True, 1])]))    # bool as integer
        self.assertRejected(lambda d: d['layers'][0].__setitem__(
            'shots', [shot([0, 0, 1])]))          # wrong length

    def test_duplicate_ids_and_limits(self):
        dup = {'id': 'L1', 'shots': [
            {'id': 's', 'bounds': [0, 0, 1, 1], 'state': 'ready'},
            {'id': 's', 'bounds': [0, 0, 2, 2], 'state': 'ready'}]}
        self.assertRejected(lambda d: d.__setitem__('layers', [dup]))
        many_layers = [{'id': f'L{i}', 'shots': [
            {'id': 's', 'bounds': [0, 0, 1, 1], 'state': 'ready'}]}
            for i in range(9)]
        self.assertRejected(lambda d: d.__setitem__('layers', many_layers))
        many_shots = {'id': 'L1', 'shots': [
            {'id': f's{i}', 'bounds': [i, 0, i + 1, 1], 'state': 'ready'}
            for i in range(129)]}
        self.assertRejected(lambda d: d.__setitem__('layers', [many_shots]))
        self.assertRejected(lambda d: d['layers'][0].__setitem__('shots', []))

    def test_candidate_limit_exceeded(self):
        # Radius 10000 on a unit grid produces far more than 100000 centers.
        data = payload(radius=10000, pitch=(1, 1))
        with self.assertRaises(ValueError):
            compile_exposure_map(data)

    def test_probeable_sites_limit_exceeded(self):
        # One giant ready shot makes every in-circle die probeable; the
        # 20000-site calibration contract must still be enforced.
        layers = [{'id': 'L1', 'shots': [
            {'id': 'big', 'bounds': [-10000, -10000, 10000, 10000],
             'state': 'ready'}]}]
        data = payload(radius=3000, width=4, height=4, pitch=(20, 20),
                       layers=layers)
        # Sanity: the same grid comfortably exceeds the in-circle cap only
        # for probeable sites here (about 70k candidates, >20k probeable).
        with self.assertRaises(ValueError):
            compile_exposure_map(data)


class ContractAndIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.data = payload()

    def test_json_serializable_and_input_untouched(self):
        snapshot = copy.deepcopy(self.data)
        result = compile_exposure_map(self.data)
        self.assertEqual(self.data, snapshot)
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        self.assertEqual(set(result), {'schema', 'wafer', 'sites', 'dies'})
        self.assertEqual(result['schema'], 'wafer-exposure-map-v1')
        for die in result['dies']:
            self.assertEqual(set(die), {'at', 'status', 'exposures',
                                        'reasons'})
            self.assertIn(die['status'], ('probeable', 'excluded'))

    def test_sites_feed_process_calibrated_with_same_order(self):
        result = compile_exposure_map(self.data)
        self.assertTrue(result['sites'])
        calibrated = {
            'wafer': result['wafer'],
            'sites': copy.deepcopy(result['sites']),
            'runs': [{
                'id': 'R0',
                'bins': {'P': 'pass'},
                'records': [{'at': list(site), 'bin': 'P'}
                            for site in result['sites']],
                'candidates': [{'id': 'id', 'matrix': IDENTITY,
                                'offset': ZERO}]}],
            'links': []}
        fused = process_calibrated(calibrated)
        self.assertEqual([die['at'] for die in fused['dies']],
                         result['sites'])
        self.assertEqual(fused['pick_coordinates'], result['sites'])

    def test_reordering_invariant_and_no_silent_drops(self):
        shuffled = copy.deepcopy(self.data)
        shuffled['layers'].reverse()
        for layer in shuffled['layers']:
            layer['shots'].reverse()
        result = compile_exposure_map(self.data)
        self.assertEqual(compile_exposure_map(shuffled), result)
        # Every in-circle center appears exactly once, including excluded.
        centers = [tuple(d['at']) for d in result['dies']]
        self.assertEqual(len(centers), len(set(centers)))
        self.assertEqual(
            sorted(centers, key=lambda p: (p[1], p[0])), centers)


if __name__ == '__main__':
    unittest.main()
