"""The optional TCP source is explicit, bounded and requires process success."""

from hashlib import sha256
from io import BytesIO
from os import environ
from pathlib import Path
from subprocess import TimeoutExpired
from threading import Timer
from time import sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dt_cloud.chstore import hot_l2_native_source as module


@pytest.fixture
def binary(tmp_path: Path) -> Path:
    path = tmp_path / 'clickhouse'
    path.touch()
    path.chmod(0o700)
    return path


def control(**changes) -> SimpleNamespace:
    return SimpleNamespace(**({'url': 'http://localhost:8123', 'headers': {}, 'db': 'fleet',
                              'settings': {'database': 'fleet', 'session_id': 'omitted', 'session_timeout': '600',
                                           'max_threads': '4', 'max_execution_time': '120', 'log_comment': 'owned'}} | changes))


class Child:
    def __init__(self, status: int | None = 0, stderr=None):
        self.stdout = BytesIO(b'complete-rowbinary')
        self.stderr = BytesIO(b'private diagnostic') if stderr is None else stderr
        self.returncode, self.events = status, []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.events.append(('wait', timeout))
        return self.returncode

    def terminate(self):
        self.events.append('terminate')
        self.returncode = -15

    def kill(self):
        self.events.append('kill')
        self.returncode = -9


def install(monkeypatch: pytest.MonkeyPatch, child: Child) -> list:
    calls = []

    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        return child

    monkeypatch.setattr(module, 'Popen', launch)
    return calls


def test_exact_command_is_scoped_and_ambient_configuration_absent(binary: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    child = Child(stderr=BytesIO(b'x' * 100_000))
    calls = install(monkeypatch, child)
    source = module.NativeSource(control(), binary, 'owned_query', 120)
    assert list(source.stream(' SELECT x; ', 'RowBinary')) == [b'complete-rowbinary']
    assert calls == [([str(binary.resolve()), 'client', f'--config-file={module.CONFIG.resolve()}',
                      '--host=127.0.0.1', '--port=9000', '--user=default', '--password=', '--database=fleet',
                      '--query_id=owned_query', '--progress=off', '--send_logs_level=none',
                      '--receive_timeout=180', '--send_timeout=180', '--max_threads=4',
                      '--max_execution_time=120', '--log_comment=owned', '--query', 'SELECT x FORMAT RowBinary'],
                     {'stdin': module.DEVNULL, 'stdout': module.PIPE, 'stderr': module.PIPE, 'bufsize': 0,
                      'env': {'LC_ALL': 'C.UTF-8'}})]
    assert bytes(source.stderr_tail) == b'x' * 65536
    assert (child.stdout.closed, child.stderr.closed, source.closed, source.stderr_failed) == (True, True, True, False)
    source.close()
    assert child.events == [('wait', 5)]


def test_exit_failure_after_complete_bytes_never_accepts(binary: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    child = Child(status=7)
    install(monkeypatch, child)
    source = module.NativeSource(control(), binary, 'owned_query', 120)
    stream = source.stream('SELECT x', 'RowBinary')
    assert next(stream) == b'complete-rowbinary'
    with pytest.raises(RuntimeError) as caught:
        next(stream)
    assert str(caught.value) == 'native paired source exited with status 7; diagnostics withheld'
    assert (child.stdout.closed, child.stderr.closed, source.closed) == (True, True, True)


def test_hanging_child_is_killed_after_bounded_terminate_wait(binary: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Hanging(Child):
        def terminate(self):
            self.events.append('terminate')

        def wait(self, timeout):
            self.events.append(('wait', timeout))
            if self.returncode is None:
                raise TimeoutExpired('private command', timeout)
            return self.returncode

    child = Hanging(status=None)
    source = module.NativeSource(control(), binary, 'owned_query', 120)
    source.child = child
    source.close()
    assert child.events == ['terminate', ('wait', 5), 'kill', ('wait', 5)]
    assert (child.stdout.closed, child.stderr.closed, source.closed) == (True, True, True)


def test_stderr_worker_failure_is_sanitized_and_invalidates_complete_output(binary: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    class BrokenDiagnostic(BytesIO):
        def read(self, size):
            raise OSError('private source contents must not escape')

    child = Child(stderr=BrokenDiagnostic())
    install(monkeypatch, child)
    source = module.NativeSource(control(), binary, 'owned_query', 120)
    with pytest.raises(RuntimeError) as caught:
        list(source.stream('SELECT x', 'RowBinary'))
    assert str(caught.value) == 'native paired source diagnostic reader failed; diagnostics withheld'
    assert capsys.readouterr() == ('', '')
    assert (child.stdout.closed, child.stderr.closed, source.stderr_failed) == (True, True, True)


def test_cleanup_failures_do_not_mask_origin_or_skip_other_pipe(binary: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenPipe(BytesIO):
        def close(self):
            super().close()
            raise OSError('private close details')

    child = Child(status=7)
    child.stdout = BrokenPipe(b'complete-rowbinary')
    install(monkeypatch, child)
    source = module.NativeSource(control(), binary, 'owned_query', 120)
    with pytest.raises(ExceptionGroup) as caught:
        list(source.stream('SELECT x', 'RowBinary'))
    assert caught.value.message == 'native paired source failed and cleanup was incomplete'
    assert [(type(error), str(error)) for error in caught.value.exceptions] == [
        (RuntimeError, 'native paired source exited with status 7; diagnostics withheld'),
        (RuntimeError, 'native paired source pipe close failed'),
    ]
    assert (child.stdout.closed, child.stderr.closed, source.closed) == (True, True, True)


def test_join_and_kill_failures_still_close_both_pipes(binary: Path) -> None:
    class Stuck(Child):
        def terminate(self):
            raise OSError('private terminate')

        def wait(self, timeout):
            raise TimeoutExpired('private command', timeout)

        def kill(self):
            raise OSError('private kill')

    source = module.NativeSource(control(), binary, 'owned_query', 120)
    child = source.child = Stuck(status=None)

    def bad_join(timeout):
        raise RuntimeError('private thread')

    source.stderr_thread = SimpleNamespace(join=bad_join)
    with pytest.raises(ExceptionGroup) as caught:
        source.close()
    assert caught.value.message == 'native paired source cleanup was incomplete'
    assert [str(error) for error in caught.value.exceptions] == [
        'native paired source termination failed', 'native paired source kill failed',
        'native paired source did not stop within bounded cleanup', 'native paired source diagnostic reader join failed',
    ]
    assert (child.stdout.closed, child.stderr.closed, source.closed) == (True, True, True)


@pytest.mark.parametrize('changes,message', [
    ({'url': 'http://remote:8123'}, 'native paired source requires default unauthenticated loopback ClickHouse'),
    ({'url': 'http://localhost:8123/path'}, 'native paired source requires default unauthenticated loopback ClickHouse'),
    ({'headers': {'X-ClickHouse-Key': 'not printed'}}, 'native paired source requires default unauthenticated loopback ClickHouse'),
    ({'settings': {'unknown_setting': '1'}}, 'native paired source cannot silently discard unknown query settings'),
])
def test_unsafe_controls_refuse_before_process(binary: Path, monkeypatch: pytest.MonkeyPatch, changes: dict, message: str) -> None:
    calls = install(monkeypatch, Child())
    with pytest.raises(ValueError) as caught:
        module.NativeSource(control(**changes), binary, 'owned_query', 120)
    assert (str(caught.value), calls) == (message, [])


def test_executable_and_empty_config_are_verified(binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary.chmod(0o600)
    with pytest.raises(ValueError) as caught:
        module.NativeSource(control(), binary, 'owned_query', 120)
    assert str(caught.value) == 'native paired source requires an explicit executable multicall ClickHouse binary'
    binary.chmod(0o700)
    config = tmp_path / 'changed.xml'
    config.write_bytes(b'<clickhouse><password>untrusted</password></clickhouse>\n')
    monkeypatch.setattr(module, 'CONFIG', config)
    with pytest.raises(ValueError) as caught:
        module.NativeSource(control(), binary, 'owned_query', 120)
    assert str(caught.value) == 'native paired source empty client configuration changed'


def test_native_tcp_survives_real_35_second_consumer_backpressure() -> None:
    """Explicit node-only 64 MiB screen, not a fleet benchmark or config mutation."""
    binary = environ.get('HL2_NATIVE_SOURCE_BINARY')
    if not binary:
        pytest.skip('transport smoke requires explicit HL2_NATIVE_SOURCE_BINARY; no server or binary startup otherwise')
    from dt_cloud.chstore.client import Ch, statement_timeout_settings
    from dt_cloud.chstore.hot_l2_pair_stream import _cancel

    ch = Ch(session=False, timeout=180, max_threads=2, max_memory_usage=128 << 20,
            **statement_timeout_settings(120))
    tag = 'hot_l2_transport_smoke_' + uuid4().hex
    ids = [tag + '_http', tag + '_tcp']
    reference = ch.fork(query_id=ids[0])
    source = module.NativeSource(ch, Path(binary), ids[1], 120)
    query = 'SELECT number,sipHash64(number) FROM numbers(4194304)'
    aborted, abort_failures = [], []

    def abort() -> None:
        aborted.append(True)
        try:
            source.close()
        except BaseException:
            abort_failures.append('native source watchdog cleanup failed')

    watchdog = Timer(150, abort)
    watchdog.daemon = True
    try:
        expected, expected_bytes = sha256(), 0
        for chunk in reference.stream(query, fmt='RowBinary'):
            expected.update(chunk)
            expected_bytes += len(chunk)
        watchdog.start()
        chunks = source.stream(query, 'RowBinary')
        first = next(chunks)
        actual, actual_bytes = sha256(first), len(first)
        sleep(35)
        for chunk in chunks:
            actual.update(chunk)
            actual_bytes += len(chunk)
        assert (expected_bytes, actual_bytes, actual.hexdigest()) == (64 << 20, 64 << 20, expected.hexdigest())
        assert (source.child.returncode, source.closed, source.stderr_failed, aborted, abort_failures) == (0, True, False, [], [])
    finally:
        watchdog.cancel()
        try:
            source.close()
        finally:
            try:
                _cancel(ch, ids)
            finally:
                reference.close()
                ch.close()
