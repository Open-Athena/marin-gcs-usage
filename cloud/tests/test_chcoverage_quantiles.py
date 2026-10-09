from dataclasses import replace

import pytest

from dt_cloud.chstore.coverage_quantiles import Frontier, SharedWeights, paired
from dt_cloud.chstore.hot_names import Node

Own = dict[str, tuple[int, int]]


def source(
    own: Own,
    *,
    reverse: bool = False,
    root: str = '',
) -> SharedWeights:
    """Fixture from explicit OWN weights, not production prefix/frontier helpers."""
    paths = {root, *own}
    for path in tuple(paths):
        while path != root:
            path = path.rpartition('/')[0]
            paths.add(path)
    children = {path: [] for path in paths}
    for path in paths - {root}:
        children[path.rpartition('/')[0]].append(path)
    ordered, bounds = [], {}

    def visit(path: str) -> None:
        pre = len(ordered)
        ordered.append(path)
        for child in sorted(children[path], key=lambda value: value.encode(), reverse=reverse):
            visit(child)
        bounds[path] = pre, len(ordered) - 1

    visit(root)
    totals = {}
    for path in reversed(ordered):
        b, o = own.get(path, (0, 0))
        totals[path] = b + sum(totals[child][0] for child in children[path]), o + sum(totals[child][1] for child in children[path])
    nodes = [Node(*bounds[path], path, *totals[path]) for path in ordered]
    return SharedWeights(nodes)


def oracle_total(
    own: Own,
    pattern: str,
    path: str,
) -> tuple[int, int]:
    rows = [value for key, value in own.items() if (key == path or not path or key.startswith(path + '/'))
            and any(pattern.lower() in name.lower() for name in key.split('/'))]
    return sum(b for b, _ in rows), sum(o for _, o in rows)


def oracle_partition(
    own: Own,
    pattern: str,
    path: str,
    threshold: int,
) -> dict:
    # Exhaustively examine EVERY own record and group by spelling, not source IDs.
    grouped, b, o = {}, 0, 0
    for key, (own_b, own_o) in own.items():
        if ((key != path and path and not key.startswith(path + '/')) or
                not any(pattern.lower() in name.lower() for name in key.split('/'))):
            continue
        b += own_b
        o += own_o
        if key != path:
            child = (path + '/' if path else '') + key[len(path) + bool(path):].split('/')[0]
            cb, co = grouped.get(child, (0, 0))
            grouped[child] = cb + own_b, co + own_o
    children = [{'path': child, 'b': cb, 'o': co} for child, (cb, co) in grouped.items() if cb >= threshold]
    children.sort(key=lambda row: (-row['b'], row['path']))
    return {'b': b, 'o': o, 'children': children, 'other': {'b': b - sum(row['b'] for row in children), 'o': o - sum(row['o'] for row in children)}}


def exact_partition(
    frontier: Frontier,
    own: Own,
    path: str,
    threshold: int,
) -> dict:
    result = frontier.partition(path, threshold)
    assert {key: result[key] for key in ('b', 'o', 'children', 'other')} == oracle_partition(own, frontier.pattern, path, threshold)
    assert result['cost']['probes'] == result['b'] // threshold
    assert result['cost']['candidate_children'] <= result['cost']['probes']
    return result


def test_quantile_inside_covered_root_refines_to_real_child_instead_of_the_frontier_directory():
    own = {'hit': (3, 2), 'hit/a': (4, 1), 'hit/b': (4, 1), 'hit/c': (0, 3), 'hit/nest': (1, 1), 'hit/nest/hit': (7, 1)}
    frontier = Frontier(source(own), 'HIT')
    assert [(node.path, node.b, node.o) for node in frontier.roots] == [('hit', 19, 9)]
    body = exact_partition(frontier, own, 'hit', 4)
    assert body == {'path': 'hit', 'present': True, 'b': 19, 'o': 9, 'threshold_bytes': 4,
                    'children': [{'path': 'hit/nest', 'b': 8, 'o': 2}, {'path': 'hit/a', 'b': 4, 'o': 1}, {'path': 'hit/b', 'b': 4, 'o': 1}],
                    'other': {'b': 3, 'o': 5}, 'cost': {'probes': 4, 'aggregate_calls': 5, 'frontier_selects': 0, 'shared_selects': 4, 'parent_hops': 5, 'candidate_children': 3}}
    exact_partition(frontier, own, 'hit/nest', 4)


def test_frontier_select_refines_nested_directory_hits_without_overlapping_rollups():
    own = {'scope': (9, 1), 'scope/a/hit': (2, 1), 'scope/a/hit/hit': (8, 2), 'scope/b/hit': (10, 4), 'scope/c': (7, 1), 'scope/d/hit': (0, 5)}
    frontier = Frontier(source(own), 'hit')
    assert [node.path for node in frontier.roots] == ['scope/a/hit', 'scope/b/hit', 'scope/d/hit']
    body = exact_partition(frontier, own, 'scope', 10)
    assert body['children'] == [{'path': 'scope/a', 'b': 10, 'o': 3}, {'path': 'scope/b', 'b': 10, 'o': 4}]
    assert body['other'] == {'b': 0, 'o': 5}
    assert body['cost'] == {'probes': 2, 'aggregate_calls': 4, 'frontier_selects': 2, 'shared_selects': 2, 'parent_hops': 5, 'candidate_children': 2}


def test_external_ancestor_match_covers_scoped_source_and_dir_own_objects_once():
    own = {'HiT/scope': (4, 2), 'HiT/scope/a': (6, 3), 'HiT/scope/a/hit': (0, 7)}
    frontier = Frontier(source(own, root='HiT/scope'), 'hit')
    assert [node.path for node in frontier.roots] == ['HiT/scope']
    body = exact_partition(frontier, own, 'HiT/scope', 5)
    assert body['children'] == [{'path': 'HiT/scope/a', 'b': 6, 'o': 10}]
    assert body['other'] == {'b': 4, 'o': 2}
    assert frontier.total('HiT/scope/a') == (6, 10)


def test_fixed_threshold_refinement_through_three_covered_levels():
    own = {'scope/hit': (2, 1), 'scope/hit/a': (1, 1), 'scope/hit/a/deep/file': (11, 1),
           'scope/hit/a/deep/zero': (0, 3), 'scope/hit/b/file': (12, 2)}
    frontier = Frontier(source(own), 'hit')
    views = [exact_partition(frontier, own, path, 10) for path in ('scope', 'scope/hit', 'scope/hit/a', 'scope/hit/a/deep')]
    assert [row['children'] for row in views] == [
        [{'path': 'scope/hit', 'b': 26, 'o': 8}],
        [{'path': 'scope/hit/a', 'b': 12, 'o': 5}, {'path': 'scope/hit/b', 'b': 12, 'o': 2}],
        [{'path': 'scope/hit/a/deep', 'b': 11, 'o': 4}],
        [{'path': 'scope/hit/a/deep/file', 'b': 11, 'o': 1}],
    ]
    assert [row['other'] for row in views] == [{'b': 0, 'o': 0}, {'b': 2, 'o': 1}, {'b': 1, 'o': 1}, {'b': 0, 'o': 3}]


@pytest.mark.parametrize('threshold', [1, 2, 3, 4, 5, 7, 10, 30])
def test_ties_unicode_repeated_fragments_and_no_slash_crossing(threshold: int):
    own = {'scope/aaaa': (4, 1), 'scope/AAaa/file': (5, 2), 'scope/åå': (3, 3), 'scope/zero/aaaa': (0, 4), 'scope/ab/cd': (6, 1)}
    for pattern in ('aa', 'Å', 'abcd', 'file', 'scope', 'absent'):
        frontier = Frontier(source(own), pattern)
        for path in ('', 'scope', 'scope/AAaa', 'scope/zero'):
            exact_partition(frontier, own, path, threshold)
    assert Frontier(source(own), 'abcd').total('') == (0, 0)
    assert Frontier(source(own), 'aa').total('') == (9, 7)


class NoIteration:
    def __init__(self, values: tuple[Node, ...]) -> None:
        self.values = values

    def __getitem__(self, index: int) -> Node:
        return self.values[index]

    def __iter__(self) -> None:
        raise AssertionError('query enumerated source nodes/siblings')


def test_19000_flat_siblings_do_not_get_enumerated_during_discovery():
    own = {f'scope/item-{index:05d}': (1, 1) for index in range(19_000)}
    own['scope/item-01000'] = (5000, 2)
    own['scope/item-18000'] = (5000, 3)
    frontier = Frontier(source(own), 'item')
    frontier.source.nodes = NoIteration(frontier.source.nodes)
    result = exact_partition(frontier, own, 'scope', 5000)
    assert result['children'] == [{'path': 'scope/item-01000', 'b': 5000, 'o': 2}, {'path': 'scope/item-18000', 'b': 5000, 'o': 3}]
    assert result['other'] == {'b': 18998, 'o': 18998}
    assert result['cost'] == {'probes': 5, 'aggregate_calls': 7, 'frontier_selects': 5, 'shared_selects': 5, 'parent_hops': 5, 'candidate_children': 5}


def test_pair_union_common_paths_offsetting_changes_independent_ids_and_absent_side():
    a = {'scope/hit-old': (8, 2), 'scope/hit-stable': (2, 1), 'scope/hit-zero': (0, 4)}
    b = {'scope/hit-new': (8, 3), 'scope/hit-stable': (2, 1), 'scope/hit-zero': (0, 5)}
    independent = source(b, reverse=True)
    independent = SharedWeights([replace(node, pre=node.pre * 3 + 10, post=node.post * 3 + 10) for node in independent.nodes])
    before, after = Frontier(source(a), 'hit'), Frontier(independent, 'HIT')
    result = paired(before, after, 'scope', 2)
    assert {key: result[key] for key in ('before', 'after', 'delta', 'threshold_bytes', 'children', 'other', 'present')} == {
        'before': {'b': 10, 'o': 7}, 'after': {'b': 10, 'o': 9}, 'delta': {'b': 0, 'o': 2}, 'threshold_bytes': 5,
        'children': [{'path': 'scope/hit-new', 'before': {'b': 0, 'o': 0}, 'after': {'b': 8, 'o': 3}, 'delta': {'b': 8, 'o': 3}},
                     {'path': 'scope/hit-old', 'before': {'b': 8, 'o': 2}, 'after': {'b': 0, 'o': 0}, 'delta': {'b': -8, 'o': -2}}],
        'other': {'before': {'b': 2, 'o': 5}, 'after': {'b': 2, 'o': 6}, 'delta': {'b': 0, 'o': 1}}, 'present': [True, True]}
    assert result['candidate_children'] <= 4
    assert result['cost']['before']['probes'] == 2
    assert result['cost']['after']['probes'] == 2
    for key, own in [('before', a), ('after', b)]:
        assert result[key] == dict(zip(('b', 'o'), oracle_total(own, 'hit', 'scope')))
        assert [row[key] for row in result['children']] == [dict(zip(('b', 'o'), oracle_total(own, 'hit', row['path']))) for row in result['children']]
    absent = paired(before, after, 'scope/hit-old', 2)
    assert {key: absent[key] for key in ('before', 'after', 'delta', 'other', 'present')} == {
        'before': {'b': 8, 'o': 2}, 'after': {'b': 0, 'o': 0}, 'delta': {'b': -8, 'o': -2},
        'other': {'before': {'b': 8, 'o': 2}, 'after': {'b': 0, 'o': 0}, 'delta': {'b': -8, 'o': -2}}, 'present': [True, False]}


def test_zero_byte_counts_without_probes_or_division_by_zero():
    own = {'scope/hit': (0, 9), 'scope/hit/zero': (0, 7)}
    frontier = Frontier(source(own), 'hit')
    result = exact_partition(frontier, own, 'scope', 1)
    assert result == {'path': 'scope', 'present': True, 'b': 0, 'o': 16, 'threshold_bytes': 1, 'children': [], 'other': {'b': 0, 'o': 16},
                      'cost': {'probes': 0, 'aggregate_calls': 2, 'frontier_selects': 0, 'shared_selects': 0, 'parent_hops': 0, 'candidate_children': 0}}
    pair = paired(frontier, frontier, 'scope', 64)
    assert pair['threshold_bytes'] == 1
    assert pair['children'] == []
    assert pair['other'] == {'before': {'b': 0, 'o': 16}, 'after': {'b': 0, 'o': 16}, 'delta': {'b': 0, 'o': 0}}


def test_positive_directory_own_bytes_without_own_objects_are_not_a_valid_scalar_source():
    with pytest.raises(ValueError, match='^positive own bytes require a positive own object count$'):
        SharedWeights([Node(0, 1, '', 4, 1), Node(1, 1, 'hit', 3, 1)])
    valid = SharedWeights([Node(0, 1, '', 3, 2), Node(1, 1, 'hit', 3, 1)])
    assert valid.objects_prefix == (0, 1, 2)


def test_uint64_limit_paired_threshold_and_exact_ties_do_not_pass_through_float():
    limit = (1 << 64) - 1
    threshold = 6_148_914_691_236_517_205  # Exactly limit/3; float(limit/3) rounds down.
    a = {'scope/hit-a': (threshold, 1), 'scope/hit-b': (2 * threshold, 1)}
    b = {'scope/hit-a': (2 * threshold, 1), 'scope/hit-b': (threshold, 1)}
    before, after = Frontier(source(a), 'hit'), Frontier(source(b, reverse=True), 'hit')
    result = paired(before, after, 'scope', 3)
    assert result == {
        'path': 'scope', 'threshold_bytes': threshold, 'before': {'b': limit, 'o': 2}, 'after': {'b': limit, 'o': 2}, 'delta': {'b': 0, 'o': 0},
        'children': [
            {'path': 'scope/hit-a', 'before': {'b': threshold, 'o': 1}, 'after': {'b': 2 * threshold, 'o': 1}, 'delta': {'b': threshold, 'o': 0}},
            {'path': 'scope/hit-b', 'before': {'b': 2 * threshold, 'o': 1}, 'after': {'b': threshold, 'o': 1}, 'delta': {'b': -threshold, 'o': 0}},
        ],
        'other': {'before': {'b': 0, 'o': 0}, 'after': {'b': 0, 'o': 0}, 'delta': {'b': 0, 'o': 0}}, 'present': [True, True], 'candidate_children': 2,
        'cost': {
            'before': {'probes': 3, 'aggregate_calls': 4, 'frontier_selects': 3, 'shared_selects': 3, 'parent_hops': 3, 'candidate_children': 2},
            'after': {'probes': 3, 'aggregate_calls': 4, 'frontier_selects': 3, 'shared_selects': 3, 'parent_hops': 3, 'candidate_children': 2},
        },
    }
    exact_partition(before, a, 'scope', threshold)


@pytest.mark.parametrize('issue', ['negative', 'missing ancestor', 'endpoint', 'overlap', 'bool', 'duplicate'])
def test_invalid_incomplete_sources_refused(issue: str):
    nodes = [Node(0, 2, '', 3, 2), Node(1, 2, 'a', 3, 2), Node(2, 2, 'a/b', 3, 2)]
    if issue == 'negative':
        nodes[0] = replace(nodes[0], b=2)
    if issue == 'missing ancestor':
        nodes.pop(1)
    if issue == 'endpoint':
        nodes[0] = replace(nodes[0], post=3)
    if issue == 'overlap':
        nodes[1] = replace(nodes[1], post=3)
    if issue == 'bool':
        nodes[0] = replace(nodes[0], b=True)
    if issue == 'duplicate':
        nodes[2] = replace(nodes[2], path='a')
    with pytest.raises(ValueError):
        SharedWeights(nodes)


def test_invalid_queries_and_unbounded_probes_fail_before_partial_result():
    tree = source({'a': (300, 1)})
    for pattern in ('', 'a/b', '\0', 'a' * 513):
        with pytest.raises(ValueError, match='^one nonempty name literal of at most 512 characters is required$'):
            Frontier(tree, pattern)
    query = Frontier(tree, 'a')
    with pytest.raises(ValueError, match='^complete quantile discovery exceeds probe cap; no partial result$'):
        query.partition('', 1)
    with pytest.raises(ValueError, match='^positive threshold and 1..256 probe cap are required$'):
        query.partition('', 0)
    with pytest.raises(ValueError, match='^requested path is absent from both complete dated sources$'):
        paired(query, query, 'missing')
