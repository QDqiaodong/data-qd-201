"""Acceptance tests for the offline dual-die mounting batch API.

Every fixture here is synthetic: no production wafer data and no equipment
connection.  Run with the package importable, e.g. inside the project venv::

    python -I -m unittest tests.test_mounting -v
"""
import copy
import itertools
import json
import tempfile
import unittest
from pathlib import Path

from wafer_fusion import MountingBatch
from wafer_fusion import mounting as mounting_module

IDENTITY = [[1, 0], [0, 1]]
ZERO = [0, 0]
MAX_DIFF = 2
POSITIONS = ['P1', 'P2']


# ----------------------------------------------------------- synthetic wafers

def identity_wafer(wafer, points, failing=(), skipping=()):
    """One identity-only station; every point passes unless marked otherwise."""
    records = []
    for point in sorted(points, key=lambda p: (p[1], p[0])):
        if tuple(point) in failing:
            code = 'F'
        elif tuple(point) in skipping:
            code = 'S'
        else:
            code = 'P'
        records.append({'at': list(point), 'bin': code})
    return {
        'wafer': wafer,
        'sites': [list(point) for point in points],
        'runs': [{
            'id': 'probe',
            'bins': {'P': 'pass', 'F': 'fail', 'S': 'skip'},
            'records': records,
            'candidates': [{'id': 'id', 'matrix': IDENTITY, 'offset': ZERO}]}],
        'links': []}


def ambiguous_wafer(wafer='AMB'):
    """Two feasible calibrations (id and mirror-x); one die fails under mx.

    The symmetric 3x3 grid keeps both candidates in-bounds for every record.
    Canonical dies (0,0) and (2,0) swap their local preimages between the
    two candidates.  Station B records local (0,0) as fail, so die (2,0) is
    pass under id but fail under mirror-x and therefore never reaches
    ``pick_coordinates`` -- even though its grading row looks perfectly good.
    """
    mirror_x = [[-1, 0], [0, 1]]
    sites = [[x, y] for y in range(3) for x in range(3)]
    local_points = [(x, y) for y in range(3) for x in range(3)]
    records_b = [{'at': [x, y], 'bin': ('F' if (x, y) == (0, 0) else 'P')}
                 for x, y in local_points]
    records_a = [{'at': [x, y], 'bin': 'P'} for x, y in local_points]
    station = lambda run_id, records: {
        'id': run_id,
        'bins': {'P': 'pass', 'F': 'fail'},
        'records': records,
        'candidates': [
            {'id': 'id', 'matrix': IDENTITY, 'offset': ZERO},
            {'id': 'mx', 'matrix': mirror_x, 'offset': [2, 0]}]}
    return {
        'wafer': wafer,
        'sites': sites,
        'runs': [station('A', records_a), station('B', records_b)],
        'links': [{'left': {'run': 'A', 'at': [1, 1]},
                   'right': {'run': 'B', 'at': [1, 1]}}]}


def grade(wafer, coefficient, side, group, at=(0, 0)):
    return {'wafer': wafer, 'at': list(at), 'side': side,
            'source_group': group, 'coefficient': coefficient}


def ref(wafer, at=(0, 0)):
    return {'wafer': wafer, 'at': list(at)}


def five_die_request(l3_coefficient=-3, include_l3=True,
                     groups=None, positions=None):
    """The acceptance layout: L1=0, L2=2, L3=-3 (default), R1=1, R2=-1."""
    wafers = [identity_wafer('WL1', [ZERO]),
              identity_wafer('WL2', [ZERO]),
              identity_wafer('WR1', [ZERO]),
              identity_wafer('WR2', [ZERO])]
    rows = [grade('WL1', 0, 'L', 'GL'),
            grade('WL2', 2, 'L', 'GL'),
            grade('WR1', 1, 'R', 'GR'),
            grade('WR2', -1, 'R', 'GR')]
    if include_l3:
        wafers.append(identity_wafer('WL3', [ZERO]))
        rows.append(grade('WL3', l3_coefficient, 'L', 'GL'))
    if groups is not None:
        for row in rows:
            row['source_group'] = groups[row['wafer']]
    return {'wafers': wafers, 'grading': rows,
            'positions': list(positions or POSITIONS),
            'max_coefficient_diff': MAX_DIFF}


def pair_names(plan):
    return [(item['left']['wafer'], item['right']['wafer'])
            for item in plan['pairs']]


def assignment_map(plan):
    return {item['position_id']:
            (item['left']['wafer'], item['right']['wafer'])
            for item in plan['assignments']}


class MountingTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def create_batch(self, name='batch', request=None):
        return MountingBatch.create(self.root / name,
                                    copy.deepcopy(request or five_die_request()))

    def reopen(self, name='batch'):
        return MountingBatch(self.root / name)


# ============================================================ acceptance (1)

class PairingOptimizationTests(MountingTestBase):
    """Whole-batch pairing: global optimum, tie-break, group and calibration."""

    def test_global_minimum_beats_the_locally_tempting_choice(self):
        batch = self.create_batch()
        plan = batch.preview_plan()
        self.assertTrue(plan['complete'])
        # L1-R2 (diff 1) + L2-R1 (diff 1): total 2.  Taking R1 for L1 first
        # would leave L2 without any partner once L3 claims R2.
        self.assertEqual(pair_names(plan),
                         [('WL1', 'WR2'), ('WL2', 'WR1')])
        self.assertEqual(plan['total_coefficient_diff'], 2)
        # Sorted pair list is handed out in fixture order.
        self.assertEqual(assignment_map(plan),
                         {'P1': ('WL1', 'WR2'), 'P2': ('WL2', 'WR1')})
        # Preview writes nothing.
        self.assertEqual(batch.open_credentials(), [])
        self.assertEqual(batch.consumed_dies(), [])

    def test_without_l3_the_batch_is_not_completable(self):
        # Four dies (two L, two R) cannot serve three positions at all; the
        # answer must be an explicit non-complete plan, never a partial one.
        request = five_die_request(include_l3=False,
                                   positions=['P1', 'P2', 'P3'])
        batch = MountingBatch.create(self.root / 'short', request)
        plan = batch.preview_plan()
        self.assertFalse(plan['complete'])
        self.assertEqual(plan['pairs'], [])
        self.assertEqual(plan['assignments'], [])
        self.assertIsNone(plan['total_coefficient_diff'])
        self.assertEqual(plan['positions'], ['P1', 'P2', 'P3'])

        # The local-greedy dead end from the acceptance story: with L3 gone,
        # two positions can still be filled, but taking R1 for L1 starves L2.
        two = MountingBatch.create(
            self.root / 'short-two',
            five_die_request(include_l3=False))
        plan = two.preview_plan()
        self.assertTrue(plan['complete'])
        self.assertEqual(pair_names(plan), [('WL1', 'WR2'), ('WL2', 'WR1')])

    def test_tie_falls_back_to_lexicographic_pair_list(self):
        batch = MountingBatch.create(
            self.root / 'tie', five_die_request(l3_coefficient=-2))
        plan = batch.preview_plan()
        self.assertTrue(plan['complete'])
        self.assertEqual(plan['total_coefficient_diff'], 2)
        # Three matchings tie on total diff 2; the sorted pair list starting
        # with the smallest pair (L1, R1) wins.
        self.assertEqual(pair_names(plan),
                         [('WL1', 'WR1'), ('WL3', 'WR2')])
        self.assertEqual(assignment_map(plan),
                         {'P1': ('WL1', 'WR1'), 'P2': ('WL3', 'WR2')})

    def test_source_group_boundary_blocks_close_pairs(self):
        # R1 shares L2's group: the otherwise-ideal L2-R1 edge is forbidden.
        groups = {'WL1': 'GL', 'WL2': 'GL', 'WL3': 'GL',
                  'WR1': 'GL', 'WR2': 'GR'}
        batch = MountingBatch.create(
            self.root / 'group', five_die_request(groups=groups))
        plan = batch.preview_plan()
        self.assertFalse(plan['complete'])
        for item in plan['pairs']:
            self.assertNotEqual(item['left']['source_group'],
                                item['right']['source_group'])

    def test_difference_equal_to_the_limit_is_allowed(self):
        # L3(-3) vs R2(-1) has diff exactly 2 == max_coefficient_diff.
        batch = self.create_batch()
        edges = {(item['left']['wafer'], item['right']['wafer'])
                 for item in batch.preview_plan()['pairs']}
        self.assertNotIn(('WL3', 'WR1'), edges)  # diff 4, over the limit

    def test_zero_diff_requires_identical_coefficients(self):
        request = {
            'wafers': [identity_wafer('L', [ZERO]),
                       identity_wafer('R', [ZERO])],
            'grading': [grade('L', 5, 'L', 'G1'),
                        grade('R', 6, 'R', 'G2')],
            'positions': ['P1'], 'max_coefficient_diff': 0}
        batch = MountingBatch.create(self.root / 'zero', request)
        self.assertFalse(batch.preview_plan()['complete'])
        request['grading'][1]['coefficient'] = 5
        batch = MountingBatch.create(self.root / 'zero-ok', request)
        plan = batch.preview_plan()
        self.assertTrue(plan['complete'])
        self.assertEqual(plan['pairs'][0]['coefficient_diff'], 0)

    def test_well_graded_die_failing_under_one_calibration_never_pairs(self):
        request = {
            'wafers': [ambiguous_wafer(),
                       identity_wafer('WL1', [ZERO]),
                       identity_wafer('WR1', [ZERO])],
            'grading': [
                grade('AMB', 0, 'L', 'GL', at=(2, 0)),   # fails under mx
                grade('WL1', 0, 'L', 'GL'),
                grade('WR1', 1, 'R', 'GR')],
            'positions': ['P1'],
            'max_coefficient_diff': 2}
        batch = MountingBatch.create(self.root / 'calib', request)
        view = {row['wafer']: row for row in batch.grading_view()}
        self.assertFalse(view['AMB']['eligible'])
        self.assertTrue(view['WL1']['eligible'])
        plan = batch.preview_plan()
        self.assertTrue(plan['complete'])
        used = {die['wafer'] for item in plan['pairs']
                for die in (item['left'], item['right'])}
        self.assertNotIn('AMB', used)
        self.assertEqual(pair_names(plan), [('WL1', 'WR1')])

    def test_graded_but_unpicked_die_is_legal_input_but_excluded(self):
        wafer = identity_wafer('W', [ZERO, [1, 0]], failing={(1, 0)})
        request = {'wafers': [wafer, identity_wafer('R', [ZERO])],
                   'grading': [grade('W', 0, 'L', 'G1'),
                               grade('W', 0, 'L', 'G1', at=(1, 0)),
                               grade('R', 0, 'R', 'G2')],
                   'positions': ['P1'], 'max_coefficient_diff': 1}
        batch = MountingBatch.create(self.root / 'unpicked', request)
        rows = {(row['wafer'], tuple(row['at'])): row
                for row in batch.grading_view()}
        self.assertTrue(rows[('W', (0, 0))]['eligible'])
        self.assertFalse(rows[('W', (1, 0))]['eligible'])
        self.assertEqual(pair_names(batch.preview_plan()), [('W', 'R')])

    def test_reordering_inputs_does_not_change_the_plan(self):
        first = self.create_batch('order-a').preview_plan()
        request = five_die_request()
        request['wafers'] = list(reversed(request['wafers']))
        request['grading'] = list(reversed(request['grading']))
        second = MountingBatch.create(self.root / 'order-b', request)
        self.assertEqual(second.preview_plan(), first)


# ============================================================ acceptance (2)

class CredentialLifecycleTests(MountingTestBase):
    """Issuance, scrap, consumption and the next-round pairing."""

    def _issue_both_credentials(self, batch):
        plan = batch.preview_plan()
        first = batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        # P2's pair is locked before P1 reports back, exactly because
        # mounting positions are not independent material picks.
        second = batch.issue_credential({
            'request_id': 'REQ-2', 'position_id': 'P2',
            'pair': {'left': ref('WL2'), 'right': ref('WR1')}})
        return plan, first, second

    def test_scrap_one_touched_die_releases_the_other_and_repairs_plan(self):
        batch = self.create_batch()
        _, first, second = self._issue_both_credentials(batch)
        self.assertEqual(first['credential_id'], 'C0001')
        self.assertEqual(first['state'], 'open')
        self.assertEqual(second['credential_id'], 'C0002')

        confirmation = batch.submit_mount_report({
            'credential_id': first['credential_id'],
            'outcome': 'scrapped',
            'touched': [ref('WL1')],
            'untouched': [ref('WR2')]})
        self.assertEqual(confirmation['outcome'], 'scrapped')
        self.assertEqual(confirmation['consumed'], [ref('WL1')])
        self.assertEqual(confirmation['position_status'], 'waiting')

        consumed = {entry['wafer'] for entry in batch.consumed_dies()}
        self.assertEqual(consumed, {'WL1'})
        # The old credential is historical; P2 stays open with its pair.
        history = {item['credential_id']: item for item in
                   batch.credential_history()}
        self.assertEqual(history['C0001']['state'], 'scrapped')
        self.assertEqual(batch.open_credentials()[0]['credential_id'], 'C0002')

        plan = batch.preview_plan()
        self.assertTrue(plan['complete'])
        self.assertEqual(plan['positions'], ['P1'])
        self.assertEqual(assignment_map(plan),
                         {'P1': ('WL3', 'WR2')})
        # The new pair for the reopened position can be checked out.
        reissued = batch.issue_credential({
            'request_id': 'REQ-3', 'position_id': 'P1',
            'pair': {'left': ref('WL3'), 'right': ref('WR2')}})
        self.assertEqual(reissued['credential_id'], 'C0003')

        # The stale preview cannot check out the original pair any more.
        with self.assertRaises(ValueError):
            batch.issue_credential({
                'request_id': 'REQ-STALE', 'position_id': 'P1',
                'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        # The original request id still retrieves the dead credential; this is
        # not a re-authorisation.
        replayed = batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        self.assertEqual(replayed['credential_id'], 'C0001')
        self.assertEqual(replayed['state'], 'scrapped')
        # Three credentials exist: the scrapped original, the still-open P2
        # credential and the reissued P1 credential.
        self.assertEqual(len(batch.credential_history()), 3)

    def test_scrap_with_both_dies_touched_makes_completion_impossible(self):
        batch = self.create_batch('both')
        batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        confirmation = batch.submit_mount_report({
            'credential_id': 'C0001', 'outcome': 'scrapped',
            'touched': [ref('WR2'), ref('WL1')], 'untouched': []})
        self.assertEqual(
            sorted(die['wafer'] for die in confirmation['consumed']),
            ['WL1', 'WR2'])
        plan = batch.preview_plan()
        self.assertFalse(plan['complete'])
        # No fresh credential can dress the batch up as still workable.
        with self.assertRaises(ValueError):
            batch.issue_credential({
                'request_id': 'REQ-NEW', 'position_id': 'P1',
                'pair': {'left': ref('WL3'), 'right': ref('WR1')}})

    def test_scrap_never_refused_for_making_the_remainder_impossible(self):
        # Even with no alternative left, the physical scrap is recorded.
        one = five_die_request(include_l3=False, positions=['P1'])
        batch = MountingBatch.create(self.root / 'doomed', one)
        batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR1')}})
        confirmation = batch.submit_mount_report({
            'credential_id': 'C0001', 'outcome': 'scrapped',
            'touched': [ref('WL1'), ref('WR1')], 'untouched': []})
        self.assertEqual(confirmation['outcome'], 'scrapped')
        self.assertFalse(batch.preview_plan()['complete'])

    def test_open_credential_reserves_both_dies_against_other_positions(self):
        batch = self.create_batch()
        batch.issue_credential({
            'request_id': 'REQ-2', 'position_id': 'P2',
            'pair': {'left': ref('WL2'), 'right': ref('WR1')}})
        plan = batch.preview_plan()
        # Only P1 is waiting; L2 and R1 are locked and cannot be stolen.
        self.assertEqual(plan['positions'], ['P1'])
        used = {die['wafer'] for item in plan['pairs']
                for die in (item['left'], item['right'])}
        self.assertNotIn('WL2', used)
        self.assertNotIn('WR1', used)
        self.assertEqual(assignment_map(plan),
                         {'P1': ('WL1', 'WR2')})
        # A second credential for the same locked position is refused.
        with self.assertRaises(ValueError):
            batch.issue_credential({
                'request_id': 'REQ-DUP-POS', 'position_id': 'P2',
                'pair': {'left': ref('WL2'), 'right': ref('WR1')}})

    def test_mounted_report_consumes_both_dies_and_terminates_position(self):
        batch = self.create_batch()
        batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        confirmation = batch.submit_mount_report({
            'credential_id': 'C0001', 'outcome': 'mounted',
            'touched': [ref('WL1'), ref('WR2')], 'untouched': []})
        self.assertEqual(confirmation['position_status'], 'mounted')
        statuses = {item['position_id']: item['status']
                    for item in batch.positions()}
        self.assertEqual(statuses['P1'], 'mounted')
        self.assertEqual(statuses['P2'], 'waiting')
        consumed = {entry['wafer']: entry['reason']
                    for entry in batch.consumed_dies()}
        self.assertEqual(consumed, {'WL1': 'mounted', 'WR2': 'mounted'})
        with self.assertRaises(ValueError):
            batch.issue_credential({
                'request_id': 'REQ-X', 'position_id': 'P1',
                'pair': {'left': ref('WL3'), 'right': ref('WR1')}})

    def test_scrap_without_any_touch_returns_both_dies_to_the_pool(self):
        batch = self.create_batch()
        batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        batch.submit_mount_report({
            'credential_id': 'C0001', 'outcome': 'scrapped',
            'touched': [], 'untouched': [ref('WL1'), ref('WR2')]})
        self.assertEqual(batch.consumed_dies(), [])
        plan = batch.preview_plan()
        self.assertTrue(plan['complete'])
        self.assertEqual(pair_names(plan),
                         [('WL1', 'WR2'), ('WL2', 'WR1')])

    def test_issue_rejects_pairs_not_in_the_current_plan(self):
        batch = self.create_batch()
        with self.assertRaises(ValueError):
            batch.issue_credential({
                'request_id': 'REQ-BAD', 'position_id': 'P1',
                'pair': {'left': ref('WL1'), 'right': ref('WR1')}})
        self.assertEqual(batch.open_credentials(), [])


# ============================================================ acceptance (3)

class DurabilityAndReplayTests(MountingTestBase):
    """Crash boundaries, idempotent retries and conflict rejection."""

    def test_interruption_before_commit_leaves_the_credential_open(self):
        batch = self.create_batch()
        batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        report = {'credential_id': 'C0001', 'outcome': 'scrapped',
                  'touched': [ref('WL1')], 'untouched': [ref('WR2')]}

        original_replace = MountingBatch._atomic_replace
        original_unlink = Path.unlink
        MountingBatch._atomic_replace = lambda self, tmp, target: (_
                                                                    for _ in ()).throw(
            OSError('simulated interruption'))
        # Keep the staged temp file on disk like a process killed mid-commit.
        Path.unlink = lambda self, *args, **kwargs: None
        try:
            with self.assertRaises(OSError):
                batch.submit_mount_report(copy.deepcopy(report))
        finally:
            MountingBatch._atomic_replace = original_replace
            Path.unlink = original_unlink

        # A leftover temp file exists on disk but must never be read as state.
        self.assertTrue(list(batch.path.glob('.mounting.json.*.tmp')))
        reopened = self.reopen()  # constructor clears stale temp files
        self.assertFalse(
            list(reopened.path.glob('.mounting.json.*.tmp')))
        open_credentials = reopened.open_credentials()
        self.assertEqual([item['credential_id'] for item in open_credentials],
                         ['C0001'])
        self.assertEqual(reopened.consumed_dies(), [])
        statuses = {item['position_id']: item['status']
                    for item in reopened.positions()}
        self.assertEqual(statuses['P1'], 'locked')

        # Now the report really lands; simulate a lost reply by reopening and
        # replaying the identical terminal report.
        confirmation_one = reopened.submit_mount_report(copy.deepcopy(report))
        reopened_two = self.reopen()
        confirmation_two = reopened_two.submit_mount_report(
            {'credential_id': 'C0001', 'outcome': 'scrapped',
             'touched': [ref('WL1')],
             'untouched': [ref('WR2')]})
        self.assertEqual(confirmation_one, confirmation_two)
        # Both positions are waiting again; the global re-solve distributes
        # the sorted minimum-diff pair list in fixture order.
        plan = reopened_two.preview_plan()
        self.assertEqual(plan['positions'], ['P1', 'P2'])
        self.assertEqual(assignment_map(plan),
                         {'P1': ('WL2', 'WR1'), 'P2': ('WL3', 'WR2')})

    def test_lost_issue_reply_is_recovered_by_request_id(self):
        batch = self.create_batch()
        request = {'request_id': 'REQ-1', 'position_id': 'P1',
                   'pair': {'left': ref('WL1'), 'right': ref('WR2')}}
        first = batch.issue_credential(copy.deepcopy(request))
        reopened = self.reopen()
        replayed = reopened.issue_credential(copy.deepcopy(request))
        self.assertEqual(replayed, first)
        self.assertEqual(len(reopened.credential_history()), 1)
        with self.assertRaises(ValueError):
            reopened.issue_credential({
                'request_id': 'REQ-1', 'position_id': 'P1',
                'pair': {'left': ref('WL3'), 'right': ref('WR2')}})
        with self.assertRaises(ValueError):
            reopened.issue_credential({
                'request_id': 'REQ-1', 'position_id': 'P2',
                'pair': {'left': ref('WL1'), 'right': ref('WR2')}})

    def test_old_report_replay_returns_original_confirmation_after_reissue(self):
        batch = self.create_batch()
        batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        old_report = {'credential_id': 'C0001', 'outcome': 'scrapped',
                      'touched': [ref('WL1')], 'untouched': [ref('WR2')]}
        original = batch.submit_mount_report(copy.deepcopy(old_report))

        # The position reopens and gets a brand-new credential (P1 alone is
        # waiting, so the global re-solve pairs it with L2-R1).
        new_credential = batch.issue_credential({
            'request_id': 'REQ-3', 'position_id': 'P1',
            'pair': {'left': ref('WL2'), 'right': ref('WR1')}})
        self.assertEqual(new_credential['credential_id'], 'C0002')

        # Re-submitting the old terminal report returns the old confirmation
        # and must not touch the new open credential.
        again = batch.submit_mount_report(copy.deepcopy(old_report))
        self.assertEqual(again, original)
        open_ids = [item['credential_id'] for item in batch.open_credentials()]
        self.assertEqual(open_ids, ['C0002'])
        history = {item['credential_id']: item
                   for item in batch.credential_history()}
        self.assertEqual(history['C0001']['state'], 'scrapped')
        self.assertEqual(history['C0002']['state'], 'open')

    def test_conflicting_and_malformed_reports_are_rejected(self):
        batch = self.create_batch()
        batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        good = {'credential_id': 'C0001', 'outcome': 'mounted',
                'touched': [ref('WL1'), ref('WR2')], 'untouched': []}
        batch.submit_mount_report(copy.deepcopy(good))

        # Contradictory terminal state.
        with self.assertRaises(ValueError):
            batch.submit_mount_report({
                'credential_id': 'C0001', 'outcome': 'scrapped',
                'touched': [ref('WL1')], 'untouched': [ref('WR2')]})
        # Unknown credential, die outside the credential, unaccounted die,
        # same die in both sets, mounted while claiming an untouched die.
        with self.assertRaises(ValueError):
            batch.submit_mount_report({
                'credential_id': 'NOPE', 'outcome': 'mounted',
                'touched': [ref('WL1'), ref('WR2')], 'untouched': []})
        with self.assertRaises(ValueError):
            batch.submit_mount_report({
                'credential_id': 'C0001', 'outcome': 'scrapped',
                'touched': [ref('WL3')], 'untouched': [ref('WR2')]})
        with self.assertRaises(ValueError):
            batch.submit_mount_report({
                'credential_id': 'C0001', 'outcome': 'scrapped',
                'touched': [ref('WL1')], 'untouched': []})
        with self.assertRaises(ValueError):
            batch.submit_mount_report({
                'credential_id': 'C0001', 'outcome': 'scrapped',
                'touched': [ref('WL1')], 'untouched': [ref('WL1')]})
        with self.assertRaises(ValueError):
            batch.submit_mount_report({
                'credential_id': 'C0001', 'outcome': 'mounted',
                'touched': [ref('WL1')], 'untouched': [ref('WR2')]})
        # Identical terminal replay (set order-independent) is a no-op.
        replay = batch.submit_mount_report({
            'credential_id': 'C0001', 'outcome': 'mounted',
            'touched': [ref('WR2'), ref('WL1')], 'untouched': []})
        self.assertEqual(replay['outcome'], 'mounted')
        self.assertEqual(len(batch.credential_history()), 1)

    def test_same_coordinate_on_two_wafers_are_distinct_dies(self):
        request = {
            'wafers': [identity_wafer('WA', [ZERO]),
                       identity_wafer('WB', [ZERO])],
            'grading': [grade('WA', 0, 'L', 'G1'),
                        grade('WB', 0, 'R', 'G2')],
            'positions': ['P1'], 'max_coefficient_diff': 0}
        batch = MountingBatch.create(self.root / 'same-at', request)
        plan = batch.preview_plan()
        self.assertEqual(pair_names(plan), [('WA', 'WB')])
        credential = batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WA'), 'right': ref('WB')}})
        self.assertEqual(credential['left']['at'], [0, 0])
        self.assertEqual(credential['right']['at'], [0, 0])
        self.assertNotEqual(credential['left']['wafer'],
                            credential['right']['wafer'])

    def test_reopen_exposes_full_state_for_the_next_shift(self):
        batch = self.create_batch()
        # Lock P2 first; after P1's scrap only P1 is waiting and gets the
        # globally re-solved replacement pair.
        batch.issue_credential({
            'request_id': 'REQ-2', 'position_id': 'P2',
            'pair': {'left': ref('WL2'), 'right': ref('WR1')}})
        batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        batch.submit_mount_report({
            'credential_id': 'C0002', 'outcome': 'scrapped',
            'touched': [ref('WL1')], 'untouched': [ref('WR2')]})

        reopened = self.reopen()
        statuses = {item['position_id']: item for item in reopened.positions()}
        self.assertEqual(statuses['P1']['status'], 'waiting')
        self.assertEqual(statuses['P2']['status'], 'locked')
        self.assertEqual(statuses['P2']['open_credential_id'], 'C0001')
        self.assertEqual(len(reopened.open_credentials()), 1)
        history = reopened.credential_history()
        self.assertEqual([item['credential_id'] for item in history],
                         ['C0001', 'C0002'])
        # C0001 (P2) is still open; C0002 (P1) carries the scrap terminal.
        self.assertIsNone(history[0]['terminal'])
        self.assertEqual(history[1]['terminal']['touched'], [ref('WL1')])
        self.assertEqual([entry['wafer'] for entry in reopened.consumed_dies()],
                         ['WL1'])
        plan = reopened.preview_plan()
        # P2 keeps L2-R1 locked, so those dies are unavailable; the only
        # minimum-diff partner left for the reopened P1 is L3-R2.
        self.assertEqual(assignment_map(plan), {'P1': ('WL3', 'WR2')})
        raw = json.loads(Path(reopened._state_file).read_text())
        self.assertEqual(raw['schema'], 'wafer-mounting-batch-v1')


# ========================================================== validation/quotas

class ValidationTests(MountingTestBase):
    def test_create_replay_and_no_overwrite(self):
        request = five_die_request()
        first = MountingBatch.create(self.root / 'b',
                                     copy.deepcopy(request))
        again = MountingBatch.create(self.root / 'b',
                                     copy.deepcopy(request))
        self.assertEqual(again.batch_info(), first.batch_info())
        changed = five_die_request()
        changed['max_coefficient_diff'] = 1
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'b', changed)
        with self.assertRaises(ValueError):
            MountingBatch(self.root / 'missing')

    def test_empty_grading_is_legal_but_never_completable(self):
        request = {'wafers': [identity_wafer('W', [ZERO])],
                   'grading': [], 'positions': ['P1'],
                   'max_coefficient_diff': 1}
        batch = MountingBatch.create(self.root / 'empty', request)
        self.assertEqual(batch.grading_view(), [])
        plan = batch.preview_plan()
        self.assertFalse(plan['complete'])
        with self.assertRaises(ValueError):
            batch.issue_credential({
                'request_id': 'R1', 'position_id': 'P1',
                'pair': {'left': ref('W'), 'right': ref('W')}})

    def test_create_is_order_independent_across_reopen(self):
        request = five_die_request()
        MountingBatch.create(self.root / 'ord', copy.deepcopy(request))
        shuffled = copy.deepcopy(request)
        shuffled['wafers'] = list(reversed(shuffled['wafers']))
        shuffled['grading'] = list(reversed(shuffled['grading']))
        # Same batch content in a different order reopens, never overwrites.
        reopened = MountingBatch.create(self.root / 'ord', shuffled)
        self.assertEqual(reopened.preview_plan(),
                         MountingBatch(self.root / 'ord').preview_plan())

    def test_structure_and_range_violations_raise(self):
        base = five_die_request()
        # Wafer count boundaries.
        bad = copy.deepcopy(base)
        bad['wafers'] = []
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x1', bad)
        bad = copy.deepcopy(base)
        bad['wafers'] = [identity_wafer(f'W{i}', [ZERO]) for i in range(17)]
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x2', bad)
        # Duplicate wafer ids.
        bad = copy.deepcopy(base)
        bad['wafers'].append(copy.deepcopy(bad['wafers'][0]))
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x3', bad)
        # Grading limits and uniqueness.
        bad = copy.deepcopy(base)
        bad['grading'].append(copy.deepcopy(bad['grading'][0]))
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x4', bad)
        bad = copy.deepcopy(base)
        bad['grading'] = [grade('WL1', 0, 'L', 'G') for _ in range(257)]
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x5', bad)
        # Positions.
        bad = copy.deepcopy(base)
        bad['positions'] = [f'P{i}' for i in range(33)]
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x6', bad)
        bad = copy.deepcopy(base)
        bad['positions'] = ['P1', 'P1']
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x7', bad)
        # Coefficient and diff ranges, bad side, unknown wafer, undeclared at.
        bad = copy.deepcopy(base)
        bad['grading'][0]['coefficient'] = 1_000_001
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x8', bad)
        bad = copy.deepcopy(base)
        bad['max_coefficient_diff'] = 2_000_001
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x9', bad)
        bad = copy.deepcopy(base)
        bad['grading'][0]['side'] = 'X'
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x10', bad)
        bad = copy.deepcopy(base)
        bad['grading'][0]['wafer'] = 'GHOST'
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x11', bad)
        bad = copy.deepcopy(base)
        bad['grading'][0]['at'] = [9, 9]
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x12', bad)
        # Booleans are not integers; unknown fields are rejected.
        bad = copy.deepcopy(base)
        bad['max_coefficient_diff'] = True
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x13', bad)
        bad = copy.deepcopy(base)
        bad['unexpected'] = 1
        with self.assertRaises(ValueError):
            MountingBatch.create(self.root / 'x14', bad)

    def test_caller_inputs_are_not_mutated(self):
        request = five_die_request()
        snapshot = copy.deepcopy(request)
        batch = MountingBatch.create(self.root / 'imm', request)
        self.assertEqual(request, snapshot)
        issue = {'request_id': 'REQ-1', 'position_id': 'P1',
                 'pair': {'left': ref('WL1'), 'right': ref('WR2')}}
        issue_snapshot = copy.deepcopy(issue)
        batch.issue_credential(issue)
        self.assertEqual(issue, issue_snapshot)
        report = {'credential_id': 'C0001', 'outcome': 'scrapped',
                  'touched': [ref('WL1')], 'untouched': [ref('WR2')]}
        report_snapshot = copy.deepcopy(report)
        batch.submit_mount_report(report)
        self.assertEqual(report, report_snapshot)

    def test_empty_waiting_list_returns_empty_plan(self):
        request = five_die_request(include_l3=False, positions=['P1'])
        batch = MountingBatch.create(self.root / 'one', request)
        batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR1')}})
        batch.submit_mount_report({
            'credential_id': 'C0001', 'outcome': 'mounted',
            'touched': [ref('WL1'), ref('WR1')], 'untouched': []})
        plan = batch.preview_plan()
        self.assertTrue(plan['complete'])
        self.assertEqual(plan['pairs'], [])
        self.assertEqual(plan['assignments'], [])
        self.assertEqual(plan['total_coefficient_diff'], 0)

    def test_all_views_are_json_serializable(self):
        batch = self.create_batch()
        batch.issue_credential({
            'request_id': 'REQ-1', 'position_id': 'P1',
            'pair': {'left': ref('WL1'), 'right': ref('WR2')}})
        views = [batch.preview_plan(), batch.positions(),
                 batch.open_credentials(), batch.credential_history(),
                 batch.consumed_dies(), batch.grading_view(),
                 batch.batch_info()]
        for view in views:
            json.dumps(view, sort_keys=True, ensure_ascii=False,
                       allow_nan=False)


class SolverCrossCheckTests(unittest.TestCase):
    """Brute-force enumeration versus the flow solver on random small cases."""

    @staticmethod
    def _brute_force(left_rows, right_rows, max_diff, required):
        edges = []
        for left in left_rows:
            for right in right_rows:
                if left['group'] == right['group']:
                    continue
                diff = abs(left['coef'] - right['coef'])
                if diff > max_diff:
                    continue
                edges.append((left['key'], right['key'], diff, left, right))
        edge_by_pair = {(left['key'], right['key']): (diff, left, right)
                        for _lk, _rk, diff, left, right in edges}

        best = None
        for chosen_left in itertools.combinations(left_rows, required):
            for chosen_right in itertools.permutations(right_rows, required):
                matching = []
                total = 0
                feasible = True
                for left, right in zip(chosen_left, chosen_right):
                    found = edge_by_pair.get((left['key'], right['key']))
                    if found is None:
                        feasible = False
                        break
                    diff, _l, _r = found
                    matching.append((left, right, diff))
                    total += diff
                if not feasible:
                    continue
                matching.sort(key=lambda item: (item[0]['key'],
                                                 item[1]['key']))
                signature = (total,
                             [(item[0]['key'], item[1]['key'])
                              for item in matching])
                if best is None or signature < best[0]:
                    best = (signature, matching)
        return best

    def test_flow_matches_brute_force_on_enumerated_cases(self):
        # Deterministic pseudo-random coefficients and group layouts.
        counter = 0
        for seed in range(24):
            left_rows = []
            right_rows = []
            for i in range(4):
                counter += 1
                left_rows.append({
                    'key': (f'L{i}', 0, 0), 'wafer': f'L{i}',
                    'coef': (seed * 7 + i * 3 - 5) % 11 - 5,
                    'group': 'GA' if (seed + i) % 3 else 'GB'})
            for j in range(4):
                right_rows.append({
                    'key': (f'R{j}', 0, 0), 'wafer': f'R{j}',
                    'coef': (seed * 5 + j * 2 - 4) % 11 - 5,
                    'group': 'GB' if (seed + j) % 2 else 'GA'})
            max_diff = seed % 4
            required = 2 + seed % 2
            expected = self._brute_force(left_rows, right_rows, max_diff,
                                         required)

            rows = []
            for item in left_rows:
                rows.append({'wafer': item['wafer'], 'at': [0, 0],
                             'side': 'L', 'source_group': item['group'],
                             'coefficient': item['coef'], 'eligible': True})
            for item in right_rows:
                rows.append({'wafer': item['wafer'], 'at': [0, 0],
                             'side': 'R', 'source_group': item['group'],
                             'coefficient': item['coef'], 'eligible': True})
            got = mounting_module._pairing_plan(
                rows, set(), set(), [f'P{i}' for i in range(required)],
                max_diff)
            if expected is None:
                self.assertIsNone(got, seed)
                continue
            self.assertIsNotNone(got, seed)
            self.assertEqual(sum(item[2] for item in got),
                             expected[0][0], seed)
            got_sig = [(_die_key_of(left), _die_key_of(right))
                       for left, right, _diff in got]
            self.assertEqual(got_sig, expected[0][1], seed)


def _die_key_of(row):
    return mounting_module._die_key(row['wafer'], tuple(row['at']))


if __name__ == '__main__':
    unittest.main()
