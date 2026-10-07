"""Serial census coordination never overlaps jobs or signals unrelated containers."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "ch-store/hot-frequency-sweep.sh"
FIRST = "1" * 64
OWNED = ("2" * 64, "3" * 64)
STATE_FORMAT = "{{.State.Running}} {{.State.ExitCode}}"


@pytest.fixture
def fixture(tmp_path: Path) -> SimpleNamespace:
    data = tmp_path / "data"
    data.mkdir()
    (data / "image").write_text("fixture-image\n")
    config, events = tmp_path / "docker.json", tmp_path / "events.jsonl"
    config.write_text(json.dumps({"first_running": 1, "first_exit": 0, "jobs": {}, "counter": 0, "existing": []}))
    docker = tmp_path / "docker"
    docker.write_text(f"#!{sys.executable}\n" + r'''
import fcntl, json, os, sys
from pathlib import Path
args = sys.argv[1:]
config, events = Path(os.environ["SWEEP_FAKE_CONFIG"]), Path(os.environ["SWEEP_FAKE_EVENTS"])
lock = config.with_suffix(".lock").open("a")
fcntl.flock(lock, fcntl.LOCK_EX)
state = json.loads(config.read_text())
with events.open("a") as output:
    output.write(json.dumps(args) + "\n")
if args[0] == "inspect":
    name = args[-1]
    if args[2] == "{{.Id}}":
        if name == "ch-job-first": print("1" * 64)
        elif name in state["existing"]: print("e" * 64)
        else: sys.exit(1)
    elif name == "1" * 64:
        remaining = state["first_running"]
        state["first_running"] = max(0, remaining - 1)
        print("true 0" if remaining else f"false {state['first_exit']}")
    else:
        job = state["jobs"][name]
        remaining = job["running"]
        job["running"] = max(0, remaining - 1)
        print("true 0" if remaining else f"false {job['exit']}")
elif args[0] == "run":
    state["counter"] += 1
    ident = str(state["counter"] + 1) * 64
    cli = args[args.index("ch-hot-frequency-census") + 1:]
    date = cli[cli.index("-d") + 1]
    exit_code = 7 if state.get("fail_date") == date else 0
    state["jobs"][ident] = {"exit": exit_code, "running": state.get("owned_running", 0)}
    if exit_code == 0 and not state.get("missing_artifacts"):
        Path(args[args.index("-o") + 1]).write_text("fixture census\n")
        Path(args[args.index("-q") + 1]).write_text("fixture complete export\n")
    print(ident)
elif args[0] == "kill":
    job = state["jobs"][args[-1]]
    job["running"], job["exit"] = 0, 130
else:
    sys.exit(3)
config.write_text(json.dumps(state))
''')
    docker.chmod(0o755)
    return SimpleNamespace(data=data, config=config, events=events, env={**os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}", "SWEEP_FAKE_CONFIG": str(config), "SWEEP_FAKE_EVENTS": str(events)})


def configure(fixture: SimpleNamespace, **kwargs: object) -> None:
    fixture.config.write_text(json.dumps({**json.loads(fixture.config.read_text()), **kwargs}))


def run(fixture: SimpleNamespace, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPT), "--first-tag", "first", "--run-prefix", "sweep", "--target", "frozen_fixture",
                           "--data-root", str(fixture.data), "--poll-seconds", "0.02", *args],
                          env=fixture.env, text=True, capture_output=True, timeout=8)


def events(fixture: SimpleNamespace) -> list[list[str]]:
    return [json.loads(line) for line in fixture.events.read_text().splitlines()] if fixture.events.exists() else []


def preflight() -> list[list[str]]:
    return [["inspect", "--format", "{{.Id}}", name] for name in (
        "ch-job-first", "ch-job-sweep-2026-10-05", "ch-job-sweep-2026-10-04",
    )]


def launch(fixture: SimpleNamespace, date: str) -> list[str]:
    root = str(fixture.data)
    directory = f"{root}/hot-frequency-sweeps/sweep"
    return ["run", "-d", "--name", f"ch-job-sweep-{date}", "--log-opt", "max-size=10m", "--log-opt", "max-file=2",
            "--network", "host", "-v", f"{root}:{root}", "-e", f"PYTHONPATH={root}/src", "--entrypoint", "python3",
            "fixture-image", "-u", "-m", "dt_cloud.cli", "ch-hot-frequency-census", "frozen_fixture",
            "-d", date, "-t", "100000", "-h", "300000", "-h", "1000000", "-k", "16", "-c", "500000",
            "-m", "8", "-s", "8", "-w", "600", "-o", f"{directory}/{date}-t100k-k16.json",
            "-q", f"{directory}/{date}-t100k-k16.queries.jsonl"]


def test_success_waits_for_first_then_runs_two_retained_jobs_serially(fixture: SimpleNamespace) -> None:
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (0, "", [
        "wait-first ch-job-first", "first census succeeded", "start ch-job-sweep-2026-10-05",
        "complete ch-job-sweep-2026-10-05", "start ch-job-sweep-2026-10-04", "complete ch-job-sweep-2026-10-04", "sweep complete",
    ])
    assert events(fixture) == [*preflight(),
        ["inspect", "--format", STATE_FORMAT, FIRST], ["inspect", "--format", STATE_FORMAT, FIRST],
        launch(fixture, "2026-10-05"), ["inspect", "--format", STATE_FORMAT, OWNED[0]],
        launch(fixture, "2026-10-04"), ["inspect", "--format", STATE_FORMAT, OWNED[1]],
    ]
    directory = fixture.data / "hot-frequency-sweeps/sweep"
    assert sorted(path.name for path in directory.iterdir()) == [
        "2026-10-04-t100k-k16.json", "2026-10-04-t100k-k16.queries.jsonl",
        "2026-10-05-t100k-k16.json", "2026-10-05-t100k-k16.queries.jsonl", "active-container",
    ]
    assert (directory / "active-container").read_bytes() == b""


def test_first_failure_stops_without_launch_or_signal(fixture: SimpleNamespace) -> None:
    configure(fixture, first_running=0, first_exit=9)
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (1, "", [
        "wait-first ch-job-first", f"census failed: {FIRST} (false 9)",
    ])
    assert events(fixture) == [*preflight(), ["inspect", "--format", STATE_FORMAT, FIRST]]


def test_owned_failure_stops_before_second_scan_and_retains_container(fixture: SimpleNamespace) -> None:
    configure(fixture, first_running=0, fail_date="2026-10-05")
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (1, "", [
        "wait-first ch-job-first", "first census succeeded", "start ch-job-sweep-2026-10-05",
        f"census failed: {OWNED[0]} (false 7)",
    ])
    assert events(fixture) == [*preflight(), ["inspect", "--format", STATE_FORMAT, FIRST],
                               launch(fixture, "2026-10-05"), ["inspect", "--format", STATE_FORMAT, OWNED[0]]]


def test_explicit_date_selection_launches_only_that_scan(fixture: SimpleNamespace) -> None:
    configure(fixture, first_running=0)
    result = run(fixture, "--date", "2026-10-04")
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (0, "", [
        "wait-first ch-job-first", "first census succeeded", "start ch-job-sweep-2026-10-04",
        "complete ch-job-sweep-2026-10-04", "sweep complete",
    ])
    assert events(fixture) == [preflight()[0], preflight()[2], ["inspect", "--format", STATE_FORMAT, FIRST],
                               launch(fixture, "2026-10-04"), ["inspect", "--format", STATE_FORMAT, OWNED[0]]]


def test_exit_zero_without_both_artifacts_refuses_to_launch_next_scan(fixture: SimpleNamespace) -> None:
    configure(fixture, first_running=0, missing_artifacts=True)
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr.splitlines()) == (2, "", [
        "wait-first ch-job-first", "first census succeeded", "start ch-job-sweep-2026-10-05",
        "successful census container did not produce both artifacts",
    ])
    assert events(fixture) == [*preflight(), ["inspect", "--format", STATE_FORMAT, FIRST],
                               launch(fixture, "2026-10-05"), ["inspect", "--format", STATE_FORMAT, OWNED[0]]]


def test_existing_container_refuses_before_artifacts_or_launch(fixture: SimpleNamespace) -> None:
    configure(fixture, existing=["ch-job-sweep-2026-10-05"])
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr) == (2, "", "container already exists: ch-job-sweep-2026-10-05\n")
    assert events(fixture) == preflight()[:2]
    assert sorted(path.name for path in fixture.data.iterdir()) == ["image"]


def test_existing_sweep_directory_preserves_all_bytes_and_never_launches(fixture: SimpleNamespace) -> None:
    directory = fixture.data / "hot-frequency-sweeps/sweep"
    directory.mkdir(parents=True)
    existing = directory / "2026-10-05-t100k-k16.json"
    existing.write_bytes(b"existing accepted artifact\n")
    result = run(fixture)
    assert (result.returncode, result.stdout, result.stderr) == (
        2, "", "cannot create new sweep directory (existing or inaccessible); nothing overwritten\n",
    )
    assert events(fixture) == preflight()
    assert sorted(directory.iterdir()) == [existing]
    assert existing.read_bytes() == b"existing accepted artifact\n"


@pytest.mark.parametrize("phase", ["first", "owned"])
def test_wall_watchdog_interrupts_only_exact_owned_container(fixture: SimpleNamespace, phase: str) -> None:
    configure(fixture, first_running=1000 if phase == "first" else 0, owned_running=1000)
    result = run(fixture, "--wall-seconds", "1", "--grace-seconds", "1")
    assert (result.returncode, result.stdout) == (124, "")
    expected = ["wait-first ch-job-first"]
    if phase == "owned":
        expected.extend(["first census succeeded", "start ch-job-sweep-2026-10-05", f"interrupt-owned {OWNED[0]}"])
    expected.extend(["sweep wall budget expired", "sweep stopped at wall budget; retained jobs/artifacts are not auto-resumed"])
    # Watchdog and main process log order is concurrent; compare all exact lines.
    assert sorted(result.stderr.splitlines()) == sorted(expected)
    recorded = events(fixture)
    polls = [event for event in recorded if event[:3] == ["inspect", "--format", STATE_FORMAT]]
    assert sorted({event[3] for event in polls}) == ([FIRST] if phase == "first" else [FIRST, OWNED[0]])
    assert [event for event in recorded if event[:3] != ["inspect", "--format", STATE_FORMAT]] == [
        *preflight(), *([] if phase == "first" else [launch(fixture, "2026-10-05"), ["kill", "--signal", "SIGINT", OWNED[0]]]),
    ]


@pytest.mark.parametrize("arguments,error", [
    (["--first-tag", "../bad"], "invalid --first-tag (lowercase safe tag, 1..63 characters)"),
    (["--run-prefix", "bad;command"], "invalid --run-prefix (lowercase safe tag, 1..40 characters)"),
    (["--target", "not.safe"], "invalid --target (database identifier)"),
    (["--data-root", "/"], "--data-root must be an existing absolute non-root directory"),
    (["--wall-seconds", "0"], "wall/grace seconds must be positive integers"),
    (["--poll-seconds", "0.0"], "poll seconds must be positive"),
    (["--date", "bad"], "invalid --date (YYYY-MM-DD required)"),
    (["--date", "2026-10-04", "--date", "2026-10-04"], "duplicate --date"),
])
def test_unsafe_arguments_refuse_before_docker_or_artifact_creation(fixture: SimpleNamespace, arguments: list[str], error: str) -> None:
    result = run(fixture, *arguments)
    assert (result.returncode, result.stdout, result.stderr) == (2, "", error + "\n")
    assert events(fixture) == []
    assert sorted(path.name for path in fixture.data.iterdir()) == ["image"]
