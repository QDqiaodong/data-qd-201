"""Acceptance tests for the offline first-test handoff ledger.

Everything is synthetic: no probe station is connected.  Run with the package
on the path, e.g. ``PYTHONPATH=src python -m unittest tests.test_handoff``.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from wafer_fusion import FirstTestLedger, WaferArchive, process_calibrated
from wafer_fusion import handoff as handoff_module

IDENTITY = [[1, 0], [0, 1]]
ZERO = [0, 0]
CHANNEL = 'CH'
SITES = [[0, 0], [1, 0]]


def station(run_id, bins=None):
    return {'id': run_id,
            'bins': bins or {'P': 'pass', 'F': 'fail', 'S': 'skip'},
            'candidates': [{'id': 'id', 'matrix': IDENTITY, 'offset': ZERO}]}


def link(ax=0, ay=0, bx=0, by=0):
    return [{'left': {'run': 'W1', 'at': [ax, ay]},
             'right': {'run': 'W2', 'at': [bx, by]}}]


def make_config(wafer, sites=None, runs=None, links=None, extra_run=None):
    runs = runs or [station('W1'), station('W2')]
    if extra_run:
        runs[-1].update(extra_run)
    return {'wafer': wafer,
            'sites': copy.deepcopy(sites if sites is not None else SITES),
            'runs': copy.deepcopy(runs),
            'links': copy.deepcopy(links if links is not None else link())}


def source(stream, seq):
    return {'stream': stream, 'seq': seq}


def seal_request(config, sources):
    request = copy.deepcopy(config)
    request['sources'] = copy.deepcopy(sources)
    return request


def reading(stream, seq, wafer, run, at, code):
    return {'stream': stream, 'seq': seq, 'wafer': wafer, 'run': run,
            'at': list(at), 'bin': code}


class LedgerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def open_ledger(self, name='ledger', limit=2):
        return FirstTestLedger.create(self.root / name, limit)

    def reopen(self, name='ledger'):
        return FirstTestLedger(self.root / name)

    # The two shared acceptance wafers deliberately occupy the same local
    # coordinates on every station; only (stream, seq) tells them apart.
    def x_rows(self, stream=CHANNEL):
        return [reading(stream, 1, 'X', 'W1', [0, 0], 'P'),
                reading(stream, 3, 'X', 'W2', [1, 0], 'F')]

    def y_rows(self, stream=CHANNEL):
        return [reading(stream, 2, 'Y', 'W1', [0, 0], 'F'),
                reading(stream, 4, 'Y', 'W2', [1, 0], 'P')]

    def x_request(self):
        return seal_request(make_config('X'),
                            {'W1': [source(CHANNEL, 1)],
                             'W2': [source(CHANNEL, 3)]})

    def y_request(self):
        return seal_request(make_config('Y'),
                            {'W1': [source(CHANNEL, 2)],
                             'W2': [source(CHANNEL, 4)]})


class AcceptanceFlowTests(LedgerTestBase):
    """The exact acceptance scenario: interleaved X/Y on one channel."""

    def test_full_flow(self):
        ledger = self.open_ledger(limit=2)

        # Y arrives out of order and fills the shared pending quota.
        receipt = ledger.receive([self.y_rows()[1]])
        self.assertEqual(receipt, {'accepted': 1, 'replayed': 0,
                                   'pending_rows': 1})
        ledger.receive([self.y_rows()[0]])
        self.assertEqual(ledger.pending_rows, 2)

        # Seal Y: the quota is released immediately, the package is usable,
        # and the channel cursor cannot move past the missing seq 1.
        package_y = ledger.seal(self.y_request())
        self.assertEqual(package_y['schema'], 'wafer-first-test-handoff-v1')
        self.assertEqual(package_y['wafer'], 'Y')
        self.assertEqual(ledger.pending_rows, 0)
        self.assertEqual(ledger.handoff('Y'), package_y)
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)
        archive = WaferArchive.create(
            self.root / 'y-archive', copy.deepcopy(package_y['payload']),
            'rev0')
        self.assertEqual(archive.current_result()['wafer'], 'Y')
        self.assertEqual(
            [row['at'] for row in package_y['payload']['runs'][0]['records']],
            [[0, 0]])
        self.assertEqual(package_y['lineage'], [
            {'run': 'W1', 'at': [0, 0], 'stream': CHANNEL, 'seq': 2},
            {'run': 'W2', 'at': [1, 0], 'stream': CHANNEL, 'seq': 4}])

        # X's rows now fit the freed quota; 1..4 are all received but seq 1
        # is not sealed, so the cursor still stays at zero.
        self.assertEqual(ledger.receive([self.x_rows()[1]])['pending_rows'], 1)
        ledger.receive([self.x_rows()[0]])
        self.assertEqual(ledger.pending_rows, 2)
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)

        # Replaying Y's original row costs no quota; altering its bin is a
        # conflicting identity and is rejected without side effects.
        replay = ledger.receive([self.y_rows()[1]])
        self.assertEqual(replay, {'accepted': 0, 'replayed': 1,
                                  'pending_rows': 2})
        altered = reading(CHANNEL, 4, 'Y', 'W2', [1, 0], 'F')
        with self.assertRaises(ValueError):
            ledger.receive([altered])
        self.assertEqual(ledger.pending_rows, 2)
        self.assertEqual(ledger.handoff('Y'), package_y)

        # Inject an interruption right before X's commit.
        original_replace = FirstTestLedger._atomic_replace
        original_unlink = Path.unlink

        def fail_replace(self, temporary, target):
            raise OSError('simulated write interruption')

        FirstTestLedger._atomic_replace = fail_replace
        Path.unlink = lambda self, *args, **kwargs: None
        try:
            with self.assertRaises(OSError):
                ledger.seal(self.x_request())
        finally:
            FirstTestLedger._atomic_replace = original_replace
            Path.unlink = original_unlink

        interrupted = self.reopen()
        with self.assertRaises(ValueError):
            interrupted.handoff('X')
        self.assertEqual(interrupted.cleanup_cursor(CHANNEL), 0)
        self.assertEqual(interrupted.pending_rows, 2)  # quota not released
        self.assertEqual(interrupted.handoff('Y'), package_y)
        # Leftover staged files are never mistaken for state.
        self.assertFalse(list(interrupted.path.glob('.ledger.json.*.tmp')))

        # Commit X, then pretend the reply was lost.
        package_x = interrupted.seal(self.x_request())
        lost = self.reopen()
        self.assertEqual(lost.cleanup_cursor(CHANNEL), 4)
        self.assertEqual(lost.pending_rows, 0)
        self.assertEqual(lost.handoff('X'), package_x)
        # An equivalent retry needs no upstream resend.
        self.assertEqual(lost.seal(self.x_request()), package_x)
        self.assertEqual(lost.seal(self.y_request()), package_y)
        for wafer, package, path in (
                ('X', package_x, 'x-archive'), ('Y', package_y, 'y2-archive')):
            archived = WaferArchive.create(
                self.root / path, copy.deepcopy(package['payload']), 'rev0')
            result = archived.current_result()
            self.assertEqual(result['wafer'], wafer)
            self.assertEqual([line['seq'] for line in package['lineage']],
                             [1, 3] if wafer == 'X' else [2, 4])

        # The device may only be acknowledged inside the earned prefix and
        # only forwards.
        with self.assertRaises(ValueError):
            lost.confirm_cleanup(CHANNEL, 5)
        self.assertEqual(lost.confirm_cleanup(CHANNEL, 4),
                         {'stream': CHANNEL, 'cursor': 4})
        self.assertEqual(lost.confirmed_cleanup(CHANNEL), 4)
        self.assertEqual(self.reopen().confirmed_cleanup(CHANNEL), 4)
        self.assertEqual(lost.confirm_cleanup(CHANNEL, 4),
                         {'stream': CHANNEL, 'cursor': 4})
        with self.assertRaises(ValueError):
            lost.confirm_cleanup(CHANNEL, 3)

    def test_cleanup_needs_sealed_prefix_not_just_received(self):
        ledger = self.open_ledger(limit=2)
        ledger.receive(self.y_rows())
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)
        with self.assertRaises(ValueError):
            ledger.confirm_cleanup(CHANNEL, 1)
        ledger.seal(self.y_request())
        # Sealed 2 and 4 but seq 1 is still absent.
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)
        with self.assertRaises(ValueError):
            ledger.confirm_cleanup(CHANNEL, 2)


class ReceiveTests(LedgerTestBase):
    def test_pending_limit_is_shared_and_batches_are_atomic(self):
        ledger = self.open_ledger(limit=2)
        ledger.receive(self.y_rows())
        with self.assertRaises(ValueError):
            ledger.receive([reading(CHANNEL, 6, 'Y', 'W1', [1, 0], 'P')])
        self.assertEqual(ledger.pending_rows, 2)
        # A batch holding one replay and one new row still overflows whole.
        batch = [self.y_rows()[0],
                 reading(CHANNEL, 6, 'Y', 'W1', [1, 0], 'P')]
        with self.assertRaises(ValueError):
            ledger.receive(batch)
        self.assertEqual(ledger.pending_rows, 2)
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)

    def test_sealed_wafer_accepts_replays_but_no_new_identity(self):
        ledger = self.open_ledger(limit=4)
        ledger.receive(self.y_rows())
        ledger.seal(self.y_request())
        self.assertEqual(ledger.pending_rows, 0)
        with self.assertRaises(ValueError):
            ledger.receive([reading(CHANNEL, 6, 'Y', 'W1', [1, 0], 'P')])
        self.assertEqual(ledger.pending_rows, 0)
        self.assertEqual(
            ledger.receive([self.y_rows()[0]])['replayed'], 1)
        # Retried rows must still match their original content exactly.
        with self.assertRaises(ValueError):
            ledger.receive([reading(CHANNEL, 2, 'Y', 'W1', [0, 0], 'P')])

    def test_duplicate_identity_inside_one_batch(self):
        ledger = self.open_ledger(limit=4)
        row = self.y_rows()[0]
        self.assertEqual(
            ledger.receive([row, copy.deepcopy(row)]),
            {'accepted': 1, 'replayed': 1, 'pending_rows': 1})
        conflicting = reading(CHANNEL, 2, 'Y', 'W1', [0, 0], 'P')
        with self.assertRaises(ValueError):
            ledger.receive([conflicting, reading(CHANNEL, 2, 'Y', 'W1',
                                                 [0, 0], 'F')])
        self.assertEqual(ledger.pending_rows, 1)

    def test_invalid_reading_fields_rejected_without_mutation(self):
        ledger = self.open_ledger(limit=4)
        bad_rows = [
            {'stream': CHANNEL, 'seq': 1, 'wafer': 'X', 'run': 'W1',
             'at': [0, 0]},
            reading(CHANNEL, 0, 'X', 'W1', [0, 0], 'P'),
            reading(CHANNEL, -1, 'X', 'W1', [0, 0], 'P'),
            reading(CHANNEL, 1 << 63, 'X', 'W1', [0, 0], 'P'),
            reading(CHANNEL, True, 'X', 'W1', [0, 0], 'P'),
            reading('bad id!', 1, 'X', 'W1', [0, 0], 'P'),
            reading(CHANNEL, 1, 'X', 'W1', [0], 'P'),
            reading(CHANNEL, 1, 'X', 'W1', [0, 0], 'P', ) | {'extra': 1},
        ]
        for row in bad_rows:
            with self.assertRaises(ValueError):
                ledger.receive([row])
        self.assertEqual(ledger.pending_rows, 0)

    def test_receive_does_not_mutate_callers(self):
        ledger = self.open_ledger(limit=4)
        rows = self.y_rows()
        snapshot = copy.deepcopy(rows)
        ledger.receive(rows)
        self.assertEqual(rows, snapshot)


class SealValidationTests(LedgerTestBase):
    def test_missing_row_wrong_station_and_foreign_identity(self):
        ledger = self.open_ledger(limit=4)
        ledger.receive(self.y_rows())
        # Missing one received row.
        partial = seal_request(make_config('Y'),
                               {'W1': [source(CHANNEL, 2)], 'W2': []})
        with self.assertRaises(ValueError):
            ledger.seal(partial)
        # Every station still needs a list.
        no_station = seal_request(make_config('Y'),
                                  {'W1': [source(CHANNEL, 2),
                                          source(CHANNEL, 4)]})
        with self.assertRaises(ValueError):
            ledger.seal(no_station)
        # Rows listed under the wrong station.
        swapped = seal_request(make_config('Y'),
                               {'W1': [source(CHANNEL, 4)],
                                'W2': [source(CHANNEL, 2)]})
        with self.assertRaises(ValueError):
            ledger.seal(swapped)
        # A station not declared by the configuration is rejected.
        extra_station = seal_request(
            make_config('Y'),
            {'W1': [source(CHANNEL, 2)], 'W2': [source(CHANNEL, 4)],
             'W3': []})
        with self.assertRaises(ValueError):
            ledger.seal(extra_station)
        # An identity never received for this wafer.
        foreign = seal_request(make_config('Y'),
                               {'W1': [source(CHANNEL, 2),
                                       source(CHANNEL, 6)],
                                'W2': [source(CHANNEL, 4)]})
        with self.assertRaises(ValueError):
            ledger.seal(foreign)
        # Same identity listed twice, even across stations.
        twice = seal_request(make_config('Y'),
                             {'W1': [source(CHANNEL, 2)],
                              'W2': [source(CHANNEL, 2),
                                     source(CHANNEL, 4)]})
        with self.assertRaises(ValueError):
            ledger.seal(twice)
        # None of the refused requests sealed the wafer or moved the cursor.
        with self.assertRaises(ValueError):
            ledger.handoff('Y')
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)
        self.assertEqual(ledger.pending_rows, 2)
        # The complete request still seals normally afterwards.
        ledger.seal(self.y_request())
        self.assertEqual(ledger.pending_rows, 0)

    def test_coincident_local_positions_are_rejected_even_with_equal_bins(self):
        ledger = self.open_ledger(limit=4)
        rows = [
            reading(CHANNEL, 1, 'V', 'W1', [0, 0], 'P'),
            reading(CHANNEL, 2, 'V', 'W1', [0, 0], 'P'),
            reading(CHANNEL, 3, 'V', 'W2', [1, 0], 'P'),
        ]
        ledger.receive(rows)
        collision = seal_request(make_config('V'),
                                 {'W1': [source(CHANNEL, 1),
                                         source(CHANNEL, 2)],
                                  'W2': [source(CHANNEL, 3)]})
        with self.assertRaises(ValueError):
            ledger.seal(collision)
        with self.assertRaises(ValueError):
            ledger.handoff('V')
        self.assertEqual(ledger.pending_rows, 3)
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)

    def test_empty_station_list_seals_normally(self):
        ledger = self.open_ledger(limit=2)
        ledger.receive([reading(CHANNEL, 1, 'E', 'W1', [0, 0], 'P')])
        package = ledger.seal(seal_request(
            make_config('E'),
            {'W1': [source(CHANNEL, 1)], 'W2': []}))
        self.assertEqual(package['payload']['runs'][1]['records'], [])
        self.assertEqual(package['lineage'], [
            {'run': 'W1', 'at': [0, 0], 'stream': CHANNEL, 'seq': 1}])
        self.assertEqual(ledger.pending_rows, 0)
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 1)

    def test_identity_of_another_wafer_cannot_be_listed(self):
        ledger = self.open_ledger(limit=4)
        ledger.receive(self.x_rows())
        ledger.receive(self.y_rows())
        mixed = seal_request(
            make_config('X'),
            {'W1': [source(CHANNEL, 1)],
             'W2': [source(CHANNEL, 4)]})
        with self.assertRaises(ValueError):
            ledger.seal(mixed)
        self.assertEqual(ledger.pending_rows, 4)

    def test_unknown_bin_and_infeasible_calibration_are_rejected(self):
        ledger = self.open_ledger(limit=4)
        ledger.receive(self.y_rows())
        bad_bin = seal_request(make_config('Y'),
                               {'W1': [source(CHANNEL, 2)],
                                'W2': [source(CHANNEL, 4)]})
        bad_bin['runs'][0]['bins'] = {'P': 'pass'}
        with self.assertRaises(ValueError):
            ledger.seal(bad_bin)

        impossible = seal_request(
            make_config('Y', links=link(0, 0, 0, 1)),
            {'W1': [source(CHANNEL, 2)], 'W2': [source(CHANNEL, 4)]})
        with self.assertRaises(ValueError):
            ledger.seal(impossible)
        self.assertEqual(ledger.pending_rows, 2)
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)
        with self.assertRaises(ValueError):
            ledger.handoff('Y')

    def test_config_with_records_is_not_a_seal_configuration(self):
        ledger = self.open_ledger(limit=2)
        ledger.receive(self.y_rows())
        request = self.y_request()
        request['runs'][0]['records'] = []
        with self.assertRaises(ValueError):
            ledger.seal(request)

    def test_handoff_unknown_wafer_and_invalid_identifiers(self):
        ledger = self.open_ledger(limit=2)
        with self.assertRaises(ValueError):
            ledger.handoff('NOPE')
        with self.assertRaises(ValueError):
            ledger.cleanup_cursor('bad id!')
        with self.assertRaises(ValueError):
            ledger.handoff('bad id!')


class PackageTests(LedgerTestBase):
    def test_package_is_json_serialisable_and_immutable(self):
        ledger = self.open_ledger(limit=2)
        ledger.receive(self.y_rows())
        package = ledger.seal(self.y_request())
        encoded = json.dumps(package, sort_keys=True, allow_nan=False)
        self.assertEqual(json.loads(encoded), package)
        package['payload']['runs'][0]['records'][0]['bin'] = 'S'
        package['lineage'].clear()
        self.assertNotEqual(ledger.handoff('Y'), package)
        self.assertEqual(len(ledger.handoff('Y')['lineage']), 2)
        # The reconstructed payload is accepted by the plain calibration API.
        rendered = process_calibrated(
            copy.deepcopy(ledger.handoff('Y')['payload']))
        self.assertEqual(rendered['schema'], 'wafer-calibration-v1')

    def test_lineage_and_records_follow_station_then_y_then_x(self):
        sites = [[1, 0], [0, 1], [2, 0], [0, 2]]
        runs = [station('W1'), station('W2')]
        config = make_config('O', sites=sites, runs=runs,
                             links=link(0, 0, 0, 0))
        ledger = self.open_ledger(limit=8)
        ledger.receive([
            reading(CHANNEL, 7, 'O', 'W2', [0, 2], 'P'),
            reading(CHANNEL, 5, 'O', 'W1', [0, 1], 'F'),
            reading(CHANNEL, 8, 'O', 'W2', [2, 0], 'S'),
            reading(CHANNEL, 6, 'O', 'W1', [1, 0], 'P'),
        ])
        request = seal_request(
            config, {'W2': [source(CHANNEL, 8), source(CHANNEL, 7)],
                     'W1': [source(CHANNEL, 5), source(CHANNEL, 6)]})
        package = ledger.seal(request)
        self.assertEqual(package['lineage'], [
            {'run': 'W1', 'at': [1, 0], 'stream': CHANNEL, 'seq': 6},
            {'run': 'W1', 'at': [0, 1], 'stream': CHANNEL, 'seq': 5},
            {'run': 'W2', 'at': [2, 0], 'stream': CHANNEL, 'seq': 8},
            {'run': 'W2', 'at': [0, 2], 'stream': CHANNEL, 'seq': 7},
        ])
        self.assertEqual(
            [[row['at'], row['bin']]
             for row in package['payload']['runs'][0]['records']],
            [[[1, 0], 'P'], [[0, 1], 'F']])
        self.assertEqual(
            [[row['at'], row['bin']]
             for row in package['payload']['runs'][1]['records']],
            [[[2, 0], 'S'], [[0, 2], 'P']])
        # Station declaration order wins over the order in ``sources``.
        self.assertEqual([line['run'] for line in package['lineage']],
                         ['W1', 'W1', 'W2', 'W2'])

    def test_seal_replay_signature_and_configuration_order(self):
        ledger = self.open_ledger(limit=4)
        ledger.receive(self.y_rows())
        first = ledger.seal(self.y_request())

        # Source lists are sets: their order is irrelevant to a replay.
        reordered = seal_request(
            make_config('Y'),
            {'W2': [source(CHANNEL, 4)], 'W1': [source(CHANNEL, 2)]})
        self.assertEqual(ledger.seal(reordered), first)
        self.assertEqual(ledger.pending_rows, 0)

        # Array order is part of the configuration: swapped runs or sites
        # are a different request and must be refused.
        swapped_runs = seal_request(
            make_config('Y', runs=[station('W2'), station('W1')]),
            {'W1': [source(CHANNEL, 2)], 'W2': [source(CHANNEL, 4)]})
        with self.assertRaises(ValueError):
            ledger.seal(swapped_runs)
        swapped_sites = seal_request(
            make_config('Y', sites=[[1, 0], [0, 0]]),
            {'W1': [source(CHANNEL, 2)], 'W2': [source(CHANNEL, 4)]})
        with self.assertRaises(ValueError):
            ledger.seal(swapped_sites)
        different_sources = seal_request(
            make_config('Y'),
            {'W1': [source(CHANNEL, 2), source(CHANNEL, 4)],
             'W2': []})
        with self.assertRaises(ValueError):
            ledger.seal(different_sources)
        self.assertEqual(ledger.handoff('Y'), first)

    def test_seal_does_not_mutate_callers(self):
        ledger = self.open_ledger(limit=4)
        ledger.receive(self.y_rows())
        request = self.y_request()
        snapshot = copy.deepcopy(request)
        ledger.seal(request)
        self.assertEqual(request, snapshot)


class CursorAndChannelTests(LedgerTestBase):
    def test_independent_channel_is_not_blocked_by_the_gap(self):
        ledger = self.open_ledger(limit=2)
        ledger.receive([
            reading('CL', 1, 'Q', 'W1', [0, 0], 'P'),
            reading('CL', 2, 'Q', 'W2', [1, 0], 'P')])
        ledger.seal(seal_request(
            make_config('Q'),
            {'W1': [source('CL', 1)], 'W2': [source('CL', 2)]}))
        self.assertEqual(ledger.cleanup_cursor('CL'), 2)
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)
        ledger.receive(self.y_rows())
        ledger.seal(self.y_request())
        self.assertEqual(ledger.cleanup_cursor('CL'), 2)
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)
        self.assertEqual(ledger.confirm_cleanup('CL', 2),
                         {'stream': 'CL', 'cursor': 2})

    def test_cursor_skips_huge_absent_gaps_without_enumeration(self):
        ledger = self.open_ledger(limit=4)
        big = handoff_module.SEQ_MAX
        ledger.receive([
            reading(CHANNEL, 1, 'G', 'W1', [0, 0], 'P'),
            reading(CHANNEL, big, 'G', 'W2', [1, 0], 'P')])
        ledger.seal(seal_request(
            make_config('G'),
            {'W1': [source(CHANNEL, 1)], 'W2': [source(CHANNEL, big)]}))
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 1)
        with self.assertRaises(ValueError):
            ledger.confirm_cleanup(CHANNEL, big)

    def test_cursor_spans_multiple_packages_in_arrival_order(self):
        ledger = self.open_ledger(limit=4)
        ledger.receive(self.y_rows())                 # 2, 4 sealed first
        ledger.seal(self.y_request())
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 0)
        ledger.receive([
            reading(CHANNEL, 1, 'X', 'W1', [0, 0], 'P'),
            reading(CHANNEL, 3, 'X', 'W2', [1, 0], 'F')])
        ledger.seal(self.x_request())
        self.assertEqual(ledger.cleanup_cursor(CHANNEL), 4)


class LedgerLifecycleTests(LedgerTestBase):
    def test_create_reopen_and_limit(self):
        ledger = FirstTestLedger.create(self.root / 'l', 3)
        self.assertEqual(ledger.pending_limit, 3)
        again = FirstTestLedger.create(self.root / 'l', 3)
        self.assertEqual(again.pending_limit, 3)
        with self.assertRaises(ValueError):
            FirstTestLedger.create(self.root / 'l', 4)
        with self.assertRaises(ValueError):
            FirstTestLedger.create(self.root / 'l', 0)
        with self.assertRaises(ValueError):
            FirstTestLedger(self.root / 'missing')

    def test_state_file_is_json(self):
        ledger = self.open_ledger(limit=2)
        ledger.receive(self.y_rows())
        raw = json.loads(Path(ledger._state_file).read_text())
        self.assertEqual(raw['schema'], 'wafer-first-test-ledger-v1')
        self.assertEqual(raw['pending_limit'], 2)


if __name__ == '__main__':
    unittest.main()
