"""Offline dual-die mounting batch pairing and credential ledger.

A :class:`MountingBatch` fuses the wafer selection evidence produced by
:func:`wafer_fusion.process_calibrated` with per-die process grading into
whole-batch L/R pairings for a fixed list of mounting positions, then guards
the pick credentials ("领用凭据") issued to the mounting station and records
what physically came back (mounted or scrapped, including which glue-faced
dies are spent).

Design notes
------------
* Eligibility is never taken from caller-supplied pass flags: every wafer's
  complete ``process_calibrated`` input is re-run through the existing
  calibration engine, and a graded die joins the pool only when its canonical
  coordinate is among that wafer's ``pick_coordinates`` (pass under *every*
  feasible calibration).  Graded-but-unpicked rows stay stored as input.
* Pairing is a cardinality-k minimum-cost bipartite matching (k = number of
  positions still waiting).  The lexicographic tie-break over sorted pair
  lists is encoded exactly in the integer edge weights with super-increasing
  powers of two, so one min-cost flow run yields the unique deterministic
  optimum without any approximate perturbation.
* All state lives in one JSON file replaced atomically (temp file + fsync +
  rename + directory fsync), mirroring :class:`wafer_fusion.WaferArchive`.
  A rejected call or an interruption before the rename leaves no half
  credential, half consumption or half-reopened position observable.
"""
import copy
import heapq
import json
import os
import tempfile
from pathlib import Path

from .calibration import COORD_BOUND, _coord
from .checks import array, fields, ident, integer, unique
from . import process_calibrated

BATCH_SCHEMA = 'wafer-mounting-batch-v1'
PLAN_SCHEMA = 'wafer-mounting-plan-v1'
STATE_FILENAME = 'mounting.json'
_TMP_PREFIX = '.mounting.json.'
_TMP_SUFFIX = '.tmp'

WAFERS_MAX = 16
GRADING_MAX = 256
POSITIONS_MAX = 32
COEFF_BOUND = 1_000_000
DIFF_MAX = 2_000_000

_SIDE_L = 'L'
_SIDE_R = 'R'
_OUTCOMES = ('mounted', 'scrapped')


# --------------------------------------------------------------------- helpers

def _die_key(wafer, point):
    """Ordering identity of one physical die: (wafer id, y, x)."""
    return (wafer, point[1], point[0])


def _die_ref(row):
    return {'wafer': row['wafer'], 'at': list(row['at'])}


def _die_view(row):
    return {'wafer': row['wafer'], 'at': list(row['at']),
            'side': row['side'], 'source_group': row['source_group'],
            'coefficient': row['coefficient']}


def _parse_die_ref(value):
    fields(value, ('wafer', 'at'))
    wafer = ident(value['wafer'])
    point = _coord(value['at'])
    return wafer, point


def _ref_key(ref):
    return (ref['wafer'], ref['at'][1], ref['at'][0])


# ------------------------------------------------------------- min-cost flow

class _MinCostFlow:
    """Successive shortest augmenting path flow with Dijkstra + potentials.

    Edge lists are ``[to, reverse_index, capacity, cost]`` mutable records.
    All initial costs are non-negative (the planner shifts them), so the
    starting potentials are zero; potential updates keep every reduced cost
    non-negative after residual edges appear.
    """

    def __init__(self, node_count, infinity):
        self._n = node_count
        self._g = [[] for _ in range(node_count)]
        self._infinity = infinity

    def add_edge(self, fr, to, capacity, cost):
        forward = [to, len(self._g[to]), capacity, cost]
        backward = [fr, len(self._g[fr]), 0, -cost]
        self._g[fr].append(forward)
        self._g[to].append(backward)
        return forward

    def min_cost_flow(self, source, sink, required):
        n = self._n
        potential = [0] * n
        infinity = self._infinity
        flowed = 0
        while flowed < required:
            dist = [infinity] * n
            prev_vertex = [-1] * n
            prev_edge = [-1] * n
            dist[source] = 0
            queue = [(0, source)]
            while queue:
                d, vertex = heapq.heappop(queue)
                if d != dist[vertex]:
                    continue
                for edge_index, edge in enumerate(self._g[vertex]):
                    to, _rev, capacity, cost = edge
                    if capacity <= 0:
                        continue
                    reduced = d + cost + potential[vertex] - potential[to]
                    if reduced < dist[to]:
                        dist[to] = reduced
                        prev_vertex[to] = vertex
                        prev_edge[to] = edge_index
                        heapq.heappush(queue, (reduced, to))
            if dist[sink] == infinity:
                break
            for vertex in range(n):
                if dist[vertex] != infinity:
                    potential[vertex] += dist[vertex]
            vertex = sink
            while vertex != source:
                parent = prev_vertex[vertex]
                edge = self._g[parent][prev_edge[vertex]]
                edge[2] -= 1
                # The reverse record lives in the destination vertex's list.
                self._g[vertex][edge[1]][2] += 1
                vertex = parent
            flowed += 1
        return flowed


def _pairing_plan(rows, consumed_keys, locked_keys, waiting_positions,
                  max_diff):
    """Return the deterministic complete plan for the waiting positions.

    ``rows`` are the eligible grading rows.  ``consumed_keys`` and
    ``locked_keys`` are die keys removed from the pool (spent dies and dies
    reserved by open credentials).  Returns ``None`` when the waiting
    positions cannot all be filled, otherwise a sorted list of
    ``(left_row, right_row, diff)`` tuples.
    """
    required = len(waiting_positions)
    if required == 0:
        return []

    left_rows = []
    right_rows = []
    for row in rows:
        key = _die_key(row['wafer'], row['at'])
        if key in consumed_keys or key in locked_keys:
            continue
        if row['side'] == _SIDE_L:
            left_rows.append(row)
        else:
            right_rows.append(row)
    left_rows.sort(key=lambda row: _die_key(row['wafer'], row['at']))
    right_rows.sort(key=lambda row: _die_key(row['wafer'], row['at']))

    edges = []
    for left in left_rows:
        lkey = _die_key(left['wafer'], left['at'])
        for right in right_rows:
            if left['source_group'] == right['source_group']:
                continue
            diff = abs(left['coefficient'] - right['coefficient'])
            if diff > max_diff:
                continue
            rkey = _die_key(right['wafer'], right['at'])
            edges.append((lkey, rkey, left, right, diff))
    if len(edges) < required:
        return None
    edges.sort(key=lambda item: (item[0], item[1]))

    edge_count = len(edges)
    # Every feasible edge gets a unique power-of-two preference term.  A
    # lexicographically smaller sorted pair list is exactly a lexicographically
    # larger inclusion bit-vector over edges ranked from the smallest pair.
    # BIG dominates the whole preference sum, so total diff still decides
    # first; SHIFT makes every initial edge cost non-negative.
    big = 1 << (edge_count + 6)
    shift = 1 << edge_count
    infinity = 1 << (edge_count + 64)

    node_source = 0
    left_base = 1
    right_base = left_base + len(left_rows)
    node_sink = right_base + len(right_rows)
    network = _MinCostFlow(node_sink + 1, infinity)
    for i in range(len(left_rows)):
        network.add_edge(node_source, left_base + i, 1, 0)
    for j in range(len(right_rows)):
        network.add_edge(right_base + j, node_sink, 1, 0)

    forward_edges = []
    left_index = {_die_key(row['wafer'], row['at']): i
                  for i, row in enumerate(left_rows)}
    right_index = {_die_key(row['wafer'], row['at']): j
                   for j, row in enumerate(right_rows)}
    for rank, (lkey, rkey, left, right, diff) in enumerate(edges):
        preference = 1 << (edge_count - 1 - rank)
        cost = diff * big + shift - preference
        record = network.add_edge(
            left_base + left_index[lkey], right_base + right_index[rkey],
            1, cost)
        forward_edges.append((record, left, right, diff))

    flowed = network.min_cost_flow(node_source, node_sink, required)
    if flowed < required:
        return None

    selected = [(left, right, diff)
                for record, left, right, diff in forward_edges
                if record[2] == 0]
    selected.sort(key=lambda item: (_die_key(item[0]['wafer'], item[0]['at']),
                                    _die_key(item[1]['wafer'], item[1]['at'])))
    return selected


# --------------------------------------------------------------------- batch

class MountingBatch:
    """One on-disk dual-die mounting batch, driven sequentially by one caller."""

    # ------------------------------------------------------------- open/create

    def __init__(self, path):
        self.path = Path(path)
        state_file = self.path / STATE_FILENAME
        if not state_file.is_file():
            raise ValueError('not a mounting batch directory')
        self._state_file = state_file
        self._cleanup_temp_files()
        with open(state_file, 'rb') as handle:
            state = json.loads(handle.read().decode('utf-8'))
        if not isinstance(state, dict) or state.get('schema') != BATCH_SCHEMA:
            raise ValueError('unrecognised mounting batch state')
        self._state = state
        self._reindex()

    @classmethod
    def create(cls, path, request):
        """Create a batch from wafer inputs, grading rows and positions.

        ``request`` has exactly ``wafers`` (1..16 complete
        ``process_calibrated`` payloads with unique wafer ids), ``grading``
        (0..256 rows), ``positions`` (1..32 unique fixture-order ids) and
        ``max_coefficient_diff`` (0..2,000,000, inclusive).  Re-creating an
        existing batch with an equivalent request reopens it; any mismatch
        raises ``ValueError`` and never overwrites the stored batch.
        """
        fields(request, ('wafers', 'grading', 'positions',
                         'max_coefficient_diff'))
        max_diff = integer(request['max_coefficient_diff'], 0, DIFF_MAX)
        wafer_payloads = cls._validate_wafers(request['wafers'])
        positions = cls._validate_positions(request['positions'])
        grading = cls._validate_grading(request['grading'], wafer_payloads)

        signature = cls._create_signature(
            wafer_payloads, grading, positions, max_diff)
        directory = Path(path)
        state_file = directory / STATE_FILENAME
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if state_file.is_file():
                batch = cls(directory)
                if batch._state['create_signature'] != signature:
                    raise ValueError('mounting batch already exists with a '
                                     'different request')
                return batch
            raise ValueError('batch path exists without mounting state')

        stored_wafers = [info['payload'] for info in
                        sorted(wafer_payloads.values(),
                               key=lambda info: info['result']['wafer'])]
        state = {
            'schema': BATCH_SCHEMA,
            'create_signature': signature,
            'wafers': stored_wafers,
            'grading': grading,
            'positions': positions,
            'max_coefficient_diff': max_diff,
            'consumed': [],
            'credentials': [],
            'next_serial': 1,
        }
        batch = cls.__new__(cls)
        batch.path = directory
        batch._state_file = state_file
        batch._state = state
        batch._cleanup_temp_files()
        batch._reindex()
        batch._commit(state)
        return batch

    # ------------------------------------------------------------- validation

    @staticmethod
    def _validate_wafers(raw_wafers):
        """Run every wafer through the real calibration engine."""
        payloads = array(raw_wafers, 1, WAFERS_MAX)
        validated = {}
        for raw_payload in payloads:
            if not isinstance(raw_payload, dict):
                raise ValueError('wafer entry must be a process_calibrated '
                                 'payload object')
            payload = copy.deepcopy(raw_payload)
            # The existing engine decides eligibility end to end; caller
            # claimed pass markers are never read here.
            result = process_calibrated(payload)
            wafer_id = result['wafer']
            if wafer_id in validated:
                raise ValueError('duplicate wafer id in batch')
            pick = {tuple(point) for point in result['pick_coordinates']}
            declared = {tuple(site) for site in payload['sites']}
            validated[wafer_id] = {'result': result, 'payload': payload,
                                   'pick': pick, 'declared': declared}
        return validated

    @staticmethod
    def _validate_positions(raw_positions):
        positions = [ident(value) for value in
                     array(raw_positions, 1, POSITIONS_MAX)]
        unique(positions)
        return positions

    @staticmethod
    def _validate_grading(raw_grading, wafer_payloads):
        rows = []
        seen = set()
        for raw_row in array(raw_grading, 0, GRADING_MAX):
            fields(raw_row, ('wafer', 'at', 'side', 'source_group',
                             'coefficient'))
            wafer = ident(raw_row['wafer'])
            if wafer not in wafer_payloads:
                raise ValueError('grading row references an unknown wafer')
            point = _coord(raw_row['at'])
            key = (wafer, point)
            if key in seen:
                raise ValueError('duplicate grading row for one die')
            seen.add(key)
            side = raw_row['side']
            if side not in (_SIDE_L, _SIDE_R):
                raise ValueError('side must be L or R')
            source_group = ident(raw_row['source_group'])
            coefficient = integer(raw_row['coefficient'],
                                  -COEFF_BOUND, COEFF_BOUND)
            wafer_info = wafer_payloads[wafer]
            if point not in wafer_info['declared']:
                raise ValueError('grading coordinate is not declared by the '
                                 'wafer calibration input')
            eligible = point in wafer_info['pick']
            rows.append({'wafer': wafer, 'at': list(point), 'side': side,
                         'source_group': source_group,
                         'coefficient': coefficient, 'eligible': eligible})
        rows.sort(key=lambda row: _die_key(row['wafer'], row['at']))
        return rows

    @staticmethod
    def _create_signature(wafer_payloads, grading, positions, max_diff):
        wafers = sorted(
            (info['payload'] for info in wafer_payloads.values()),
            key=lambda payload: payload['wafer'])
        canonical = {'wafers': copy.deepcopy(wafers),
                     'grading': copy.deepcopy(grading),
                     'positions': list(positions),
                     'max_coefficient_diff': max_diff}
        return json.dumps(canonical, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False)

    # ------------------------------------------------------------- reindexing

    def _reindex(self):
        self._grading_by_key = {
            _die_key(row['wafer'], tuple(row['at'])): row
            for row in self._state['grading']}
        self._eligible_rows = [row for row in self._state['grading']
                               if row['eligible']]
        self._consumed_keys = {
            _die_key(entry['wafer'], tuple(entry['at']))
            for entry in self._state['consumed']}
        self._credentials_by_id = {
            credential['credential_id']: credential
            for credential in self._state['credentials']}
        self._credential_by_request = {
            credential['request_id']: credential
            for credential in self._state['credentials']}
        self._open_by_position = {
            credential['position_id']: credential
            for credential in self._state['credentials']
            if credential['terminal'] is None}
        self._mounted_by_position = {
            credential['position_id']: credential
            for credential in self._state['credentials']
            if credential['terminal'] is not None
            and credential['terminal']['outcome'] == 'mounted'}

    # ---------------------------------------------------------------- preview

    def preview_plan(self):
        """Pair every waiting position together; never writes state.

        Returns the unique minimum-total-diff complete plan with the
        contract's lexicographic tie-break, an explicit non-complete answer
        (with no usable partial pairs), or an empty plan when nothing is
        waiting.  Open credentials keep their fixed pairs and their dies do
        not re-enter the pool; consumed dies never return.
        """
        locked_keys = set()
        for credential in self._state['credentials']:
            if credential['terminal'] is None:
                locked_keys.add(_ref_key(credential['left']))
                locked_keys.add(_ref_key(credential['right']))

        waiting = [position for position in self._state['positions']
                   if position not in self._open_by_position
                   and position not in self._mounted_by_position]
        selected = _pairing_plan(
            self._eligible_rows, self._consumed_keys, locked_keys,
            waiting, self._state['max_coefficient_diff'])

        if selected is None:
            return {'schema': PLAN_SCHEMA, 'complete': False,
                    'positions': list(waiting), 'pairs': [],
                    'assignments': [], 'total_coefficient_diff': None}
        assignments = []
        for position, (left, right, diff) in zip(waiting, selected):
            assignments.append({'position_id': position,
                                'left': _die_view(left),
                                'right': _die_view(right)})
        pairs = [{'left': _die_view(left), 'right': _die_view(right),
                  'coefficient_diff': diff}
                 for left, right, diff in selected]
        return {'schema': PLAN_SCHEMA, 'complete': True,
                'positions': list(waiting), 'pairs': pairs,
                'assignments': assignments,
                'total_coefficient_diff':
                    sum(item['coefficient_diff'] for item in pairs)}

    # -------------------------------------------------------------- issuance

    def issue_credential(self, request):
        """Issue one immutable credential for one current plan pair.

        A new request is granted only while its pair is exactly the pair the
        current complete plan assigns to that position.  Stale previews are
        rejected; the station is never silently switched to other dies.
        Replaying the same request id with the same body returns the original
        credential (including after it terminated or the position reopened).
        """
        fields(request, ('request_id', 'position_id', 'pair'))
        request_id = ident(request['request_id'])
        position_id = ident(request['position_id'])
        fields(request['pair'], ('left', 'right'))
        left_wafer, left_at = _parse_die_ref(request['pair']['left'])
        right_wafer, right_at = _parse_die_ref(request['pair']['right'])

        previous = self._credential_by_request.get(request_id)
        if previous is not None:
            self._assert_same_issue_body(
                previous, position_id, left_wafer, left_at,
                right_wafer, right_at)
            return self._credential_view(previous)

        if position_id not in self._state['positions']:
            raise ValueError('unknown mounting position id')
        if position_id in self._open_by_position:
            raise ValueError('mounting position already has an open credential')
        if position_id in self._mounted_by_position:
            raise ValueError('mounting position is already terminated')

        plan = self.preview_plan()
        if not plan['complete']:
            raise ValueError('no complete pairing plan is currently available')
        assigned = next((item for item in plan['assignments']
                         if item['position_id'] == position_id), None)
        if assigned is None:
            raise ValueError('position does not need pairing right now')
        wanted = {(left_wafer, tuple(left_at)), (right_wafer, tuple(right_at))}
        planned = {(assigned['left']['wafer'], tuple(assigned['left']['at'])),
                   (assigned['right']['wafer'], tuple(assigned['right']['at']))}
        if wanted != planned:
            raise ValueError('requested pair is not the current plan pair '
                             'for this position')

        left_row = self._require_graded(left_wafer, left_at)
        right_row = self._require_graded(right_wafer, right_at)
        if left_row['side'] != _SIDE_L or right_row['side'] != _SIDE_R:
            raise ValueError('pair must reference one L die and one R die')

        serial = self._state['next_serial']
        credential = {
            'credential_id': f'C{serial:04d}',
            'serial': serial,
            'request_id': request_id,
            'position_id': position_id,
            'left': _die_ref(left_row),
            'right': _die_ref(right_row),
            'terminal': None,
        }
        state = self._snapshot()
        state['credentials'].append(credential)
        state['next_serial'] = serial + 1
        self._commit(state)
        return self._credential_view(self._credentials_by_id[
            credential['credential_id']])

    def _assert_same_issue_body(self, credential, position_id, left_wafer,
                                left_at, right_wafer, right_at):
        same = (credential['position_id'] == position_id
                and credential['left']['wafer'] == left_wafer
                and tuple(credential['left']['at']) == tuple(left_at)
                and credential['right']['wafer'] == right_wafer
                and tuple(credential['right']['at']) == tuple(right_at))
        if not same:
            raise ValueError('request id reused with a different request')

    def _require_graded(self, wafer, point):
        row = self._grading_by_key.get(_die_key(wafer, point))
        if row is None or not row['eligible']:
            raise ValueError('die is not an eligible graded candidate')
        return row

    # ---------------------------------------------------------------- reports

    def submit_mount_report(self, request):
        """Record the unique terminal outcome of one credential.

        ``mounted`` consumes exactly the two credential dies and terminates
        the position.  ``scrapped`` consumes the explicit ``touched`` glue-
        faced set; the remaining dies return to the pool only because they
        are explicitly listed in ``untouched``, and the position reopens.
        Scrap is a physical fact and is accepted even when it makes the
        remaining batch impossible to complete.  Replaying an identical
        terminal report (set order-independent) returns the original
        confirmation; any conflicting terminal state is rejected.
        """
        fields(request, ('credential_id', 'outcome', 'touched', 'untouched'))
        credential_id = ident(request['credential_id'])
        credential = self._credentials_by_id.get(credential_id)
        if credential is None:
            raise ValueError('unknown credential id')
        outcome = request['outcome']
        if outcome not in _OUTCOMES:
            raise ValueError('outcome must be mounted or scrapped')

        touched = self._parse_report_set(request['touched'], credential)
        untouched = self._parse_report_set(request['untouched'], credential)
        if touched & untouched:
            raise ValueError('a die cannot be both touched and untouched')
        credential_dies = {
            (credential['left']['wafer'], tuple(credential['left']['at'])),
            (credential['right']['wafer'], tuple(credential['right']['at']))}
        if touched | untouched != credential_dies:
            raise ValueError('report must account for both credential dies')
        if outcome == 'mounted' and (len(touched) != 2 or untouched):
            raise ValueError('a mounted credential must consume both dies')

        terminal = {
            'outcome': outcome,
            'touched': self._canonical_refs(touched),
            'untouched': self._canonical_refs(untouched)}

        if credential['terminal'] is not None:
            if credential['terminal'] != terminal:
                raise ValueError('credential already has a conflicting '
                                 'terminal state')
            return self._report_confirmation(credential)

        state = self._snapshot()
        stored = next(item for item in state['credentials']
                      if item['credential_id'] == credential_id)
        stored['terminal'] = copy.deepcopy(terminal)
        for wafer, point in sorted(touched):
            state['consumed'].append(
                {'wafer': wafer, 'at': list(point),
                 'credential_id': credential_id, 'reason': outcome})
        self._commit(state)
        return self._report_confirmation(
            self._credentials_by_id[credential_id])

    def _parse_report_set(self, raw_refs, credential):
        allowed = {(credential['left']['wafer'],
                    tuple(credential['left']['at'])),
                   (credential['right']['wafer'],
                    tuple(credential['right']['at']))}
        refs = set()
        for raw in array(raw_refs, 0, 2):
            wafer, point = _parse_die_ref(raw)
            key = (wafer, point)
            if key not in allowed:
                raise ValueError('report references a die outside the '
                                 'credential')
            if key in refs:
                raise ValueError('duplicate die in report set')
            refs.add(key)
        return refs

    @staticmethod
    def _canonical_refs(keys):
        return [{'wafer': wafer, 'at': [point[0], point[1]]}
                for wafer, point in sorted(
                    keys, key=lambda item: (item[0], item[1][1], item[1][0]))]

    # ---------------------------------------------------------------- queries

    def positions(self):
        """Fixture-order lifecycle state of every mounting position."""
        result = []
        for position_id in self._state['positions']:
            open_credential = self._open_by_position.get(position_id)
            mounted_credential = self._mounted_by_position.get(position_id)
            result.append({
                'position_id': position_id,
                'status': ('mounted' if mounted_credential is not None
                           else 'locked' if open_credential is not None
                           else 'waiting'),
                'open_credential_id': (None if open_credential is None
                                       else open_credential['credential_id']),
                'mounted': (None if mounted_credential is None
                            else self._credential_view(mounted_credential))})
        return result

    def open_credentials(self):
        """Credentials currently holding two reserved dies."""
        return [self._credential_view(credential)
                for credential in self._state['credentials']
                if credential['terminal'] is None]

    def credential_history(self):
        """Every credential in issuance order, terminal state included."""
        return [self._credential_view(credential)
                for credential in self._state['credentials']]

    def consumed_dies(self):
        """All spent dies (mounted or scrapped after touching glue)."""
        result = []
        for entry in sorted(
                self._state['consumed'],
                key=lambda item: _die_key(item['wafer'], tuple(item['at']))):
            row = self._grading_by_key[
                _die_key(entry['wafer'], tuple(entry['at']))]
            view = _die_view(row)
            view['credential_id'] = entry['credential_id']
            view['reason'] = entry['reason']
            result.append(view)
        return result

    def grading_view(self):
        """All input grading rows, including rows not eligible to pair."""
        return [{'wafer': row['wafer'], 'at': list(row['at']),
                 'side': row['side'], 'source_group': row['source_group'],
                 'coefficient': row['coefficient'],
                 'eligible': row['eligible']}
                for row in self._state['grading']]

    def batch_info(self):
        return {'schema': BATCH_SCHEMA,
                'wafers': [payload['wafer'] for payload in
                           self._state['wafers']],
                'positions': list(self._state['positions']),
                'max_coefficient_diff': self._state['max_coefficient_diff'],
                'grading': self.grading_view()}

    # ------------------------------------------------------------- internals

    def _credential_view(self, credential):
        row_left = self._grading_by_key[_ref_key(credential['left'])]
        row_right = self._grading_by_key[_ref_key(credential['right'])]
        return {
            'credential_id': credential['credential_id'],
            'serial': credential['serial'],
            'request_id': credential['request_id'],
            'position_id': credential['position_id'],
            'left': _die_view(row_left),
            'right': _die_view(row_right),
            'state': ('open' if credential['terminal'] is None
                      else credential['terminal']['outcome']),
            'terminal': (None if credential['terminal'] is None
                         else copy.deepcopy(credential['terminal']))}

    def _report_confirmation(self, credential):
        terminal = credential['terminal']
        position_status = 'mounted' if terminal['outcome'] == 'mounted' \
            else 'waiting'
        return {'credential_id': credential['credential_id'],
                'request_id': credential['request_id'],
                'position_id': credential['position_id'],
                'outcome': terminal['outcome'],
                'touched': copy.deepcopy(terminal['touched']),
                'untouched': copy.deepcopy(terminal['untouched']),
                'consumed': copy.deepcopy(terminal['touched']),
                'position_status': position_status}

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
