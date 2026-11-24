"""Offline first-test handoff ledger (standard library only).

A probe station first writes its raw readings into a device buffer and then
forwards them in batches and out of order.  One transmission channel can
interleave readings of several wafers, so a reading is identified inside the
ledger by its source identity ``(stream, seq)`` rather than by arrival order.

The ledger receives those readings, seals one wafer at a time into an
immutable, JSON-serialisable handoff package, and tracks the single
contiguous cleanup cursor the device is allowed to delete.  A package is
self-contained: it carries the complete first-test payload accepted by
:func:`wafer_fusion.process_calibrated` / :meth:`wafer_fusion.WaferArchive.create`
and one lineage row per received reading.

Durability follows the archive conventions: one state file replaced through a
same-directory temporary file, ``fsync`` and an atomic rename, so a rejected
call or an interruption before the commit never exposes a half package, a
freed quota row or an advanced cleanup cursor.
"""
import copy
import json
import os
import tempfile
from pathlib import Path

from .calibration import (
    RUNS_MAX,
    _Engine,
    _coord,
    _parse,
)
from .checks import array, fields, ident, integer, unique

LEDGER_SCHEMA = 'wafer-first-test-ledger-v1'
PACKAGE_SCHEMA = 'wafer-first-test-handoff-v1'
STATE_FILENAME = 'ledger.json'
_TMP_PREFIX = '.ledger.json.'
_TMP_SUFFIX = '.tmp'

SEQ_MAX = (1 << 63) - 1

_WAFER_LINE_FIELDS = ('wafer', 'run', 'at', 'bin')


def _positive_int(value, label):
    """A positive integer with no upper quota (the row limit may be large)."""
    if type(value) is not int or value < 1:
        raise ValueError(f'positive integer {label} required')
    return value


def _parse_reading(value):
    """Validate one device reading without touching the caller's object."""
    fields(value, ('stream', 'seq', 'wafer', 'run', 'at', 'bin'))
    stream = ident(value['stream'])
    seq = integer(value['seq'], 1, SEQ_MAX)
    wafer = ident(value['wafer'])
    run = ident(value['run'])
    at = list(_coord(value['at']))
    code = ident(value['bin'])
    return {'stream': stream, 'seq': seq, 'wafer': wafer, 'run': run,
            'at': at, 'bin': code}


def _content(reading):
    return {name: reading[name] for name in _WAFER_LINE_FIELDS}


def _reading_key(reading):
    return (reading['stream'], reading['seq'])


def _normalise_sources(sources, run_ids):
    """Validate the per-station source lists and return a replay signature."""
    if not isinstance(sources, dict):
        raise ValueError('sources must map every station to a source list')
    if set(sources.keys()) != set(run_ids):
        raise ValueError('every station must have exactly one source list')
    listed = []
    seen = set()
    for run_id in run_ids:
        items = sources[run_id]
        if not isinstance(items, list):
            raise ValueError('source list must be an array')
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('source identity must be an object')
            fields(item, ('stream', 'seq'))
            key = (ident(item['stream']), integer(item['seq'], 1, SEQ_MAX))
            if key in seen:
                raise ValueError('source identity listed more than once')
            seen.add(key)
            listed.append((run_id, key))
    signature = [[run_id, key[0], key[1]]
                 for run_id, key in sorted(listed,
                                           key=lambda item: (item[0],
                                                             item[1][0],
                                                             item[1][1]))]
    return listed, signature


class FirstTestLedger:
    """One on-disk receive ledger, driven sequentially by a single writer."""

    # ------------------------------------------------------------------ open

    def __init__(self, path):
        self.path = Path(path)
        state_file = self.path / STATE_FILENAME
        if not state_file.is_file():
            raise ValueError('not a first-test ledger directory')
        self._state_file = state_file
        self._cleanup_temp_files()
        with open(state_file, 'rb') as handle:
            state = json.loads(handle.read().decode('utf-8'))
        if (not isinstance(state, dict) or state.get('schema') != LEDGER_SCHEMA
                or type(state.get('pending_limit')) is not int
                or state['pending_limit'] < 1
                or not isinstance(state.get('readings'), list)
                or not isinstance(state.get('sealed'), list)
                or not isinstance(state.get('cleanups'), dict)):
            raise ValueError('unrecognised ledger state')
        self._state = state
        self._reindex()

    @classmethod
    def create(cls, path, pending_limit):
        """Create a ledger with a shared pending-row limit, or reopen it.

        Reopening an existing ledger through ``create`` is only allowed with
        the same pending limit; a different limit is rejected rather than
        silently changing the quota of already received rows.
        """
        pending_limit = _positive_int(pending_limit, 'pending row limit')
        directory = Path(path)
        state_file = directory / STATE_FILENAME
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if state_file.is_file():
                ledger = cls(directory)
                if ledger._state['pending_limit'] != pending_limit:
                    raise ValueError('ledger already exists with a different '
                                     'pending limit')
                return ledger
            raise ValueError('ledger path exists without ledger state')

        state = {'schema': LEDGER_SCHEMA, 'pending_limit': pending_limit,
                 'readings': [], 'sealed': [], 'cleanups': {}}
        ledger = cls.__new__(cls)
        ledger.path = directory
        ledger._state_file = state_file
        ledger._state = state
        ledger._reindex()
        ledger._cleanup_temp_files()
        ledger._commit(state)
        return ledger

    # ------------------------------------------------------------- receiving

    def receive(self, readings):
        """Accept one batch of device readings atomically.

        A reading is ``{stream, seq, wafer, run, at, bin}``.  Order inside the
        batch is irrelevant.  Replaying an already received source identity
        with identical content is a no-op (also after sealing); conflicting
        content, a new identity of a sealed wafer, or a batch that would
        exceed the pending-row limit rejects the whole batch without side
        effects.
        """
        if isinstance(readings, dict):
            readings = [readings]
        if not isinstance(readings, list):
            raise ValueError('readings must be an array or a single reading')

        staged = {}
        replayed = 0
        for raw in readings:
            reading = _parse_reading(raw)
            key = _reading_key(reading)
            previous = staged.get(key)
            if previous is not None:
                if _content(previous) != _content(reading):
                    raise ValueError('source identity reused with different '
                                     'content')
                replayed += 1
                continue
            existing = self._readings.get(key)
            if existing is not None:
                if _content(existing) != _content(reading):
                    raise ValueError('source identity reused with different '
                                     'content')
                replayed += 1
                continue
            if reading['wafer'] in self._sealed_by_wafer:
                raise ValueError('sealed wafer accepts no new source identity')
            staged[key] = reading

        pending = self._pending + len(staged)
        if pending > self._state['pending_limit']:
            raise ValueError('pending first-test row limit exceeded')

        if staged:
            state = self._snapshot()
            state['readings'].extend(staged.values())
            state['readings'].sort(key=lambda row: (row['stream'], row['seq']))
            self._commit(state)  # reindexes from the committed state
        return {'accepted': len(staged), 'replayed': replayed,
                'pending_rows': self._pending}

    # ---------------------------------------------------------------- sealing

    def seal(self, request):
        """Seal one wafer into an immutable handoff package.

        ``request`` is the station configuration without per-run ``records``
        (``wafer``, ``sites``, ``runs`` with ``id``/``bins``/``candidates``,
        ``links``) plus ``sources``: a mapping from every station id to the
        complete list of source identities read at that station.  The lists
        must account for every reading received for the wafer exactly once.

        The reconstructed first-test payload must be accepted by the existing
        calibration entry, including feasibility and scale limits.  A replay
        for an already sealed wafer returns the original package when the
        configuration (array order included) and every station's source set
        agree; any other request for that wafer is rejected.
        """
        fields(request, ('wafer', 'sites', 'runs', 'links', 'sources'))
        wafer = ident(request['wafer'])
        config = {'wafer': request['wafer'], 'sites': request['sites'],
                  'runs': request['runs'], 'links': request['links']}

        raw_runs = array(config['runs'], 1, RUNS_MAX)
        run_ids = []
        for raw_run in raw_runs:
            if not isinstance(raw_run, dict):
                raise ValueError('station must be an object')
            # ``records`` is deliberately not accepted: it is reconstructed
            # from the source identities of this request.
            fields(raw_run, ('id', 'bins', 'candidates'))
            run_ids.append(ident(raw_run['id']))
        unique(run_ids)

        listed, signature = _normalise_sources(request['sources'], run_ids)

        sealed = self._sealed_by_wafer.get(wafer)
        if sealed is not None:
            if sealed['config'] != config or sealed['sources'] != signature:
                raise ValueError('wafer already sealed with a different request')
            return copy.deepcopy(sealed['package'])

        received = {key for key, reading in self._readings.items()
                    if reading['wafer'] == wafer}
        seen = {key for _, key in listed}
        if seen != received:
            raise ValueError('source lists must cover exactly the readings '
                             'received for this wafer')
        by_run = {run_id: [] for run_id in run_ids}
        for run_id, key in listed:
            reading = self._readings.get(key)
            if reading is None or reading['wafer'] != wafer:
                raise ValueError('source identity was not received for this '
                                 'wafer')
            if reading['run'] != run_id:
                raise ValueError('source identity listed under the wrong '
                                 'station')
            by_run[run_id].append(reading)

        payload = copy.deepcopy(config)
        package_runs = []
        lineage = []
        for raw_run in payload['runs']:
            run_id = raw_run['id']
            entries = sorted(by_run[run_id],
                             key=lambda row: (row['at'][1], row['at'][0]))
            raw_run['records'] = [{'at': list(row['at']), 'bin': row['bin']}
                                  for row in entries]
            package_runs.append(raw_run)
            for row in entries:
                lineage.append({'run': run_id, 'at': list(row['at']),
                                'stream': row['stream'], 'seq': row['seq']})
        payload['runs'] = package_runs

        # Same validation path as the calibration/archive entry points: the
        # reconstructed records must name declared bins, have no duplicate
        # local position (even with equal bins), respect the record scale
        # limits and leave at least one feasible calibration.
        parsed = _parse(payload)
        engine = _Engine(parsed)
        if not engine.feasible_global:
            raise ValueError('no calibration satisfies the benchmark links')

        package = {'schema': PACKAGE_SCHEMA, 'wafer': wafer,
                   'payload': payload, 'lineage': lineage}
        entry = {'wafer': wafer, 'config': copy.deepcopy(config),
                 'sources': signature, 'package': package}
        state = self._snapshot()
        state['sealed'].append(entry)
        self._commit(state)
        return copy.deepcopy(package)

    # ---------------------------------------------------------------- package

    def handoff(self, wafer):
        """Return the immutable handoff package of a sealed wafer."""
        wafer = ident(wafer)
        sealed = self._sealed_by_wafer.get(wafer)
        if sealed is None:
            raise ValueError('wafer is not sealed')
        return copy.deepcopy(sealed['package'])

    # --------------------------------------------------------------- cleanup

    def cleanup_cursor(self, stream):
        """Largest contiguous sealed prefix of one channel (0 when empty).

        Only readings folded into a durable package count.  Merely received
        rows, rejected seals and sequence numbers that have not arrived cannot
        be crossed.  The loop walks the present sealed sequence numbers, never
        the (possibly astronomically large) gap of absent ones.
        """
        stream = ident(stream)
        present = self._sealed_seqs.get(stream)
        if not present:
            return 0
        cursor = 0
        for seq in sorted(present):
            if seq == cursor + 1:
                cursor = seq
            else:
                break
        return cursor

    def confirm_cleanup(self, stream, cursor):
        """Record that the device may delete one channel up to ``cursor``.

        The cursor must lie inside the currently earned contiguous prefix and
        may not move backwards.  Re-confirming the acknowledged value is an
        idempotent no-op.
        """
        stream = ident(stream)
        cursor = integer(cursor, 0, SEQ_MAX)
        earned = self.cleanup_cursor(stream)
        acknowledged = self._cleanups.get(stream, 0)
        if cursor < acknowledged:
            raise ValueError('cleanup cursor cannot move backwards')
        if cursor > earned:
            raise ValueError('cleanup cursor is not yet covered by packages')
        if cursor != acknowledged:
            state = self._snapshot()
            state['cleanups'][stream] = cursor
            self._commit(state)
        return {'stream': stream, 'cursor': cursor}

    def confirmed_cleanup(self, stream):
        """Return the last cleanup cursor acknowledged for one channel."""
        stream = ident(stream)
        return self._cleanups.get(stream, 0)

    # --------------------------------------------------------------- queries

    @property
    def pending_rows(self):
        """Rows received for wafers that are not sealed yet (quota in use)."""
        return self._pending

    @property
    def pending_limit(self):
        return self._state['pending_limit']

    # ------------------------------------------------------------- internals

    def _reindex(self):
        self._readings = {}
        self._wafer_readings = {}
        for reading in self._state['readings']:
            key = (reading['stream'], reading['seq'])
            self._readings[key] = reading
            self._wafer_readings.setdefault(reading['wafer'], set()).add(key)

        self._sealed_by_wafer = {}
        self._sealed_seqs = {}
        for entry in self._state['sealed']:
            self._sealed_by_wafer[entry['wafer']] = entry
            for line in entry['package']['lineage']:
                self._sealed_seqs.setdefault(line['stream'], set()).add(
                    line['seq'])

        self._pending = sum(
            len(keys) for wafer, keys in self._wafer_readings.items()
            if wafer not in self._sealed_by_wafer)
        self._cleanups = dict(self._state['cleanups'])

    def _snapshot(self):
        return copy.deepcopy(self._state)

    def _cleanup_temp_files(self):
        for temporary in self.path.glob(_TMP_PREFIX + '*' + _TMP_SUFFIX):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _commit(self, state):
        payload = (json.dumps(state, ensure_ascii=False, sort_keys=True,
                              indent=2, allow_nan=False) + '\n').encode('utf-8')
        temporary = None
        try:
            handle, temporary_name = tempfile.mkstemp(
                dir=self.path, prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX)
            temporary = Path(temporary_name)
            with os.fdopen(handle, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._atomic_replace(temporary, self._state_file)
            directory_fd = os.open(self.path, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._state = state
        self._reindex()

    def _atomic_replace(self, temporary, target):
        os.replace(temporary, target)
