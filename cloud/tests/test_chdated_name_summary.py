from copy import deepcopy
from threading import BoundedSemaphore
from types import SimpleNamespace

import pytest

from dt_cloud.chstore.dated_name_summary import CAPABILITIES, DatedNameSummaryRuntime
from dt_cloud.chstore.hot_l1_catalog import CatalogRequest, SCOPE
from dt_cloud.chstore.name_summary import SummaryBusy, SummaryUnavailable

QUALIFICATION = ['2026-10-04', '2026-10-05']
OLD_DATES = ('2026-10-04', '2026-10-05')
GENERATION = 'a' * 32
REGISTRY = {'qualification_dates': QUALIFICATION, 'target': 'frozen', 'patterns': 4, 'threshold_paths': 10,
            'max_chars': 16, 'selection_contract': 'membership on declared qualification dates; no current-scan frequency claim'}


def raw_daily(day: str = '2026-10-06', pattern: str = 'foo') -> dict:
    if day == '2026-10-06':
        rows = [{'path': 'a', 'pre': 1, 'post': 4, 'b': 7, 'o': 3}, {'path': 'b', 'pre': 5, 'post': 10, 'b': 0, 'o': 2}]
    else:
        rows = [{'path': 'b', 'pre': 1, 'post': 5, 'b': 2, 'o': 2}, {'path': 'a', 'pre': 6, 'post': 11, 'b': 12, 'o': 5}]
    if pattern == 'zero':
        rows = [{**row, 'b': 0, 'o': int(row['path'] == 'b')} for row in rows]
    elif pattern == 'empty':
        rows = [{**row, 'b': 0, 'o': 0} for row in rows]
    return {'schema': 'dated-hot-l1-v1', 'logical_store': 'gcs_fleet', 'date': day, 'pattern': pattern, 'path': '',
            'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE,
            'root': {key: sum(row[key] for row in rows) for key in ('b', 'o')}, 'buckets': rows,
            'source': {'artifact_sha256': 'c' * 64, 'artifact_bytes': 123, 'target': 'day_' + day.replace('-', ''),
                       'snapshot_db': 'day_' + day.replace('-', ''), 'source_manifest_sha256': 'd' * 64, 'source_prefix_proofs_checked': True},
            'registry': deepcopy(REGISTRY), 'validation': {'description': 'accepted fixture scalar catalog; selected-only source check',
            'source_prefix_proofs_checked': True, 'independent_full_catalog_source_oracle': False}, 'capabilities': dict(CAPABILITIES)}


def raw_old(day: str = '2026-10-05', pattern: str = 'foo') -> dict:
    rows = [{'path': 'b', 'pre': 1, 'post': 3, 'b': 2, 'o': 1}, {'path': 'a', 'pre': 4, 'post': 8, 'b': 4, 'o': 2}]
    return {'schema': 'name-summary-v1', 'target': 'frozen', 'date': day, 'pattern': pattern, 'path': '', 'exact': True,
            'incremental': False, 'levels': 1, 'scope': SCOPE, 'plan': 'bounded-name-postings' if pattern == 'datakit' else 'catalog',
            'source': 'old runtime source', 'source_identity': {'generation': 'old-generation', 'snapshot_db': 'old_' + day.replace('-', ''), 'history_manifest_sha256': 'e' * 64},
            'validation': {'description': 'old runtime validation', 'independent_query_source_oracle': False},
            'root': {'b': 6, 'o': 3}, 'buckets': rows}


def expected_daily(day: str = '2026-10-06', pattern: str = 'foo') -> dict:
    raw = raw_daily(day, pattern)
    return {**raw, 'schema': 'dated-name-summary-v1', 'plan': 'catalog', 'target': raw['source']['target'],
            'source': 'published dated precomputed batch artifact',
            'source_identity': {**raw['source'], 'kind': 'daily-scalar-source-v1', 'generation': GENERATION},
            'buckets': sorted(raw['buckets'], key=lambda row: row['path'])}


def expected_old(day: str = '2026-10-05', pattern: str = 'foo') -> dict:
    raw = raw_old(day, pattern)
    return {**raw, 'schema': 'dated-name-summary-v1', 'logical_store': 'gcs_fleet',
            'source_identity': {**raw['source_identity'], 'kind': 'frozen-history'}, 'capabilities': dict(CAPABILITIES),
            'buckets': sorted(raw['buckets'], key=lambda row: row['path'])}


@pytest.fixture
def sources():
    state = SimpleNamespace(events=[], change=None, runtime=None, old_view=None,
                            old_diff={'schema': 'unchanged-legacy-body', 'combined_compute_seconds': 5.0}, inspect_old=None)
    def legacy_view(day: str, pattern: str, *, path: str = '') -> dict:
        state.events.append(('legacy.view', day, pattern, path))
        if state.inspect_old:
            state.inspect_old()
        return state.old_view if state.old_view is not None else raw_old(day, pattern.lower())
    def legacy_diff(before: str, after: str, pattern: str, *, path: str = '') -> dict:
        state.events.append(('legacy.diff', before, after, pattern, path))
        return state.old_diff
    def daily(day: str):
        def view(date: str, pattern: str) -> dict:
            state.events.append(('daily.view', date, pattern))
            body = raw_daily(date, pattern)
            if state.change:
                state.change(body)
            return body
        return SimpleNamespace(date=day, paths=('a', 'b'), logical_store='gcs_fleet',
                               selection=SimpleNamespace(patterns=('foo', 'zero', 'empty', 'datakit'), buckets=((1, 4, 'a'), (5, 10, 'b'))), view=view,
                               metadata=lambda: {'registry': deepcopy(REGISTRY), 'source': deepcopy(raw_daily(day)['source'])})
    state.daily_catalog = daily
    state.legacy = SimpleNamespace(binding=SimpleNamespace(dates=tuple((day, 'old_' + day.replace('-', '')) for day in OLD_DATES),
                                  buckets=((1, 3, 'b'), (4, 8, 'a')), target='frozen'),
                                  catalog=SimpleNamespace(metadata=lambda: {'dates': [{'date': day, 'patterns': 4, 'registry_date': '2026-10-05'} for day in OLD_DATES]}),
                                  gate=BoundedSemaphore(1), view=legacy_view, diff=legacy_diff,
                                  metadata=lambda: {'compute_seconds': 5.0, 'cold_slots': 1})
    state.published = SimpleNamespace(catalogs={'2026-10-06': daily('2026-10-06')}, manifest={
        'schema': 'dated-hot-l1-published-generation-v1', 'complete': True, 'generation': GENERATION,
        'logical_store': 'gcs_fleet', 'bucket_paths': ['a', 'b'], 'dates': ['2026-10-06']})
    return state


def runtime(sources) -> DatedNameSummaryRuntime:
    result = DatedNameSummaryRuntime(sources.legacy, sources.published, logical_store='gcs_fleet', bucket_paths=('b', 'a'))
    sources.runtime = result
    return result


def test_old_only_delegation_is_exact_identity_and_original_argument_budget_contract(sources) -> None:
    reader = runtime(sources)
    sources.old_view = {'unmodified': 'legacy view sentinel'}
    assert reader.view('2026-10-05', 'DATAKIT', path='old-error-path') is sources.old_view
    assert reader.diff('2026-10-04', '2026-10-05', 'DATAKIT') is sources.old_diff
    assert sources.events == [('legacy.view', '2026-10-05', 'DATAKIT', 'old-error-path'), ('legacy.diff', '2026-10-04', '2026-10-05', 'DATAKIT', '')]


def test_old_only_reversed_date_error_is_the_same_legacy_exception(sources) -> None:
    reader = runtime(sources)
    failure = CatalogRequest('legacy baseline must precede selected scan')
    def refused(before: str, after: str, pattern: str, *, path: str = '') -> dict:
        sources.events.append(('legacy.diff', before, after, pattern, path))
        raise failure
    sources.legacy.diff = refused
    with pytest.raises(CatalogRequest) as caught:
        reader.diff('2026-10-05', '2026-10-04', 'foo')
    assert caught.value is failure
    assert sources.events == [('legacy.diff', '2026-10-05', '2026-10-04', 'foo', '')]


def test_new_complete_view_preserves_actual_source_hashes_and_qualification_not_scan_date(sources) -> None:
    reader = runtime(sources)
    assert reader.view('2026-10-06', 'FOO') == expected_daily()
    assert sources.events == [('daily.view', '2026-10-06', 'foo')]
    body = reader.view('2026-10-06', 'foo'); body['registry']['qualification_dates'].append('2026-10-06')
    assert reader.view('2026-10-06', 'foo') == expected_daily()


def test_mixed_diff_aligns_paths_not_numeric_bounds_and_keeps_each_source_identity(sources) -> None:
    reader = runtime(sources)
    assert reader.diff('2026-10-05', '2026-10-06', 'FOO') == {
        'schema': 'dated-name-summary-diff-v1', 'logical_store': 'gcs_fleet', 'from': '2026-10-05', 'date': '2026-10-06',
        'pattern': 'foo', 'path': '', 'exact': True, 'incremental': False, 'levels': 1, 'scope': SCOPE,
        'before': expected_old(), 'after': expected_daily(), 'delta': {'b': 1, 'o': 2}, 'capabilities': dict(CAPABILITIES),
        'buckets': [{'path': 'a', 'before': {'pre': 4, 'post': 8, 'b': 4, 'o': 2}, 'after': {'pre': 1, 'post': 4, 'b': 7, 'o': 3}, 'delta': {'b': 3, 'o': 1}},
                    {'path': 'b', 'before': {'pre': 1, 'post': 3, 'b': 2, 'o': 1}, 'after': {'pre': 5, 'post': 10, 'b': 0, 'o': 2}, 'delta': {'b': -2, 'o': 1}}]}
    assert sources.events == [('daily.view', '2026-10-06', 'foo'), ('legacy.view', '2026-10-05', 'foo', '')]


def test_two_new_dates_keep_reordered_bounds_and_added_weights(sources) -> None:
    sources.published.catalogs['2026-10-07'] = sources.daily_catalog('2026-10-07')
    sources.published.manifest['dates'].append('2026-10-07')
    reader = runtime(sources)
    result = reader.diff('2026-10-06', '2026-10-07', 'foo')
    assert (result['before'], result['after'], result['delta'], result['buckets']) == (expected_daily(), expected_daily('2026-10-07'), {'b': 7, 'o': 2}, [
        {'path': 'a', 'before': {'pre': 1, 'post': 4, 'b': 7, 'o': 3}, 'after': {'pre': 6, 'post': 11, 'b': 12, 'o': 5}, 'delta': {'b': 5, 'o': 2}},
        {'path': 'b', 'before': {'pre': 5, 'post': 10, 'b': 0, 'o': 2}, 'after': {'pre': 1, 'post': 5, 'b': 2, 'o': 2}, 'delta': {'b': 2, 'o': 0}},
    ])
    assert sources.events == [('daily.view', '2026-10-06', 'foo'), ('daily.view', '2026-10-07', 'foo')]


@pytest.mark.parametrize('pattern,objects', [('empty', 0), ('zero', 1)])
def test_registered_true_zero_bytes_are_not_unavailable(sources, pattern: str, objects: int) -> None:
    result = runtime(sources).view('2026-10-06', pattern)
    assert result == expected_daily(pattern=pattern)
    assert result['root'] == {'b': 0, 'o': objects}


def test_metadata_advertises_separate_plan_and_original_qualification_dates(sources) -> None:
    reader = runtime(sources)
    assert reader.metadata() == {'schema': 'dated-name-summary-registry-v1', 'logical_store': 'gcs_fleet', 'bucket_paths': ['a', 'b'],
        'dates': [{'date': day, 'plans': ['catalog', 'bounded-name-postings'], 'kind': 'frozen-history',
                   'registry': {'qualification_dates': ['2026-10-05'], 'target': 'frozen', 'patterns': 4,
                                'selection_contract': 'membership on declared qualification dates; no current-scan frequency claim'}} for day in OLD_DATES] +
                 [{'date': '2026-10-06', 'plans': ['catalog'], 'kind': 'daily-scalar-source-v1', 'registry': REGISTRY,
                   'source': raw_daily()['source'], 'generation': GENERATION}],
        'levels': 1, 'scope': SCOPE, 'daily_catalog_slots': 2, 'legacy': {'compute_seconds': 5.0, 'cold_slots': 1}, 'capabilities': dict(CAPABILITIES)}
    assert sources.events == []


@pytest.mark.parametrize('change,message', [
    (lambda s: s.published.manifest.update(logical_store='other'), 'dated name summary source dates/logical scope conflict'),
    (lambda s: s.published.manifest.update(dates=['2026-10-06', '2026-10-06']), 'dated name summary source dates/logical scope conflict'),
    (lambda s: s.legacy.binding.__setattr__('buckets', ((1, 8, 'a'),)), 'dated name summary frozen bucket set differs from declared logical scope'),
    (lambda s: s.published.catalogs['2026-10-06'].__setattr__('paths', ('a',)), 'dated name summary daily bucket/store/date differs from declared logical scope'),
])
def test_boot_refuses_scope_or_duplicate_date_declarations(sources, change, message: str) -> None:
    change(sources)
    with pytest.raises(ValueError) as caught:
        runtime(sources)
    assert (str(caught.value), sources.events) == (message, [])


def test_boot_refuses_date_in_both_legacy_and_daily(sources) -> None:
    sources.published.catalogs = {'2026-10-05': sources.daily_catalog('2026-10-05')}
    sources.published.manifest['dates'] = ['2026-10-05']
    with pytest.raises(ValueError) as caught:
        runtime(sources)
    assert str(caught.value) == 'dated name summary source dates/logical scope conflict'


@pytest.mark.parametrize('before,after,pattern,path,message', [
    ('2026-10-05', '2026-10-06', 'unknown', '', 'dated name summary new scan/literal is not registered; no cold fallback'),
    ('2026-10-05', '2026-10-09', 'foo', '', 'dated name summary scan is unavailable; no scan fallback'),
    ('2026-10-06', '2026-10-05', 'foo', '', 'dated name summary baseline must precede the selected scan'),
    ('2026-10-06', '2026-10-06', 'foo', '', 'dated name summary baseline must precede the selected scan'),
    ('2026-10-05', '2026-10-06', 'foo', 'a', 'dated name summary serves the global root only; no drill fallback'),
    ('2026-10-05', '2026-10-06', 'a/b', '', 'dated name summary requires valid ISO scans and one UTF-8 NUL/slash-free literal'),
])
def test_new_or_mixed_invalid_requests_never_call_cold_or_catalog(sources, before: str, after: str, pattern: str, path: str, message: str) -> None:
    reader = runtime(sources)
    with pytest.raises(CatalogRequest) as caught:
        reader.diff(before, after, pattern, path=path)
    assert (str(caught.value), sources.events) == (message, [])


def assert_slots_free(reader: DatedNameSummaryRuntime) -> None:
    assert [reader.catalog_gate.acquire(blocking=False) for _ in range(3)] == [True, True, False]
    reader.catalog_gate.release(); reader.catalog_gate.release()


def test_new_slots_fail_fast_separately_and_old_only_still_delegates(sources) -> None:
    reader = runtime(sources)
    assert [reader.catalog_gate.acquire(blocking=False) for _ in range(2)] == [True, True]
    try:
        with pytest.raises(SummaryBusy) as caught:
            reader.diff('2026-10-05', '2026-10-06', 'foo')
        assert str(caught.value) == 'dated name summary daily catalog slots busy; retry shortly'
        assert reader.view('2026-10-05', 'foo') == raw_old()
        assert sources.events == [('legacy.view', '2026-10-05', 'foo', '')]
    finally:
        reader.catalog_gate.release(); reader.catalog_gate.release()


def test_new_slots_released_before_old_cold_view_and_independent_of_legacy_cold_gate(sources) -> None:
    reader = runtime(sources)
    assert sources.legacy.gate.acquire(blocking=False) is True
    try:
        assert reader.view('2026-10-06', 'foo') == expected_daily()
    finally:
        sources.legacy.gate.release()
    sources.inspect_old = lambda: assert_slots_free(reader)
    result = reader.diff('2026-10-05', '2026-10-06', 'datakit')
    assert (result['before']['plan'], result['after']['plan']) == ('bounded-name-postings', 'catalog')
    assert sources.events == [('daily.view', '2026-10-06', 'foo'), ('daily.view', '2026-10-06', 'datakit'), ('legacy.view', '2026-10-05', 'datakit', '')]


def test_old_cold_failure_keeps_original_exception_and_releases_daily_slots(sources) -> None:
    reader = runtime(sources)
    failure = SummaryBusy('legacy cold lane busy')
    def refuse() -> None:
        assert_slots_free(reader)
        raise failure
    sources.inspect_old = refuse
    with pytest.raises(SummaryBusy) as caught:
        reader.diff('2026-10-05', '2026-10-06', 'datakit')
    assert caught.value is failure
    assert_slots_free(reader)
    assert sources.events == [('daily.view', '2026-10-06', 'datakit'), ('legacy.view', '2026-10-05', 'datakit', '')]


def test_published_pointer_or_caller_mapping_changes_do_not_replace_pinned_readers(sources) -> None:
    reader = runtime(sources)
    sources.published.manifest['generation'] = 'b' * 32
    sources.published.catalogs.clear()
    assert reader.view('2026-10-06', 'foo') == expected_daily()
    with pytest.raises(TypeError):
        reader.daily['2026-10-07'] = sources.daily_catalog('2026-10-07')


@pytest.mark.parametrize('change,message', [
    (lambda b: b.update(pattern='wrong'), 'dated name summary returned mismatched exact scope'),
    (lambda b: b['buckets'].pop(), 'dated name summary returned incomplete logical buckets'),
    (lambda b: b['buckets'][0].update(post=5), 'dated name summary returned incomplete bucket geometry'),
    (lambda b: b['root'].update(b=999), 'dated name summary bucket/root weights do not conserve'),
    (lambda b: b['buckets'][0].update(o=True), 'dated name summary returned invalid exact weights'),
])
def test_bad_complete_body_refuses_and_releases_new_slots_without_old_work(sources, change, message: str) -> None:
    reader = runtime(sources); sources.change = change
    with pytest.raises(SummaryUnavailable) as caught:
        reader.diff('2026-10-05', '2026-10-06', 'foo')
    assert (str(caught.value), sources.events) == (message, [('daily.view', '2026-10-06', 'foo')])
    assert_slots_free(reader)


def cold_index(day: str = '2026-10-06') -> dict:
    source = raw_daily(day)['source']
    return {'schema': 'daily-name-index-v1', 'complete': True, 'target': source['snapshot_db'], 'logical_store': 'gcs_fleet', 'date': day,
            'prefix': '', 'names': 5, 'postings': 11, 'buckets': [{'path': 'a', 'pre': 1, 'post': 4}, {'path': 'b', 'pre': 5, 'post': 10}],
            'source_manifest_sha256': source['source_manifest_sha256'], 'source_manifest_bytes': 99, 'stages': {}}


def cold_runtime(sources, monkeypatch: pytest.MonkeyPatch) -> DatedNameSummaryRuntime:
    from dt_cloud.chstore import dated_name_summary

    def bounded(target: str, compute):
        sources.events.append(('legacy.bounded', target))
        return compute('owned-source', lambda: sources.events.append(('checkpoint',)))

    def build(source, target, day, pattern, *, daily, **caps):
        sources.events.append(('build', source, target, day, pattern, daily, caps))
        rows = [{'pre': 1, 'post': 4, 'path': 'a', 'b': 5, 'o': 1}, {'pre': 5, 'post': 10, 'path': 'b', 'b': 0, 'o': 2}]
        return {'schema': 'hot-l1-v1', 'target': target, 'snapshot_db': target, 'date': day, 'pattern': pattern.lower(), 'exact': True,
                'incremental': False, 'scope': SCOPE, 'root': {'b': 5, 'o': 3}, 'buckets': rows, 'all_buckets_covered': False,
                'stages': {'vocabulary_s': 0.1}, 'work_bounds': {'max_names': 1, 'max_postings': 2, 'max_outer_roots': 3}, 'direct_matching_rows': 3}

    sources.legacy.bounded = bounded
    monkeypatch.setattr(dated_name_summary, 'build', build)
    result = DatedNameSummaryRuntime(sources.legacy, sources.published, logical_store='gcs_fleet', bucket_paths=('b', 'a'),
                                     cold={'2026-10-06': cold_index()})
    sources.runtime = result
    return result


def test_unregistered_new_literal_is_bounded_over_the_scans_own_name_index(sources, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = cold_runtime(sources, monkeypatch)
    source = raw_daily()['source']
    assert reader.view('2026-10-06', 'UnKnown') == {
        **expected_daily(pattern='unknown'), 'plan': 'bounded-name-postings',
        'root': {'b': 5, 'o': 3}, 'buckets': [{'path': 'a', 'pre': 1, 'post': 4, 'b': 5, 'o': 1}, {'path': 'b', 'pre': 5, 'post': 10, 'b': 0, 'o': 2}],
        'source': "bounded dated name postings over the scan's own name index; directory rollups are atomic",
        'validation': {'description': "bounded exact first-hit coverage over the scan's own name index; no per-request source oracle",
                       'source_prefix_proofs_checked': True, 'independent_full_catalog_source_oracle': False},
    }
    assert sources.events == [
        ('legacy.bounded', source['snapshot_db']), ('checkpoint',),
        ('build', 'owned-source', source['snapshot_db'], '2026-10-06', 'unknown', True, {'max_names': 200_000, 'max_postings': 100_000, 'max_roots': 100_000}),
        ('checkpoint',),
    ]


def test_mixed_diff_cold_new_side_then_legacy_old_side(sources, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = cold_runtime(sources, monkeypatch)
    result = reader.diff('2026-10-05', '2026-10-06', 'unknown')
    assert (result['before']['plan'], result['after']['plan'], result['delta']) == ('catalog', 'bounded-name-postings', {'b': -1, 'o': 0})
    assert [event[0] for event in sources.events] == ['legacy.bounded', 'checkpoint', 'build', 'checkpoint', 'legacy.view']


def test_registered_new_literal_stays_on_the_catalog_with_a_cold_index(sources, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = cold_runtime(sources, monkeypatch)
    assert (reader.view('2026-10-06', 'foo'), sources.events) == (expected_daily(), [('daily.view', '2026-10-06', 'foo')])


def test_metadata_advertises_cold_plan_only_for_indexed_scans(sources, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {row['date']: row for row in cold_runtime(sources, monkeypatch).metadata()['dates']}
    assert [(day, row['plans']) for day, row in sorted(rows.items())] == [
        ('2026-10-04', ['catalog', 'bounded-name-postings']), ('2026-10-05', ['catalog', 'bounded-name-postings']), ('2026-10-06', ['catalog', 'bounded-name-postings']),
    ]


@pytest.mark.parametrize('change', [
    lambda index: index.update(source_manifest_sha256='0' * 64),
    lambda index: index.update(target='elsewhere'),
    lambda index: index.update(date='2026-10-07'),
    lambda index: index.update(complete=False),
    lambda index: index.update(buckets=[{'path': 'a', 'pre': 1, 'post': 10}]),
])
def test_boot_refuses_cold_index_bound_to_another_source(sources, change) -> None:
    index = cold_index()
    change(index)
    with pytest.raises(ValueError) as caught:
        DatedNameSummaryRuntime(sources.legacy, sources.published, logical_store='gcs_fleet', bucket_paths=('b', 'a'), cold={'2026-10-06': index})
    assert str(caught.value) == 'dated name summary cold name index differs from its scan catalog source'


MEGA = {'schema': 'mega-name-binding-v1', 'target': 'default', 'postings': 'm', 'through': '2026-10-06',
        'geometry': {'2026-09-15': [[1, 2, 'a'], [3, 9, 'b']], '2026-10-05': [[1, 5, 'a'], [6, 8, 'b']], '2026-10-06': [[1, 4, 'a'], [5, 10, 'b']]}}
MEGA_VALIDATION = {'description': "bounded exact first-hit coverage over the consolidated store's name index; no per-request source oracle",
                   'source_prefix_proofs_checked': True, 'independent_full_catalog_source_oracle': False}


def mega_runtime(sources, monkeypatch: pytest.MonkeyPatch, mega: dict = MEGA) -> DatedNameSummaryRuntime:
    from dt_cloud.chstore import dated_name_summary

    def bounded(target: str, compute):
        sources.events.append(('legacy.bounded', target))
        return compute('owned-source', lambda: sources.events.append(('checkpoint',)))

    def answer(source, day, pattern, *, postings, max_names):
        sources.events.append(('mega.answer', source, day, pattern, postings, max_names))
        rows = [{'path': 'a', 'b': 4, 'o': 2}, {'path': 'b', 'b': 1, 'o': 1}]
        return {'schema': 'mega-name-totals-v1', 'date': day, 'pattern': pattern.lower(), 'exact': True, 'root': {'b': 5, 'o': 3}, 'buckets': rows}

    sources.legacy.bounded = bounded
    monkeypatch.setattr(dated_name_summary.mega_names, 'answer', answer)
    result = DatedNameSummaryRuntime(sources.legacy, sources.published, logical_store='gcs_fleet', bucket_paths=('b', 'a'), mega=deepcopy(mega))
    sources.runtime = result
    return result


def test_metadata_adds_consolidated_scans_and_the_daily_cold_plan(sources, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = mega_runtime(sources, monkeypatch).metadata()['dates']
    assert [(row['date'], row['kind'], row['plans']) for row in rows] == [
        ('2026-09-15', 'consolidated-store-v1', ['bounded-name-postings']),
        ('2026-10-04', 'frozen-history', ['catalog', 'bounded-name-postings']),
        ('2026-10-05', 'frozen-history', ['catalog', 'bounded-name-postings']),
        ('2026-10-06', 'daily-scalar-source-v1', ['catalog', 'bounded-name-postings']),
    ]
    assert rows[0] == {'date': '2026-09-15', 'plans': ['bounded-name-postings'], 'kind': 'consolidated-store-v1',
                       'source': {'target': 'default', 'postings': 'm', 'through': '2026-10-06'}}


def test_unregistered_daily_literal_answers_from_the_consolidated_store(sources, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = mega_runtime(sources, monkeypatch)
    assert reader.view('2026-10-06', 'UnKnown') == {
        **expected_daily(pattern='unknown'), 'plan': 'bounded-name-postings', 'root': {'b': 5, 'o': 3},
        'buckets': [{'path': 'a', 'pre': 1, 'post': 4, 'b': 4, 'o': 2}, {'path': 'b', 'pre': 5, 'post': 10, 'b': 1, 'o': 1}],
        'source': 'bounded name postings over the consolidated store; directory rollups are atomic', 'validation': MEGA_VALIDATION,
    }
    assert sources.events == [('legacy.bounded', 'default'), ('checkpoint',), ('mega.answer', 'owned-source', '2026-10-06', 'unknown', 'm', 200_000), ('checkpoint',)]


def test_a_scan_without_catalogs_answers_any_literal_from_the_consolidated_store(sources, monkeypatch: pytest.MonkeyPatch) -> None:
    assert mega_runtime(sources, monkeypatch).view('2026-09-15', 'foo') == {
        'schema': 'dated-name-summary-v1', 'logical_store': 'gcs_fleet', 'date': '2026-09-15', 'pattern': 'foo', 'path': '', 'exact': True,
        'incremental': False, 'levels': 1, 'scope': SCOPE, 'plan': 'bounded-name-postings', 'target': 'default',
        'source': 'bounded name postings over the consolidated store; directory rollups are atomic', 'validation': MEGA_VALIDATION,
        'source_identity': {'kind': 'consolidated-store-v1', 'target': 'default', 'postings': 'm', 'through': '2026-10-06'},
        'root': {'b': 5, 'o': 3}, 'buckets': [{'path': 'a', 'pre': 1, 'post': 2, 'b': 4, 'o': 2}, {'path': 'b', 'pre': 3, 'post': 9, 'b': 1, 'o': 1}],
        'capabilities': dict(CAPABILITIES),
    }


def test_diff_across_consolidated_and_daily_scans_is_one_bounded_computation(sources, monkeypatch: pytest.MonkeyPatch) -> None:
    result = mega_runtime(sources, monkeypatch).diff('2026-09-15', '2026-10-06', 'unknown')
    assert (result['before']['source_identity']['kind'], result['after']['source_identity']['kind'], result['delta']) == (
        'consolidated-store-v1', 'daily-scalar-source-v1', {'b': 0, 'o': 0})
    assert [event[:3] for event in sources.events] == [('legacy.bounded', 'default'), ('checkpoint',),
                                                       ('mega.answer', 'owned-source', '2026-10-06'), ('checkpoint',),
                                                       ('mega.answer', 'owned-source', '2026-09-15'), ('checkpoint',)]


def test_registered_daily_literal_stays_on_the_catalog_with_the_consolidated_store(sources, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = mega_runtime(sources, monkeypatch)
    assert (reader.view('2026-10-06', 'foo'), sources.events) == (expected_daily(), [('daily.view', '2026-10-06', 'foo')])


@pytest.mark.parametrize('change,message', [
    (lambda mega: mega['geometry'].update({'2026-10-06': [[1, 5, 'a'], [6, 10, 'b']]}), 'dated name summary consolidated geometry differs from its scan catalog'),
    (lambda mega: mega['geometry'].update({'2026-09-15': [[1, 2, 'a'], [4, 9, 'b']]}), 'dated name summary consolidated geometry is incomplete'),
    (lambda mega: mega['geometry'].update({'2026-09-15': [[1, 2, 'a']]}), 'dated name summary consolidated geometry is incomplete'),
    (lambda mega: mega.update(through='2026-10-05'), 'dated name summary consolidated geometry is incomplete'),
    (lambda mega: mega.update(postings='m; DROP'), 'dated name summary consolidated binding is invalid'),
])
def test_boot_refuses_a_consolidated_binding_that_does_not_fit(sources, monkeypatch: pytest.MonkeyPatch, change, message) -> None:
    mega = deepcopy(MEGA)
    change(mega)
    with pytest.raises(ValueError) as caught:
        mega_runtime(sources, monkeypatch, mega)
    assert str(caught.value) == message
