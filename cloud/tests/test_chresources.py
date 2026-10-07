"""HTTP benchmark RSS samples cover the workload, not a post-run snapshot."""

import json
from pathlib import Path
from threading import Event, current_thread
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from dt_cloud.chstore import resources
from dt_cloud.chstore import bench
from dt_cloud.cli import err, main


def process(
    root: Path,
    pid: int,
    rss_kib: int,
    *,
    ticks: int = 10,
) -> None:
    directory = root / str(pid)
    directory.mkdir(exist_ok=True)
    (directory / "stat").write_text(f"{pid} (server ) name) S" + " 0" * 18 + f" {ticks}\n")
    (directory / "comm").write_text("server\n")
    (directory / "status").write_text(f"Name:\tserver\nVmRSS:\t{rss_kib} kB\n")


def test_rss_samples_cover_context_and_simultaneous_peak(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    process(root, 1, 10)
    process(root, 2, 100)
    output = tmp_path / "rss.jsonl"
    times = iter([0.0, .25, .5, .75])
    calls = []
    monkeypatch.setattr(resources, "time", SimpleNamespace(monotonic=lambda: next(times)))
    monkeypatch.setattr(resources, "Thread", lambda **kw: SimpleNamespace(start=lambda: calls.append("start"), join=lambda: calls.append("join")))
    with resources.RssMonitor((1, 2), output, proc_root=root, interval=.5) as monitor:
        process(root, 1, 100)
        process(root, 2, 10)
        monitor.observe()
    assert calls == ["start", "join"]
    assert [json.loads(line) for line in output.read_text().splitlines()] == [
        {"type": "start", "interval_s": .5, "processes": [{"pid": 1, "name": "server", "started_ticks": 10}, {"pid": 2, "name": "server", "started_ticks": 10}]},
        {"type": "sample", "elapsed_ms": 250, "rss_bytes": {"1": 10240, "2": 102400}},
        {"type": "sample", "elapsed_ms": 500, "rss_bytes": {"1": 102400, "2": 10240}},
        {"type": "sample", "elapsed_ms": 750, "rss_bytes": {"1": 102400, "2": 10240}},
    ]
    assert monitor.summary() == {
        "samples": 3, "interval_s": .5, "sampled_peak_total_rss_bytes": 112640,
        "processes": [{"pid": 1, "name": "server", "sampled_peak_rss_bytes": 102400},
                      {"pid": 2, "name": "server", "sampled_peak_rss_bytes": 102400}],
    }


@pytest.mark.parametrize("pids,interval,error", [
    ((), .5, "RSS PIDs must be positive and distinct"),
    ((0,), .5, "RSS PIDs must be positive and distinct"),
    ((1, 1), .5, "RSS PIDs must be positive and distinct"),
    ((1,), 0, "RSS sampling interval must be finite and positive"),
    ((1,), float("nan"), "RSS sampling interval must be finite and positive"),
])
def test_invalid_rss_monitor_configuration(tmp_path, pids, interval, error):
    with pytest.raises(ValueError) as caught:
        resources.RssMonitor(pids, tmp_path / "ignored", interval=interval)
    assert str(caught.value) == error


def test_rss_monitor_detects_pid_reuse_and_preserves_record(tmp_path, monkeypatch):
    process(tmp_path, 1, 10)
    output = tmp_path / "rss.jsonl"
    monkeypatch.setattr(resources, "Thread", lambda **kw: SimpleNamespace(start=lambda: None, join=lambda: None))
    with pytest.raises(RuntimeError) as caught:
        with resources.RssMonitor((1,), output, proc_root=tmp_path):
            process(tmp_path, 1, 20, ticks=11)
    assert str(caught.value) == "monitored process identity changed: PID 1"
    assert [json.loads(line)["type"] for line in output.read_text().splitlines()] == ["start", "sample"]


def test_rss_monitor_refuses_existing_record(tmp_path):
    process(tmp_path, 1, 10)
    output = tmp_path / "rss.jsonl"
    output.write_text("existing\n")
    with pytest.raises(FileExistsError):
        with resources.RssMonitor((1,), output, proc_root=tmp_path):
            raise AssertionError("existing sample records must not be overwritten")
    assert output.read_text() == "existing\n"


def test_rss_background_sample_and_join_cover_workload(tmp_path, monkeypatch):
    process(tmp_path, 1, 10)
    observed = Event()
    observer = resources.RssMonitor((1,), tmp_path / "rss.jsonl", proc_root=tmp_path)
    original = observer.observe

    class StepEvent:
        def __init__(self):
            self.calls = 0
            self.stop = Event()

        def wait(self, interval):
            self.calls += 1
            return False if self.calls == 1 else self.stop.wait()

        def set(self):
            self.stop.set()

    def observe():
        original()
        if current_thread().name == "benchmark-rss":
            observed.set()

    observer._stop = StepEvent()
    monkeypatch.setattr(observer, "observe", observe)
    with observer:
        assert observed.wait(timeout=2) is True
    assert observer._thread.is_alive() is False
    assert observer.summary() == {
        "samples": 3, "interval_s": .5, "sampled_peak_total_rss_bytes": 10240,
        "processes": [{"pid": 1, "name": "server", "sampled_peak_rss_bytes": 10240}],
    }


def test_background_monitor_failure_is_raised_after_join(tmp_path, monkeypatch):
    process(tmp_path, 1, 10)
    failed = Event()
    read = resources.read_process

    def read_process(root, pid):
        if current_thread().name == "benchmark-rss":
            failed.set()
            raise RuntimeError("sample failed")
        return read(root, pid)

    monkeypatch.setattr(resources, "read_process", read_process)
    with pytest.raises(RuntimeError) as caught:
        with resources.RssMonitor((1,), tmp_path / "rss.jsonl", proc_root=tmp_path, interval=.01) as observer:
            assert failed.wait(timeout=2) is True
    assert str(caught.value) == "sample failed"
    assert observer._thread.is_alive() is False
    assert observer.summary() == {
        "samples": 1, "interval_s": .01, "sampled_peak_total_rss_bytes": 10240,
        "processes": [{"pid": 1, "name": "server", "sampled_peak_rss_bytes": 10240}],
    }


@pytest.mark.parametrize("arguments", [["-m", "1"], ["-M", "samples.jsonl"]])
def test_rss_cli_requires_pids_and_output_together(arguments):
    result = CliRunner().invoke(main, ["ch-bench", *arguments, "a=/a"])
    assert result.exit_code == 2
    assert result.output.splitlines() == [
        "Usage: main ch-bench [OPTIONS] [REQUESTS]...",
        "Try 'main ch-bench --help' for help.",
        "",
        "Error: --rss-pid and --rss-out are required together",
    ]


def test_rss_cli_wraps_requests_and_reports_sampled_peak(monkeypatch):
    calls = []
    report = {"samples": 2, "sampled_peak_total_rss_bytes": 100}

    class Monitor:
        def __enter__(self):
            calls.append("start")
            return self

        def __exit__(self, *args):
            calls.append("stop")

        def summary(self):
            return report

    def monitor(pids, output, **kwargs):
        calls.append((pids, output, kwargs))
        return Monitor()

    def run(base, requests, **kwargs):
        calls.append((base, requests, kwargs))
        return [bench.Rec("a", 0, "/a", 200, 123, None, None, None, 2, "sha", False)]

    monkeypatch.setenv("QUERY_BOX_TOKEN", "test-token")
    monkeypatch.setattr(resources, "RssMonitor", monitor)
    monkeypatch.setattr(bench, "run", run)
    result = CliRunner().invoke(main, ["ch-bench", "-m", "1", "-m", "2", "-M", "samples.jsonl", "-r", "proc", "a=/a"])
    assert result.exit_code == 0, result.output
    assert calls == [
        ((1, 2), Path("samples.jsonl"), {"proc_root": Path("proc")}), "start",
        ("http://localhost:8080", [("a", "/a")], {
            "token": "test-token", "trials": 1, "seed": None, "cold": False, "ch_url": None,
            "timeout": 300.0, "log": err, "record_path": None, "parallel": 1,
        }), "stop",
    ]
    assert [json.loads(line) for line in result.output.splitlines()] == [
        {"name": "a", "n": 1, "ok": 1, "p50": 123, "max": 123, "statuses": [200], "bytes": 2, "engines": [""]},
        {"resource_samples": report},
    ]
