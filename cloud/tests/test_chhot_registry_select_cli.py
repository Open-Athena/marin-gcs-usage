"""Private explicit registry selection, precise bytes/mode/compact stdout."""

from hashlib import sha256
from errno import EEXIST
from json import dumps, loads
from pathlib import Path
from stat import S_IMODE

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_registry_selection as module
from dt_cloud.chstore.daily_scalar import manifest_bytes
from dt_cloud.cli import main
from test_chhot_registry_selection import fixture as selection_fixture  # noqa: F401


@pytest.fixture
def files(selection_fixture, tmp_path: Path):
    registry_raw, source_raw, document, _ = selection_fixture
    registry, source, out = tuple(tmp_path / name for name in ('original.jsonl', 'source.json', 'selection.json'))
    registry.write_bytes(registry_raw)
    source.write_bytes(source_raw)
    return registry, source, out, document


def invoke(registry: Path, source: Path, out: Path, *, store: str = 'gcs_fleet'):
    return CliRunner().invoke(main, ['ch-hot-registry-select', '-i', str(source), '-l', store, '-o', str(out), '-r', str(registry)])


def test_complete_cli_precise_args_output_bytes_private_mode_and_original_inputs(files, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, source, out, document = files
    registry_raw, source_raw = registry.read_bytes(), source.read_bytes()
    calls, original = [], module.envelope
    def envelope(registry_bytes, source_bytes, *, logical_store):
        calls.append((registry_bytes, source_bytes, logical_store))
        return original(registry_bytes, source_bytes, logical_store=logical_store)
    monkeypatch.setattr(module, 'envelope', envelope)
    result = invoke(registry, source, out)
    raw = manifest_bytes(document)
    expected = {'schema': 'hot-registry-selection-v1', 'date': '2026-10-06', 'patterns': 4,
                'selection_sha256': sha256(raw).hexdigest(), 'selection_bytes': len(raw)}
    assert (result.exit_code, result.stdout, result.stderr) == (0, dumps(expected) + '\n', '')
    assert out.read_bytes() == raw
    assert S_IMODE(out.stat().st_mode) == 0o600
    assert registry.read_bytes() == registry_raw
    assert source.read_bytes() == source_raw
    assert calls == [(registry_raw, source_raw, 'gcs_fleet')]
    assert module.load(out, registry, source).patterns == ('a', 'b', 'c', 'd')


@pytest.mark.parametrize('kind', ['existing', 'symlink', 'dangling', 'absent-parent'])
def test_output_refusal_before_input_reads_and_no_overwrite(files, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    registry, source, out, _ = files
    target = tmp_path / 'keep.json'
    if kind == 'existing':
        out.write_text('keep\n')
    elif kind == 'symlink':
        target.write_text('keep\n')
        out.symlink_to(target)
    elif kind == 'dangling':
        out.symlink_to(target)
    else:
        out = tmp_path / 'absent' / 'selection.json'
    calls = []
    original = Path.open
    def read(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', read)
    result = invoke(registry, source, out)
    assert result.exit_code == 1
    assert (result.stdout, result.stderr) == ('', '')
    assert str(result.exception) == 'registry selection output must be fresh with an existing parent directory'
    assert calls == []
    if kind == 'existing':
        assert out.read_text() == 'keep\n'
    elif kind == 'symlink':
        assert (out.is_symlink(), target.read_text()) == (True, 'keep\n')
    elif kind == 'dangling':
        assert (out.is_symlink(), target.exists()) == (True, False)
    else:
        assert out.exists() is False


def test_input_reads_are_bounded_once_each(files, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, source, out, _ = files
    reads, original = [], Path.open
    class Reader:
        def __init__(self, path):
            self.path, self.file = path, original(path, 'rb')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.file.close()
        def read(self, count):
            reads.append((self.path, count))
            return self.file.read(count)
    def open(path, *args, **kwargs):
        return Reader(path) if path in (registry, source) else original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', open)
    result = invoke(registry, source, out)
    assert result.exit_code == 0
    assert reads == [(registry, module.REGISTRY_LIMIT + 1), (source, module.DOCUMENT_LIMIT + 1)]


@pytest.mark.parametrize('which,error', [
    ('registry', 'registry selection union export must be nonempty immutable bytes at most 64 MiB'),
    ('source', 'registry selection requires bounded nonempty immutable byte inputs'),
])
def test_oversized_inputs_refused_without_output(files, monkeypatch: pytest.MonkeyPatch, which, error: str) -> None:
    registry, source, out, _ = files
    if which == 'registry':
        monkeypatch.setattr(module, 'REGISTRY_LIMIT', 8)
    else:
        monkeypatch.setattr(module, 'DOCUMENT_LIMIT', 8)
    result = invoke(registry, source, out)
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert str(result.exception) == error
    assert out.exists() is False


def test_subtree_manifest_not_relabelled_global(files) -> None:
    registry, source, out, _ = files
    body = loads(source.read_bytes())
    body['prefix'] = 'bucket/complete-subtree'
    source.write_bytes(manifest_bytes(body))
    result = invoke(registry, source, out)
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert str(result.exception) == 'registry selection requires a completed global daily-scalar-source-v1 manifest'
    assert out.exists() is False


def test_explicit_logical_store_mismatch_refused(files) -> None:
    registry, source, out, _ = files
    result = invoke(registry, source, out, store='other_store')
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert str(result.exception) == 'registry selection logical store differs from the accepted daily source'
    assert out.exists() is False


def test_exclusive_create_preserves_an_output_created_after_preflight(files, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud import cli
    registry, source, out, _ = files
    calls, original = [], cli.os.open
    def create(path, flags, mode=0o777, **kwargs):
        if path == out:
            calls.append((path, flags, mode))
            out.write_text('other creator\n')
        return original(path, flags, mode, **kwargs)
    monkeypatch.setattr(cli.os, 'open', create)
    result = invoke(registry, source, out)
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert (type(result.exception), result.exception.errno, result.exception.filename) == (FileExistsError, EEXIST, str(out))
    assert calls == [(out, cli.os.O_WRONLY | cli.os.O_CREAT | cli.os.O_EXCL, 0o600)]
    assert out.read_text() == 'other creator\n'


def test_permission_failure_removes_only_owned_partial_output(files, monkeypatch: pytest.MonkeyPatch) -> None:
    from dt_cloud import cli
    registry, source, out, _ = files
    original_registry, original_source = registry.read_bytes(), source.read_bytes()
    def fail(*args):
        raise RuntimeError('fixture chmod failure')
    monkeypatch.setattr(cli.os, 'fchmod', fail)
    result = invoke(registry, source, out)
    assert (result.exit_code, result.stdout, result.stderr) == (1, '', '')
    assert str(result.exception) == 'fixture chmod failure'
    assert out.exists() is False
    assert (registry.read_bytes(), source.read_bytes()) == (original_registry, original_source)
