"""Compile a product bin-recipe catalog into effective code definitions.

Products share an enterprise-wide bin baseline and then add, override or
retire codes per recipe.  :func:`compile_bin_catalog` walks the recipe DAG
parents-first and produces, for every recipe, the effective code set with its
canonical alias endpoint, final disposition and the upstream recipes that
authored each definition.

Semantics worth calling out:

* Parent order is never business priority.  Identical definitions arriving
  from several parents merge into one definition whose provenance is the
  union of the actual authoring recipes; differing definitions are an
  unresolved conflict unless the current recipe explicitly overrides the
  code with a disposition, an alias or ``null`` (retire).
* ``null`` only retires a code inherited from at least one parent, and the
  retirement is local to this recipe: siblings and descendants are unaffected.
* Aliases are resolved only after all inheritance, local changes and
  conflicts of a recipe are settled.  A dangling alias, an alias cycle or an
  alias to a retired code invalidates the whole catalog.
* Input objects are only read; every result is a fresh JSON-serializable
  structure.  Any invalid structure or business error raises ``ValueError``
  and no partial catalog is returned.
"""
from .checks import array, fields, ident, unique

RECIPES_MAX = 64
PARENTS_MAX = 8
CHANGES_MAX = 128
CODES_MAX = 256

DISPOSITIONS = frozenset(('pass', 'fail', 'skip'))

_CONFLICT = object()


# --------------------------------------------------------------------- parsing

def _definition(value):
    """Parse one change value into an immutable definition or ``None``.

    ``None`` means "retire the inherited code"; a disposition is
    ``('disposition', value)``; an alias is ``('alias', target)``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if value not in DISPOSITIONS:
            raise ValueError('invalid bin disposition')
        return ('disposition', value)
    if isinstance(value, dict):
        fields(value, ('alias',))
        return ('alias', ident(value['alias']))
    raise ValueError('invalid bin definition')


def _parse(data):
    """Validate the catalog structure without mutating the caller's data."""
    fields(data, ('recipes',))
    parsed = []
    recipe_ids = []
    for raw_recipe in array(data['recipes'], 1, RECIPES_MAX):
        fields(raw_recipe, ('id', 'parents', 'changes'))
        recipe_id = ident(raw_recipe['id'])
        recipe_ids.append(recipe_id)

        parent_ids = [ident(value)
                      for value in array(raw_recipe['parents'], 0, PARENTS_MAX)]
        unique(parent_ids)

        raw_changes = raw_recipe['changes']
        if not isinstance(raw_changes, dict):
            raise ValueError('changes must be an object')
        if len(raw_changes) > CHANGES_MAX:
            raise ValueError('change table size')
        changes = {}
        for raw_code, raw_value in raw_changes.items():
            changes[ident(raw_code)] = _definition(raw_value)

        parsed.append({'id': recipe_id, 'parents': parent_ids,
                       'changes': changes})
    unique(recipe_ids)
    return parsed


def _topological_order(recipes):
    """Parents-first order of the recipe DAG; rejects unknown parents/cycles.

    The traversal only follows declared parent links, so the result is a
    function of the graph, not of the input array order.
    """
    by_id = {recipe['id']: recipe for recipe in recipes}
    for recipe in recipes:
        for parent in recipe['parents']:
            if parent not in by_id:
                raise ValueError(f'unknown parent recipe: {parent}')

    state = {}
    order = []

    def visit(recipe_id):
        mark = state.get(recipe_id)
        if mark == 'done':
            return
        if mark == 'active':
            raise ValueError('recipe inheritance cycle')
        state[recipe_id] = 'active'
        for parent in by_id[recipe_id]['parents']:
            visit(parent)
        state[recipe_id] = 'done'
        order.append(recipe_id)

    for recipe in recipes:
        visit(recipe['id'])
    return by_id, order


# ------------------------------------------------------- effective definitions

def _merge_parents(parent_maps):
    """Combine parent effective maps for one recipe.

    Returns code -> definition, with :data:`_CONFLICT` for codes whose
    parents disagree structurally.  Provenance of merged identical
    definitions is the union of the contributing definitions' sources.
    """
    definitions_by_code = {}
    for parent_map in parent_maps:
        for code, definition in parent_map.items():
            definitions_by_code.setdefault(code, []).append(definition)

    merged = {}
    for code, definitions in definitions_by_code.items():
        signature = definitions[0][:2]
        if all(definition[:2] == signature for definition in definitions):
            sources = frozenset()
            for definition in definitions:
                sources |= definition[2]
            merged[code] = (signature[0], signature[1], sources)
        else:
            merged[code] = _CONFLICT
    return merged


def _effective_map(recipe, parent_maps):
    """Apply inheritance and local changes to one recipe."""
    merged = _merge_parents(parent_maps)

    for code, change in recipe['changes'].items():
        if change is None:
            if code not in merged:
                raise ValueError('cannot retire a code no parent provides')
            del merged[code]
        else:
            # An explicit local change replaces any inherited definition and
            # is authored by this recipe; the alias target's provenance is
            # added later when the chain is resolved.
            merged[code] = (change[0], change[1], frozenset((recipe['id'],)))

    for definition in merged.values():
        if definition is _CONFLICT:
            raise ValueError('unresolved inherited bin definition conflict')

    return merged


# ------------------------------------------------------------ alias resolution

def _resolve(recipe_id, effective):
    """Resolve every alias chain in one recipe and build its output codes."""
    resolved = {}
    active = set()

    def resolve(code):
        if code in resolved:
            return resolved[code]
        if code in active:
            raise ValueError(f'alias cycle in recipe {recipe_id}')

        kind, value, sources = effective[code]
        if kind == 'disposition':
            result = (code, value, sources)
        else:
            target = value
            if target not in effective:
                # Covers both unknown targets and locally retired codes: a
                # disabled code is absent from the effective map.
                raise ValueError(f'alias to unavailable code: {target}')
            active.add(code)
            canonical, disposition, target_sources = resolve(target)
            active.discard(code)
            result = (canonical, disposition, sources | target_sources)
        resolved[code] = result
        return result

    codes = []
    for code in sorted(effective):
        canonical, disposition, sources = resolve(code)
        codes.append({'code': code,
                      'canonical_code': canonical,
                      'disposition': disposition,
                      'definition_sources': sorted(sources)})
    return codes


# ----------------------------------------------------------------------- entry

def compile_bin_catalog(data):
    """Compile a product bin-recipe catalog.

    ``data`` is exactly an object with a ``recipes`` array (1..64 recipes).
    Each recipe has exactly ``id``, ``parents`` and ``changes``.  The result
    is ``{'recipes': [...]}`` with recipes in ID order; every recipe lists
    its effective codes in code order with ``canonical_code``,
    ``disposition`` and the sorted upstream ``definition_sources``.

    Raises ``ValueError`` for any structural, inheritance, conflict, alias or
    size violation.  The input is never modified and no partial result is
    produced.
    """
    recipes = _parse(data)
    by_id, order = _topological_order(recipes)

    effective = {}
    for recipe_id in order:
        recipe = by_id[recipe_id]
        merged = _effective_map(
            recipe, [effective[parent] for parent in recipe['parents']])
        if len(merged) > CODES_MAX:
            raise ValueError('effective code limit exceeded')
        effective[recipe_id] = merged

    recipes_out = []
    for recipe_id in sorted(effective):
        recipes_out.append({'id': recipe_id,
                            'codes': _resolve(recipe_id,
                                              effective[recipe_id])})
    return {'recipes': recipes_out}
