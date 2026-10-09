"""Explicit pinned inputs, bounded offline settings, metadata-only stdout."""

from hashlib import sha256
from json import loads
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import client, dated_hot_l1, hot_registry_selection
from dt_cloud.cli import main


@pytest.fixture
def mocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = SimpleNamespace(calls=[], out=tmp_path / 'dated.json', selection=SimpleNamespace(patterns=('a', 'b')),
                            raw=b'private completed artifact\n', body={'schema': 'dated-hot-l1-native-v1', 'date': '2026-10-06',
                            'stages': {'build_s': .25}, 'root': {'b': 123456789, 'o': 999}})

    def load(selection, registry, source):
        state.calls.append(('load', selection, registry, source))
        return state.selection

    state.ch = SimpleNamespace(close=lambda: state.calls.append(('close',)))

    def connect(url, **settings):
        state.calls.append(('client', url, settings))
        return state.ch

    def build(ch, selection, *, binary, out):
        state.calls.append(('build', ch is state.ch, selection is state.selection, binary, out))
        out.write_bytes(state.raw)
        return state.body

    monkeypatch.setattr(hot_registry_selection, 'load', load)
    monkeypatch.setattr(client, 'Ch', connect)
    monkeypatch.setattr(dated_hot_l1, 'build', build)
    state.args = ['ch-dated-hot-l1-build', '-b', 'native', '-i', 'source.json', '-o', str(state.out), '-r', 'registry.jsonl', '-s', 'selection.json']
    return state


@pytest.mark.parametrize('extra,settings', [
    ([], {'timeout': 1860, 'max_threads': 4, 'max_memory_usage': 8 << 30, 'max_temporary_data_on_disk_size_for_query': 8 << 30,
          'max_execution_time': 1800, 'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}),
    (['-m', '2', '-T', '3', '-t', '60'], {'timeout': 120, 'max_threads': 4, 'max_memory_usage': 2 << 30,
          'max_temporary_data_on_disk_size_for_query': 3 << 30, 'max_execution_time': 60,
          'timeout_before_checking_execution_speed': 0, 'timeout_overflow_mode': 'throw'}),
])
def test_exact_cli_forwarding_and_summary_never_emits_usage_weights(mocked, extra: list[str], settings: dict) -> None:
    result = CliRunner().invoke(main, mocked.args + ['-U', 'http://dev-ch:8123', *extra])
    assert (result.exit_code, result.exception) == (0, None)
    assert loads(result.output) == {'schema': 'dated-hot-l1-native-v1', 'date': '2026-10-06', 'patterns': 2, 'aliases': 0,
                                   'artifact_bytes': len(mocked.raw), 'artifact_sha256': sha256(mocked.raw).hexdigest(), 'stages': {'build_s': .25}}
    assert mocked.calls == [('load', Path('selection.json'), Path('registry.jsonl'), Path('source.json')),
                            ('client', 'http://dev-ch:8123', settings), ('build', True, True, Path('native'), mocked.out), ('close',)]


def test_fresh_output_guard_runs_before_loading_any_source_or_client(mocked) -> None:
    mocked.out.write_bytes(b'preserve')
    result = CliRunner().invoke(main, mocked.args)
    assert (result.exit_code, str(result.exception), result.output) == (1, 'dated L1 output must be fresh with an existing parent directory', '')
    assert (mocked.out.read_bytes(), mocked.calls) == (b'preserve', [])


def test_bad_selection_never_constructs_client_or_output(mocked, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args):
        mocked.calls.append(('load', *args))
        raise ValueError('fixture pinned source mismatch')

    monkeypatch.setattr(hot_registry_selection, 'load', refuse)
    result = CliRunner().invoke(main, mocked.args)
    assert (result.exit_code, str(result.exception), result.output) == (1, 'fixture pinned source mismatch', '')
    assert (mocked.out.exists(), mocked.calls) == (False, [('load', Path('selection.json'), Path('registry.jsonl'), Path('source.json'))])


def test_native_failure_closes_client_and_emits_no_success_summary(mocked, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args, **kwargs):
        raise ValueError('fixture native refusal')

    monkeypatch.setattr(dated_hot_l1, 'build', refuse)
    result = CliRunner().invoke(main, mocked.args)
    assert (result.exit_code, str(result.exception), result.output) == (1, 'fixture native refusal', '')
    assert (mocked.out.exists(), mocked.calls[-1]) == (False, ('close',))


def test_cli_help_lists_pinned_inputs_and_no_publication() -> None:
    result = CliRunner().invoke(main, ['ch-dated-hot-l1-build', '--help'], terminal_width=80)
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        'Usage: main ch-dated-hot-l1-build [OPTIONS]', '',
        '  Build root-only dated L1 with unchanged registry qualification; no',
        '  publication.', '', 'Options:',
        '  -b, --binary PATH               Explicit native L1 executable to pin by SHA256',
        '                                  [required]',
        '  -i, --source-manifest PATH      Pinned completed global daily scalar source',
        '                                  manifest  [required]',
        '  -m, --memory-gib INTEGER RANGE  Offline per-statement memory cap  [1<=x<=8]',
        '  -o, --out PATH                  Fresh private dated L1 artifact; never',
        '                                  overwrites  [required]',
        '  -r, --registry PATH             Original completed dated-union registry JSONL;',
        '                                  never rewritten  [required]',
        '  -s, --selection PATH            Explicit registry-to-daily-source selection',
        '                                  envelope  [required]',
        '  -t, --timeout-seconds INTEGER RANGE',
        '                                  Per-statement deadline, not an overall build',
        '                                  SLA  [1<=x<=3600]',
        '  -T, --spill-gib INTEGER RANGE   Offline per-query simultaneous temporary disk',
        '                                  cap  [1<=x<=8]',
        '  -U, --url TEXT                  Existing development ClickHouse HTTP endpoint',
        '  --help                          Show this message and exit.',
    ]
