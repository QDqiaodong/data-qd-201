"""Compile wafer geometry and per-layer exposure shots into canonical dies.

The probe map cannot be built from an ideal grid alone: wafer edges truncate
dies and the step-and-repeat fields of individual exposure layers may leave
gaps, overlaps or temporarily held shots.  :func:`compile_exposure_map`
turns those offline records into the canonical die coordinates accepted by
:func:`wafer_fusion.process_calibrated`, while keeping the exposure
attribution and exclusion reason of every in-circle grid point.

All geometry is evaluated with exact integer arithmetic: comparisons work on
doubled coordinates so that odd die widths/heights and tangencies never
depend on floating-point rounding.  The module is pure standard library,
never touches equipment and never mutates its input.
"""
from .checks import array, fields, ident, integer, unique

COORD_BOUND = 10000
RADIUS_MAX = 10000
DIE_SIDE_MAX = 20000
LAYERS_MAX = 8
SHOTS_PER_LAYER_MAX = 128
CANDIDATES_MAX = 100000
SITES_MAX = 20000

PROBEABLE = 'probeable'
EXCLUDED = 'excluded'
SCHEMA = 'wafer-exposure-map-v1'

_WAFER_EDGE = 'wafer_edge'
_UNCOVERED = 'uncovered'
_AMBIGUOUS = 'ambiguous'
_HELD = 'held'


def _bounded_coord(value):
    return integer(value, -COORD_BOUND, COORD_BOUND)


def _parse(data):
    """Validate the exposure payload without mutating it."""
    fields(data, ('wafer', 'radius', 'die', 'lattice', 'layers'))
    wafer = ident(data['wafer'])
    radius = integer(data['radius'], 1, RADIUS_MAX)

    fields(data['die'], ('width', 'height'))
    width = integer(data['die']['width'], 1, DIE_SIDE_MAX)
    height = integer(data['die']['height'], 1, DIE_SIDE_MAX)

    fields(data['lattice'], ('origin', 'pitch'))
    origin = tuple(_bounded_coord(v) for v in array(data['lattice']['origin'], 2, 2))
    pitch = tuple(integer(v, 1, COORD_BOUND)
                  for v in array(data['lattice']['pitch'], 2, 2))

    layers = []
    layer_ids = []
    for raw_layer in array(data['layers'], 1, LAYERS_MAX):
        fields(raw_layer, ('id', 'shots'))
        layer_id = ident(raw_layer['id'])
        layer_ids.append(layer_id)
        shots = []
        shot_ids = []
        for raw_shot in array(raw_layer['shots'], 1, SHOTS_PER_LAYER_MAX):
            fields(raw_shot, ('id', 'bounds', 'state'))
            shot_id = ident(raw_shot['id'])
            shot_ids.append(shot_id)
            bounds = array(raw_shot['bounds'], 4, 4)
            min_x, min_y, max_x, max_y = (_bounded_coord(v) for v in bounds)
            if not min_x < max_x or not min_y < max_y:
                raise ValueError('shot bounds must enclose a positive area')
            state = raw_shot['state']
            if state not in ('ready', 'hold'):
                raise ValueError('invalid shot state')
            shots.append({'id': shot_id,
                          'bounds': (min_x, min_y, max_x, max_y),
                          'state': state})
        unique(shot_ids)
        # Shot order is never meaningful: sort by id once so every later
        # result is independent of the input arrangement.
        shots.sort(key=lambda shot: shot['id'])
        layers.append({'id': layer_id, 'shots': shots})
    unique(layer_ids)
    layers.sort(key=lambda layer: layer['id'])

    return {'wafer': wafer, 'radius': radius, 'width': width,
            'height': height, 'origin': origin, 'pitch': pitch,
            'layers': layers}


def _index_span(origin, pitch, radius):
    """Integer index range whose grid points fit the coordinate bound.

    Both |origin + i*pitch| <= COORD_BOUND and the in-circle condition
    |center| <= radius (radius <= COORD_BOUND) are covered, so every
    candidate grid point lies inside the span and every generated point
    respects the repository coordinate boundary.
    """
    low = (-COORD_BOUND - origin) // pitch
    high = (COORD_BOUND - origin) // pitch
    if -radius - origin > -COORD_BOUND:
        bound = (-radius - origin) // pitch
        if bound > low:
            low = bound
    if radius - origin < COORD_BOUND:
        bound = (radius - origin) // pitch
        if bound < high:
            high = bound
    return low, high


def _candidate_centers(parsed):
    """All distinct in-circle lattice points, sorted by y then x."""
    ox, oy = parsed['origin']
    px, py = parsed['pitch']
    radius = parsed['radius']
    radius_sq = radius * radius
    ix_low, ix_high = _index_span(ox, px, radius)
    iy_low, iy_high = _index_span(oy, py, radius)

    total = 0
    columns = []
    for i in range(ix_low, ix_high + 1):
        x = ox + i * px
        limit_sq = radius_sq - x * x
        if limit_sq < 0:
            columns.append((x, []))
            continue
        row_indices = []
        for j in range(iy_low, iy_high + 1):
            y = oy + j * py
            if y * y <= limit_sq:
                row_indices.append(j)
                total += 1
                if total > CANDIDATES_MAX:
                    raise ValueError('in-circle candidate limit exceeded')
        columns.append((x, row_indices))

    points = []
    seen = set()
    for x, row_indices in columns:
        for j in row_indices:
            point = (x, oy + j * py)
            if point in seen:
                raise ValueError('duplicate candidate center')
            seen.add(point)
            points.append(point)
    points.sort(key=lambda point: (point[1], point[0]))
    return points


def _grid_span(low_value, high_value, origin, pitch):
    """Grid index range whose points lie inside the closed integer interval.

    ``origin`` and ``pitch`` here are already expressed in doubled units, so
    the same routine handles odd die sides exactly.  The lower bound uses
    ``ceil(a/p) = -floor(-a/p)`` rather than an unsigned rounding trick.
    """
    low_index = -((origin - low_value) // pitch)
    high_index = (high_value - origin) // pitch
    return low_index, high_index


def _layer_coverage(parsed, points, columns, layer):
    """Bit mask (bit k = k-th shot id) of full covers for every point.

    A shot fully covers a die when the closed die rectangle fits inside the
    closed shot bounds.  All comparisons use doubled coordinates, keeping
    half-unit edges exact for odd die widths and heights.  A per-column
    sweep over shot entry/exit events keeps the work proportional to the
    number of actual (shot, die) coverings rather than the Cartesian
    product of shots and candidates.
    """
    ox, oy = parsed['origin']
    px, py = parsed['pitch']
    half_w = parsed['width']
    half_h = parsed['height']
    shots = layer['shots']

    # For each shot, the closed index rectangle of grid points it can cover.
    x_intervals = []
    y_intervals = []
    for k, shot in enumerate(shots):
        min_x, min_y, max_x, max_y = shot['bounds']
        ix_lo, ix_hi = _grid_span(2 * min_x + half_w,
                                  2 * max_x - half_w, 2 * ox, 2 * px)
        iy_lo, iy_hi = _grid_span(2 * min_y + half_h,
                                  2 * max_y - half_h, 2 * oy, 2 * py)
        if ix_lo <= ix_hi and iy_lo <= iy_hi:
            x_intervals.append((ix_lo, ix_hi, k))
            y_intervals.append((iy_lo, iy_hi, k))
    entries = sorted((lo, k) for lo, _, k in x_intervals)
    exits = sorted((hi + 1, k) for _, hi, k in x_intervals)
    y_by_shot = {k: (lo, hi) for lo, hi, k in y_intervals}

    coverage = [0] * len(points)
    active = []
    entry_pos = exit_pos = 0
    for i, column in columns:
        while entry_pos < len(entries) and entries[entry_pos][0] <= i:
            active.append(entries[entry_pos][1])
            entry_pos += 1
        while exit_pos < len(exits) and exits[exit_pos][0] <= i:
            active.remove(exits[exit_pos][1])
            exit_pos += 1
        if not active or not column:
            continue
        # Sweep y inside this column; each active shot contributes one
        # contiguous row interval.
        events = []
        for k in active:
            lo, hi = y_by_shot[k]
            bit = 1 << k
            events.append((lo, bit))
            events.append((hi + 1, -bit))
        events.sort(key=lambda event: event[0])
        mask = 0
        event_pos = 0
        for point_index, j in column:
            while event_pos < len(events) and events[event_pos][0] <= j:
                delta = events[event_pos][1]
                if delta > 0:
                    mask |= delta
                else:
                    mask &= ~(-delta)
                event_pos += 1
            if mask:
                coverage[point_index] = mask
    return coverage


def compile_exposure_map(data):
    """Compile wafer geometry and layer exposure records into canonical dies.

    Returns a JSON-serializable mapping with schema
    ``wafer-exposure-map-v1``: ``sites`` holds the probeable die centers
    (directly usable as the ``sites`` of :func:`process_calibrated`) while
    ``dies`` keeps every in-circle candidate with its full exposure
    attribution and exclusion reasons.
    """
    parsed = _parse(data)
    points = _candidate_centers(parsed)

    radius_sq = parsed['radius'] * parsed['radius']
    half_w = parsed['width']
    half_h = parsed['height']
    # Columns keyed by grid index i, each holding (point index, j) rows;
    # points is y-then-x sorted, so rows inside a column stay y ordered.
    ox, oy = parsed['origin']
    px, py = parsed['pitch']
    column_map = {}
    for idx, (x, y) in enumerate(points):
        i = (x - ox) // px
        j = (y - oy) // py
        column_map.setdefault(i, []).append((idx, j))
    columns = sorted(column_map.items())

    layer_results = []
    for layer in parsed['layers']:
        coverage = _layer_coverage(parsed, points, columns, layer)
        states = [shot['state'] for shot in layer['shots']]
        layer_results.append((layer['id'], layer['shots'], coverage, states))

    dies = []
    sites = []
    four_radius_sq = 4 * radius_sq
    for idx, (x, y) in enumerate(points):
        # Exact wafer test on doubled coordinates: the four die corners must
        # all satisfy x^2 + y^2 <= r^2.  The farthest corner of an
        # axis-aligned rectangle from the origin is the one maximizing both
        # |dx| and |dy|, so a single squared distance decides.
        far_x = 2 * abs(x) + half_w
        far_y = 2 * abs(y) + half_h
        edge_excluded = far_x * far_x + far_y * far_y > four_radius_sq

        exposures = []
        reasons = []
        if edge_excluded:
            reasons.append(_WAFER_EDGE)
        fully_ready = not edge_excluded
        for layer_id, shots, coverage, states in layer_results:
            mask = coverage[idx]
            if mask:
                shot_ids = [shot['id'] for k, shot in enumerate(shots)
                            if (mask >> k) & 1]
            else:
                shot_ids = []
            exposures.append({'layer': layer_id, 'shot_ids': shot_ids})
            count = len(shot_ids)
            if count == 0:
                reasons.append({'layer': layer_id, 'reason': _UNCOVERED})
                fully_ready = False
            elif count > 1:
                reasons.append({'layer': layer_id, 'reason': _AMBIGUOUS})
                fully_ready = False
            elif states[mask.bit_length() - 1] == 'hold':
                reasons.append({'layer': layer_id, 'reason': _HELD})
                fully_ready = False

        if fully_ready:
            status = PROBEABLE
            sites.append([x, y])
            if len(sites) > SITES_MAX:
                raise ValueError('probeable sites limit exceeded')
        else:
            status = EXCLUDED
        dies.append({'at': [x, y], 'status': status,
                     'exposures': exposures, 'reasons': reasons})

    return {'schema': SCHEMA, 'wafer': parsed['wafer'],
            'sites': sites, 'dies': dies}
