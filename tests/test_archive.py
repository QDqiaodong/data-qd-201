"""Acceptance tests for the persistent wafer retest archive.

All fixtures are synthetic; nothing here talks to equipment.  Tests run
with ``python -I`` against the installed/linked package.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from wafer_fusion import WaferArchive, process_calibrated

ID = [[1, 0], [0, 1]]
MX = [[-1, 0], [0, 1]]
MY = [[1, 0], [0, -1]]
ZERO = [0, 0]
SITES = [[0, 0], [-1, 1], [1, 1]]
DIE_O = [0, 0]
DIE_L = [-1, 1]
DIE_R = [1, 1]


def base_payload():
    """Three dies; station A identity-only, B has id / mirror-x / mirror-y."""
    return {
        'wafer': 'W',
        'sites': copy.deepcopy(SITES),
        'runs': [
            {'id': 'A',
             'bins': {'P': 'pass', 'F': 'fail', 'S': 'skip'},
             'records': [{'at': copy.deepcopy(DIE_O), 'bin': 'P'},
                         {'at': copy.deepcopy(DIE_L), 'bin': 'P'},
                         {'at': copy.deepcopy(DIE_R), 'bin': 'P'}],
             'candidates': [{'id': 'a_id', 'matrix': ID, 'offset': ZERO}]},
            {'id': 'B',
             'bins': {'P': 'pass', 'F': 'fail', 'S': 'skip'},
             'records': [{'at': [0, 0], 'bin': 'F'}],
             'candidates': [{'id': 'b_id', 'matrix': ID, 'offset': ZERO},
                            {'id': 'b_mx', 'matrix': MX, 'offset': ZERO},
                            {'id': 'b_my', 'matrix': MY, 'offset': ZERO}]}],
        'links': [{'left': {'run': 'A', 'at': [0, 1]},
                   'right': {'run': 'B', 'at': [0, 1]}}],
    }


def no_links():
    return []


def link_at(ax, ay, bx, by):
    return [{'left': {'run': 'A', 'at': [ax, ay]},
             'right': {'run': 'B', 'at': [bx, by]}}]


def statuses_by_at(result):
    return {tuple(die['at']): die['possible_statuses'] for die in result['dies']}


def order_status(result, order_id):
    return next(item for item in result['work_orders']
                if item['order_id'] == order_id)


class ArchiveTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def open_archive(self, name='w', revision_id='rev0', payload=None):
        path = self.root / name
        return WaferArchive.create(
            path, copy.deepcopy(payload or base_payload()), revision_id)

    def reopen(self, name='w'):
        return WaferArchive(self.root / name)


class GroupIssuanceTests(ArchiveTestBase):
    """Acceptance group 1: provable groups despite B's mirror-y ambiguity."""

    def test_mirror_y_supported_but_single_die_request_refused(self):
        archive = self.open_archive()
        result = archive.current_result()
        supported = {item['run']: item['candidates']
                     for item in result['supported_candidates']}
        self.assertEqual(supported['A'], ['a_id'])
        self.assertEqual(supported['B'], ['b_id', 'b_mx'])

        with self.assertRaises(ValueError):
            archive.issue_order(
                {'order_id': 'o-single', 'run_id': 'B', 'dies': [DIE_L]})
        # The refused order left nothing behind.
        self.assertEqual(archive.current_result()['work_orders'], [])
        self.assertEqual(archive._state['orders'], [])

    def test_three_die_group_is_issued_with_original_three_pins(self):
        archive = self.open_archive()
        receipt = archive.issue_order({
            'order_id': 'o-group', 'run_id': 'B',
            'dies': [DIE_R, DIE_O, DIE_L]})
        self.assertEqual(receipt['serial'], 1)
        self.assertEqual(receipt['revision_id'], 'rev0')
        self.assertEqual(sorted(map(tuple, receipt['local_pins'])),
                         sorted(map(tuple, SITES)))
        self.assertEqual(receipt['approved'],
                         [[0, 0], [-1, 1], [1, 1]])
        # Re-opening shows the same immutable order.
        again = self.reopen().history_order('o-group')
        self.assertEqual(again['local_pins'], receipt['local_pins'])
        self.assertIsNone(again['report'])

    def test_ambiguous_group_report_keeps_both_upper_dies_unpickable(self):
        archive = self.open_archive()
        archive.issue_order({'order_id': 'o-group', 'run_id': 'B',
                             'dies': [DIE_O, DIE_L, DIE_R]})
        # Asymmetric report: origin pass, upper-left fail, upper-right pass.
        receipt = archive.receive_report('o-group', [
            {'at': [0, 0], 'bin': 'P'},
            {'at': [-1, 1], 'bin': 'F'},
            {'at': [1, 1], 'bin': 'P'}])
        self.assertEqual([row['at'] for row in receipt['rows']],
                         [[0, 0], [-1, 1], [1, 1]])

        result = archive.current_result()
        got = statuses_by_at(result)
        # Each upper die may be pass or fail depending on the feasible
        # calibration; the mapping must never be fixed by candidate order.
        self.assertEqual(set(got[tuple(DIE_L)]), {'pass', 'fail'})
        self.assertEqual(set(got[tuple(DIE_R)]), {'pass', 'fail'})
        self.assertNotIn([-1, 1], result['pick_coordinates'])
        self.assertNotIn([1, 1], result['pick_coordinates'])
        # Origin: B fail replaced by retest pass, A stays pass.
        self.assertEqual(got[(0, 0)], ['pass'])
        self.assertIn([0, 0], result['pick_coordinates'])
        status = order_status(result, 'o-group')
        self.assertTrue(status['scope_holds'])
        self.assertTrue(status['reported'])
        # Effective lines carry the provenance needed for audits.
        self.assertEqual(
            [(line['run'], tuple(line['at']), line['source_order'])
             for line in result['effective_retests']],
            [('B', (0, 0), 'o-group'),
             ('B', (-1, 1), 'o-group'),
             ('B', (1, 1), 'o-group')])


class RetestLifecycleTests(ArchiveTestBase):
    """Acceptance group 2: suspension, fallback, recovery and late reports."""

    def _issue_two_orders(self, archive):
        origin = archive.issue_order(
            {'order_id': 'o-origin', 'run_id': 'B', 'dies': [DIE_O]})
        group = archive.issue_order(
            {'order_id': 'o-group', 'run_id': 'B',
             'dies': [DIE_O, DIE_L, DIE_R]})
        self.assertEqual(origin['serial'], 1)
        self.assertEqual(group['serial'], 2)
        return origin, group

    def test_newest_report_wins_then_suspension_falls_back(self):
        archive = self.open_archive()
        self._issue_two_orders(archive)

        # Group report arrives first: origin skip, both upper dies pass.
        archive.receive_report('o-group', [
            {'at': [0, 0], 'bin': 'S'},
            {'at': [-1, 1], 'bin': 'P'},
            {'at': [1, 1], 'bin': 'P'}])
        # The later, smaller order's report arrives afterwards.
        archive.receive_report('o-origin', [{'at': [0, 0], 'bin': 'P'}])

        result = archive.current_result()
        got = statuses_by_at(result)
        # Highest serial per (run, local): origin comes from the group (skip),
        # regardless of receive order.
        self.assertEqual(got[(0, 0)], ['incomplete'])
        origin_lines = [line for line in result['effective_retests']
                        if tuple(line['at']) == (0, 0)]
        self.assertEqual(origin_lines[0]['source_order'], 'o-group')
        self.assertEqual(got[(-1, 1)], ['pass'])
        self.assertEqual(got[(1, 1)], ['pass'])
        self.assertEqual(result['pick_coordinates'],
                         [[-1, 1], [1, 1]])

        # Delete the only link: mirror-y returns as a supported candidate.
        archive.submit_revision('rev-no-link', no_links())
        relaxed = archive.current_result()
        supported = {item['run']: item['candidates']
                     for item in relaxed['supported_candidates']}
        self.assertEqual(supported['B'], ['b_id', 'b_mx', 'b_my'])
        group_status = order_status(relaxed, 'o-group')
        origin_status = order_status(relaxed, 'o-origin')
        self.assertFalse(group_status['scope_holds'])
        self.assertTrue(group_status['reported'])  # archived but unusable
        self.assertTrue(origin_status['scope_holds'])
        self.assertTrue(origin_status['reported'])
        # Whole group suspended: origin falls back to the earlier order's
        # pass even though its mapping never moved; upper dies lose coverage.
        got = statuses_by_at(relaxed)
        self.assertEqual(got[(0, 0)], ['pass'])
        self.assertEqual(got[(-1, 1)], ['incomplete'])
        self.assertEqual(got[(1, 1)], ['incomplete'])
        self.assertEqual(relaxed['pick_coordinates'], [[0, 0]])
        # Only the origin order contributes effective lines now.
        self.assertEqual(
            [line['source_order'] for line in relaxed['effective_retests']],
            ['o-origin'])
        # The raw group report is still retrievable from history, unchanged.
        historical = archive.history_order('o-group')
        self.assertEqual(historical['report']['rows'], [
            {'at': [0, 0], 'bin': 'S'},
            {'at': [-1, 1], 'bin': 'P'},
            {'at': [1, 1], 'bin': 'P'}])
        self.assertEqual(historical['revision_id'], 'rev0')

        # Restore the link: the group becomes effective again, same picture
        # as right after the reports were received.
        archive.submit_revision(
            'rev-restore',
            link_at(0, 1, 0, 1))
        recovered = archive.current_result()
        got = statuses_by_at(recovered)
        self.assertEqual(got[(0, 0)], ['incomplete'])
        self.assertEqual(got[(-1, 1)], ['pass'])
        self.assertEqual(got[(1, 1)], ['pass'])
        self.assertEqual(recovered['pick_coordinates'], [[-1, 1], [1, 1]])
        self.assertTrue(order_status(recovered, 'o-group')['scope_holds'])

    def test_report_first_arriving_while_suspended_is_kept_then_activates(self):
        archive = self.open_archive()
        # Two-upper-die B order: provable while only id/mx are supported,
        # disproved once mirror-y rejoins after the link is removed.
        archive.issue_order(
            {'order_id': 'o-b', 'run_id': 'B', 'dies': [DIE_L, DIE_R]})
        archive.submit_revision('rev-no-link', no_links())
        suspended = archive.current_result()
        status = order_status(suspended, 'o-b')
        self.assertFalse(status['scope_holds'])
        self.assertFalse(status['reported'])

        # The report first arrives while the scope cannot be proved: it is
        # still archived with the exact dispatched shape.
        receipt = archive.receive_report('o-b', [
            {'at': [-1, 1], 'bin': 'F'}, {'at': [1, 1], 'bin': 'F'}])
        self.assertEqual(receipt['order_id'], 'o-b')
        broken = archive.current_result()
        status = order_status(broken, 'o-b')
        self.assertFalse(status['scope_holds'])
        self.assertTrue(status['reported'])
        self.assertEqual(broken['effective_retests'], [])
        # Suspended retest must not affect the pick map while it is unusable.
        got = statuses_by_at(broken)
        self.assertEqual(got[(-1, 1)], ['incomplete'])
        self.assertEqual(got[(1, 1)], ['incomplete'])

        # Evidence recovery makes the archived report effective without
        # needing a resubmission (both fail bins now block the upper dies).
        archive.submit_revision('rev-restore', link_at(0, 1, 0, 1))
        healed = archive.current_result()
        self.assertTrue(order_status(healed, 'o-b')['scope_holds'])
        got = statuses_by_at(healed)
        self.assertEqual(got[(-1, 1)], ['fail'])
        self.assertEqual(got[(1, 1)], ['fail'])
        self.assertEqual(
            [line['source_order'] for line in healed['effective_retests']],
            ['o-b', 'o-b'])

    def test_unreported_newer_order_reexposes_older_effective_line(self):
        archive = self.open_archive()
        self._issue_two_orders(archive)
        # Only the earlier origin order reports; the newer group is silent.
        archive.receive_report('o-origin', [{'at': [0, 0], 'bin': 'P'}])
        result = archive.current_result()
        origin_lines = [line for line in result['effective_retests']
                        if tuple(line['at']) == (0, 0)]
        self.assertEqual([line['source_order'] for line in origin_lines],
                         ['o-origin'])
        got = statuses_by_at(result)
        self.assertEqual(got[(0, 0)], ['pass'])
        # Once the group reports, it supersedes the earlier line per pin.
        archive.receive_report('o-group', [
            {'at': [0, 0], 'bin': 'S'},
            {'at': [-1, 1], 'bin': 'P'},
            {'at': [1, 1], 'bin': 'P'}])
        result = archive.current_result()
        origin_lines = [line for line in result['effective_retests']
                        if tuple(line['at']) == (0, 0)]
        self.assertEqual([line['source_order'] for line in origin_lines],
                         ['o-group'])
        self.assertEqual(statuses_by_at(result)[(0, 0)], ['incomplete'])

    def test_stations_are_adjudicated_independently(self):
        archive = self.open_archive()
        # Station A keeps identity under every revision; its order survives
        # the link deletion even while a B group order gets suspended.
        archive.issue_order(
            {'order_id': 'a-order', 'run_id': 'A', 'dies': [DIE_L]})
        archive.issue_order(
            {'order_id': 'b-order', 'run_id': 'B',
             'dies': [DIE_O, DIE_L, DIE_R]})
        archive.receive_report('a-order', [{'at': [-1, 1], 'bin': 'F'}])
        archive.receive_report('b-order', [
            {'at': [0, 0], 'bin': 'P'},
            {'at': [-1, 1], 'bin': 'P'},
            {'at': [1, 1], 'bin': 'P'}])
        archive.submit_revision('rev-no-link', no_links())
        result = archive.current_result()
        status = {item['order_id']: item for item in result['work_orders']}
        self.assertTrue(status['a-order']['scope_holds'])
        self.assertFalse(status['b-order']['scope_holds'])
        # A's fail still lands on the upper-left die; B contributes nothing.
        got = statuses_by_at(result)
        self.assertEqual(got[(-1, 1)], ['fail'])
        sources = {line['source_order'] for line in result['effective_retests']}
        self.assertEqual(sources, {'a-order'})

    def test_skip_replaces_fail_and_old_readings_are_not_double_counted(self):
        archive = self.open_archive()
        archive.issue_order(
            {'order_id': 'o-origin', 'run_id': 'B', 'dies': [DIE_O]})
        # First-test B origin is fail; a skip retest fully replaces it.
        archive.receive_report('o-origin', [{'at': [0, 0], 'bin': 'S'}])
        result = archive.current_result()
        got = statuses_by_at(result)
        self.assertEqual(got[(0, 0)], ['incomplete'])
        self.assertNotIn([0, 0], result['pick_coordinates'])


class ReplayValidationAndDurabilityTests(ArchiveTestBase):
    """Acceptance group 3: idempotency, conflicts, rejection and crash edges."""

    def test_equivalent_replays_ignore_row_order(self):
        archive = self.open_archive()
        request = {'order_id': 'o', 'run_id': 'B',
                   'dies': [DIE_R, DIE_O, DIE_L]}
        first = archive.issue_order(copy.deepcopy(request))
        shuffled = {'order_id': 'o', 'run_id': 'B',
                    'dies': [DIE_L, DIE_R, DIE_O]}
        again = archive.issue_order(copy.deepcopy(shuffled))
        self.assertEqual(again, first)
        self.assertEqual(len(archive._state['orders']), 1)

        rows = [{'at': [0, 0], 'bin': 'P'},
                {'at': [-1, 1], 'bin': 'F'},
                {'at': [1, 1], 'bin': 'P'}]
        report_one = archive.receive_report('o', copy.deepcopy(rows))
        report_two = archive.receive_report(
            'o', list(reversed(copy.deepcopy(rows))))
        self.assertEqual(report_two, report_one)
        self.assertIsNotNone(archive.history_order('o')['report'])

        # Same replay works on a freshly opened archive (lost reply).
        reopened = self.reopen()
        self.assertEqual(
            reopened.issue_order(copy.deepcopy(shuffled)), first)
        self.assertEqual(
            reopened.receive_report('o', list(reversed(rows))), report_one)

    def test_conflicting_order_and_report_are_rejected(self):
        archive = self.open_archive()
        archive.issue_order(
            {'order_id': 'o', 'run_id': 'B', 'dies': [DIE_O]})
        with self.assertRaises(ValueError):
            archive.issue_order(
                {'order_id': 'o', 'run_id': 'B', 'dies': [DIE_L]})
        with self.assertRaises(ValueError):
            archive.issue_order(
                {'order_id': 'o', 'run_id': 'A', 'dies': [DIE_O]})
        archive.receive_report('o', [{'at': [0, 0], 'bin': 'P'}])
        with self.assertRaises(ValueError):
            archive.receive_report('o', [{'at': [0, 0], 'bin': 'F'}])
        # Missing row, extra position, duplicate position, unknown bin.
        order_two = archive.issue_order(
            {'order_id': 'g', 'run_id': 'B',
             'dies': [DIE_O, DIE_L, DIE_R]})
        self.assertEqual(len(order_two['local_pins']), 3)
        for bad_rows in (
                [{'at': [0, 0], 'bin': 'P'}, {'at': [-1, 1], 'bin': 'P'}],
                [{'at': [0, 0], 'bin': 'P'}, {'at': [-1, 1], 'bin': 'P'},
                 {'at': [1, 1], 'bin': 'P'}, {'at': [1, 0], 'bin': 'P'}],
                [{'at': [0, 0], 'bin': 'P'}, {'at': [0, 0], 'bin': 'F'},
                 {'at': [1, 1], 'bin': 'P'}],
                [{'at': [0, 0], 'bin': 'X'}, {'at': [-1, 1], 'bin': 'P'},
                 {'at': [1, 1], 'bin': 'P'}]):
            with self.assertRaises(ValueError):
                archive.receive_report('g', copy.deepcopy(bad_rows))
        self.assertIsNone(archive.history_order('g')['report'])
        with self.assertRaises(ValueError):
            archive.receive_report('nope', [])

    def test_infeasible_revision_and_revision_id_replay_rules(self):
        archive = self.open_archive()
        before = archive.current_result()
        # A [0,2] -> [0,1] correspondence has no solution for station B.
        with self.assertRaises(ValueError):
            archive.submit_revision(
                'bad', link_at(0, 2, 0, 1))
        self.assertEqual(archive.current_revision_id, 'rev0')
        self.assertEqual(archive.current_result(), before)
        self.assertEqual(len(archive._state['revisions']), 1)

        # Replaying a revision id with identical (reshuffled) links returns
        # the original receipt and never rolls the current revision back.
        archive.submit_revision('rev-no-link', no_links())
        archive.submit_revision('rev-restore', link_at(0, 1, 0, 1))
        replay = archive.submit_revision('rev-no-link', no_links())
        self.assertEqual(replay['revision_id'], 'rev-no-link')
        self.assertEqual(archive.current_revision_id, 'rev-restore')
        with self.assertRaises(ValueError):
            archive.submit_revision('rev-no-link', link_at(0, 1, 0, 1))
        self.assertEqual(archive.current_revision_id, 'rev-restore')

    def test_revision_keeps_wafer_sites_runs_and_first_records_immutable(self):
        archive = self.open_archive()
        payload = base_payload()
        archive.submit_revision('rev-no-link', no_links())
        state = archive._state
        self.assertEqual(state['initial'], payload)
        self.assertEqual(
            [revision['revision_id'] for revision in state['revisions']],
            ['rev0', 'rev-no-link'])
        # The stored first-test evidence is untouched by revisions.
        self.assertEqual(state['initial']['runs'][1]['records'],
                         [{'at': [0, 0], 'bin': 'F'}])

    def test_interrupted_write_leaves_old_revision_and_old_pick_map(self):
        archive = self.open_archive()
        archive.issue_order(
            {'order_id': 'o', 'run_id': 'B', 'dies': [DIE_O]})
        archive.receive_report('o', [{'at': [0, 0], 'bin': 'P'}])
        expected = archive.current_result()
        state_file = archive._state_file

        original_replace = WaferArchive._atomic_replace
        original_unlink = Path.unlink
        interrupted = {'calls': 0}

        def fail_once(self, temporary, target):
            interrupted['calls'] += 1
            raise OSError('simulated write interruption')

        WaferArchive._atomic_replace = fail_once
        # Keep the staged temp file on disk like a process killed mid-commit.
        Path.unlink = lambda self, *args, **kwargs: None
        try:
            with self.assertRaises(OSError):
                archive.submit_revision('rev-no-link', no_links())
        finally:
            WaferArchive._atomic_replace = original_replace
            Path.unlink = original_unlink
        self.assertEqual(interrupted['calls'], 1)

        # A leftover temp file must not be mistaken for state; reopen shows
        # the old revision and the old pick map.
        leftovers = list(archive.path.glob('.archive.json.*.tmp'))
        self.assertTrue(leftovers)
        reopened = self.reopen()
        self.assertEqual(reopened.current_revision_id, 'rev0')
        self.assertEqual(reopened.current_result(), expected)
        self.assertEqual(reopened.history_order('o')['report']['rows'],
                         [{'at': [0, 0], 'bin': 'P'}])

        # Now the revision commits; simulate the caller losing the reply and
        # reopening: only the complete new picture is visible.
        receipt = reopened.submit_revision('rev-no-link', no_links())
        self.assertEqual(receipt['revision_id'], 'rev-no-link')
        healed = self.reopen()
        self.assertEqual(healed.current_revision_id, 'rev-no-link')
        self.assertEqual(healed.current_result()['revision_id'], 'rev-no-link')
        # The original report remains archived verbatim.
        self.assertEqual(healed.history_order('o')['report']['rows'],
                         [{'at': [0, 0], 'bin': 'P'}])
        self.assertFalse(
            list(Path(state_file).parent.glob('.archive.json.*.tmp')))

    def test_create_replay_on_existing_archive(self):
        path = self.root / 'w'
        first = WaferArchive.create(path, base_payload(), 'rev0')
        first.issue_order(
            {'order_id': 'o', 'run_id': 'B', 'dies': [DIE_O]})
        # Same payload + same initial revision id: reopen, keep history.
        again = WaferArchive.create(path, base_payload(), 'rev0')
        self.assertEqual(again.history_order('o')['serial'], 1)
        with self.assertRaises(ValueError):
            WaferArchive.create(path, base_payload(), 'different')
        changed = base_payload()
        changed['wafer'] = 'OTHER'
        with self.assertRaises(ValueError):
            WaferArchive.create(path, changed, 'rev0')

    def test_state_is_json_and_inputs_are_not_mutated(self):
        payload = base_payload()
        snapshot = copy.deepcopy(payload)
        archive = self.open_archive(payload=payload)
        request = {'order_id': 'o', 'run_id': 'B',
                   'dies': [DIE_R, DIE_O, DIE_L]}
        request_snapshot = copy.deepcopy(request)
        archive.issue_order(request)
        archive.receive_report('o', [
            {'at': [0, 0], 'bin': 'P'},
            {'at': [-1, 1], 'bin': 'F'},
            {'at': [1, 1], 'bin': 'P'}])
        self.assertEqual(payload, snapshot)
        self.assertEqual(request, request_snapshot)
        raw = json.loads(Path(archive._state_file).read_text())
        self.assertEqual(raw['schema'], 'wafer-archive-v1')
        json.dumps(archive.current_result(), sort_keys=True,
                   ensure_ascii=False, allow_nan=False)


class QuotaAndBaselineTests(ArchiveTestBase):
    def test_local_pins_are_canonical_inverse_images(self):
        # Two candidates whose FORWARD images of die (-4,-4) coincide at
        # (4,-4) while their inverse local preimages differ: mirror-x gives
        # local (4,-4), r90 gives local (-4,4).  Issuance must compare the
        # inverse frame (the station's local coordinates), where reports live.
        sites = [[x, y] for y in range(-4, 5) for x in range(-4, 5)]
        r90 = [[0, -1], [1, 0]]
        payload = {
            'wafer': 'ROT', 'sites': sites,
            'runs': [
                {'id': 'A', 'bins': {'P': 'pass'},
                 'records': [{'at': [x, y], 'bin': 'P'}
                             for x in range(-3, 1) for y in range(-2, 2)],
                 'candidates': [{'id': 'a', 'matrix': ID, 'offset': ZERO}]},
                {'id': 'B', 'bins': {'P': 'pass'},
                 'records': [{'at': [x, y], 'bin': 'P'}
                             for x in range(-3, 1) for y in range(-2, 2)],
                 'candidates': [{'id': 'mx', 'matrix': MX, 'offset': ZERO},
                                {'id': 'r90', 'matrix': r90, 'offset': ZERO}]}],
            # Origin-only link cannot distinguish the two gauges: both stay
            # supported, so an ambiguous group must be refused.
            'links': [{'left': {'run': 'A', 'at': [0, 0]},
                       'right': {'run': 'B', 'at': [0, 0]}}]}
        archive = self.open_archive(payload=payload, name='rot')
        with self.assertRaises(ValueError):
            archive.issue_order(
                {'order_id': 'ambiguous', 'run_id': 'B', 'dies': [[-4, -4]]})
        # A die whose inverse preimage coincides across both gauges remains
        # issuable, and the dispatched pin is exactly the inverse image.
        receipt = archive.issue_order(
            {'order_id': 'origin', 'run_id': 'B', 'dies': [[0, 0]]})
        self.assertEqual(receipt['local_pins'], [[0, 0]])
        # Pin the r90 gauge with a non-origin benchmark.
        pinned = copy.deepcopy(payload)
        pinned['links'].append(
            {'left': {'run': 'A', 'at': [1, 0]},
             'right': {'run': 'B', 'at': [0, -1]}})
        archive.submit_revision('pinned', pinned['links'])
        supported = {item['run']: item['candidates']
                     for item in archive.current_result()['supported_candidates']}
        self.assertEqual(supported['B'], ['r90'])
        # The old origin order still holds (origin maps to itself).
        self.assertTrue(
            order_status(archive.current_result(), 'origin')['scope_holds'])
        # The formerly ambiguous die is now provable: local pin is r90's
        # inverse image (-4,4), never the coincident forward image (4,-4).
        new_receipt = archive.issue_order(
            {'order_id': 'now-clear', 'run_id': 'B', 'dies': [[-4, -4]]})
        self.assertEqual(new_receipt['local_pins'], [[-4, 4]])
        with self.assertRaises(ValueError):
            archive.receive_report(
                'now-clear', [{'at': [4, -4], 'bin': 'P'}])
        archive.receive_report(
            'now-clear', [{'at': [-4, 4], 'bin': 'P'}])

    def test_initial_archive_matches_process_calibrated(self):
        payload = base_payload()
        standalone = process_calibrated(copy.deepcopy(payload))
        archive = self.open_archive(payload=payload)
        result = archive.current_result()
        for key in ('wafer', 'supported_candidates', 'dies', 'pick_coordinates'):
            self.assertEqual(result[key], standalone[key])
        self.assertEqual(result['work_orders'], [])
        self.assertEqual(result['effective_retests'], [])

    def test_station_and_wafer_coordinate_quotas(self):
        # Small synthetic wafer: one identity station, two records.  The
        # station union cap is enforced against first-test plus every
        # dispatched pin (distinct positions count once in the union).
        sites = [[x, 0] for x in range(4)]
        payload = {
            'wafer': 'Q', 'sites': sites,
            'runs': [{'id': 'A', 'bins': {'P': 'pass'},
                      'records': [{'at': [0, 0], 'bin': 'P'},
                                  {'at': [1, 0], 'bin': 'P'}],
                      'candidates': [{'id': 'id', 'matrix': ID,
                                      'offset': ZERO}]}],
            'links': []}
        archive = WaferArchive.create(self.root / 'q', payload, 'r0')
        # Override the station cap downward to exercise the union accounting.
        import wafer_fusion.archive as archive_module
        old = archive_module.STATION_PIN_MAX
        archive_module.STATION_PIN_MAX = 2
        try:
            # [0,0],[1,0] already occupy the union; [2,0] would exceed it.
            with self.assertRaises(ValueError):
                archive.issue_order(
                    {'order_id': 'over', 'run_id': 'A', 'dies': [[2, 0]]})
            # Re-issuing an already covered position stays inside the quota.
            receipt = archive.issue_order(
                {'order_id': 'dup', 'run_id': 'A', 'dies': [[1, 0]]})
            self.assertEqual(receipt['local_pins'], [[1, 0]])
            with self.assertRaises(ValueError):
                archive.history_order('over')
        finally:
            archive_module.STATION_PIN_MAX = old

    def test_cumulative_pin_line_budget_counts_overlaps(self):
        sites = [[x, 0] for x in range(3)]
        payload = {
            'wafer': 'Q2', 'sites': sites,
            'runs': [{'id': 'A', 'bins': {'P': 'pass'},
                      'records': [{'at': [0, 0], 'bin': 'P'}],
                      'candidates': [{'id': 'id', 'matrix': ID,
                                      'offset': ZERO}]}],
            'links': []}
        archive = WaferArchive.create(self.root / 'q2', payload, 'r0')
        import wafer_fusion.archive as archive_module
        old_lines = archive_module.PIN_LINES_TOTAL_MAX
        old_station = archive_module.STATION_PIN_MAX
        archive_module.PIN_LINES_TOTAL_MAX = 1
        archive_module.STATION_PIN_MAX = 100000
        try:
            archive.issue_order(
                {'order_id': 'a', 'run_id': 'A', 'dies': [[1, 0]]})
            # Same physical pin again: overlaps still spend line budget.
            with self.assertRaises(ValueError):
                archive.issue_order(
                    {'order_id': 'b', 'run_id': 'A', 'dies': [[1, 0]]})
            # Replaying the existing order is unaffected by the cap.
            replay = archive.issue_order(
                {'order_id': 'a', 'run_id': 'A', 'dies': [[1, 0]]})
            self.assertEqual(replay['serial'], 1)
        finally:
            archive_module.PIN_LINES_TOTAL_MAX = old_lines
            archive_module.STATION_PIN_MAX = old_station

    def test_revision_cap(self):
        archive = self.open_archive()
        import wafer_fusion.archive as archive_module
        old = archive_module.REVISIONS_MAX
        archive_module.REVISIONS_MAX = 2
        try:
            archive.submit_revision('r1', no_links())
            with self.assertRaises(ValueError):
                archive.submit_revision('r2', link_at(0, 1, 0, 1))
            # Existing revision replays still work.
            replay = archive.submit_revision('r1', no_links())
            self.assertEqual(replay['serial'], 1)
        finally:
            archive_module.REVISIONS_MAX = old

    def test_order_cap(self):
        archive = self.open_archive()
        import wafer_fusion.archive as archive_module
        old = archive_module.ORDERS_MAX
        archive_module.ORDERS_MAX = 1
        try:
            archive.issue_order(
                {'order_id': 'a', 'run_id': 'B', 'dies': [DIE_O]})
            with self.assertRaises(ValueError):
                archive.issue_order(
                    {'order_id': 'b', 'run_id': 'B', 'dies': [DIE_O]})
        finally:
            archive_module.ORDERS_MAX = old

    def test_whole_wafer_union_quota_sums_per_station_unions(self):
        sites = [[x, 0] for x in range(6)]
        payload = {
            'wafer': 'QW', 'sites': sites,
            'runs': [
                {'id': 'A', 'bins': {'P': 'pass'},
                 'records': [{'at': [0, 0], 'bin': 'P'}],
                 'candidates': [{'id': 'id', 'matrix': ID, 'offset': ZERO}]},
                {'id': 'B', 'bins': {'P': 'pass'},
                 'records': [{'at': [0, 0], 'bin': 'P'}],
                 'candidates': [{'id': 'id', 'matrix': ID, 'offset': ZERO}]}],
            'links': []}
        archive = WaferArchive.create(self.root / 'qw', payload, 'r0')
        import wafer_fusion.archive as archive_module
        old_station = archive_module.STATION_PIN_MAX
        old_wafer = archive_module.WAFER_PIN_MAX
        old_lines = archive_module.PIN_LINES_TOTAL_MAX
        archive_module.STATION_PIN_MAX = 3
        archive_module.WAFER_PIN_MAX = 5
        archive_module.PIN_LINES_TOTAL_MAX = 100000
        try:
            archive.issue_order(
                {'order_id': 'a1', 'run_id': 'A', 'dies': [[1, 0]]})
            # Same physical pin again: distinct-position union is unchanged.
            archive.issue_order(
                {'order_id': 'a2', 'run_id': 'A', 'dies': [[1, 0]]})
            archive.issue_order(
                {'order_id': 'b1', 'run_id': 'B', 'dies': [[1, 0]]})
            archive.issue_order(
                {'order_id': 'b2', 'run_id': 'B', 'dies': [[2, 0]]})
            # B station union would reach 4 > 3.
            with self.assertRaises(ValueError):
                archive.issue_order(
                    {'order_id': 'b3', 'run_id': 'B', 'dies': [[3, 0]]})
            # Wafer union is A{0,1} + B{0,1,2} = 5; one more distinct A pin
            # pushes it to 6 even though the position exists at B.
            archive_module.STATION_PIN_MAX = 100
            with self.assertRaises(ValueError):
                archive.issue_order(
                    {'order_id': 'a3', 'run_id': 'A', 'dies': [[2, 0]]})
            # A pin already inside A's union stays within every quota.
            replay = archive.issue_order(
                {'order_id': 'a1', 'run_id': 'A', 'dies': [[1, 0]]})
            self.assertEqual(replay['serial'], 1)
        finally:
            archive_module.STATION_PIN_MAX = old_station
            archive_module.WAFER_PIN_MAX = old_wafer
            archive_module.PIN_LINES_TOTAL_MAX = old_lines

    def test_unknown_run_and_undelared_die_rejected(self):
        archive = self.open_archive()
        with self.assertRaises(ValueError):
            archive.issue_order(
                {'order_id': 'o', 'run_id': 'Z', 'dies': [DIE_O]})
        with self.assertRaises(ValueError):
            archive.issue_order(
                {'order_id': 'o', 'run_id': 'B', 'dies': [[5, 5]]})
        with self.assertRaises(ValueError):
            archive.issue_order(
                {'order_id': 'o', 'run_id': 'B', 'dies': []})


if __name__ == '__main__':
    unittest.main()
