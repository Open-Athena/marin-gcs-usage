"""Opt-in same-host RSS observation during an HTTP benchmark.

Samples are process RSS, not query allocations or a guaranteed true peak.
Linux's asynchronous RSS accounting is itself approximate, and the simultaneous
sum can double-count shared pages. Read only stat/comm/status,
never command lines or environments, and detect exit/PID reuse rather than
silently following a different process. Use /hostproc in the dev VM wrapper.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from threading import Event, Thread
from types import TracebackType
from typing import TextIO


@dataclass(frozen=True)
class DiskSample:
    """Whole-device counters, including unrelated activity; not process I/O."""

    device: str
    major: int
    minor: int
    at: float
    counters: tuple[int, ...]


def read_disk(device: str, root: Path = Path("/hostproc")) -> DiskSample:
    rows = [line.split() for line in (root / "diskstats").read_text().splitlines()]
    matches = [row for row in rows if len(row) >= 3 and row[2] == device]
    if len(matches) != 1 or len(matches[0]) < 14:
        raise ValueError(f"expected one complete diskstats entry: {device}")
    row = matches[0]
    counters = tuple(int(value) for value in row[3:14])
    if min(counters) < 0:
        raise ValueError(f"negative diskstats counter: {device}")
    return DiskSample(device, int(row[0]), int(row[1]), time.monotonic(), counters)


def disk_delta(before: DiskSample, after: DiskSample) -> dict:
    """Kernel sectors are 512 bytes; reject reset/wrapped counters explicitly.

    See docs.kernel.org/block/stat.html and admin-guide/iostats.html. Busy time
    can undercount concurrent I/O on modern kernels: it is not a saturation test.
    """
    if (before.device, before.major, before.minor) != (after.device, after.major, after.minor):
        raise ValueError("monitored disk identity changed")
    seconds = after.at - before.at
    if seconds <= 0:
        raise ValueError("disk observation interval must be positive")
    delta = tuple(end - start for start, end in zip(before.counters, after.counters, strict=True))
    if any(value < 0 for index, value in enumerate(delta) if index != 8):
        raise ValueError("monitored disk counters reset or wrapped")
    return {
        "device": before.device, "major": before.major, "minor": before.minor,
        "scope": "whole-device", "elapsed_s": round(seconds, 6),
        "reads": delta[0], "read_merges": delta[1], "read_bytes": delta[2] * 512, "read_ms": delta[3],
        "writes": delta[4], "write_merges": delta[5], "write_bytes": delta[6] * 512, "write_ms": delta[7],
        "busy_ms": delta[9], "weighted_io_ms": delta[10],
        "in_flight_before": before.counters[8], "in_flight_after": after.counters[8],
        "read_mib_per_s": round(delta[2] / 2048 / seconds, 3),
    }


@dataclass(frozen=True)
class Process:
    pid: int
    name: str
    started_ticks: int
    rss_bytes: int


def started_ticks(path: Path, pid: int) -> int:
    head, separator, tail = path.read_text().rpartition(") ")
    fields = tail.split()
    if not separator or not head.startswith(f"{pid} (") or len(fields) < 20:
        raise ValueError(f"invalid process stat: PID {pid}")
    return int(fields[19])  # starttime is field 22; tail begins at state (3).


def read_process(root: Path, pid: int) -> Process:
    directory = root / str(pid)
    before = started_ticks(directory / "stat", pid)
    name = (directory / "comm").read_text().strip()
    rss = [line.split() for line in (directory / "status").read_text().splitlines() if line.startswith("VmRSS:")]
    if len(rss) != 1 or len(rss[0]) != 3 or rss[0][2] != "kB":
        raise ValueError(f"invalid VmRSS: PID {pid}")
    if started_ticks(directory / "stat", pid) != before:
        raise RuntimeError(f"monitored process identity changed: PID {pid}")
    return Process(pid, name, before, int(rss[0][1]) * 1024)


class RssMonitor:
    def __init__(
        self,
        pids: tuple[int, ...],
        output: Path,
        *,
        proc_root: Path = Path("/hostproc"),
        interval: float = .5,
    ) -> None:
        if not pids or len(set(pids)) != len(pids) or any(pid <= 0 for pid in pids):
            raise ValueError("RSS PIDs must be positive and distinct")
        if not isfinite(interval) or interval <= 0:
            raise ValueError("RSS sampling interval must be finite and positive")
        self.pids, self.output, self.root, self.interval = pids, output, proc_root, interval
        self._stop = Event()
        self._error: Exception | None = None
        self._file: TextIO | None = None
        self._initial: dict[int, Process] = {}
        self._peaks = dict.fromkeys(pids, 0)
        self._total_peak = self._samples = 0

    def __enter__(self) -> RssMonitor:
        self._initial = {pid: read_process(self.root, pid) for pid in self.pids}
        self._file = self.output.open("x")
        self._start = time.monotonic()
        try:
            self._write({"type": "start", "interval_s": self.interval, "processes": [
                {"pid": p.pid, "name": p.name, "started_ticks": p.started_ticks} for p in self._initial.values()
            ]})
            self.observe()
            self._thread = Thread(target=self._watch, name="benchmark-rss", daemon=True)
            self._thread.start()
        except BaseException:
            self._file.close()
            raise
        return self

    def _write(self, row: dict) -> None:
        if self._file is None:
            raise RuntimeError("RSS monitor has not started")
        self._file.write(json.dumps(row) + "\n")
        self._file.flush()

    def observe(self) -> None:
        values = {}
        for pid in self.pids:
            process = read_process(self.root, pid)
            initial = self._initial[pid]
            if (process.started_ticks, process.name) != (initial.started_ticks, initial.name):
                raise RuntimeError(f"monitored process identity changed: PID {pid}")
            values[pid] = process.rss_bytes
            self._peaks[pid] = max(self._peaks[pid], process.rss_bytes)
        self._total_peak = max(self._total_peak, sum(values.values()))
        self._samples += 1
        self._write({"type": "sample", "elapsed_ms": round((time.monotonic() - self._start) * 1000), "rss_bytes": values})

    def _watch(self) -> None:
        try:
            while not self._stop.wait(self.interval):
                self.observe()
        except Exception as error:
            # A background failure must reach the caller after joining.
            self._error = error
            self._stop.set()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()
        try:
            if self._error is not None:
                raise self._error
            self.observe()
        finally:
            self._file.close()

    def summary(self) -> dict:
        return {"samples": self._samples, "interval_s": self.interval, "sampled_peak_total_rss_bytes": self._total_peak,
                "processes": [{"pid": pid, "name": self._initial[pid].name, "sampled_peak_rss_bytes": self._peaks[pid]} for pid in self.pids]}
