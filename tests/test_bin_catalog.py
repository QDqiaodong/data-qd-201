"""Acceptance tests for compile_bin_catalog; every fixture is synthetic.

Run with the package importable, e.g. from the repository root::

    .venv/bin/python -I -m unittest discover -s tests -v
"""
import copy
import itertools
import json
import unittest

from wafer_fusion import compile_bin_catalog


def recipe(recipe_id, parents=(), changes=None):
    return {'id': recipe_id, 'parents': list(parents),
            'changes': dict(changes or {})}


def catalog(*recipes):
    return {'recipes': list(recipes)}


def by_id(result):
    return {item['id']: item for item in result['recipes']}


def codes_of(result, recipe_id):
    return {item['code']: item for item in by_id(result)[recipe_id]['codes']}


def branches():
    """Base with a pass code, a skip code and an alias; two product branches.

    ``alpha`` overrides the pass code with ``fail`` and ``beta`` retires it;
    the alias points at the skip code, so beta stays a valid catalog.
    """
    return [
        recipe('base', [], {'P': 'pass', 'S': 'skip', 'A': {'alias': 'S'}}),
        recipe('alpha', ['base'], {'P': 'fail'}),
        recipe('beta', ['base'], {'P': None}),
        recipe('sibling', ['base'], {'S': 'fail'}),
        recipe('leaf', ['base'], {'Z': {'alias': 'A'}}),
    ]


class BranchIsolationTests(unittest.TestCase):
    """Acceptance group 1: branches never leak into siblings or ancestors."""

    def test_override_and_retirement_are_isolated(self):
        result = compile_bin_catalog(catalog(*branches()))
        ga = codes_of(result, 'alpha')
        gb = codes_of(result, 'beta')
        gs = codes_of(result, 'sibling')

        # Alpha overrides P to fail; S and the alias chain stay intact.
        self.assertEqual(ga['P']['disposition'], 'fail')
        self.assertEqual(ga['P']['canonical_code'], 'P')
        self.assertEqual(ga['P']['definition_sources'], ['alpha'])
        self.assertEqual(ga['A']['canonical_code'], 'S')
        self.assertEqual(ga['A']['disposition'], 'skip')
        self.assertEqual(ga['A']['definition_sources'], ['base'])

        # Beta retires P: the code disappears but nothing else changes.
        self.assertNotIn('P', gb)
        self.assertEqual(gb['S']['disposition'], 'skip')
        self.assertEqual(gb['A']['canonical_code'], 'S')
        self.assertEqual(gb['A']['definition_sources'], ['base'])

        # The sibling's own override does not disturb anything else.
        self.assertEqual(gs['S']['disposition'], 'fail')
        self.assertEqual(gs['S']['definition_sources'], ['sibling'])
        self.assertEqual(gs['A']['canonical_code'], 'S')
        self.assertEqual(gs['A']['disposition'], 'fail')
        self.assertEqual(gs['P']['disposition'], 'pass')

    def test_ancestor_catalog_is_unchanged(self):
        result = compile_bin_catalog(catalog(*branches()))
        base = codes_of(result, 'base')
        self.assertEqual(sorted(base), ['A', 'P', 'S'])
        self.assertEqual(base['P']['disposition'], 'pass')
        self.assertEqual(base['S']['disposition'], 'skip')
        self.assertEqual(base['A']['canonical_code'], 'S')
        self.assertEqual(base['A']['disposition'], 'skip')
        self.assertEqual(base['A']['definition_sources'], ['base'])
        # Every output recipe keeps only its effective codes in code order.
        for item in result['recipes']:
            self.assertEqual([entry['code'] for entry in item['codes']],
                             sorted(entry['code'] for entry in item['codes']))

    def test_multilevel_alias_tracks_every_chain_node(self):
        data = catalog(
            recipe('root', [], {'P': 'pass', 'A1': {'alias': 'P'}}),
            recipe('mid', ['root'], {'A2': {'alias': 'A1'}}),
            recipe('leaf', ['mid'], {'A3': {'alias': 'A2'}}))
        got = codes_of(compile_bin_catalog(data), 'leaf')
        self.assertEqual(got['A3']['canonical_code'], 'P')
        self.assertEqual(got['A3']['disposition'], 'pass')
        self.assertEqual(got['A3']['definition_sources'], ['leaf', 'mid', 'root'])
        self.assertEqual(got['A2']['definition_sources'], ['mid', 'root'])
        self.assertEqual(got['A1']['definition_sources'], ['root'])
        self.assertEqual(got['P']['definition_sources'], ['root'])

    def test_no_partial_output_on_any_invalid_recipe(self):
        data = catalog(
            recipe('good', [], {'P': 'pass'}),
            recipe('bad', ['good'], {'B': {'alias': 'missing'}}))
        with self.assertRaises(ValueError):
            compile_bin_catalog(data)


class ParentMergeTests(unittest.TestCase):
    """Acceptance group 2: provenance unions and conflict handling."""

    def test_identical_definition_merges_sources(self):
        data = catalog(
            recipe('left', [], {'X': 'pass'}),
            recipe('right', [], {'X': 'pass'}),
            recipe('child', ['left', 'right'], {}))
        got = codes_of(compile_bin_catalog(data), 'child')
        self.assertEqual(got['X'],
                         {'code': 'X', 'canonical_code': 'X',
                          'disposition': 'pass',
                          'definition_sources': ['left', 'right']})

    def test_identical_definition_through_shared_ancestor_merges_sources(self):
        data = catalog(
            recipe('base', [], {'X': 'pass'}),
            recipe('left', ['base'], {}),
            recipe('right', ['base'], {}),
            recipe('child', ['left', 'right'], {}))
        got = codes_of(compile_bin_catalog(data), 'child')
        # Both paths carry the definition actually authored by base once.
        self.assertEqual(got['X']['definition_sources'], ['base'])

    def test_conflicting_definitions_are_rejected_in_either_order(self):
        left = recipe('left', [], {'X': 'pass'})
        right = recipe('right', [], {'X': 'fail'})
        with self.assertRaises(ValueError):
            compile_bin_catalog(catalog(left, right,
                                        recipe('child', ['left', 'right'])))
        with self.assertRaises(ValueError):
            compile_bin_catalog(catalog(right, left,
                                        recipe('child', ['right', 'left'])))

    def test_explicit_disposition_resolves_conflict(self):
        data = catalog(
            recipe('left', [], {'X': 'pass'}),
            recipe('right', [], {'X': 'fail'}),
            recipe('child', ['left', 'right'], {'X': 'skip'}))
        got = codes_of(compile_bin_catalog(data), 'child')
        self.assertEqual(got['X']['disposition'], 'skip')
        self.assertEqual(got['X']['definition_sources'], ['child'])

    def test_explicit_null_resolves_conflict(self):
        data = catalog(
            recipe('left', [], {'X': 'pass', 'Y': 'skip'}),
            recipe('right', [], {'X': 'fail', 'Y': 'skip'}),
            recipe('child', ['left', 'right'], {'X': None}))
        got = codes_of(compile_bin_catalog(data), 'child')
        self.assertNotIn('X', got)
        self.assertEqual(got['Y']['definition_sources'], ['left', 'right'])

    def test_explicit_alias_resolves_conflict_with_parent_target(self):
        data = catalog(
            recipe('left', [], {'X': 'pass', 'Y': 'skip'}),
            recipe('right', [], {'X': 'fail'}),
            recipe('child', ['left', 'right'], {'X': {'alias': 'Y'}}))
        got = codes_of(compile_bin_catalog(data), 'child')
        self.assertEqual(got['X']['canonical_code'], 'Y')
        self.assertEqual(got['X']['disposition'], 'skip')
        self.assertEqual(got['X']['definition_sources'], ['child', 'left'])

    def test_alias_to_code_from_other_parent_inherits_sources(self):
        data = catalog(
            recipe('left', [], {'AX': {'alias': 'Y'}, 'Y': 'pass'}),
            recipe('right', [], {'Y': 'pass', 'RX': {'alias': 'Y'}}),
            recipe('child', ['left', 'right'], {}))
        got = codes_of(compile_bin_catalog(data), 'child')
        # The alias is authored by left; Y is provided identically by both.
        self.assertEqual(got['AX']['canonical_code'], 'Y')
        self.assertEqual(got['AX']['disposition'], 'pass')
        self.assertEqual(got['AX']['definition_sources'], ['left', 'right'])
        self.assertEqual(got['Y']['definition_sources'], ['left', 'right'])
        # The mirror alias authored by right reaches the same endpoint.
        self.assertEqual(got['RX']['canonical_code'], 'Y')
        self.assertEqual(got['RX']['definition_sources'], ['left', 'right'])

    def test_identical_alias_over_multiple_parent_paths_merges_sources(self):
        data = catalog(
            recipe('base', [], {'Y': 'fail'}),
            recipe('left', ['base'], {'X': {'alias': 'Y'}}),
            recipe('right', ['base'], {'X': {'alias': 'Y'}}),
            recipe('child', ['left', 'right'], {}))
        got = codes_of(compile_bin_catalog(data), 'child')
        self.assertEqual(got['X']['canonical_code'], 'Y')
        self.assertEqual(got['X']['disposition'], 'fail')
        self.assertEqual(got['X']['definition_sources'],
                         ['base', 'left', 'right'])

    def test_missing_code_in_one_parent_is_not_a_conflict(self):
        data = catalog(
            recipe('left', [], {'X': 'pass'}),
            recipe('right', [], {'Y': 'fail'}),
            recipe('child', ['left', 'right'], {}))
        got = codes_of(compile_bin_catalog(data), 'child')
        self.assertEqual(got['X']['definition_sources'], ['left'])
        self.assertEqual(got['Y']['definition_sources'], ['right'])

    def test_retirement_in_one_parent_leaves_other_path(self):
        data = catalog(
            recipe('base', [], {'X': 'pass', 'Y': 'skip'}),
            recipe('retire', ['base'], {'X': None}),
            recipe('child', ['retire', 'base'], {}))
        got = codes_of(compile_bin_catalog(data), 'child')
        # Only the still-visible base path contributes X.
        self.assertEqual(got['X']['definition_sources'], ['base'])
        self.assertEqual(got['Y']['definition_sources'], ['base'])


class RejectionTests(unittest.TestCase):
    """Acceptance group 3: every violation rejects the whole catalog."""

    def assertRejected(self, data):
        snapshot = copy.deepcopy(data)
        with self.assertRaises(ValueError):
            compile_bin_catalog(data)
        self.assertEqual(data, snapshot)

    def test_top_level_structure(self):
        self.assertRejected(None)
        self.assertRejected([])
        self.assertRejected({})
        self.assertRejected({'recipes': [], 'extra': 1})
        self.assertRejected({'recipes': []})
        self.assertRejected({'recipes': 'nope'})

    def test_recipe_fields(self):
        self.assertRejected(catalog({'id': 'r', 'parents': []}))
        self.assertRejected(catalog({'id': 'r', 'parents': [],
                                     'changes': {}, 'extra': 1}))
        self.assertRejected(catalog({'id': 'r', 'parents': [], 'changes': []}))
        self.assertRejected(catalog({'id': 'r', 'parents': None,
                                     'changes': {}}))
        self.assertRejected(catalog(recipe('r', [], {'X': 'unknown'})))
        self.assertRejected(catalog(recipe('r', [], {'X': 'PASS'})))
        self.assertRejected(catalog(recipe('r', [], {'X': True})))
        self.assertRejected(catalog(recipe('r', [], {'X': 3})))
        self.assertRejected(catalog(recipe('r', [], {'X': {'alias': 'Y',
                                                           'note': 'x'}})))
        self.assertRejected(catalog(recipe('r', [], {'X': {}})))
        self.assertRejected(catalog(recipe('r', [], {'X': 'pass',
                                                      'bad code': 'pass'})))
        self.assertRejected(catalog(recipe('bad id', [], {'X': 'pass'})))

    def test_duplicate_identities(self):
        self.assertRejected(catalog(recipe('r', [], {'X': 'pass'}),
                                    recipe('r', [], {'Y': 'pass'})))
        self.assertRejected(catalog(
            recipe('a', [], {'X': 'pass'}),
            recipe('b', ['a', 'a'], {})))

    def test_missing_parent_and_cycles(self):
        self.assertRejected(catalog(recipe('r', ['ghost'], {})))
        self.assertRejected(catalog(recipe('r', ['r'], {})))
        self.assertRejected(catalog(recipe('a', ['b'], {}),
                                    recipe('b', ['a'], {})))
        self.assertRejected(catalog(recipe('a', ['c'], {}),
                                    recipe('b', ['a'], {}),
                                    recipe('c', ['b'], {})))

    def test_retirement_without_parent_source(self):
        self.assertRejected(catalog(recipe('r', [], {'X': None})))
        self.assertRejected(catalog(
            recipe('base', [], {'Y': 'pass'}),
            recipe('child', ['base'], {'X': None})))

    def test_alias_errors(self):
        self.assertRejected(catalog(recipe('r', [], {'X': {'alias': 'Y'}})))
        self.assertRejected(catalog(
            recipe('r', [], {'X': {'alias': 'Y'}, 'Y': {'alias': 'X'}})))
        self.assertRejected(catalog(
            recipe('r', [], {'X': {'alias': 'X'}})))
        self.assertRejected(catalog(
            recipe('base', [], {'P': 'pass', 'A': {'alias': 'P'}}),
            recipe('child', ['base'], {'P': None})))

    def test_unreferenced_invalid_recipe_rejects_catalog(self):
        self.assertRejected(catalog(
            recipe('good', [], {'X': 'pass'}),
            recipe('orphan', [], {'Z': {'alias': 'nope'}})))

    def test_conflict_rejected_even_if_unreferenced(self):
        self.assertRejected(catalog(
            recipe('left', [], {'X': 'pass'}),
            recipe('right', [], {'X': 'fail'}),
            recipe('child', ['left', 'right'], {}),
            recipe('standalone', [], {'Q': 'skip'})))

    def test_size_limits(self):
        # Single recipe at the change boundary is fine; one more is not.
        ok = catalog(recipe('r', [], {f'C{i:03d}': 'pass'
                                      for i in range(128)}))
        self.assertEqual(len(compile_bin_catalog(ok)['recipes'][0]['codes']),
                         128)
        self.assertRejected(catalog(recipe('r', [], {f'C{i:03d}': 'pass'
                                                     for i in range(129)})))

        # 64 recipes compile; 65 are rejected.
        for size in (64, 65):
            data = catalog(*[recipe(f'R{i:02d}', [], {}) for i in range(size)])
            if size == 64:
                self.assertEqual(len(compile_bin_catalog(data)['recipes']), 64)
            else:
                self.assertRejected(data)

        # 8 parents are accepted, 9 are rejected.
        parent_recipes = [recipe(f'P{i}', [], {}) for i in range(8)]
        eight_parents = catalog(
            *copy.deepcopy(parent_recipes),
            recipe('C', [f'P{i}' for i in range(8)], {}))
        self.assertEqual(len(compile_bin_catalog(eight_parents)['recipes']), 9)
        nine = [recipe(f'P{i}', [], {}) for i in range(9)]
        self.assertRejected(catalog(
            *nine, recipe('C', [f'P{i}' for i in range(9)], {})))

    def test_effective_code_limit(self):
        # 256 effective codes (128 inherited + 128 local) is fine; 257 is not.
        base = {f'A{i:03d}': 'pass' for i in range(128)}
        ok = catalog(recipe('base', [], base),
                     recipe('child', ['base'],
                            {f'B{i:03d}': 'skip' for i in range(128)}))
        compiled = by_id(compile_bin_catalog(ok))
        self.assertEqual(len(compiled['child']['codes']), 256)
        self.assertRejected(catalog(
            recipe('base', [], base),
            recipe('child', ['base'],
                   {f'B{i:03d}': 'skip' for i in range(129)})))


class PermutationAndPurityTests(unittest.TestCase):
    """Acceptance group 4: order independence and non-mutation."""

    def test_recipe_parent_and_change_permutations(self):
        data = catalog(*branches())
        expected = compile_bin_catalog(data)
        orders = list(itertools.permutations(data['recipes']))
        for recipe_order in orders[:6] + orders[-6:]:
            for reverse_parents in (False, True):
                variant = catalog(*copy.deepcopy(recipe_order))
                for item in variant['recipes']:
                    if reverse_parents:
                        item['parents'].reverse()
                    item['changes'] = dict(reversed(list(item['changes'].items())))
                self.assertEqual(compile_bin_catalog(variant), expected)

    def test_change_table_order_does_not_matter(self):
        forward = catalog(recipe('base', [], {'P': 'pass', 'S': 'skip'}),
                          recipe('child', ['base'],
                                 {'A': {'alias': 'S'}, 'B': 'fail'}))
        backward = catalog(recipe('base', [], {'S': 'skip', 'P': 'pass'}),
                           recipe('child', ['base'],
                                  {'B': 'fail', 'A': {'alias': 'S'}}))
        self.assertEqual(compile_bin_catalog(forward),
                         compile_bin_catalog(backward))

    def test_parent_order_does_not_change_effective_output(self):
        left = recipe('left', [], {'X': 'pass', 'A': {'alias': 'X'}})
        right = recipe('right', [], {'X': 'pass', 'Y': 'skip'})
        one = compile_bin_catalog(catalog(
            left, right, recipe('child', ['left', 'right'], {})))
        two = compile_bin_catalog(catalog(
            right, left, recipe('child', ['right', 'left'], {})))
        self.assertEqual(one, two)

    def test_input_is_never_mutated(self):
        data = catalog(*branches())
        snapshot = copy.deepcopy(data)
        result = compile_bin_catalog(data)
        self.assertEqual(data, snapshot)
        # Result is JSON-serializable and detached from the input.
        json.dumps(result)
        result['recipes'][0]['codes'].clear()
        self.assertEqual(data, snapshot)


if __name__ == '__main__':
    unittest.main()
