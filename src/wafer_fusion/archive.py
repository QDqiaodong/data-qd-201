"""Persistent wafer retest archives (offline, standard-library only).

An archive owns exactly one legal ``process_calibrated`` input (the first
test evidence) plus an ordered history of link revisions, dispatched work
orders and the final reports that came back.  Everything is stored in one
JSON state file replaced atomically, so a crashed or rejected write can
never leave half a revision or half a report observable to the next shift.

Calibration semantics are reused from :mod:`wafer_fusion.calibration`:

* a revision's feasible calibrations are determined solely by the first
  test records and that revision's links — retest reports never prune
  candidates;
* a work order's group is provable exactly when every *supported*
  candidate of its station maps the approved canonical die set onto the
  very same local pin set (every supported candidate belongs to at least
  one complete feasible calibration);
* fusion runs the original engine, so new readings cannot push a
  candidate off-sites, while the per-candidate label lookup uses the
  fused local record map (latest in-scope retest line wins, first test is
  the fallback, missing stays missing).
"""
import copy
import json
import os
import tempfile
from pathlib import Path

from .checks import array, fields, ident
from .calibration import (
    COORD_BOUND,
    SITES_MAX,
    RECORDS_TOTAL_MAX,
    _FAIL,
    _INCOMPLETE,
    _PASS,
    _STATUS_NAME,
    _Engine,
    _coord,
    _parse,
)

ARCHIVE_SCHEMA = 'wafer-archive-v1'
RESULT_SCHEMA = 'wafer-archive-result-v1'
STATE_FILENAME = 'archive.json'
_TMP_PREFIX = '.archive.json.'
_TMP_SUFFIX = '.tmp'

REVISIONS_MAX = 256
ORDERS_MAX = 1024
PIN_LINES_TOTAL_MAX = RECORDS_TOTAL_MAX  # 200000 cumulative dispatched rows
STATION_PIN_MAX = SITES_MAX              # 20000 per station union
WAFER_PIN_MAX = RECORDS_TOTAL_MAX        # 200000 whole-wafer union


def _as_coord(value):
    """Validate either a list payload coordinate or an internal tuple point."""
    return _coord(list(value) if isinstance(value, tuple) else value)


def _local_point(candidate, canonical_point):
    """Inverse-transform a canonical die into the station's local frame."""
    a, b, c, d, kx, ky = candidate['inv']
    sx, sy = canonical_point
    return (a * sx + b * sy + kx, c * sx + d * sy + ky)


def _canonical_coords(points):
    """Normalize an iterable of coordinate pairs to sorted y-then-x lists."""
    return [list(point) for point in sorted(
        (_as_coord(point) for point in points),
        key=lambda point: (point[1], point[0]))]


def _coord_set(points):
    result = [_as_coord(point) for point in points]
    if len(set(result)) != len(result):
        raise ValueError('duplicate coordinate')
    return set(result)


def _canonical_links(raw_links):
    """Validate shape lightly and return links in a canonical order.

    Full calibration validation happens later through ``_parse``; here we
    only need a stable, order-independent representation for replay.
    """
    normalized = []
    for raw_link in array(raw_links, 0, 256):
        fields(raw_link, ('left', 'right'))
        endpoints = []
        for side in ('left', 'right'):
            fields(raw_link[side], ('run', 'at'))
            endpoints.append((ident(raw_link[side]['run']),
                              _coord(raw_link[side]['at'])))
        normalized.append((endpoints[0], endpoints[1]))
    if len(set(normalized)) != len(normalized):
        raise ValueError('duplicate link endpoint pair')
    normalized.sort()
    return [{'left': {'run': left[0], 'at': list(left[1])},
             'right': {'run': right[0], 'at': list(right[1])}}
            for left, right in normalized]


def _links_signature(canonical_links):
    return json.dumps(canonical_links, sort_keys=True,
                      separators=(',', ':'), ensure_ascii=False)


class WaferArchive:
    """One on-disk wafer archive, opened by a single writer at a time."""

    # ------------------------------------------------------------------ open

    def __init__(self, path):
        self.path = Path(path)
        state_file = self.path / STATE_FILENAME
        if not state_file.is_file():
            raise ValueError('not a wafer archive directory')
        self._state_file = state_file
        self._cleanup_temp_files()
        with open(state_file, 'rb') as handle:
            state = json.loads(handle.read().decode('utf-8'))
        if not isinstance(state, dict) or state.get('schema') != ARCHIVE_SCHEMA:
            raise ValueError('unrecognised archive state')
        self._state = state
        # revision_id -> (parsed payload, engine, supported candidate masks)
        self._calibration_cache = {}

    @classmethod
    def create(cls, path, payload, revision_id):
        """Create a new archive for one validated first-test payload.

        Re-opening the same path with the identical payload and initial
        revision id returns the existing archive instead of overwriting it;
        any mismatch raises ``ValueError`` and leaves the archive untouched.
        """
        ident(revision_id)
        if not isinstance(payload, dict):
            raise ValueError('process_calibrated payload required')
        stored_payload = copy.deepcopy(payload)
        # A legal archive starts from a feasible, fully validated payload.
        parsed = _parse(stored_payload)
        engine = _Engine(parsed)
        if not engine.feasible_global:
            raise ValueError('no calibration satisfies the benchmark links')
        canonical_links = _canonical_links(stored_payload['links'])

        directory = Path(path)
        state_file = directory / STATE_FILENAME
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if state_file.is_file():
                archive = cls(directory)
                if (archive._state['initial'] != stored_payload
                        or archive._state['initial_revision_id'] != revision_id):
                    raise ValueError('archive already exists with different input')
                return archive
            raise ValueError('archive path exists without archive state')

        state = {
            'schema': ARCHIVE_SCHEMA,
            'wafer': parsed['wafer'],
            'initial': stored_payload,
            'initial_revision_id': revision_id,
            'current_revision_id': revision_id,
            'revisions': [{
                'revision_id': revision_id,
                'serial': 0,
                'links': canonical_links,
            }],
            'orders': [],
        }
        archive = cls.__new__(cls)
        archive.path = directory
        archive._state_file = state_file
        archive._state = state
        archive._calibration_cache = {}
        archive._cleanup_temp_files()
        archive._commit(state)
        return archive

    # ------------------------------------------------------------- revisions

    @property
    def current_revision_id(self):
        return self._state['current_revision_id']

    def submit_revision(self, revision_id, links):
        """Replace the current link correspondence with a full new revision.

        Same id plus same links (order-independent) replays the original
        receipt without changing the current revision; same id with different
        links is rejected.  Infeasible revisions are rejected and the old
        revision stays current.
        """
        ident(revision_id)
        canonical_links = _canonical_links(links)
        signature = _links_signature(canonical_links)

        for revision in self._state['revisions']:
            if revision['revision_id'] == revision_id:
                if _links_signature(revision['links']) != signature:
                    raise ValueError('revision id reused with different links')
                return self._revision_receipt(revision)

        if len(self._state['revisions']) >= REVISIONS_MAX:
            raise ValueError('revision limit reached')

        candidate_payload = copy.deepcopy(self._state['initial'])
        candidate_payload['links'] = copy.deepcopy(canonical_links)
        parsed = _parse(candidate_payload)
        engine = _Engine(parsed)
        if not engine.feasible_global:
            raise ValueError('no calibration satisfies the benchmark links')

        revision = {
            'revision_id': revision_id,
            'serial': len(self._state['revisions']),
            'links': canonical_links,
        }
        state = self._snapshot()
        state['revisions'].append(revision)
        state['current_revision_id'] = revision_id
        self._commit(state)
        return self._revision_receipt(revision)

    def _revision_receipt(self, revision):
        return {'revision_id': revision['revision_id'],
                'serial': revision['serial'],
                'links': copy.deepcopy(revision['links'])}

    # ------------------------------------------------------------- dispatch

    def issue_order(self, request):
        """Dispatch one non-empty approved die set on one existing run.

        Issuance succeeds only when, under every complete feasible
        calibration of the current revision, the same local pin set covers
        the approved set exactly.  Replaying the same id with the same
        request returns the original order; any conflicting reuse is
        rejected.
        """
        fields(request, ('order_id', 'run_id', 'dies'))
        order_id = ident(request['order_id'])
        run_id = ident(request['run_id'])
        approved = _coord_set(request['dies'])
        if not approved:
            raise ValueError('approved die set must be non-empty')

        for order in self._state['orders']:
            if order['order_id'] == order_id:
                if (order['run_id'] != run_id
                        or set(map(tuple, order['approved'])) != approved):
                    raise ValueError('order id reused with different request')
                return self._order_receipt(order)

        parsed, engine, supported_masks = self._current_calibration()
        run_index = self._run_index(parsed, run_id)
        run = parsed['runs'][run_index]
        site_set = set(parsed['sites'])
        outside = sorted(point for point in approved if point not in site_set)
        if outside:
            raise ValueError('approved coordinate is not a declared die')

        mask = supported_masks[run_index]
        surviving = run['surviving']
        pins = None
        for local, candidate_index in enumerate(surviving):
            if not ((mask >> local) & 1):
                continue
            candidate = run['candidates'][candidate_index]
            images = {_local_point(candidate, point) for point in approved}
            if any(not (-COORD_BOUND <= coord <= COORD_BOUND)
                   for point in images for coord in point):
                raise ValueError('local pin outside coordinate bounds')
            if pins is None:
                pins = images
            elif images != pins:
                raise ValueError('group is not provable under every feasible '
                                 'calibration')
        if pins is None:  # impossible for a feasible calibration, kept for safety
            raise ValueError('station has no supported candidate')

        self._check_quotas(parsed, run_index, pins)
        if len(self._state['orders']) >= ORDERS_MAX:
            raise ValueError('work order limit reached')

        order = {
            'order_id': order_id,
            'serial': len(self._state['orders']) + 1,
            'run_id': run_id,
            'approved': _canonical_coords(approved),
            'local_pins': _canonical_coords(pins),
            'revision_id': self._state['current_revision_id'],
            'report': None,
        }
        state = self._snapshot()
        state['orders'].append(order)
        self._commit(state)
        return self._order_receipt(order)

    def _order_receipt(self, order):
        return {'order_id': order['order_id'],
                'serial': order['serial'],
                'run_id': order['run_id'],
                'approved': copy.deepcopy(order['approved']),
                'local_pins': copy.deepcopy(order['local_pins']),
                'revision_id': order['revision_id']}

    # -------------------------------------------------------------- reports

    def receive_report(self, order_id, rows):
        """Archive the unique final report of a work order.

        A report must cover exactly the dispatched local positions, no more
        and no less, with bins declared at the station.  Replaying the same
        report (row order-independent) returns the original receipt.  A
        shape-correct report is archived even when the order's scope can no
        longer be proved under the current revision; it simply stays
        ineffective until the evidence recovers.
        """
        ident(order_id)
        order = self._find_order(order_id)
        parsed, _, _ = self._current_calibration()
        run_index = self._run_index(parsed, order['run_id'])
        bin_table = parsed['runs'][run_index]['bins']

        normalized = []
        seen = set()
        for item in array(rows, len(order['local_pins']), len(order['local_pins'])):
            fields(item, ('at', 'bin'))
            point = _coord(item['at'])
            code = ident(item['bin'])
            if code not in bin_table:
                raise ValueError('report bin is not declared at this station')
            if point in seen:
                raise ValueError('duplicate report position')
            seen.add(point)
            normalized.append({'at': list(point), 'bin': code})
        expected = set(map(tuple, order['local_pins']))
        if seen != expected:
            raise ValueError('report must cover the dispatched pins exactly')
        normalized.sort(key=lambda item: (item['at'][1], item['at'][0]))
        report = {'rows': normalized}

        if order['report'] is not None:
            if order['report'] != report:
                raise ValueError('work order already received a different report')
            return {'order_id': order_id, 'rows': copy.deepcopy(normalized)}

        state = self._snapshot()
        for stored in state['orders']:
            if stored['order_id'] == order_id:
                stored['report'] = copy.deepcopy(report)
                break
        self._commit(state)
        return {'order_id': order_id, 'rows': copy.deepcopy(normalized)}

    # -------------------------------------------------------------- queries

    def history_order(self, order_id):
        """Return the immutable original order and its archived raw report."""
        order = self._find_order(order_id)
        return {'order_id': order['order_id'],
                'serial': order['serial'],
                'run_id': order['run_id'],
                'approved': copy.deepcopy(order['approved']),
                'local_pins': copy.deepcopy(order['local_pins']),
                'revision_id': order['revision_id'],
                'report': copy.deepcopy(order['report'])}

    def current_result(self):
        """Fuse evidence under the current revision and return the pick map."""
        parsed, engine, supported_masks = self._current_calibration()
        run_index_by_id = {run['id']: i for i, run in enumerate(parsed['runs'])}

        statuses = []
        effective = {}  # (run index, local point) -> (serial, bin, order id)
        for order in self._state['orders']:
            holds = self._scope_holds(parsed, supported_masks, order)
            reported = order['report'] is not None
            statuses.append({
                'order_id': order['order_id'],
                'serial': order['serial'],
                'run_id': order['run_id'],
                'issued_revision_id': order['revision_id'],
                'scope_holds': holds,
                'reported': reported,
            })
            if holds and reported:
                run_index = run_index_by_id[order['run_id']]
                for row in order['report']['rows']:
                    key = (run_index, tuple(row['at']))
                    if key not in effective or order['serial'] > effective[key][0]:
                        effective[key] = (order['serial'], row['bin'],
                                          order['order_id'])

        fused_records = [dict(run['records']) for run in parsed['runs']]
        for (run_index, point), (_, code, _) in effective.items():
            fused_records[run_index][point] = parsed['runs'][run_index]['bins'][code]

        effective_lines = [
            {'run': parsed['runs'][run_index]['id'],
             'at': list(point),
             'bin': code,
             'source_order': order_id}
            for (run_index, point), (_, code, order_id) in sorted(
                effective.items(),
                key=lambda item: (item[0][0], item[0][1][1], item[0][1][0]))
        ]

        view = _calibrated_view(parsed, engine, supported_masks, fused_records)
        view['schema'] = RESULT_SCHEMA
        view['revision_id'] = self._state['current_revision_id']
        view['work_orders'] = statuses
        view['effective_retests'] = effective_lines
        return view

    # ------------------------------------------------------------- internals

    def _current_calibration(self):
        revision_id = self._state['current_revision_id']
        cached = self._calibration_cache.get(revision_id)
        if cached is not None:
            return cached
        revision = next(item for item in self._state['revisions']
                        if item['revision_id'] == revision_id)
        payload = copy.deepcopy(self._state['initial'])
        payload['links'] = copy.deepcopy(revision['links'])
        parsed = _parse(payload)
        engine = _Engine(parsed)
        if not engine.feasible_global:
            raise ValueError('stored revision is not feasible')
        supported_masks = engine.supported()
        cached = (parsed, engine, supported_masks)
        self._calibration_cache[revision_id] = cached
        return cached

    def _scope_holds(self, parsed, supported_masks, order):
        """Re-prove an order's group against a (possibly new) revision."""
        run_index = self._run_index(parsed, order['run_id'])
        run = parsed['runs'][run_index]
        approved = set(map(tuple, order['approved']))
        pins = set(map(tuple, order['local_pins']))
        mask = supported_masks[run_index]
        for local, candidate_index in enumerate(run['surviving']):
            if not ((mask >> local) & 1):
                continue
            candidate = run['candidates'][candidate_index]
            images = {_local_point(candidate, point) for point in approved}
            if images != pins:
                return False
        return True

    def _check_quotas(self, parsed, extra_run_index, extra_pins):
        per_station = [set(run['records'].keys()) for run in parsed['runs']]
        total_lines = 0
        run_index_by_id = {run['id']: i for i, run in enumerate(parsed['runs'])}
        for order in self._state['orders']:
            run_index = run_index_by_id[order['run_id']]
            per_station[run_index].update(map(tuple, order['local_pins']))
            total_lines += len(order['local_pins'])

        projected = set(per_station[extra_run_index]) | set(extra_pins)
        if len(projected) > STATION_PIN_MAX:
            raise ValueError('station coordinate quota exceeded')
        added = len(projected) - len(per_station[extra_run_index])
        wafer_total = sum(len(union) for union in per_station) + added
        if wafer_total > WAFER_PIN_MAX:
            raise ValueError('wafer coordinate quota exceeded')
        if total_lines + len(extra_pins) > PIN_LINES_TOTAL_MAX:
            raise ValueError('cumulative pin line quota exceeded')

    def _run_index(self, parsed, run_id):
        for index, run in enumerate(parsed['runs']):
            if run['id'] == run_id:
                return index
        raise ValueError('unknown run id')

    def _find_order(self, order_id):
        for order in self._state['orders']:
            if order['order_id'] == order_id:
                return order
        raise ValueError('unknown work order id')

    def _snapshot(self):
        return copy.deepcopy(self._state)

    def _cleanup_temp_files(self):
        for temporary in self.path.glob(_TMP_PREFIX + '*' + _TMP_SUFFIX):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _commit(self, state):
        """Durably replace the state file; nothing visible changes before this."""
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
        self._calibration_cache.clear()

    def _atomic_replace(self, temporary, target):
        os.replace(temporary, target)


def _calibrated_view(parsed, engine, supported_masks, records_by_run):
    """Build the process_calibrated-shaped output with fused local records.

    The engine (hence candidate exclusion and feasible calibrations) stays
    anchored to the first-test evidence; only the label lookup swaps in the
    fused record maps, so retest readings can never eliminate a candidate by
    mapping off-sites.
    """
    supported_candidates = []
    for run_index, run in enumerate(parsed['runs']):
        mask = supported_masks[run_index]
        ids = sorted(run['candidates'][candidate_index]['id']
                     for local, candidate_index in enumerate(run['surviving'])
                     if (mask >> local) & 1)
        supported_candidates.append({'run': run['id'], 'candidates': ids})

    dies = []
    pick_coordinates = []
    cache = {}
    for site in sorted(parsed['sites'], key=lambda point: (point[1], point[0])):
        labels = []
        key_parts = []
        sx, sy = site
        for run_index, run in enumerate(parsed['runs']):
            records = records_by_run[run_index]
            per_candidate = []
            pass_mask = incomplete_mask = fail_mask = 0
            for local, candidate_index in enumerate(run['surviving']):
                a, b, c, d, kx, ky = run['candidates'][candidate_index]['inv']
                local_x = a * sx + b * sy + kx
                local_y = c * sx + d * sy + ky
                disposition = records.get((local_x, local_y))
                if disposition is None or disposition == 'skip':
                    label = _INCOMPLETE
                    incomplete_mask |= 1 << local
                elif disposition == 'fail':
                    label = _FAIL
                    fail_mask |= 1 << local
                else:
                    label = _PASS
                    pass_mask |= 1 << local
                per_candidate.append(label)
            labels.append(per_candidate)
            key_parts.append((pass_mask, incomplete_mask, fail_mask))
        key = tuple(key_parts)
        if key in cache:
            status_mask = cache[key]
        else:
            status_mask = engine.die_statuses(labels)
            cache[key] = status_mask
        possible = [_STATUS_NAME[i] for i in range(3) if (status_mask >> i) & 1]
        dies.append({'die_id': f"{parsed['wafer']}:{site[0]}:{site[1]}",
                     'at': list(site), 'possible_statuses': possible})
        if possible == ['pass']:
            pick_coordinates.append(list(site))

    return {'wafer': parsed['wafer'],
            'supported_candidates': supported_candidates,
            'dies': dies,
            'pick_coordinates': pick_coordinates}
