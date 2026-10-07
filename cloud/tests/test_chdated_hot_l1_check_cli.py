"""Bounded explicit dated reader, private selected proof and exact CLI calls."""

from copy import deepcopy
from hashlib import sha256
from json import dumps
from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import client, dated_hot_l1, dated_hot_l1_check
from dt_cloud.chstore.daily_scalar import manifest_bytes
from dt_cloud.cli import main

REAL_LOAD = dated_hot_l1.DatedHotL1Catalog.load


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    catalog = SimpleNamespace(date='2026-10-06', selection=SimpleNamespace(snapshot_db='fresh', nodes=100,
                                                                        patterns=('a', '.npy', 'ångström')))
    body = {'schema': 'dated-hot-l1-check-v1', 'complete': True, 'date': '2026-10-06', 'source_nodes': 100,
            'selected_patterns_checked': 2, 'check_s': .125, 'independent_full_catalog_source_oracle': False,
            'selected_patterns': [{'pattern': 'a', 'validation': 'complete independent full-path first-hit source scan'},
                                  {'pattern': '.npy', 'validation': 'complete independent full-path first-hit source scan'}]}
    state = SimpleNamespace(catalog=catalog, body=body, artifact=tmp_path / 'artifact.json', out=tmp_path / 'proof.json',
                            calls=[], error=None)
    def load(path):
        state.calls.append(('load', path))
        return catalog
    class Client:
        def __init__(self, *args, **kwargs):
            state.calls.append(('client', args, kwargs))
        def close(self):
            state.calls.append(('close',))
    def check(ch, loaded, patterns):
        state.calls.append(('check', ch, loaded, patterns))
        if state.error:
            raise state.error
        return deepcopy(state.body)
    monkeypatch.setattr(dated_hot_l1.DatedHotL1Catalog, 'load', load)
    monkeypatch.setattr(client, 'Ch', Client)
    monkeypatch.setattr(dated_hot_l1_check, 'check', check)
    return state


def invoke(state, *extra, patterns=('A', '.NPY')):
    return CliRunner().invoke(main, ['ch-dated-hot-l1-check', str(state.artifact), '-o', str(state.out),
                                   *(arg for pattern in patterns for arg in ('-n', pattern)), *extra])


@pytest.mark.parametrize('explicit', [False, True])
def test_exact_forwarding_complete_canonical_proof_private_mode_and_compact_stdout(fixture, explicit: bool) -> None:
    args = ('-m', '8', '-t', '45', '-U', 'http://fixture.invalid:8123') if explicit else ()
    result = invoke(fixture, *args)
    raw = manifest_bytes(fixture.body)
    expected = {'schema': 'dated-hot-l1-check-v1', 'date': '2026-10-06', 'source_nodes': 100,
                'selected_patterns_checked': 2, 'check_s': .125, 'proof_sha256': sha256(raw).hexdigest(), 'proof_bytes': len(raw)}
    assert (result.exit_code, result.stdout, result.stderr) == (0, dumps(expected) + '\n', '')
    assert fixture.out.read_bytes() == raw
    assert S_IMODE(fixture.out.stat().st_mode) == 0o600
    called_client = fixture.calls[2][1]
    assert fixture.calls == [('load', fixture.artifact),
                             ('client', ('http://fixture.invalid:8123' if explicit else 'http://127.0.0.1:8123',),
                              {'db': 'fresh', 'timeout': 105 if explicit else 660, 'max_threads': 1,
                               'max_memory_usage': (8 if explicit else 4) << 30, 'max_execution_time': 45 if explicit else 600,
                               'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}),
                             ('check', called_client, fixture.catalog, ('a', '.npy')), ('close',)]


@pytest.mark.parametrize('kind', ['existing', 'symlink', 'dangling', 'absent-parent'])
def test_output_refusal_precedes_artifact_load_and_ch(fixture, tmp_path: Path, kind: str) -> None:
    target = tmp_path / 'keep'
    if kind == 'existing':
        fixture.out.write_text('keep\n')
    elif kind == 'symlink':
        target.write_text('keep\n')
        fixture.out.symlink_to(target)
    elif kind == 'dangling':
        fixture.out.symlink_to(target)
    else:
        fixture.out = tmp_path / 'absent' / 'proof.json'
    result = invoke(fixture)
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert str(result.exception) == 'dated L1 check output must be fresh with an existing parent directory'
    assert fixture.calls == []
    if kind == 'existing':
        assert fixture.out.read_text() == 'keep\n'
    elif kind == 'symlink':
        assert (fixture.out.is_symlink(), target.read_text()) == (True, 'keep\n')
    elif kind == 'dangling':
        assert (fixture.out.is_symlink(), target.exists()) == (True, False)
    else:
        assert fixture.out.exists() is False


@pytest.mark.parametrize('patterns,loaded', [(('a', 'A'), False), (('a',) * 9, False), (('unknown',), True)])
def test_invalid_or_unregistered_selection_refused_before_ch(fixture, patterns, loaded: bool) -> None:
    result = invoke(fixture, patterns=patterns)
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert str(result.exception) == 'dated L1 check requires one to eight unique registered literals'
    assert fixture.calls == ([('load', fixture.artifact)] if loaded else [])
    assert fixture.out.exists() is False


def test_reader_cap_is_enforced_before_ch_with_no_unbounded_read(fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture.artifact.write_bytes(b'x' * 100)
    monkeypatch.setattr(dated_hot_l1, 'LIMIT', 8)
    monkeypatch.setattr(dated_hot_l1.DatedHotL1Catalog, 'load', REAL_LOAD)
    result = invoke(fixture)
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert str(result.exception) == 'dated L1 private artifact must be nonempty bytes at most 64MiB'
    assert fixture.calls == []
    assert fixture.out.exists() is False


def test_checker_failure_closes_client_and_creates_no_proof(fixture) -> None:
    fixture.error = AssertionError('source parity failure')
    result = invoke(fixture)
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert str(result.exception) == 'source parity failure'
    assert [row[0] for row in fixture.calls] == ['load', 'client', 'check', 'close']
    assert fixture.out.exists() is False


@pytest.mark.parametrize('change', [
    lambda b: b.update(complete=False), lambda b: b.update(source_nodes=99),
    lambda b: b.update(selected_patterns_checked=1), lambda b: b.update(check_s=True),
])
def test_incomplete_or_misbound_checker_result_never_written(fixture, change) -> None:
    change(fixture.body)
    result = invoke(fixture)
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert str(result.exception) == 'dated L1 checker did not return a complete matching selected-source proof'
    assert [row[0] for row in fixture.calls] == ['load', 'client', 'check', 'close']
    assert fixture.out.exists() is False


def test_write_permission_failure_closes_and_removes_only_owned_partial(fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud import cli
    def fail(*args):
        raise RuntimeError('private file permission failure')
    monkeypatch.setattr(cli.os, 'fchmod', fail)
    result = invoke(fixture)
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert str(result.exception) == 'private file permission failure'
    assert [row[0] for row in fixture.calls] == ['load', 'client', 'check', 'close']
    assert fixture.out.exists() is False
