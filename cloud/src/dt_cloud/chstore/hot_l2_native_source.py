"""Explicit loopback native source transport for the offline paired experiment.

Uses the operator's immutable multicall ClickHouse binary, never a shell,
ambient credentials/configuration, global server settings or snapshot spool.
The enclosing paired runner owns the wall deadline and exact query cancellation.
"""

from os import X_OK, access
from pathlib import Path
from subprocess import DEVNULL, PIPE, Popen, TimeoutExpired
from threading import Lock, Thread
from typing import Iterator

from .client import Ch

CONFIG = Path(__file__).with_suffix('.xml')
RESOURCE_SETTINGS = (
    'max_threads', 'max_memory_usage', 'max_execution_time', 'timeout_before_checking_execution_speed', 'timeout_overflow_mode',
    'max_bytes_before_external_sort', 'max_bytes_ratio_before_external_sort', 'max_bytes_before_external_group_by',
    'max_bytes_ratio_before_external_group_by', 'max_temporary_data_on_disk_size_for_query', 'log_comment',
)


def validate_endpoint(url: str, binary: Path, wall_seconds: int) -> None:
    if url not in ('http://localhost:8123', 'http://127.0.0.1:8123'):
        raise ValueError('native paired source requires default unauthenticated loopback ClickHouse')
    if not binary.is_file() or not access(binary, X_OK):
        raise ValueError('native paired source requires an explicit executable multicall ClickHouse binary')
    if type(wall_seconds) is not int or not 1 <= wall_seconds <= 4500:
        raise ValueError('native paired source requires the bounded experiment wall deadline')
    if CONFIG.read_bytes() != b'<clickhouse/>\n':
        raise ValueError('native paired source empty client configuration changed')


def validate(ch: Ch, binary: Path, wall_seconds: int) -> None:
    validate_endpoint(ch.url, binary, wall_seconds)
    if ch.headers:
        raise ValueError('native paired source requires default unauthenticated loopback ClickHouse')


class NativeSource:
    def __init__(
        self,
        ch: Ch,
        binary: Path,
        query_id: str,
        wall_seconds: int,
    ) -> None:
        validate(ch, binary, wall_seconds)
        self.binary, self.query_id, self.idle_seconds = binary.resolve(), query_id, wall_seconds + 60
        self.db = ch.db
        self.settings = {key: ch.settings[key] for key in RESOURCE_SETTINGS if key in ch.settings}
        unknown = set(ch.settings) - set(RESOURCE_SETTINGS) - {'database', 'session_id', 'session_timeout'}
        if unknown:
            raise ValueError('native paired source cannot silently discard unknown query settings')
        self.child = None
        self.stderr_thread = None
        self.stderr_tail = bytearray()
        self.stderr_failed = False
        self.drain_termination_failed = False
        self.closed = False
        self.close_lock = Lock()

    def stream(self, sql: str, fmt: str) -> Iterator[bytes]:
        if self.child is not None or fmt != 'RowBinary':
            raise ValueError('native paired source is one owned RowBinary query only')
        argv = [str(self.binary), 'client', f'--config-file={CONFIG.resolve()}', '--host=127.0.0.1', '--port=9000',
                '--user=default', '--password=', f'--database={self.db}', f'--query_id={self.query_id}',
                '--progress=off', '--send_logs_level=none', f'--receive_timeout={self.idle_seconds}', f'--send_timeout={self.idle_seconds}',
                *[f'--{key}={value}' for key, value in self.settings.items()], '--query', sql.strip().rstrip(';') + ' FORMAT RowBinary']
        def drain() -> None:
            try:
                while chunk := self.child.stderr.read(4096):
                    self.stderr_tail.extend(chunk)
                    del self.stderr_tail[:-65536]
            except BaseException:
                # No raw diagnostic/exception crosses the worker boundary.
                # Stop this exact child so stdout cannot wait behind undrained stderr.
                self.stderr_failed = True
                try:
                    if self.child.poll() is None:
                        self.child.terminate()
                except BaseException:
                    self.drain_termination_failed = True

        try:
            try:
                self.child = Popen(argv, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, bufsize=0, env={'LC_ALL': 'C.UTF-8'})
            except OSError:
                raise RuntimeError('native paired source process could not start') from None
            self.stderr_thread = Thread(target=drain, name='hot-l2-native-source-stderr', daemon=True)
            self.stderr_thread.start()
            while True:
                try:
                    chunk = self.child.stdout.read(1 << 20)
                except OSError:
                    raise RuntimeError('native paired source output read failed') from None
                if not chunk:
                    break
                yield chunk
            try:
                status = self.child.wait(timeout=5)
            except TimeoutExpired:
                raise RuntimeError('native paired source did not exit after complete output') from None
            if status != 0:
                raise RuntimeError(f'native paired source exited with status {status}; diagnostics withheld')
        except BaseException as original:
            try:
                self.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup('native paired source failed and cleanup was incomplete', [original, cleanup]) from None
            raise
        else:
            self.close()

    def close(self) -> None:
        with self.close_lock:
            if self.child is None or self.closed:
                return
            failures = []

            def failed(message: str) -> None:
                failures.append(RuntimeError(message))

            try:
                running = self.child.poll() is None
            except BaseException:
                running = True
                failed('native paired source process status failed during cleanup')
            if running:
                try:
                    self.child.terminate()
                except BaseException:
                    failed('native paired source termination failed')
                try:
                    self.child.wait(timeout=5)
                except BaseException:
                    try:
                        self.child.kill()
                    except BaseException:
                        failed('native paired source kill failed')
                    try:
                        self.child.wait(timeout=5)
                    except BaseException:
                        failed('native paired source did not stop within bounded cleanup')
            if self.stderr_thread is not None:
                try:
                    self.stderr_thread.join(timeout=2)
                    if self.stderr_thread.is_alive():
                        failed('native paired source diagnostic reader did not stop')
                except BaseException:
                    failed('native paired source diagnostic reader join failed')
            # Always attempt both pipe closes, even if kill/join/another close failed.
            for handle in (self.child.stdout, self.child.stderr):
                try:
                    if handle is not None and not handle.closed:
                        handle.close()
                except BaseException:
                    failed('native paired source pipe close failed')
            if self.stderr_failed:
                failed('native paired source diagnostic reader failed; diagnostics withheld')
            if self.drain_termination_failed:
                failed('native paired source diagnostic-failure termination failed')
            self.closed = True
            if len(failures) == 1:
                raise failures[0] from None
            if failures:
                raise ExceptionGroup('native paired source cleanup was incomplete', failures) from None
