"""Publication CLI forwards explicit scope and emits metadata only."""

from json import loads
from pathlib import Path

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import dated_hot_l1_publish as module
from dt_cloud.cli import main


ARGS = ['ch-dated-hot-l1-publish', 'private/root', '-a', 'later.json', '-a', 'earlier.json',
        '-b', 'beta', '-b', 'alfa', '-l', 'gcs_fleet', '-p', 'earlier-check.json', '-p', 'later-check.json']


def test_exact_tuple_forwarding_and_summary_has_no_private_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    body = {'schema': module.SCHEMA, 'generation': 'b' * 32, 'dates': ['2026-10-06', '2026-10-07'],
            'artifacts': [{'bytes': 10}, {'bytes': 20}], 'proofs': [{'bytes': 3}, {'bytes': 4}],
            'private_usage': {'b': 12345, 'o': 999}}

    def publish(artifacts, root, **scope):
        calls.append((artifacts, root, scope))
        return body

    monkeypatch.setattr(module, 'publish', publish)
    result = CliRunner().invoke(main, ARGS)
    assert (result.exit_code, result.exception) == (0, None)
    assert loads(result.output) == {'schema': module.SCHEMA, 'generation': 'b' * 32, 'dates': ['2026-10-06', '2026-10-07'],
                                   'artifacts': 2, 'artifact_bytes': 30, 'proof_bytes': 7}
    assert calls == [((Path('later.json'), Path('earlier.json')), Path('private/root'),
                     {'proofs': (Path('earlier-check.json'), Path('later-check.json')), 'logical_store': 'gcs_fleet', 'bucket_paths': ('beta', 'alfa')})]


def test_publication_failure_emits_no_success_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def refuse(artifacts, root, **scope):
        calls.append((artifacts, root, scope))
        raise ValueError('fixture proof binding mismatch')

    monkeypatch.setattr(module, 'publish', refuse)
    result = CliRunner().invoke(main, ARGS)
    assert (result.exit_code, str(result.exception), result.output) == (1, 'fixture proof binding mismatch', '')
    assert calls == [((Path('later.json'), Path('earlier.json')), Path('private/root'),
                     {'proofs': (Path('earlier-check.json'), Path('later-check.json')), 'logical_store': 'gcs_fleet', 'bucket_paths': ('beta', 'alfa')})]


def test_pair_count_guard_is_existing_publisher_and_creates_no_root(tmp_path: Path) -> None:
    root = tmp_path / 'private'
    result = CliRunner().invoke(main, ['ch-dated-hot-l1-publish', str(root), '-a', 'one.json', '-a', 'two.json',
                                      '-b', 'alfa', '-l', 'gcs_fleet', '-p', 'one-check.json'])
    assert (result.exit_code, str(result.exception), result.output) == (1, 'dated publication requires one artifact and proof per scan, at most 64 scans', '')
    assert root.exists() is False


@pytest.mark.parametrize('args,error', [
    (['private/root', '-a', 'a.json', '-b', 'alfa', '-p', 'proof.json'], "Error: Missing option '-l' / '--logical-store'."),
    (['-a', 'a.json', '-b', 'alfa', '-l', 'gcs_fleet', '-p', 'proof.json'], "Error: Missing argument 'ROOT'."),
])
def test_required_cli_scope_and_root_arguments_refuse_before_publish(monkeypatch: pytest.MonkeyPatch, args: list[str], error: str) -> None:
    calls = []
    monkeypatch.setattr(module, 'publish', lambda *args, **kwargs: calls.append((args, kwargs)))
    result = CliRunner().invoke(main, ['ch-dated-hot-l1-publish', *args])
    assert result.exit_code == 2
    assert result.output.splitlines() == ['Usage: main ch-dated-hot-l1-publish [OPTIONS] ROOT', "Try 'main ch-dated-hot-l1-publish --help' for help.", '', error]
    assert calls == []


def test_cli_help_exact_publish_arguments() -> None:
    result = CliRunner().invoke(main, ['ch-dated-hot-l1-publish', '--help'], terminal_width=80)
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        'Usage: main ch-dated-hot-l1-publish [OPTIONS] ROOT', '',
        '  Atomically publish accepted dated L1 files; no routing or deployment.', '', 'Options:',
        '  -a, --artifact PATH       Complete private dated native L1 artifact; repeat',
        '                            for each scan  [required]',
        '  -b, --bucket-path TEXT    Explicit complete bucket path scope; repeat for each',
        '                            bucket  [required]',
        '  -l, --logical-store TEXT  Explicit logical store shared by all dated artifacts',
        '                            [required]',
        '  -p, --proof PATH          Artifact-bound selected full-source proof; repeat',
        '                            for each scan  [required]',
        '  --help                    Show this message and exit.',
    ]
