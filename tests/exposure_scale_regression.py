"""Reproducible scale regression for compile_exposure_map.

Run from the repository root with the package importable::

    PYTHONPATH=src python -I tests/exposure_scale_regression.py

The synthetic fixture follows the fixed acceptance specification: radius
5000 on a 30-unit lattice (about 87k in-circle candidate centers), eight
exposure layers, and 128 rectangular shots per layer that tile the full
index grid, so every candidate has exactly one covering shot per layer.
The four-by-four central tile block stays ready in every layer; all other
shots are on hold, which keeps the released probeable set inside the
20000-site calibration contract without sampling or truncating any die.

Only the compile_exposure_map call is timed, using process CPU time in
this single process; the full, unsampled result is asserted on.  The
budget is 15 seconds on stock Linux x86-64 CPython 3.12.
"""
import copy
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from wafer_fusion import compile_exposure_map  # noqa: E402

TIME_BUDGET_SECONDS = 15.0
RADIUS = 5000
PITCH = 30
DIE_SIDE = 20
X_GROUPS = 16
Y_GROUPS = 8
READY_X = range(6, 10)
READY_Y = range(2, 6)
MIN_CANDIDATES = 80000


def _split(count, groups):
    base, remainder = divmod(count, groups)
    sizes = [base + (1 if g < remainder else 0) for g in range(groups)]
    starts = []
    start = 0
    for size in sizes:
        starts.append(start)
        start += size
    return starts, sizes


def build_payload():
    # In-circle lattice indices run from -166..166 on each axis.
    extent = RADIUS // PITCH
    indices = list(range(-extent, extent + 1))
    x_starts, x_sizes = _split(len(indices), X_GROUPS)
    y_starts, y_sizes = _split(len(indices), Y_GROUPS)
    assert sum(x_sizes) == len(indices)
    assert sum(y_sizes) == len(indices)

    layers = []
    for layer_index in range(8):
        shots = []
        for gx in range(X_GROUPS):
            ix_lo = indices[x_starts[gx]]
            ix_hi = indices[x_starts[gx] + x_sizes[gx] - 1]
            min_x = PITCH * ix_lo - DIE_SIDE // 2
            max_x = PITCH * ix_hi + DIE_SIDE // 2
            for gy in range(Y_GROUPS):
                iy_lo = indices[y_starts[gy]]
                iy_hi = indices[y_starts[gy] + y_sizes[gy] - 1]
                min_y = PITCH * iy_lo - DIE_SIDE // 2
                max_y = PITCH * iy_hi + DIE_SIDE // 2
                state = ('ready' if gx in READY_X and gy in READY_Y
                         else 'hold')
                shots.append({
                    'id': f's-{layer_index:02d}-{gx:02d}-{gy:02d}',
                    'bounds': [min_x, min_y, max_x, max_y],
                    'state': state})
        # Input order must never influence the compiled result; give every
        # layer a different deterministic permutation of its shots.
        shots = [shots[k] for k in range(len(shots))
                 if (k + 3 * layer_index) % 2 == 0] + \
                [shots[k] for k in range(len(shots))
                 if (k + 3 * layer_index) % 2 == 1]
        layers.append({'id': f'L{layer_index:02d}', 'shots': shots})

    return {
        'wafer': 'W-SCALE-EXPOSURE',
        'radius': RADIUS,
        'die': {'width': DIE_SIDE, 'height': DIE_SIDE},
        'lattice': {'origin': [0, 0], 'pitch': [PITCH, PITCH]},
        'layers': layers}


def main():
    payload = build_payload()
    assert len(payload['layers']) == 8
    assert all(len(layer['shots']) == 128 for layer in payload['layers'])

    start = time.process_time()
    result = compile_exposure_map(payload)
    elapsed = time.process_time() - start

    assert result['schema'] == 'wafer-exposure-map-v1'

    # Full, unsampled accounting: one result per distinct in-circle center.
    centers = [tuple(die['at']) for die in result['dies']]
    assert len(centers) == len(set(centers))
    assert MIN_CANDIDATES <= len(centers) <= 100000, len(centers)
    assert centers == sorted(centers, key=lambda p: (p[1], p[0]))

    # The tiling covers the whole index rectangle, so every die has exactly
    # one covering shot in every layer; exposures stay sorted by layer id.
    layer_ids = [layer['id'] for layer in
                 sorted(payload['layers'], key=lambda layer: layer['id'])]
    for die in result['dies']:
        assert [exposure['layer'] for exposure in die['exposures']] == \
            layer_ids
        for exposure in die['exposures']:
            assert len(exposure['shot_ids']) == 1
            assert exposure['shot_ids'] == sorted(exposure['shot_ids'])

    # sites must equal the probeable coordinates exactly, in the same order.
    probeable = [die['at'] for die in result['dies']
                 if die['status'] == 'probeable']
    excluded = [die for die in result['dies']
                if die['status'] == 'excluded']
    assert result['sites'] == probeable
    assert 1 <= len(result['sites']) <= 20000, len(result['sites'])
    assert len(probeable) + len(excluded) == len(centers)
    # Tiling leaves no uncovered/ambiguous layer; exclusion is wafer edge
    # or held shots only.
    for die in excluded:
        reasons = die['reasons']
        assert reasons and all(
            reason == 'wafer_edge' or reason['reason'] == 'held'
            for reason in reasons)

    json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)

    # Reordering layers and shots must leave the complete JSON unchanged.
    shuffled = copy.deepcopy(payload)
    shuffled['layers'].reverse()
    for layer in shuffled['layers']:
        layer['shots'].reverse()
    assert compile_exposure_map(shuffled) == result

    print(f'exposure scale regression OK: {len(centers)} candidates, '
          f'{len(result["sites"])} probeable sites, '
          f'process CPU time {elapsed:.3f}s (budget {TIME_BUDGET_SECONDS}s)')
    if elapsed >= TIME_BUDGET_SECONDS:
        raise SystemExit(f'time budget exceeded: {elapsed:.3f}s')


if __name__ == '__main__':
    main()
