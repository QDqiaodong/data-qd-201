"""Reproducible scale regression for process_calibrated.

Run from the repository root with an installed (or .pth-linked) package:

    python -I tests/scale_regression.py

The construction below follows the fixed acceptance specification: 141x141
canonical sites, 20 stations, 99x99 local readings per station, all eight
zero-offset orthogonal candidates, origin benchmark links over the
three-station window, modular binning and the axial overrides.

Only the process_calibrated call is timed, using process CPU time in this
single process; the full, unsampled result is asserted on. The budget is
30 seconds on stock Linux x86-64 CPython 3.12.
"""
import json
import time

from wafer_fusion import process_calibrated

TIME_BUDGET_SECONDS = 30.0

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
AXIAL = [(1, 0), (-1, 0), (0, 1), (0, -1)]


def build_payload():
    sites = [[x, y] for y in range(-70, 71) for x in range(-70, 71)]
    runs = []
    for i in range(20):
        records = []
        for y in range(-49, 50):
            for x in range(-49, 50):
                code = (13 * x + 7 * y + (i + 1) * x * y + 11 * i) % 23
                if code == 0:
                    bin_code = 'F'
                elif code == 1:
                    bin_code = 'S'
                else:
                    bin_code = 'P'
                records.append({'at': [x, y], 'bin': bin_code})
        # The origin passes at every station.
        origin = next(item for item in records if item['at'] == [0, 0])
        origin['bin'] = 'P'
        # Axial overrides: run 0 passes only at [1,0]; run 1 is the inverse.
        if i in (0, 1):
            for item in records:
                if tuple(item['at']) in AXIAL:
                    at_one = item['at'] == [1, 0]
                    item['bin'] = 'P' if (at_one if i == 0 else not at_one) else 'F'
        runs.append({
            'id': f'R{i:02d}',
            'bins': {'P': 'pass', 'F': 'fail', 'S': 'skip'},
            'records': records,
            'candidates': [{'id': cid, 'matrix': matrix, 'offset': [0, 0]}
                           for cid, matrix in EIGHT],
        })
    links = []
    for i in range(20):
        for gap in (1, 2):
            j = i + gap
            if j < 20:
                links.append({'left': {'run': f'R{i:02d}', 'at': [0, 0]},
                              'right': {'run': f'R{j:02d}', 'at': [0, 0]}})
    # The first two stations additionally share the [1,0] benchmark.
    links.append({'left': {'run': 'R00', 'at': [1, 0]},
                  'right': {'run': 'R01', 'at': [1, 0]}})
    return {'wafer': 'W-SCALE', 'sites': sites, 'runs': runs, 'links': links}


def main():
    payload = build_payload()
    assert len(payload['sites']) == 141 * 141
    assert sum(len(run['records']) for run in payload['runs']) == 20 * 99 * 99

    start = time.process_time()
    result = process_calibrated(payload)
    elapsed = time.process_time() - start

    assert result['schema'] == 'wafer-calibration-v1'
    # Every station keeps all eight supported candidates.
    assert len(result['supported_candidates']) == 20
    for item in result['supported_candidates']:
        assert item['candidates'] == sorted(cid for cid, _ in EIGHT)

    by_at = {tuple(die['at']): die['possible_statuses'] for die in result['dies']}
    assert len(by_at) == 141 * 141

    # The origin is pass under every calibration and therefore picked.
    assert by_at[(0, 0)] == ['pass']
    assert [0, 0] in result['pick_coordinates']

    # Each axial die can fail (and none is released).
    for point in AXIAL:
        assert 'fail' in by_at[point], (point, by_at[point])
        assert list(point) not in result['pick_coordinates']

    # The extra [1,0] benchmark (beyond the origin-only links) is what
    # forces every axial die to fail: dropping it makes all three statuses
    # reachable, while origin links alone cannot distinguish the gauges.
    trimmed = build_payload()
    trimmed['links'] = [link for link in trimmed['links']
                        if link['left']['at'] != [1, 0]]
    relaxed = process_calibrated(trimmed)
    relaxed_by_at = {tuple(die['at']): die['possible_statuses']
                     for die in relaxed['dies']}
    for point in AXIAL:
        assert by_at[point] == ['fail'], (point, by_at[point])
        assert set(relaxed_by_at[point]) == {'pass', 'incomplete', 'fail'}

    # Anything beyond the recorded 99x99 square is missing at every station
    # under every transform and can only be incomplete.
    edge_dies = [p for p in by_at if abs(p[0]) > 49 or abs(p[1]) > 49]
    assert edge_dies, 'regression fixture must include out-of-square dies'
    for point in edge_dies:
        assert by_at[point] == ['incomplete'], (point, by_at[point])
        assert list(point) not in result['pick_coordinates']

    # Every released coordinate is exactly the pass-only die set, in order.
    assert result['pick_coordinates'] == [
        die['at'] for die in result['dies']
        if die['possible_statuses'] == ['pass']]
    json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)

    print(f'scale regression OK: {len(by_at)} dies, '
          f'{len(result["pick_coordinates"])} picked, '
          f'process CPU time {elapsed:.3f}s (budget {TIME_BUDGET_SECONDS}s)')
    if elapsed >= TIME_BUDGET_SECONDS:
        raise SystemExit(f'time budget exceeded: {elapsed:.3f}s')


if __name__ == '__main__':
    main()
