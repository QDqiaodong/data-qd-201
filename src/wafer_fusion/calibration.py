"""Offline calibration-aware wafer fusion.

Unlike :func:`wafer_fusion.service.process`, each test station here offers
several candidate coordinate transforms.  Benchmark-point links between
adjacent stations constrain which candidate *combinations* are admissible; a
die status is only reported as possible when a complete calibration
satisfying every link witnesses it.
"""
from .checks import array, fields, ident, integer, unique

COORD_BOUND = 10000
SITES_MAX = 20000
RUNS_MAX = 20
CANDIDATES_MAX = 8
LINKS_MAX = 256
RECORDS_PER_RUN_MAX = 20000
RECORDS_TOTAL_MAX = 200000
BINS_MAX = 100
LINK_WINDOW = 2

_PASS, _INCOMPLETE, _FAIL = 0, 1, 2
_STATUS_NAME = ('pass', 'incomplete', 'fail')


def _coord(value):
    return tuple(integer(v, -COORD_BOUND, COORD_BOUND) for v in array(value, 2, 2))


def _matrix(value):
    matrix = [tuple(integer(v, -1, 1) for v in array(row, 2, 2))
              for row in array(value, 2, 2)]
    if any(sum(v * v for v in row) != 1 for row in matrix):
        raise ValueError('signed orthogonal integer matrix required')
    if sum(a * b for a, b in zip(*matrix)) != 0:
        raise ValueError('signed orthogonal integer matrix required')
    return matrix


def _apply(matrix, offset, point):
    return (matrix[0][0] * point[0] + matrix[0][1] * point[1] + offset[0],
            matrix[1][0] * point[0] + matrix[1][1] * point[1] + offset[1])


def _parse(data):
    """Validate the calibrated payload without mutating it."""
    fields(data, ('wafer', 'sites', 'runs', 'links'))
    wafer = ident(data['wafer'])
    sites = [_coord(v) for v in array(data['sites'], 1, SITES_MAX)]
    unique(sites)
    site_set = set(sites)

    runs = []
    total_records = 0
    for raw_run in array(data['runs'], 1, RUNS_MAX):
        fields(raw_run, ('id', 'bins', 'records', 'candidates'))
        run_id = ident(raw_run['id'])

        fields(raw_run['bins'], (),
               tuple(raw_run['bins']) if isinstance(raw_run['bins'], dict) else ())
        if not 1 <= len(raw_run['bins']) <= BINS_MAX:
            raise ValueError('bin table size')
        bins = {}
        for code, state in raw_run['bins'].items():
            ident(code)
            if state not in ('pass', 'fail', 'skip'):
                raise ValueError('invalid bin disposition')
            bins[code] = state

        records = {}
        for item in array(raw_run['records'], 0, RECORDS_PER_RUN_MAX):
            fields(item, ('at', 'bin'))
            point = _coord(item['at'])
            code = ident(item['bin'])
            if code not in bins:
                raise ValueError('unknown bin')
            if point in records:
                raise ValueError('duplicate device coordinate')
            records[point] = bins[code]
        total_records += len(records)
        if total_records > RECORDS_TOTAL_MAX:
            raise ValueError('record work limit')

        candidates = []
        candidate_ids = []
        transforms = []
        for raw_candidate in array(raw_run['candidates'], 1, CANDIDATES_MAX):
            fields(raw_candidate, ('id', 'matrix', 'offset'))
            cid = ident(raw_candidate['id'])
            matrix = _matrix(raw_candidate['matrix'])
            offset = _coord(raw_candidate['offset'])
            transform = (matrix, offset)
            if transform in transforms:
                raise ValueError('duplicate candidate transform')
            transforms.append(transform)
            candidate_ids.append(cid)
            # Inverse of an orthogonal integer matrix is its transpose:
            # local = M^T * (canonical - offset).
            a, b = matrix[0][0], matrix[1][0]
            c, d = matrix[0][1], matrix[1][1]
            kx = -(a * offset[0] + b * offset[1])
            ky = -(c * offset[0] + d * offset[1])
            candidates.append({'id': cid, 'matrix': matrix, 'offset': offset,
                               'inv': (a, b, c, d, kx, ky)})
        unique(candidate_ids)

        # Out-of-sites candidates are excluded from calibration but never
        # make the payload illegal; structural validation above still ran.
        surviving = []
        for index, candidate in enumerate(candidates):
            matrix, offset = candidate['matrix'], candidate['offset']
            if all(_apply(matrix, offset, point) in site_set for point in records):
                surviving.append(index)
        runs.append({'id': run_id, 'bins': bins, 'records': records,
                     'candidates': candidates, 'surviving': surviving})

    unique([run['id'] for run in runs])
    run_index = {run['id']: i for i, run in enumerate(runs)}

    links = []
    endpoint_pairs = set()
    for raw_link in array(data['links'], 0, LINKS_MAX):
        fields(raw_link, ('left', 'right'))
        endpoints = []
        for side in ('left', 'right'):
            fields(raw_link[side], ('run', 'at'))
            side_run = ident(raw_link[side]['run'])
            if side_run not in run_index:
                raise ValueError('unknown link run')
            endpoints.append((run_index[side_run], _coord(raw_link[side]['at'])))
        (left_i, left_at), (right_i, right_at) = endpoints
        if not left_i < right_i:
            raise ValueError('link stations must follow run order')
        if right_i - left_i > LINK_WINDOW:
            raise ValueError('link outside three-station window')
        pair = ((left_i, left_at), (right_i, right_at))
        if pair in endpoint_pairs:
            raise ValueError('duplicate link endpoint pair')
        endpoint_pairs.add(pair)
        links.append((left_i, left_at, right_i, right_at))

    return {'wafer': wafer, 'sites': sites, 'runs': runs, 'links': links}


class _Engine:
    """Pair-DP solver over the bandwidth-two station graph."""

    def __init__(self, parsed):
        self.runs = parsed['runs']
        self.n = len(self.runs)
        self.domains = [run['surviving'] for run in self.runs]
        self.widths = [len(domain) for domain in self.domains]
        self._alive_sets = [set(domain) for domain in self.domains]
        self._local_maps = [{index: local for local, index in enumerate(domain)}
                            for domain in self.domains]
        if any(width == 0 for width in self.widths):
            self.feasible_global = False
            return
        self.full_masks = [(1 << width) - 1 for width in self.widths]
        self.fwd1 = [None] * self.n
        self.bwd2 = [None] * self.n
        self._build_relations(parsed['links'])
        self.feasible_global = self._feasible(self.full_masks)

    def _build_relations(self, links):
        # edge (u, v) -> set of (local candidate index u, local index v)
        edges = {}
        for left_i, left_at, right_i, right_at in links:
            allowed = set()
            left_run, right_run = self.runs[left_i], self.runs[right_i]
            for a, candidate_a in enumerate(left_run['candidates']):
                if a not in self._alive(left_i):
                    continue
                image_a = _apply(candidate_a['matrix'], candidate_a['offset'], left_at)
                for b, candidate_b in enumerate(right_run['candidates']):
                    if b not in self._alive(right_i):
                        continue
                    image_b = _apply(candidate_b['matrix'], candidate_b['offset'], right_at)
                    if image_a == image_b:
                        allowed.add((self._local(left_i, a), self._local(right_i, b)))
            key = (left_i, right_i)
            if key in edges:
                edges[key] &= allowed
            else:
                edges[key] = allowed

        # fwd1[u][a]: candidate mask in run u+1 compatible with candidate a.
        self.fwd1 = [None] * self.n
        self.bwd2 = [None] * self.n
        for (u, v), pairs in edges.items():
            distance = v - u
            if distance == 1:
                fwd = [0] * self.widths[u]
                for a, b in pairs:
                    fwd[a] |= 1 << b
                self.fwd1[u] = fwd
            elif distance == 2:
                bwd = [0] * self.widths[v]
                for a, b in pairs:
                    bwd[b] |= 1 << a
                self.bwd2[v] = bwd

    def _alive(self, run_i):
        return self._alive_sets[run_i]

    def _local(self, run_i, candidate_index):
        return self._local_maps[run_i][candidate_index]

    def _feasible(self, allow):
        """True iff a complete calibration exists within allow masks."""
        n = self.n
        if n == 1:
            return bool(allow[0])
        widths = self.widths
        fwd0 = self.fwd1[0]
        prev = [0] * widths[1]
        for c in range(widths[1]):
            if not (allow[1] >> c) & 1:
                continue
            mask = 0
            allowed_a = allow[0]
            relation = fwd0
            for a in range(widths[0]):
                if ((allowed_a >> a) & 1 and
                        (relation is None or (relation[a] >> c) & 1)):
                    mask |= 1 << a
            prev[c] = mask
        if not any(prev):
            return False
        for i in range(2, n):
            fwd = self.fwd1[i - 1]
            bwd = self.bwd2[i]
            allow_i, allow_prev, allow_older = allow[i], allow[i - 1], allow[i - 2]
            new = [0] * widths[i]
            full_i = self.full_masks[i]
            full_older = self.full_masks[i - 2]
            for a in range(widths[i - 1]):
                before = prev[a]
                if not before or not (allow_prev >> a) & 1:
                    continue
                before &= allow_older
                candidates = full_i if fwd is None else fwd[a]
                candidates &= allow_i
                while candidates:
                    bit = candidates & -candidates
                    c = bit.bit_length() - 1
                    candidates ^= bit
                    relation = full_older if bwd is None else bwd[c]
                    if before & relation:
                        new[c] |= 1 << a
            prev = new
            if not any(prev):
                return False
        return True

    def supported(self):
        """Candidate mask per run that belongs to at least one calibration."""
        result = []
        for run_i, width in enumerate(self.widths):
            mask = 0
            for c in range(width):
                allow = list(self.full_masks)
                allow[run_i] = 1 << c
                if self._feasible(allow):
                    mask |= 1 << c
            result.append(mask)
        return result

    def die_statuses(self, labels):
        """Possible aggregate status mask for one die.

        labels[i] gives P/I/F (0/1/2) per surviving candidate of run i.
        """
        n = self.n
        widths = self.widths
        if n == 1:
            mask = 0
            for label in labels[0]:
                mask |= 1 << label
            return mask
        fwd0 = self.fwd1[0]
        state0 = [0] * widths[1]
        state1 = [0] * widths[1]
        state2 = [0] * widths[1]
        labels0, labels1 = labels[0], labels[1]
        for a in range(widths[0]):
            la = labels0[a]
            for c in range(widths[1]):
                if fwd0 is not None and not (fwd0[a] >> c) & 1:
                    continue
                status = la if la > labels1[c] else labels1[c]
                if status == _PASS:
                    state0[c] |= 1 << a
                elif status == _INCOMPLETE:
                    state1[c] |= 1 << a
                else:
                    state2[c] |= 1 << a
        for i in range(2, n):
            fwd = self.fwd1[i - 1]
            bwd = self.bwd2[i]
            labels_i = labels[i]
            full_i = self.full_masks[i]
            full_older = self.full_masks[i - 2]
            new0 = [0] * widths[i]
            new1 = [0] * widths[i]
            new2 = [0] * widths[i]
            for a in range(widths[i - 1]):
                b0, b1, b2 = state0[a], state1[a], state2[a]
                if not (b0 or b1 or b2):
                    continue
                candidates = full_i if fwd is None else fwd[a]
                while candidates:
                    bit = candidates & -candidates
                    c = bit.bit_length() - 1
                    candidates ^= bit
                    relation = full_older if bwd is None else bwd[c]
                    x0 = b0 & relation
                    x1 = b1 & relation
                    x2 = b2 & relation
                    label = labels_i[c]
                    if label == _FAIL:
                        if x0 or x1 or x2:
                            new2[c] |= 1 << a
                    elif label == _INCOMPLETE:
                        # Missing/skip cannot downgrade an earlier fail.
                        if x0 or x1:
                            new1[c] |= 1 << a
                        if x2:
                            new2[c] |= 1 << a
                    else:
                        if x0:
                            new0[c] |= 1 << a
                        if x1:
                            new1[c] |= 1 << a
                        if x2:
                            new2[c] |= 1 << a
            state0, state1, state2 = new0, new1, new2
        result = 0
        if any(state0):
            result |= 1 << _PASS
        if any(state1):
            result |= 1 << _INCOMPLETE
        if any(state2):
            result |= 1 << _FAIL
        return result


def process_calibrated(data):
    """Fuse station records under every link-consistent calibration.

    Returns pick coordinates that are safe for every admissible calibration
    and the exact possible-status evidence for every declared die.
    """
    parsed = _parse(data)
    engine = _Engine(parsed)
    if not engine.feasible_global:
        raise ValueError('no calibration satisfies the benchmark links')

    supported_masks = engine.supported()
    supported_candidates = []
    for run_i, run in enumerate(parsed['runs']):
        mask = supported_masks[run_i]
        ids = sorted(run['candidates'][index]['id']
                     for local, index in enumerate(run['surviving'])
                     if (mask >> local) & 1)
        supported_candidates.append({'run': run['id'], 'candidates': ids})

    dies = []
    pick_coordinates = []
    cache = {}
    for site in sorted(parsed['sites'], key=lambda point: (point[1], point[0])):
        labels = []
        key_parts = []
        for run_i, run in enumerate(parsed['runs']):
            records = run['records']
            per_candidate = []
            pass_mask = incomplete_mask = fail_mask = 0
            sx, sy = site
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

    return {'schema': 'wafer-calibration-v1', 'wafer': parsed['wafer'],
            'supported_candidates': supported_candidates,
            'dies': dies, 'pick_coordinates': pick_coordinates}
