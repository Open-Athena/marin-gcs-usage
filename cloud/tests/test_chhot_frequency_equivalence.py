"""Only contained literals with complete equal dated counts prove aliases."""

from hashlib import sha256
from json import dumps, loads
from pathlib import Path

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_frequency_equivalence as module
from dt_cloud.chstore.hot_frequency_registry import FREQUENCY_SEMANTICS, UNION_SCHEMA

DATES = ['2026-10-04', '2026-10-05']


def export(tmp_path: Path) -> tuple[Path, dict]:
    frequencies = {'a': [10, 10], 'ab': [10, 10], 'ac': [10, 10], 'abc': [10, 10], 'd': [10, 10],
                   'x': [None, 10], 'xx': [None, 10], 'p': [10, 11], 'pq': [10, 12], 'å': [20, 21], 'åå': [20, 21]}
    header = {'schema': UNION_SCHEMA, 'target': 'fixture', 'dates': DATES, 'threshold_paths': 1, 'max_chars': 8, 'max_patterns': 500_000,
              'sources': [{'date': day, 'snapshot_db': f'snapshot_{i}', 'threshold_paths': 1, 'max_chars': 8,
                           'accepted_hot_pattern_cap': 500_000, 'census': {'sha256': 'a' * 64, 'bytes': 1},
                           'queries': {'sha256': 'b' * 64, 'bytes': 1, 'patterns': len(frequencies)}} for i, day in enumerate(DATES)],
              'frequency_semantics': FREQUENCY_SEMANTICS}
    rows = [{'chars': len(pattern), 'pattern': pattern, 'direct_matching_paths': dict(zip(DATES, vector, strict=True))}
            for pattern, vector in sorted(frequencies.items(), key=lambda item: (len(item[0]), item[0]))]
    path = tmp_path / 'queries.jsonl'
    path.write_text('\n'.join(map(dumps, [header, *rows, {'complete': True, 'patterns': len(rows)}])) + '\n')
    return path, header


def test_complete_proof_transitive_containment_null_date_vectors_and_unicode(tmp_path: Path) -> None:
    path, header = export(tmp_path)
    raw = path.read_bytes()
    out = tmp_path / 'aliases.json'
    result = module.report(path, out, expected_sha256=sha256(raw).hexdigest())
    assert result == {
        'schema': 'hot-frequency-equivalence-report-v1', 'complete': True, 'target': 'fixture', 'dates': DATES,
        'queries': {'path': str(path), 'sha256': sha256(raw).hexdigest(), 'bytes': len(raw), 'header': header},
        'patterns': 11, 'classes': 7, 'aliases_removed': 4, 'nontrivial_classes': 2, 'proof_edges': 4,
        'representative_policy': 'longest Unicode literal; ascending lexical tie break',
        'proof_basis': 'proper literal containment plus equal non-null exact direct-path cardinalities on every registry date; transitive closure',
        'valid_only_for_registry_dates': True, 'accepted_for_kernel': False, 'independent_path_set_validation': False,
        'persistent_index_created': False,
        'direct_hit_work': [
            {'date': DATES[0], 'known_predicate_frequency_sum': 110, 'representative_known_frequency_sum': 60,
             'removed_known_frequency_sum': 50, 'unknown_predicates': 2, 'unknown_source_minimum': 1,
             'unknown_frequency_sum_upper_bound': 0, 'removed_unknown_frequency_sum': 0,
             'removed_fraction_bounds': {'lower': {'numerator': 50, 'denominator': 110}, 'upper': {'numerator': 50, 'denominator': 110}}},
            {'date': DATES[1], 'known_predicate_frequency_sum': 135, 'representative_known_frequency_sum': 84,
             'removed_known_frequency_sum': 51, 'unknown_predicates': 0, 'unknown_source_minimum': 1,
             'unknown_frequency_sum_upper_bound': 0, 'removed_unknown_frequency_sum': 0,
             'removed_fraction_bounds': {'lower': {'numerator': 51, 'denominator': 135}, 'upper': {'numerator': 51, 'denominator': 135}}},
        ],
        'work_metric': 'sum of direct name-hit outputs across predicates, not unique paths, coverage bytes or a runtime promise',
        'equivalence_classes': [
            {'representative': 'd', 'members': ['d'], 'direct_matching_paths': dict(zip(DATES, [10, 10], strict=True))},
            {'representative': 'p', 'members': ['p'], 'direct_matching_paths': dict(zip(DATES, [10, 11], strict=True))},
            {'representative': 'x', 'members': ['x'], 'direct_matching_paths': dict(zip(DATES, [None, 10], strict=True))},
            {'representative': 'pq', 'members': ['pq'], 'direct_matching_paths': dict(zip(DATES, [10, 12], strict=True))},
            {'representative': 'xx', 'members': ['xx'], 'direct_matching_paths': dict(zip(DATES, [None, 10], strict=True))},
            {'representative': 'åå', 'members': ['å', 'åå'], 'direct_matching_paths': dict(zip(DATES, [20, 21], strict=True))},
            {'representative': 'abc', 'members': ['a', 'ab', 'ac', 'abc'], 'direct_matching_paths': dict(zip(DATES, [10, 10], strict=True))},
        ],
        'edges': [
            {'shorter': 'a', 'longer': 'ab', 'direct_matching_paths': dict(zip(DATES, [10, 10], strict=True))},
            {'shorter': 'a', 'longer': 'ac', 'direct_matching_paths': dict(zip(DATES, [10, 10], strict=True))},
            {'shorter': 'å', 'longer': 'åå', 'direct_matching_paths': dict(zip(DATES, [20, 21], strict=True))},
            {'shorter': 'a', 'longer': 'abc', 'direct_matching_paths': dict(zip(DATES, [10, 10], strict=True))},
        ],
    }
    assert loads(out.read_bytes()) == result


def test_unknowns_bound_work_with_source_minimum_not_union_cutoff(tmp_path: Path) -> None:
    path, _ = export(tmp_path)
    rows = [loads(line) for line in path.read_text().splitlines()]
    rows[0]['threshold_paths'] = 10
    rows[0]['sources'][0]['threshold_paths'] = 5
    path.write_text('\n'.join(map(dumps, rows)) + '\n')
    actual = module.report(path, tmp_path / 'bounded.json')['direct_hit_work'][0]
    assert actual == {'date': DATES[0], 'known_predicate_frequency_sum': 110, 'representative_known_frequency_sum': 60,
                      'removed_known_frequency_sum': 50, 'unknown_predicates': 2, 'unknown_source_minimum': 5,
                      'unknown_frequency_sum_upper_bound': 8, 'removed_unknown_frequency_sum': 0,
                      'removed_fraction_bounds': {'lower': {'numerator': 50, 'denominator': 118}, 'upper': {'numerator': 50, 'denominator': 110}}}


def test_longest_representative_lexical_tie_and_deterministic_bytes(tmp_path: Path) -> None:
    path, _ = export(tmp_path)
    rows = [loads(line) for line in path.read_text().splitlines()]
    rows = [row for row in rows if row.get('pattern') != 'abc']
    rows[-1]['patterns'] = 10
    path.write_text('\n'.join(map(dumps, rows)) + '\n')
    first, second = tmp_path / 'first.json', tmp_path / 'second.json'
    result = module.report(path, first)
    module.report(path, second)
    assert first.read_bytes() == second.read_bytes()
    assert [row for row in result['equivalence_classes'] if len(row['members']) == 3] == [
        {'representative': 'ab', 'members': ['a', 'ab', 'ac'], 'direct_matching_paths': dict(zip(DATES, [10, 10], strict=True))}]


@pytest.mark.parametrize('kind,message', [
    ('hash', 'equivalence registry SHA256 differs from the pinned expectation'),
    ('hash-format', 'equivalence expected SHA256 must be 64 lowercase hexadecimal characters'),
    ('single', 'equivalence requires one completed dated-union query export'),
    ('footer', 'hot query export lacks a valid exact-count completion footer'),
    ('zero', 'union query requires valid dated frequencies qualifying on at least one source date'),
    ('dates', 'union registry requires matching sorted dates and complete valid source provenance'),
])
def test_invalid_or_unpinned_input_refuses_without_output(tmp_path: Path, kind: str, message: str) -> None:
    path, _ = export(tmp_path)
    rows = [loads(line) for line in path.read_text().splitlines()]
    expected = None
    if kind == 'hash': expected = '0' * 64
    elif kind == 'hash-format': expected = 'ABC'
    elif kind == 'single': rows[0]['schema'] = 'hot-frequency-queries-v1'
    elif kind == 'footer': rows.pop()
    elif kind == 'zero': rows[1]['direct_matching_paths'][DATES[0]] = 0
    else: rows[0]['dates'] = list(reversed(DATES))
    path.write_text('\n'.join(map(dumps, rows)) + '\n')
    out = tmp_path / 'out.json'
    with pytest.raises(ValueError) as caught:
        module.report(path, out, expected_sha256=expected)
    assert str(caught.value) == message
    assert out.exists() is False


def test_input_bytes_are_pinned_once_and_existing_output_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, _ = export(tmp_path)
    calls, original = [], Path.read_bytes
    def read(source: Path) -> bytes:
        calls.append(source)
        return original(source)
    monkeypatch.setattr(Path, 'read_bytes', read)
    out = tmp_path / 'out.json'
    module.report(path, out)
    assert calls == [path]
    before = out.read_text()
    with pytest.raises(ValueError) as caught:
        module.report(path, out)
    assert str(caught.value) == 'equivalence report output must be new in an existing directory'
    assert (calls, out.read_text()) == ([path], before)


def test_cli_forwards_pinned_hash_and_prints_counts_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud.cli import main
    calls = []
    query, out = tmp_path / 'queries', tmp_path / 'out'
    body = {'schema': 'hot-frequency-equivalence-report-v1', 'dates': DATES, 'patterns': 11, 'classes': 7,
            'aliases_removed': 4, 'nontrivial_classes': 2, 'accepted_for_kernel': False, 'private_frequencies': 'not printed',
            'direct_hit_work': [{'date': DATES[0], 'removed_fraction_bounds': {'lower': {'numerator': 1, 'denominator': 4}, 'upper': {'numerator': 1, 'denominator': 2}}}]}
    def report(*args, **kwargs):
        calls.append((args, kwargs))
        return body
    monkeypatch.setattr(module, 'report', report)
    result = CliRunner().invoke(main, ['ch-hot-frequency-equivalence', str(query), '-o', str(out), '-s', 'a' * 64])
    assert (result.exit_code, result.stderr) == (0, '')
    assert loads(result.stdout) == {**{key: body[key] for key in ('schema', 'dates', 'patterns', 'classes', 'aliases_removed', 'nontrivial_classes', 'accepted_for_kernel')},
                                  'direct_hit_work_savings_percent_bounds': [{'date': DATES[0], 'lower_percent': 25., 'upper_percent': 50.}], 'out': str(out)}
    assert calls == [((query, out), {'expected_sha256': 'a' * 64})]
