"""Node coordinator: no real Docker, network, clock waits, or source processing."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'ch-store/daily-root-refresh.sh'
IDENT = '1' * 64
STATE = '{{.State.Status}} {{.State.Running}} {{.State.ExitCode}} {{.State.OOMKilled}}'
PHASES = ['wait source', 'select registry', 'validate selected literals and bucket scope',
          'build dated root catalog', 'check four selected literals against full source',
          'publish new private generation', 'private publication complete; serving activation remains manual']
PREFLIGHT = '''from pathlib import Path
from sys import argv
from dt_cloud.chstore.hot_registry_selection import load
pinned = load(*(Path(path) for path in argv[1:4]))
if any(pattern not in pinned.patterns for pattern in (".json", ".npy", "zarr.json", "zarr")):
    raise ValueError("all four selected oracle literals must be registered")
if pinned.logical_store != argv[4] or sorted(row[2] for row in pinned.buckets) != sorted(argv[5:]):
    raise ValueError("complete source bucket scope or logical store differs")
'''


@pytest.fixture
def fixture(tmp_path: Path) -> SimpleNamespace:
    data = tmp_path / 'data'
    data.mkdir()
    paths = {key: data / name for key, name in {
        'source': 'source.json', 'registry': 'registry.jsonl', 'binary': 'native', 'selection': 'selection.json',
        'artifact': 'artifact.json', 'proof': 'proof.json', 'publication': 'catalog',
    }.items()}
    for key in ('source', 'registry', 'binary'):
        paths[key].write_text('fixture\n')
    paths['binary'].chmod(0o700)
    (data / 'image').write_text('fixture-image\n')
    config, events = tmp_path / 'config.json', tmp_path / 'events.jsonl'
    config.write_text(json.dumps({'clock': 0, 'running': 0, 'exit': 0, 'oom': False,
                                 'patterns': ['.json', '.npy', 'zarr.json', 'zarr'], 'buckets': ['bucket-a', 'bucket-b']}))
    mock = f'#!{sys.executable}\n' + r'''
import json, os, sys, types
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
config, events = Path(os.environ['REFRESH_CONFIG']), Path(os.environ['REFRESH_EVENTS'])
state = json.loads(config.read_text())
with events.open('a') as output: output.write(json.dumps([name, *args]) + '\n')
def save(): config.write_text(json.dumps(state))
if name == 'date':
    print(state['clock'])
elif name == 'sleep':
    state['clock'] += int(args[0]); save()
elif args[0] == 'inspect':
    if args[2] == '{{.Id}}': print('1' * 64)
    else:
        running = state['running'] > 0
        state['running'] = max(0, state['running'] - 1); save()
        status = state.get('status', 'running' if running else 'exited')
        print(f"{status} {str(running).lower()} {state['exit']} {str(state['oom']).lower()}")
else:
    if '-c' in args:
        module = types.ModuleType('dt_cloud.chstore.hot_registry_selection')
        module.load = lambda *paths: types.SimpleNamespace(patterns=state['patterns'], logical_store='fixture_store',
                                                          buckets=tuple((1, 2, bucket) for bucket in state['buckets']))
        sys.modules[module.__name__] = module
        sys.argv = ['-c', *args[args.index('-c') + 2:]]
        try: exec(args[args.index('-c') + 1])
        except ValueError as error: print(str(error), file=sys.stderr); sys.exit(7)
    else:
        cli = args[args.index('dt_cloud.cli') + 1:]
        phase = cli[0]
        if state.get('fail') == phase:
            print('mock phase failed', file=sys.stderr); sys.exit(9)
        if '-o' in cli: Path(cli[cli.index('-o') + 1]).write_text('private artifact\n')
        if phase == 'ch-dated-hot-l1-build' and state.get('create_publication'):
            Path(state['create_publication']).mkdir()
        if phase == 'ch-dated-hot-l1-publish':
            root = Path(cli[-1]); root.mkdir(); (root / 'current.json').write_text('accepted\n')
        print('suppressed private CLI result')
'''
    for command in ('docker', 'date', 'sleep'):
        program = tmp_path / command
        program.write_text(mock)
        program.chmod(0o700)
    return SimpleNamespace(data=data, paths=paths, config=config, events=events,
                           env={**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}',
                                'REFRESH_CONFIG': str(config), 'REFRESH_EVENTS': str(events)})


def configure(fixture: SimpleNamespace, **changes: object) -> None:
    fixture.config.write_text(json.dumps(json.loads(fixture.config.read_text()) | changes))


def run(fixture: SimpleNamespace, *extra: str) -> subprocess.CompletedProcess:
    p = fixture.paths
    return subprocess.run(['bash', str(SCRIPT), '--data-root', str(fixture.data), '-j', 'recovery',
                           '-m', str(p['source']), '-r', str(p['registry']), '-b', str(p['binary']),
                           '-s', str(p['selection']), '-a', str(p['artifact']), '-p', str(p['proof']),
                           '-g', str(p['publication']), '-l', 'fixture_store', '-B', 'bucket-b', '-B', 'bucket-a',
                           *extra], env=fixture.env, capture_output=True, text=True, timeout=10)


def events(fixture: SimpleNamespace) -> list[list[str]]:
    return [json.loads(line) for line in fixture.events.read_text().splitlines()] if fixture.events.exists() else []


def commands(fixture: SimpleNamespace, timeout: int = 3600) -> list[list[str]]:
    p = {key: str(path) for key, path in fixture.paths.items()}
    data = str(fixture.data)
    docker = ['docker', 'run', '--rm', '--network', 'host', '-v', f'{data}:{data}', '-e', f'PYTHONPATH={data}/src',
              '--entrypoint', 'python3', 'fixture-image']
    cli = [*docker, '-u', '-m', 'dt_cloud.cli']
    return [
        [*cli, 'ch-hot-registry-select', '-i', p['source'], '-l', 'fixture_store', '-o', p['selection'], '-r', p['registry']],
        [*docker, '-c', PREFLIGHT, p['selection'], p['registry'], p['source'], 'fixture_store', 'bucket-b', 'bucket-a'],
        [*cli, 'ch-dated-hot-l1-build', '-b', p['binary'], '-i', p['source'], '-m', '8', '-o', p['artifact'], '-r',
         p['registry'], '-s', p['selection'], '-t', str(timeout), '-T', '8'],
        [*cli, 'ch-dated-hot-l1-check', '-m', '4', '-n', '.json', '-n', '.npy', '-n', 'zarr.json', '-n', 'zarr',
         '-o', p['proof'], '-t', '600', p['artifact']],
        [*cli, 'ch-dated-hot-l1-publish', '-a', p['artifact'], '-l', 'fixture_store', '-p', p['proof'],
         '-b', 'bucket-b', '-b', 'bucket-a', p['publication']],
    ]


def initial_events() -> list[list[str]]:
    return [['docker', 'inspect', '--format', '{{.Id}}', 'ch-job-recovery'], ['date', '+%s'],
            ['docker', 'inspect', '--format', STATE, IDENT]]


@pytest.mark.parametrize('timeout', [3600, 1800])
def test_exact_serial_commands_and_private_phase_only_output(fixture: SimpleNamespace, timeout: int) -> None:
    result = run(fixture, *(['-t', '1800'] if timeout == 1800 else []))
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (0, '', PHASES)
    assert events(fixture) == [*initial_events(), *commands(fixture, timeout)]
    assert (fixture.paths['publication'] / 'current.json').read_text() == 'accepted\n'
    assert [fixture.paths[key].stat().st_mode & 0o777 for key in ('selection', 'artifact', 'proof')] == [0o600] * 3


@pytest.mark.parametrize('exit_code,oom', [(9, False), (0, True)])
def test_failed_source_stops_every_downstream_phase(fixture: SimpleNamespace, exit_code: int, oom: bool) -> None:
    configure(fixture, exit=exit_code, oom=oom)
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr) == (1, '',
        'wait source\nsource job failed or was OOM-killed; no downstream work\n')
    assert events(fixture) == initial_events()
    assert [fixture.paths[key].exists() for key in ('selection', 'artifact', 'proof', 'publication')] == [False] * 4


def test_created_container_with_preexisting_manifest_is_not_completed_source(fixture: SimpleNamespace) -> None:
    configure(fixture, status='created')
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr) == (2, '', 'wait source\ninvalid source Docker state\n')
    assert events(fixture) == initial_events()
    assert fixture.paths['source'].read_text() == 'fixture\n'
    assert [fixture.paths[key].exists() for key in ('selection', 'artifact', 'proof', 'publication')] == [False] * 4


def test_exponential_wait_caps_at_sixty_and_never_overlaps(fixture: SimpleNamespace) -> None:
    configure(fixture, running=6)
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (0, '', PHASES)
    waited = []
    for pause in (5, 10, 20, 40, 60, 60):
        waited.extend([['date', '+%s'], ['sleep', str(pause)], ['docker', 'inspect', '--format', STATE, IDENT]])
    assert events(fixture) == [*initial_events(), *waited, *commands(fixture)]


def test_dependency_wait_deadline_has_no_downstream_calls(fixture: SimpleNamespace) -> None:
    configure(fixture, running=99)
    result = run(fixture, '-w', '8')
    assert (result.returncode, result.stdout, result.stderr) == (124, '',
        'wait source\nsource wait deadline exceeded; no downstream work\n')
    assert events(fixture) == [*initial_events(), ['date', '+%s'], ['sleep', '5'],
        ['docker', 'inspect', '--format', STATE, IDENT], ['date', '+%s'], ['sleep', '3'],
        ['docker', 'inspect', '--format', STATE, IDENT], ['date', '+%s']]


@pytest.mark.parametrize('field', ['selection', 'artifact', 'proof', 'publication'])
def test_existing_outputs_refuse_before_even_inspecting_source(fixture: SimpleNamespace, field: str) -> None:
    fixture.paths[field].write_text('retained accepted bytes\n')
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr) == (2, '',
        'output or publication root already exists; nothing overwritten\n')
    assert events(fixture) == []
    assert fixture.paths[field].read_text() == 'retained accepted bytes\n'


@pytest.mark.parametrize('change,error', [
    ({'patterns': ['.json', '.npy', 'zarr.json']}, 'all four selected oracle literals must be registered'),
    ({'buckets': ['bucket-a']}, 'complete source bucket scope or logical store differs'),
])
def test_offline_membership_and_scope_guard_before_native_build(fixture: SimpleNamespace, change: dict, error: str) -> None:
    configure(fixture, **change)
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (7, '', [*PHASES[:3], error])
    assert events(fixture) == [*initial_events(), *commands(fixture)[:2]]


@pytest.mark.parametrize('phase,index', [('ch-hot-registry-select', 0), ('ch-dated-hot-l1-build', 2),
                                       ('ch-dated-hot-l1-check', 3), ('ch-dated-hot-l1-publish', 4)])
def test_each_failed_phase_stops_serial_pipeline(fixture: SimpleNamespace, phase: str, index: int) -> None:
    configure(fixture, fail=phase)
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (9, '', [*PHASES[:index + 2], 'mock phase failed'])
    assert events(fixture) == [*initial_events(), *commands(fixture)[:index + 1]]
    assert fixture.paths['publication'].exists() is False


@pytest.mark.parametrize('size', [0, 65537])
def test_manifest_size_is_bounded_before_selection(fixture: SimpleNamespace, size: int) -> None:
    fixture.paths['source'].write_bytes(b'x' * size)
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (2, '',
        ['wait source', 'source manifest must be nonempty and at most 64 KiB'])
    assert events(fixture) == initial_events()


def test_successful_container_without_manifest_cannot_select(fixture: SimpleNamespace) -> None:
    fixture.paths['source'].unlink()
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (2, '',
        ['wait source', 'successful source job did not produce a manifest file'])
    assert events(fixture) == initial_events()


@pytest.mark.parametrize('kind', ['alias', 'outside', 'symlink'])
def test_unsafe_output_paths_refuse_before_any_docker_call(fixture: SimpleNamespace, kind: str) -> None:
    if kind == 'alias':
        path = str(fixture.paths['source'])
    elif kind == 'outside':
        path = str(fixture.data.parent / 'outside.json')
    else:
        path = str(fixture.data / 'linked.json')
        Path(path).symlink_to(fixture.paths['source'])
    result = run(fixture, '-a', path)
    assert (result.returncode, result.stdout, result.stderr) == (2, '',
        'all paths must be distinct owned paths within data root\n')
    assert events(fixture) == []
    assert fixture.paths['source'].read_text() == 'fixture\n'


def test_publication_root_created_during_build_is_retained_and_not_replaced(fixture: SimpleNamespace) -> None:
    configure(fixture, create_publication=str(fixture.paths['publication']))
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (2, '', [*PHASES[:5],
        'output or publication root already exists; nothing overwritten'])
    assert events(fixture) == [*initial_events(), *commands(fixture)[:4]]
    assert list(fixture.paths['publication'].iterdir()) == []


@pytest.mark.parametrize('args,error', [
    (('-j', '../unsafe'), 'invalid source tag (1..63 safe lowercase characters)'),
    (('-w', '7201'), 'source wait must be 1..7200 seconds'),
    (('-t', '600'), 'native timeout must be 1800 or 3600 seconds'),
    (('-l', 'gcs;echo'), 'invalid logical store identifier'),
    (('-B', 'bucket-a'), 'duplicate bucket path'),
])
def test_invalid_configuration_makes_no_docker_calls(fixture: SimpleNamespace, args: tuple, error: str) -> None:
    result = run(fixture, *args)
    assert (result.returncode, result.stdout, result.stderr) == (2, '', error + '\n')
    assert events(fixture) == []
