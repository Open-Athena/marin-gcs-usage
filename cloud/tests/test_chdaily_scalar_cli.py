from json import dumps, loads
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import client, daily_scalar, daily_scalar_check
from dt_cloud.cli import main


def test_daily_scalar_cli_exact_forwarding_and_private_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor, parquet, out = (tmp_path / name for name in ('input.json', 'input.parquet', 'source.json'))
    descriptor.write_text(dumps({'schema': 'fixture-pinned-input'}) + '\n')
    calls = []
    ch = SimpleNamespace(close=lambda: calls.append(('close',)))
    monkeypatch.setattr(client, 'Ch', lambda url: calls.append(('client', url)) or ch)
    body = {'schema': 'daily-scalar-source-v1', 'complete': True, 'date': '2026-10-06',
            'target': 'fresh', 'prefix': 'a/run', 'source_rows': 10, 'selected_source_rows': 4,
            'nodes': 3, 'stages': {'upload': .1}}

    def build(actual_ch, target, local_parquet, source, **limits):
        progress = limits.pop('progress')
        calls.append(('build', actual_ch is ch, target, local_parquet, source, limits))
        progress('daily scalar upload: started')
        progress('daily scalar upload: complete')
        return body

    monkeypatch.setattr(daily_scalar, 'build', build)
    result = CliRunner().invoke(main, [
        'ch-daily-scalar-build', 'fresh', str(parquet), '-s', str(descriptor), '-o', str(out),
        '-p', 'a/run', '-n', '100', '-m', '2', '-T', '3', '-t', '60', '-b', '4', '-f', '21',
        '-U', 'http://dev-ch:8123',
    ])
    assert (result.exit_code, result.exception) == (0, None)
    assert loads(result.stdout) == {**body, 'out': str(out)}
    assert result.stderr.splitlines() == ['daily scalar upload: started', 'daily scalar upload: complete']
    assert out.read_bytes() == daily_scalar.manifest_bytes(body)
    assert out.stat().st_mode & 0o777 == 0o600
    assert calls == [
        ('client', 'http://dev-ch:8123'),
        ('build', True, 'fresh', parquet, {'schema': 'fixture-pinned-input'},
         {'prefix': 'a/run', 'max_nodes': 100, 'memory_bytes': 2 << 30, 'spill_bytes': 3 << 30,
          'query_seconds': 60, 'max_owned_bytes': 4 << 30, 'min_free_bytes': 21 << 30,
          'resume': None, 'sort_spill_bytes': 256 << 20, 'order_plan': 'window'}),
        ('close',),
    ]


@pytest.mark.parametrize('case,error', [
    ('existing', 'daily scalar output must be fresh with an existing parent directory'),
    ('oversized', 'daily scalar descriptor exceeds 64 KiB'),
    ('duplicate', 'hot L1 artifact contains duplicate JSON keys'),
])
def test_bad_cli_files_refuse_before_clickhouse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    error: str,
) -> None:
    descriptor, out = tmp_path / 'input.json', tmp_path / 'source.json'
    descriptor.write_bytes(b'x' * ((64 << 10) + 1) if case == 'oversized' else b'{"x":1,"x":2}')
    if case == 'existing':
        out.write_bytes(b'keep\n')
    calls = []
    monkeypatch.setattr(client, 'Ch', lambda url: calls.append(url))
    result = CliRunner().invoke(main, ['ch-daily-scalar-build', 'fresh', 'input.parquet', '-s', str(descriptor), '-o', str(out)])
    assert result.exit_code == 1
    assert str(result.exception) == error
    assert result.output == ''
    assert calls == []
    if case == 'existing':
        assert out.read_bytes() == b'keep\n'


def test_daily_scalar_resume_cli_explicit_bounded_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor, evidence, out = (tmp_path / name for name in ('input.json', 'resume.json', 'source.json'))
    descriptor.write_text(dumps({'schema': 'fixture-pinned-input'}) + '\n')
    proof = {'schema': 'fixture-explicit-resume', 'queries': {'upload': 'private-owned-query'}}
    evidence.write_text(dumps(proof) + '\n')
    calls = []
    ch = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(client, 'Ch', lambda url: ch)
    body = {'schema': 'daily-scalar-source-v1', 'complete': True, 'date': '2026-10-06', 'target': 'retained',
            'prefix': '', 'source_rows': 10, 'selected_source_rows': 10, 'nodes': 9, 'stages': {'ordered': .1}}

    def build(actual_ch, target, parquet, source, **limits):
        limits.pop('progress')
        calls.append((actual_ch is ch, target, parquet, source, limits['resume'], limits['sort_spill_bytes'], limits['order_plan']))
        return body

    monkeypatch.setattr(daily_scalar, 'build', build)
    result = CliRunner().invoke(main, ['ch-daily-scalar-build', 'retained', 'input.parquet', '-s', str(descriptor),
                                    '-r', str(evidence), '-S', '1024', '-e', 'physical', '-o', str(out)])
    assert (result.exit_code, result.exception) == (0, None)
    assert calls == [(True, 'retained', Path('input.parquet'), {'schema': 'fixture-pinned-input'}, proof, 1 << 30, 'physical')]
    assert loads(result.stdout) == {**body, 'out': str(out)}
    assert result.stderr == ''
    assert out.read_bytes() == daily_scalar.manifest_bytes(body)
    assert out.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('case,error', [
    ('oversized', 'daily scalar resume evidence exceeds 64 KiB'),
    ('duplicate', 'hot L1 artifact contains duplicate JSON keys'),
    ('sort', 'daily scalar sort threshold must not exceed one quarter of its memory cap'),
])
def test_daily_scalar_resume_cli_refuses_bad_inputs_before_clickhouse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    error: str,
) -> None:
    descriptor, evidence, out = (tmp_path / name for name in ('input.json', 'resume.json', 'source.json'))
    descriptor.write_text('{}\n')
    evidence.write_bytes(b'x' * ((64 << 10) + 1) if case == 'oversized' else b'{"x":1,"x":2}')
    calls = []
    monkeypatch.setattr(client, 'Ch', lambda url: calls.append(url))
    result = CliRunner().invoke(main, ['ch-daily-scalar-build', 'retained', 'input.parquet', '-s', str(descriptor),
                                    '-r', str(evidence), '-S', '1024' if case == 'sort' else '256', '-m', '1', '-o', str(out)])
    assert result.exit_code == 1
    assert str(result.exception) == error
    assert result.output == ''
    assert calls == []
    assert out.exists() is False


def test_daily_scalar_check_cli_exact_forwarding(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    body = {'schema': 'daily-scalar-check-v1', 'complete': True, 'nodes_checked': 100, 'sampling': False}

    def check(manifest, parquet, url, out, **limits):
        calls.append((manifest, parquet, url, out, limits))
        return body

    monkeypatch.setattr(daily_scalar_check, 'check', check)
    result = CliRunner().invoke(main, [
        'ch-daily-scalar-check', 'manifest.json', 'input.parquet', '-o', 'check.json',
        '-n', '100', '-t', '60', '-U', 'http://dev-ch:8123',
    ])
    assert (result.exit_code, result.exception) == (0, None)
    assert loads(result.output) == body
    assert calls == [(Path('manifest.json'), Path('input.parquet'), 'http://dev-ch:8123', Path('check.json'),
                      {'max_nodes': 100, 'seconds': 60})]
