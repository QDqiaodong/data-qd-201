"""Acceptance tests for process_calibrated; all data is synthetic."""
import copy
import json
import unittest

from wafer_fusion import process, process_calibrated

IDENTITY = [[1, 0], [0, 1]]
MIRROR_X = [[-1, 0], [0, 1]]
ZERO = [0, 0]
# Every signed orthogonal 2x2 matrix with zero offset.
EIGHT = [
    ('r0', [[1, 0], [0, 1]]),
    ('r90', [[0, -1], [1, 0]]),
    ('r180', [[-1, 0], [0, -1]]),
    ('r270', [[0, 1], [-1, 0]]),
    ('mx', [[-1, 0], [0, 1]]),
    ('my', [[1, 0], [0, -1]]),
    ('d1', [[0, 1], [1, 0]]),
    ('d2', [[0, -1], [-1, 0]]),
]


def two_candidate_run(run_id, records):
    """Identity and x -> 1-x candidates over [0,1] device coordinates."""
    return {'id': run_id,
            'bins': {'P': 'pass', 'F': 'fail', 'S': 'skip'},
            'records': records,
            'candidates': [
                {'id': 'id', 'matrix': IDENTITY, 'offset': ZERO},
                {'id': 'mx', 'matrix': MIRROR_X, 'offset': [1, 0]}]}


def linked_two_run(records_a, records_b, left_at, right_at, sites=((0, 0), (1, 0))):
    return {'wafer': 'W',
            'sites': [list(p) for p in sites],
            'runs': [two_candidate_run('A', records_a),
                     two_candidate_run('B', records_b)],
            'links': [{'left': {'run': 'A', 'at': list(left_at)},
                       'right': {'run': 'B', 'at': list(right_at)}}]}


def statuses(result):
    return [die['possible_statuses'] for die in result['dies']]


class TwoRunLinkTests(unittest.TestCase):
    """Acceptance group 1: two dies, two stations, same/opposite direction."""

    def test_same_direction_link_forces_fail(self):
        data = linked_two_run(
            [{'at': [0, 0], 'bin': 'P'}, {'at': [1, 0], 'bin': 'F'}],
            [{'at': [0, 0], 'bin': 'F'}, {'at': [1, 0], 'bin': 'P'}],
            (0, 0), (0, 0))
        result = process_calibrated(data)
        self.assertEqual(result['pick_coordinates'], [])
        self.assertEqual(statuses(result), [['fail'], ['fail']])
        self.assertEqual(result['supported_candidates'],
                         [{'run': 'A', 'candidates': ['id', 'mx']},
                          {'run': 'B', 'candidates': ['id', 'mx']}])

    def test_reversed_link_allows_pass_or_fail(self):
        data = linked_two_run(
            [{'at': [0, 0], 'bin': 'P'}, {'at': [1, 0], 'bin': 'F'}],
            [{'at': [0, 0], 'bin': 'F'}, {'at': [1, 0], 'bin': 'P'}],
            (0, 0), (1, 0))
        result = process_calibrated(data)
        self.assertEqual(result['pick_coordinates'], [])
        self.assertEqual(statuses(result),
                         [['pass', 'fail'], ['pass', 'fail']])

    def test_skip_coincident_with_fail_is_never_incomplete(self):
        data = linked_two_run(
            [{'at': [0, 0], 'bin': 'S'}, {'at': [1, 0], 'bin': 'P'}],
            [{'at': [0, 0], 'bin': 'F'}, {'at': [1, 0], 'bin': 'P'}],
            (0, 0), (0, 0))
        result = process_calibrated(data)
        self.assertEqual(result['pick_coordinates'], [])
        for die in result['dies']:
            self.assertEqual(set(die['possible_statuses']), {'pass', 'fail'})
            self.assertNotIn('incomplete', die['possible_statuses'])

    def test_pure_skip_or_missing_is_incomplete(self):
        # No fail bins anywhere: skips and missing yield incomplete,
        # never fail; pass remains possible when a calibration witnesses it.
        data = linked_two_run(
            [{'at': [0, 0], 'bin': 'S'}, {'at': [1, 0], 'bin': 'P'}],
            [{'at': [1, 0], 'bin': 'P'}],
            (0, 0), (0, 0))
        result = process_calibrated(data)
        got = {tuple(die['at']): die['possible_statuses'] for die in result['dies']}
        # identity: die(0,0) sees skip+missing; mirror: both stations pass.
        self.assertEqual(got[(0, 0)], ['pass', 'incomplete'])
        self.assertEqual(got[(1, 0)], ['pass', 'incomplete'])
        self.assertEqual(result['pick_coordinates'], [])
        self.assertFalse(any('fail' in die['possible_statuses']
                             for die in result['dies']))

        # Sparser records: every die incomplete under every calibration.
        sparse = linked_two_run(
            [{'at': [0, 0], 'bin': 'S'}],
            [{'at': [0, 0], 'bin': 'P'}],
            (0, 0), (0, 0))
        result = process_calibrated(sparse)
        self.assertTrue(all(die['possible_statuses'] == ['incomplete']
                            for die in result['dies']))
        self.assertEqual(result['pick_coordinates'], [])


class ThreeStationWindowTests(unittest.TestCase):
    """Acceptance group 2: A/B/C pairwise solvable, jointly unsolvable."""

    @staticmethod
    def _payload(links):
        def run(run_id):
            return two_candidate_run(
                run_id, [{'at': [0, 0], 'bin': 'P'},
                         {'at': [1, 0], 'bin': 'P'}])
        return {'wafer': 'W',
                'sites': [[0, 0], [1, 0]],
                'runs': [run('A'), run('B'), run('C')],
                'links': links}

    @staticmethod
    def _link(left, left_at, right, right_at):
        return {'left': {'run': left, 'at': list(left_at)},
                'right': {'run': right, 'at': list(right_at)}}

    def test_each_pair_alone_is_feasible(self):
        # Same-direction pairs A/B and B/C, opposite A/C.
        ab = self._payload([self._link('A', (0, 0), 'B', (0, 0))])
        bc = self._payload([self._link('B', (0, 0), 'C', (0, 0))])
        ac = self._payload([self._link('A', (0, 0), 'C', (1, 0))])
        for payload in (ab, bc, ac):
            result = process_calibrated(payload)
            self.assertEqual(result['pick_coordinates'], [[0, 0], [1, 0]])

    def test_jointly_contradictory_links_raise(self):
        payload = self._payload([
            self._link('A', (0, 0), 'B', (0, 0)),
            self._link('B', (0, 0), 'C', (0, 0)),
            self._link('A', (0, 0), 'C', (1, 0))])
        with self.assertRaises(ValueError):
            process_calibrated(payload)


class CandidateExclusionTests(unittest.TestCase):
    """Out-of-sites candidates drop out but do not poison other candidates."""

    @staticmethod
    def _payload():
        # Asymmetric sites: mirror x -> -x sends the (2,0) record off-sites.
        sites = [[0, 0], [1, 0], [2, 0]]
        run_a = {'id': 'A',
                 'bins': {'P': 'pass', 'F': 'fail'},
                 'records': [{'at': [0, 0], 'bin': 'P'},
                             {'at': [2, 0], 'bin': 'P'}],
                 'candidates': [
                     {'id': 'a_id', 'matrix': IDENTITY, 'offset': ZERO},
                     {'id': 'a_mx', 'matrix': [[-1, 0], [0, 1]], 'offset': ZERO}]}
        run_b = {'id': 'B',
                 'bins': {'P': 'pass', 'F': 'fail'},
                 'records': [{'at': [0, 0], 'bin': 'F'},
                             {'at': [1, 0], 'bin': 'P'},
                             {'at': [2, 0], 'bin': 'P'}],
                 'candidates': [
                     {'id': 'b_id', 'matrix': IDENTITY, 'offset': ZERO},
                     {'id': 'b_mx', 'matrix': MIRROR_X, 'offset': [2, 0]}]}
        return {'wafer': 'W', 'sites': sites,
                'runs': [run_a, run_b],
                'links': [{'left': {'run': 'A', 'at': [0, 0]},
                           'right': {'run': 'B', 'at': [0, 0]}}]}

    def test_excluded_candidate_shapes_partner_support_and_pick(self):
        result = process_calibrated(self._payload())
        supported = {item['run']: item['candidates']
                     for item in result['supported_candidates']}
        self.assertEqual(supported['A'], ['a_id'])
        self.assertEqual(supported['B'], ['b_id'])
        # Identity-only calibration: B marks canonical (0,0) fail.
        got = {tuple(die['at']): die['possible_statuses'] for die in result['dies']}
        self.assertEqual(got[(0, 0)], ['fail'])
        self.assertEqual(got[(1, 0)], ['incomplete'])
        self.assertEqual(got[(2, 0)], ['pass'])
        self.assertEqual(result['pick_coordinates'], [[2, 0]])

    def test_excluded_without_links_is_not_an_error(self):
        payload = self._payload()
        payload['links'] = []
        result = process_calibrated(payload)
        supported = {item['run']: item['candidates']
                     for item in result['supported_candidates']}
        self.assertEqual(supported['A'], ['a_id'])
        self.assertEqual(supported['B'], ['b_id', 'b_mx'])

    def test_all_candidates_excluded_raises(self):
        payload = self._payload()
        payload['runs'][0]['candidates'] = [
            {'id': 'gone', 'matrix': [[-1, 0], [0, 1]], 'offset': ZERO}]
        with self.assertRaises(ValueError):
            process_calibrated(payload)


class UniqueTransformParityTests(unittest.TestCase):
    """A single surviving candidate reproduces the legacy process verdict."""

    def test_matches_legacy_process(self):
        import json
        from pathlib import Path
        legacy = json.loads(Path('fixtures/input.json').read_text())
        legacy_result = process(copy.deepcopy(legacy))
        calibrated = {'wafer': legacy['wafer'], 'sites': legacy['sites'],
                      'links': [], 'runs': []}
        for run in legacy['runs']:
            calibrated['runs'].append({
                'id': run['id'], 'bins': run['bins'],
                'records': copy.deepcopy(run['records']),
                'candidates': [{'id': 'only',
                                'matrix': copy.deepcopy(run['matrix']),
                                'offset': copy.deepcopy(run['offset'])}]})
        result = process_calibrated(calibrated)
        self.assertEqual(result['pick_coordinates'], legacy_result['pick_coordinates'])
        self.assertEqual([die['at'] for die in result['dies']],
                         [die['at'] for die in legacy_result['dies']])
        for new_die, old_die in zip(result['dies'], legacy_result['dies']):
            self.assertEqual(new_die['possible_statuses'], [old_die['status']])
            self.assertEqual(new_die['die_id'], old_die['die_id'])

    def test_multiple_solutions_still_allow_safe_picks(self):
        # No links, no fails: both calibrations stay feasible; die (1,0)
        # is pass under both and must still be released.
        data = linked_two_run(
            [{'at': [0, 0], 'bin': 'P'}, {'at': [1, 0], 'bin': 'P'}],
            [{'at': [0, 0], 'bin': 'P'}, {'at': [1, 0], 'bin': 'P'}],
            (0, 0), (0, 0))
        data['links'] = []
        result = process_calibrated(data)
        self.assertTrue(all(die['possible_statuses'] == ['pass']
                            for die in result['dies']))
        self.assertEqual(result['pick_coordinates'], [[0, 0], [1, 0]])
        for item in result['supported_candidates']:
            self.assertEqual(item['candidates'], ['id', 'mx'])


class EightTransformSmallTests(unittest.TestCase):
    """Full-result checks over all eight orthogonal transforms."""

    @staticmethod
    def _payload(links, offsets=None):
        # Non-square sites exclude nothing here: the 7x7 device square is
        # mapped into the wide site rectangle by every zero-offset transform.
        sites = [[x, y] for y in range(-8, 9) for x in range(-16, 17)]
        records = [{'at': [x, y], 'bin': 'P'}
                   for y in range(-3, 4) for x in range(-3, 4)]
        runs = []
        for i in range(3):
            run_candidates = []
            for cid, matrix in EIGHT:
                offset = ZERO if offsets is None else offsets[i][cid]
                run_candidates.append(
                    {'id': cid, 'matrix': matrix, 'offset': list(offset)})
            runs.append({'id': f'R{i}', 'bins': {'P': 'pass'},
                         'records': copy.deepcopy(records),
                         'candidates': run_candidates})
        return {'wafer': 'W8', 'sites': sites, 'runs': runs, 'links': links}

    def test_eight_supported_per_run_with_full_result_check(self):
        result = process_calibrated(self._payload([]))
        for item in result['supported_candidates']:
            self.assertEqual(item['candidates'], sorted(cid for cid, _ in EIGHT))
        # The union of the eight transform images is exactly the 7x7
        # centered square; everything else is incomplete under all of them.
        got = {tuple(die['at']): die['possible_statuses'] for die in result['dies']}
        for (x, y), possible in got.items():
            if max(abs(x), abs(y)) <= 3:
                self.assertEqual(possible, ['pass'], (x, y))
            else:
                self.assertEqual(possible, ['incomplete'], (x, y))
        expected_pick = sorted(([x, y] for y in range(-3, 4) for x in range(-3, 4)),
                               key=lambda p: (p[1], p[0]))
        self.assertEqual(result['pick_coordinates'], expected_pick)

    def test_links_constrain_relative_transforms_and_gauge_freedom(self):
        # Relative pinning: R1 must be R0 rotated 180 degrees, R2 likewise.
        links = [
            {'left': {'run': 'R0', 'at': [0, 0]},
             'right': {'run': 'R1', 'at': [0, 0]}},
            {'left': {'run': 'R0', 'at': [1, 0]},
             'right': {'run': 'R1', 'at': [-1, 0]}},
            {'left': {'run': 'R0', 'at': [0, 1]},
             'right': {'run': 'R1', 'at': [0, -1]}},
            {'left': {'run': 'R1', 'at': [0, 0]},
             'right': {'run': 'R2', 'at': [0, 0]}},
            {'left': {'run': 'R1', 'at': [1, 0]},
             'right': {'run': 'R2', 'at': [-1, 0]}}]
        result = process_calibrated(self._payload(links))
        # The global gauge rotation stays free: every candidate still
        # appears in some complete calibration (8 relative solutions).
        for item in result['supported_candidates']:
            self.assertEqual(item['candidates'], sorted(cid for cid, _ in EIGHT))
        got = {tuple(die['at']): die['possible_statuses'] for die in result['dies']}
        for (x, y), possible in got.items():
            if max(abs(x), abs(y)) <= 3:
                self.assertEqual(possible, ['pass'], (x, y))
            else:
                self.assertEqual(possible, ['incomplete'], (x, y))
        # Adding an R0->R2 correspondence that demands identity-relative
        # motion contradicts the required 180-degree relative rotation:
        # zero offsets make origin correspondences vacuous, so the
        # non-origin benchmark witnesses the contradiction.
        contradictory = links + [
            {'left': {'run': 'R0', 'at': [0, 0]},
             'right': {'run': 'R2', 'at': [0, 0]}},
            {'left': {'run': 'R0', 'at': [1, 0]},
             'right': {'run': 'R2', 'at': [-1, 0]}},
            {'left': {'run': 'R0', 'at': [1, 0]},
             'right': {'run': 'R2', 'at': [1, 0]}}]
        with self.assertRaises(ValueError):
            process_calibrated(self._payload(contradictory))

    def test_distinct_offsets_pin_absolute_candidates(self):
        # Give every R1 candidate a distinct offset identifying the
        # transform; an origin link then admits exactly one candidate.
        offsets = [
            {cid: ZERO for cid, _ in EIGHT},
            {cid: (k, 0) for k, (cid, _) in enumerate(EIGHT)},
            {cid: ZERO for cid, _ in EIGHT},
        ]
        links = [
            {'left': {'run': 'R0', 'at': [0, 0]},
             'right': {'run': 'R1', 'at': [0, 0]}},
            {'left': {'run': 'R1', 'at': [0, 0]},
             'right': {'run': 'R2', 'at': [0, 0]}}]
        result = process_calibrated(self._payload(links, offsets))
        supported = {item['run']: item['candidates']
                     for item in result['supported_candidates']}
        self.assertEqual(supported['R1'], ['r0'])
        self.assertEqual(supported['R0'], sorted(cid for cid, _ in EIGHT))
        self.assertEqual(supported['R2'], sorted(cid for cid, _ in EIGHT))
        # All surviving calibrations place the recorded square on the same
        # canonical points, so the full die table stays deterministic.
        got = {tuple(die['at']): die['possible_statuses'] for die in result['dies']}
        for (x, y), possible in got.items():
            if max(abs(x), abs(y)) <= 3:
                self.assertEqual(possible, ['pass'], (x, y))
            else:
                self.assertEqual(possible, ['incomplete'], (x, y))


class ValidationTests(unittest.TestCase):
    """Illegal fields, ids, values, references and duplicates raise ValueError."""

    def setUp(self):
        self.base = linked_two_run(
            [{'at': [0, 0], 'bin': 'P'}, {'at': [1, 0], 'bin': 'F'}],
            [{'at': [0, 0], 'bin': 'F'}, {'at': [1, 0], 'bin': 'P'}],
            (0, 0), (0, 0))

    def assertRejected(self, mutate):
        data = copy.deepcopy(self.base)
        mutate(data)
        with self.assertRaises(ValueError):
            process_calibrated(data)

    def test_top_level_shape(self):
        self.assertRejected(lambda d: d.pop('links'))
        self.assertRejected(lambda d: d.update(extra=1))
        self.assertRejected(lambda d: d.update(wafer='bad id!'))

    def test_sites_limits(self):
        self.assertRejected(lambda d: d['sites'].append([0, 0]))
        self.assertRejected(lambda d: d['sites'].append([0, 10001]))
        self.assertRejected(lambda d: d['sites'].__setitem__(0, [0, True]))

    def test_run_shape(self):
        self.assertRejected(lambda d: d['runs'][0].pop('candidates'))
        self.assertRejected(lambda d: d['runs'][0].update(extra=1))
        self.assertRejected(lambda d: d['runs'][1].__setitem__('id', 'A'))

    def test_candidate_limits(self):
        self.assertRejected(
            lambda d: d['runs'][0]['candidates'].append(
                {'id': 'id', 'matrix': MIRROR_X, 'offset': ZERO}))
        self.assertRejected(
            lambda d: d['runs'][0]['candidates'].append(
                {'id': 'c3', 'matrix': IDENTITY, 'offset': ZERO}))
        self.assertRejected(
            lambda d: d['runs'][0]['candidates'].append(
                {'id': 'c3', 'matrix': [[1, 1], [0, 1]], 'offset': ZERO}))
        self.assertRejected(
            lambda d: d['runs'][0]['candidates'].__setitem__(
                0, {'id': 'id', 'matrix': [[1, 0], [0, 1]], 'offset': [0]}))
        self.assertRejected(
            lambda d: d['runs'][0].update(candidates=[]))

    def test_record_rules_unchanged(self):
        self.assertRejected(
            lambda d: d['runs'][0]['records'].append(
                {'at': [1, 0], 'bin': 'P'}))
        self.assertRejected(
            lambda d: d['runs'][0]['records'][0].update(bin='NO'))
        self.assertRejected(
            lambda d: d['runs'][0]['records'][0].__setitem__('at', [0, 10001]))

    def test_total_record_limit(self):
        # 11 runs at the 20000 per-run cap give 220000 records, over the
        # 200000 aggregate work limit; every per-run limit is respected.
        runs = []
        for i in range(11):
            runs.append({
                'id': f'R{i}', 'bins': {'P': 'pass'},
                'records': [{'at': [(j // 100) - 100, (j % 100) - 50],
                             'bin': 'P'} for j in range(20000)],
                'candidates': [{'id': 'only',
                                'matrix': IDENTITY, 'offset': ZERO}]})
        payload = {'wafer': 'W',
                   'sites': [[x, y] for y in range(-50, 50)
                             for x in range(-100, 100)],
                   'runs': runs, 'links': []}
        with self.assertRaises(ValueError):
            process_calibrated(payload)

    def test_link_rules(self):
        link = self.base['links'][0]
        self.assertRejected(lambda d: d['links'].append(copy.deepcopy(link)))
        self.assertRejected(lambda d: d['links'][0].update(extra=1))
        self.assertRejected(
            lambda d: d['links'][0]['left'].update(run='B'))
        self.assertRejected(
            lambda d: d['links'][0]['right'].update(run='A'))
        self.assertRejected(
            lambda d: d['links'][0]['left'].__setitem__('at', [0, 10001]))

    def test_link_window_and_unknown_run(self):
        payload = {'wafer': 'W',
                   'sites': [[0, 0]],
                   'runs': [two_candidate_run(chr(65 + i), []) for i in range(4)],
                   'links': []}
        payload['links'].append(
            {'left': {'run': 'A', 'at': [0, 0]},
             'right': {'run': 'D', 'at': [0, 0]}})
        with self.assertRaises(ValueError):
            process_calibrated(payload)
        payload['links'] = [
            {'left': {'run': 'A', 'at': [0, 0]},
             'right': {'run': 'Z', 'at': [0, 0]}}]
        with self.assertRaises(ValueError):
            process_calibrated(payload)

    def test_invalid_candidate_structure_rejected_even_when_unused(self):
        # The illegal candidate would be excluded anyway (off-sites record),
        # but structural illegality must still reject the payload.
        self.assertRejected(
            lambda d: d['runs'][0]['candidates'].append(
                {'id': 'bad', 'matrix': [[2, 0], [0, 1]], 'offset': ZERO}))

    def test_candidate_and_link_caps(self):
        payload = {'wafer': 'W', 'sites': [[0, 0]],
                   'runs': [{'id': 'A', 'bins': {'P': 'pass'}, 'records': [],
                             'candidates': [
                                 {'id': cid, 'matrix': matrix, 'offset': ZERO}
                                 for cid, matrix in EIGHT]}],
                   'links': []}
        result = process_calibrated(payload)
        self.assertEqual(result['supported_candidates'],
                         [{'run': 'A',
                           'candidates': sorted(cid for cid, _ in EIGHT)}])
        payload['runs'][0]['candidates'].append(
            {'id': 'extra', 'matrix': IDENTITY, 'offset': [1, 0]})
        with self.assertRaises(ValueError):
            process_calibrated(payload)

        many_links = {'wafer': 'W', 'sites': [[0, 0]],
                      'runs': [two_candidate_run('A', []),
                               two_candidate_run('B', [])],
                      'links': [
                          {'left': {'run': 'A', 'at': [k // 16, k % 16]},
                           'right': {'run': 'B', 'at': [k // 16, k % 16]}}
                          for k in range(256)]}
        result = process_calibrated(many_links)
        self.assertEqual(len(result['dies']), 1)
        many_links['links'].append(copy.deepcopy(many_links['links'][0]))
        with self.assertRaises(ValueError):
            process_calibrated(many_links)

    def test_single_run_without_links_uses_all_survivors(self):
        payload = {'wafer': 'W',
                   'sites': [[0, 0], [1, 0]],
                   'runs': [two_candidate_run(
                       'A', [{'at': [0, 0], 'bin': 'P'},
                             {'at': [1, 0], 'bin': 'S'}])],
                   'links': []}
        result = process_calibrated(payload)
        self.assertEqual(result['supported_candidates'],
                         [{'run': 'A', 'candidates': ['id', 'mx']}])
        # Each die is pass under one transform and skip under the other.
        for die in result['dies']:
            self.assertEqual(die['possible_statuses'], ['pass', 'incomplete'])
        self.assertEqual(result['pick_coordinates'], [])


class DeterminismAndContractTests(unittest.TestCase):
    def setUp(self):
        self.data = linked_two_run(
            [{'at': [0, 0], 'bin': 'P'}, {'at': [1, 0], 'bin': 'F'}],
            [{'at': [0, 0], 'bin': 'F'}, {'at': [1, 0], 'bin': 'P'}],
            (0, 0), (1, 0))

    def test_reordering_does_not_change_output(self):
        expected = process_calibrated(self.data)
        shuffled = copy.deepcopy(self.data)
        shuffled['sites'].reverse()
        for run in shuffled['runs']:
            run['records'].reverse()
            run['candidates'].reverse()
        shuffled['links'].reverse()
        self.assertEqual(process_calibrated(shuffled), expected)

    def test_input_is_not_mutated_and_json_serializable(self):
        snapshot = copy.deepcopy(self.data)
        result = process_calibrated(self.data)
        self.assertEqual(self.data, snapshot)
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        self.assertEqual(result['schema'], 'wafer-calibration-v1')
        self.assertEqual(set(result),
                         {'schema', 'wafer', 'supported_candidates',
                          'dies', 'pick_coordinates'})
        for die in result['dies']:
            self.assertEqual(set(die), {'die_id', 'at', 'possible_statuses'})
        # pick_coordinates keeps the same y-then-x order as dies.
        self.assertEqual(result['pick_coordinates'],
                         [die['at'] for die in result['dies']
                          if die['possible_statuses'] == ['pass']])

    def test_link_benchmark_need_not_be_a_die_or_record(self):
        # The benchmark point [5,5] is neither a site nor a record; only the
        # equality of transformed coordinates matters.
        data = copy.deepcopy(self.data)
        data['links'] = [
            {'left': {'run': 'A', 'at': [5, 5]},
             'right': {'run': 'B', 'at': [5, 5]}}]
        result = process_calibrated(data)
        self.assertEqual(statuses(result), [['fail'], ['fail']])

    def test_multiple_links_between_same_station_pair(self):
        data = copy.deepcopy(self.data)
        # Two distinct correspondences on one station pair are allowed and
        # jointly enforced; here both pin the same (opposite) direction.
        data['links'] = [
            {'left': {'run': 'A', 'at': [0, 0]},
             'right': {'run': 'B', 'at': [1, 0]}},
            {'left': {'run': 'A', 'at': [1, 0]},
             'right': {'run': 'B', 'at': [0, 0]}}]
        result = process_calibrated(data)
        self.assertEqual(statuses(result),
                         [['pass', 'fail'], ['pass', 'fail']])
        # One correspondence contradicting the other makes calibration fail.
        data['links'].append(
            {'left': {'run': 'A', 'at': [0, 0]},
             'right': {'run': 'B', 'at': [0, 0]}})
        with self.assertRaises(ValueError):
            process_calibrated(data)


if __name__ == '__main__':
    unittest.main()
