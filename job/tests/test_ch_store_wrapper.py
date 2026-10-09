"""The dev-node wrapper preserves CLI argv and stages only script files."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def remote_tools(tmp_path: Path) -> dict[str, str]:
    for name, source in {
        "gcloud": "#!/bin/sh\nprintf '%s\\n' '127.0.0.1'\n",
        "ssh": f"#!{sys.executable}\nimport subprocess, sys\nsubprocess.run(['/bin/bash', '-c', sys.argv[-1]], check=True)\n",
        "sudo": '#!/bin/sh\nexec "$@"\n',
        "cat": "#!/bin/sh\ncase \"$1\" in /data/token) echo fixture-token;; /data/image) echo fixture-image;; *) exit 1;; esac\n",
        "docker": f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n",
    }.items():
        script = tmp_path / name
        script.write_text(source)
        script.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "USER": "wrapper-test", "NAME_SUMMARY": "0",
            "DATED_L1_GENERATION": "", "DATED_NAME_STORE": ""}


@pytest.mark.parametrize("arguments", [
    ["ch-narrow-build", "-p", "", "narrow_test"],
    ["transport-probe", "-q", "hello 'world' $UNCHANGED; \\tail"],
    ["transport-probe", "-q", "first\nsecond\t漢字 'quoted'"],
])
def test_dev_node_python_preserves_cli_arguments(remote_tools, arguments):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    result = subprocess.run(
        [str(wrapper), "py", *arguments],
        env=remote_tools,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == [
        "run", "--rm", "--privileged", "--network", "host",
        "-v", "/data:/data", "-v", "/proc:/hostproc",
        "-e", "PYTHONPATH=/data/src", "-e", "QUERY_BOX_TOKEN=fixture-token",
        "--entrypoint", "python3", "fixture-image", "-u", "-m", "dt_cloud.cli", *arguments,
    ]


def test_detached_python_preserves_arguments_and_retains_job(remote_tools):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    arguments = ["ch-narrow-build", "-p", "", "narrow_test"]
    result = subprocess.run(
        [str(wrapper), "py-bg", "fleet-oct05", *arguments],
        env=remote_tools,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout) == [
        "run", "-d", "--name", "ch-job-fleet-oct05",
        "--log-opt", "max-size=10m", "--log-opt", "max-file=2",
        "--privileged", "--network", "host",
        "-v", "/data:/data", "-v", "/proc:/hostproc",
        "-e", "PYTHONPATH=/data/src", "-e", "QUERY_BOX_TOKEN=fixture-token",
        "--entrypoint", "python3", "fixture-image", "-u", "-m", "dt_cloud.cli", *arguments,
    ]


def test_native_runner_preserves_all_explicit_binary_paths(remote_tools):
    runner = Path(__file__).resolve().parents[1] / "ch-store/tests.sh"
    left, paired, source = "/data/l1-stream", "/data/quoted '$UNCHANGED; 漢字/l2-stream", "/data/ch-client"
    arguments = ["cloud/tests/test_chhot_l2_native_stream.py", "-q"]
    result = subprocess.run(
        ["bash", str(runner), *arguments],
        env={**remote_tools, "HL1_NATIVE_BINARY": left, "HL2_NATIVE_BINARY": paired, "HL2_NATIVE_SOURCE_BINARY": source},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout) == [
        "run", "--rm", "--network", "host", "-v", "/data:/data",
        "-e", "PYTHONPATH=/data/src", "-e", "CLICKHOUSE_URL=http://localhost:8123",
        "-e", f"HL1_NATIVE_BINARY={left}", "-e", f"HL2_NATIVE_BINARY={paired}",
        "-e", f"HL2_NATIVE_SOURCE_BINARY={source}",
        "--entrypoint", "bash", "fixture-image", "-c",
        '\n    set -euo pipefail\n    uv pip install --system pytest\n    cd /data/test-checkout\n    exec python3 -m pytest "$@"\n  ',
        "pytest", *arguments,
    ]


def test_fixture_wrapper_forwards_native_paths_to_remote_runner(remote_tools):
    fake_bin = Path(remote_tools["PATH"].split(os.pathsep)[0])
    ssh = fake_bin / "ssh"
    ssh.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[-1]))\n")
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    result = subprocess.run(
        [str(wrapper), "test", "cloud/tests/test_chhot_l2_native_stream.py", "-q"],
        env={**remote_tools, "HL1_NATIVE_BINARY": "/data/l1-stream", "HL2_NATIVE_BINARY": "/data/l2-stream", "HL2_NATIVE_SOURCE_BINARY": "/data/ch-client"},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stderr) == (0, "127.0.0.1\n" * 3)
    base = "gs://oa-gcs-usage-dvx/scratch/bench/ch-store"
    assert json.loads(result.stdout) == (
        f"sudo mkdir -p /data/test-checkout && sudo gcloud storage rsync -r '{base}/test-checkout' /data/test-checkout > /dev/null 2>&1 && "
        f"    sudo gcloud storage cp '{base}/scripts/tests.sh' /data/tests.sh > /dev/null 2>&1 && sudo env "
        "HL1_NATIVE_BINARY=/data/l1-stream HL2_NATIVE_BINARY=/data/l2-stream HL2_NATIVE_SOURCE_BINARY=/data/ch-client  bash /data/tests.sh cloud/tests/test_chhot_l2_native_stream.py -q "
    )


@pytest.mark.parametrize("command,expected", [
    ("py-status", ["inspect", "--format", "{{json .State}}", "ch-job-fleet-oct05"]),
    ("py-logs", ["logs", "ch-job-fleet-oct05"]),
])
def test_detached_job_observation(remote_tools, command, expected):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    result = subprocess.run(
        [str(wrapper), command, "fleet-oct05"],
        env=remote_tools,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("command", ["py-bg", "py-status", "py-logs"])
@pytest.mark.parametrize("tag", ["", "../bad", "bad;touch sentinel", "UPPER", "x" * 64])
def test_invalid_detached_job_tag_is_rejected(remote_tools, command, tag):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    result = subprocess.run(
        [str(wrapper), command, tag, "ignored"],
        env=remote_tools,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        2, "", "job tag must be 1..63 lowercase letters, digits, underscores or hyphens, starting with a letter or digit\n",
    )


def test_detached_job_requires_a_cli_command(remote_tools):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    result = subprocess.run(
        [str(wrapper), "py-bg", "fleet-oct05"],
        env=remote_tools,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stdout, result.stderr) == (2, "", "py-bg requires a CLI command\n")


def test_unknown_wrapper_command_is_not_silent_success(remote_tools):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    result = subprocess.run(
        [str(wrapper), "pybg", "fleet-oct05"],
        env=remote_tools,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stdout, result.stderr) == (2, "", "unknown ch-store command: pybg\n")


@pytest.mark.parametrize("variant", ["", "g64"])
@pytest.mark.parametrize("catalog", ["", "/data/hot-l1-catalog", "/data/quoted '$UNCHANGED;\n漢字"])
@pytest.mark.parametrize("rich,parent,root_plan", [
    (False, False, "rich"), (True, False, "rich"), (False, True, "rich"), (True, True, "rich"),
    (True, True, "rich 'quoted' $UNCHANGED;\n漢字"),
])
def test_serve_preserves_index_flags_and_cli_arguments(remote_tools, rich, parent, root_plan, variant, catalog):
    rich = rich or bool(variant)
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    result = subprocess.run(
        [str(wrapper), "serve"],
        env={**remote_tools, "NARROW_TARGET": "narrow_test", "NARROW_RICH_NAME_INDEX": str(int(rich)),
             "NARROW_DIRECTORY_PARENT_INDEX": str(int(parent)), "NARROW_RICH_NAME_VARIANT": variant,
             "ROOT_PLAN": root_plan, "THREADS": "4", "CONCURRENCY": "1", "HOT_L1_GENERATION": catalog},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == [
        "run", "-d", "--name", "serve-query", "--restart", "unless-stopped", "--network", "host", "-v", "/data:/data",
        "-e", "PYTHONPATH=/data/src", "-e", "QUERY_BOX_TOKEN=fixture-token",
        "--entrypoint", "python3", "fixture-image", "-u", "-m", "dt_cloud.cli",
        "serve-query", "-e", "ch", "-p", "8080", "-c", "1", "-t", "4", "-r", root_plan,
        *(["-g", catalog] if catalog else []),
        "-N", "narrow_test", *(["-i"] if rich else []), *(["-v", variant] if variant else []),
        *(["-j"] if parent else []), "http://localhost:8123",
    ]


@pytest.mark.parametrize("variant,rich,error", [
    ("g64", "0", "NARROW_RICH_NAME_VARIANT requires NARROW_RICH_NAME_INDEX=1"),
    ("bad;echo x", "1", "NARROW_RICH_NAME_VARIANT must match [a-z][a-z0-9_]*"),
    ("64", "1", "NARROW_RICH_NAME_VARIANT must match [a-z][a-z0-9_]*"),
    ("G64", "1", "NARROW_RICH_NAME_VARIANT must match [a-z][a-z0-9_]*"),
    ("_g64", "1", "NARROW_RICH_NAME_VARIANT must match [a-z][a-z0-9_]*"),
])
def test_name_variant_validation_precedes_remote_access(remote_tools, variant, rich, error):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    result = subprocess.run(
        [str(wrapper), "serve"],
        env={**remote_tools, "NARROW_TARGET": "narrow_test", "NARROW_RICH_NAME_INDEX": rich,
             "NARROW_RICH_NAME_VARIANT": variant, "NARROW_DIRECTORY_PARENT_INDEX": "0"},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stdout, result.stderr) == (2, "", error + "\n")


def test_serve_preserves_both_accepted_l2_paths(remote_tools):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    artifact, check = "/data/quoted '$UNCHANGED;\n漢字/l2.json", "/data/l2-check.json"
    result = subprocess.run(
        [str(wrapper), "serve"],
        env={**remote_tools, "NARROW_TARGET": "", "NARROW_RICH_NAME_INDEX": "0",
             "NARROW_DIRECTORY_PARENT_INDEX": "0", "NARROW_RICH_NAME_VARIANT": "", "NARROW_PLAN": "legacy",
             "ROOT_PLAN": "rich", "THREADS": "4", "CONCURRENCY": "1", "HOT_L1_GENERATION": "/data/l1",
             "HOT_L2_ARTIFACT": artifact, "HOT_L2_CHECK": check},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout) == [
        "run", "-d", "--name", "serve-query", "--restart", "unless-stopped", "--network", "host", "-v", "/data:/data",
        "-e", "PYTHONPATH=/data/src", "-e", "QUERY_BOX_TOKEN=fixture-token",
        "--entrypoint", "python3", "fixture-image", "-u", "-m", "dt_cloud.cli",
        "serve-query", "-e", "ch", "-p", "8080", "-c", "1", "-t", "4", "-r", "rich",
        "-g", "/data/l1", "-H", artifact, "-J", check, "http://localhost:8123",
    ]


@pytest.mark.parametrize("artifact,check", [("/data/l2.json", ""), ("", "/data/check.json")])
def test_partial_l2_selection_refuses_before_remote_access(remote_tools, artifact, check):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    result = subprocess.run(
        [str(wrapper), "serve"],
        env={**remote_tools, "NARROW_TARGET": "", "NARROW_RICH_NAME_INDEX": "0",
             "NARROW_DIRECTORY_PARENT_INDEX": "0", "NARROW_RICH_NAME_VARIANT": "",
             "HOT_L2_ARTIFACT": artifact, "HOT_L2_CHECK": check},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        2, "", "HOT_L2_ARTIFACT and HOT_L2_CHECK are required together\n",
    )


@pytest.mark.parametrize('value,target,generation,error', [
    ('1', '', '/data/l1', 'NAME_SUMMARY requires HOT_L1_GENERATION and NARROW_TARGET'),
    ('1', 'fleet', '', 'NAME_SUMMARY requires HOT_L1_GENERATION and NARROW_TARGET'),
    ('invalid', 'fleet', '/data/l1', 'NAME_SUMMARY must be 0 or 1'),
])
def test_name_summary_selection_refuses_before_remote_access(remote_tools, value, target, generation, error):
    wrapper = Path(__file__).resolve().parents[1] / 'ch-store.sh'
    result = subprocess.run([str(wrapper), 'serve'],
                            env={**remote_tools, 'NAME_SUMMARY': value, 'NARROW_TARGET': target, 'HOT_L1_GENERATION': generation},
                            text=True, capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (2, '', error + '\n')


def test_name_summary_is_an_explicit_preserved_serve_flag(remote_tools):
    wrapper = Path(__file__).resolve().parents[1] / 'ch-store.sh'
    result = subprocess.run([str(wrapper), 'serve'],
                            env={**remote_tools, 'NAME_SUMMARY': '1', 'NARROW_TARGET': 'fleet', 'HOT_L1_GENERATION': '/data/l1',
                                 'HOT_L2_ARTIFACT': '', 'HOT_L2_CHECK': '', 'NARROW_RICH_NAME_INDEX': '0',
                                 'NARROW_DIRECTORY_PARENT_INDEX': '0', 'NARROW_RICH_NAME_VARIANT': '', 'NARROW_PLAN': 'legacy',
                                 'ROOT_PLAN': 'rich', 'THREADS': '4', 'CONCURRENCY': '1'},
                            text=True, capture_output=True, timeout=10)
    assert (result.returncode, result.stderr) == (0, '')
    assert json.loads(result.stdout) == [
        'run', '-d', '--name', 'serve-query', '--restart', 'unless-stopped', '--network', 'host', '-v', '/data:/data',
        '-e', 'PYTHONPATH=/data/src', '-e', 'QUERY_BOX_TOKEN=fixture-token',
        '--entrypoint', 'python3', 'fixture-image', '-u', '-m', 'dt_cloud.cli',
        'serve-query', '-e', 'ch', '-p', '8080', '-c', '1', '-t', '4', '-r', 'rich',
        '-g', '/data/l1', '-L', '-N', 'fleet', 'http://localhost:8123',
    ]


@pytest.mark.parametrize('generation,store,name_summary,error', [
    ('/data/dated', '', '1', 'DATED_L1_GENERATION and DATED_NAME_STORE are required together'),
    ('', 'gcs_fleet', '1', 'DATED_L1_GENERATION and DATED_NAME_STORE are required together'),
    ('/data/dated', 'gcs_fleet', '0', 'DATED_L1_GENERATION and DATED_NAME_STORE require NAME_SUMMARY=1'),
    ('/data/dated', 'bad;touch sentinel', '1', 'DATED_NAME_STORE must match [a-z][a-z0-9_]*'),
    ('/data/dated', 'GCS', '1', 'DATED_NAME_STORE must match [a-z][a-z0-9_]*'),
    ('/data/dated', '1gcs', '1', 'DATED_NAME_STORE must match [a-z][a-z0-9_]*'),
])
def test_dated_name_selection_refuses_before_any_remote_action(remote_tools, tmp_path: Path, generation: str, store: str, name_summary: str, error: str) -> None:
    fake_bin = Path(remote_tools['PATH'].split(os.pathsep)[0])
    calls = tmp_path / 'remote-calls.jsonl'
    for command in ('gcloud', 'ssh'):
        script = fake_bin / command
        script.write_text(f'#!{sys.executable}\nimport json, sys\nwith open({str(calls)!r}, "a") as file:\n    file.write(json.dumps(sys.argv) + "\\n")\nprint("127.0.0.1")\n')
        script.chmod(0o755)
    wrapper = Path(__file__).resolve().parents[1] / 'ch-store.sh'
    result = subprocess.run([str(wrapper), 'serve'], env={**remote_tools, 'DATED_L1_GENERATION': generation, 'DATED_NAME_STORE': store,
                            'NAME_SUMMARY': name_summary, 'HOT_L1_GENERATION': '/data/l1', 'NARROW_TARGET': 'fleet'},
                            text=True, capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr, calls.exists()) == (2, '', error + '\n', False)


@pytest.mark.parametrize('all_legacy_flags', [False, True])
def test_dated_name_serve_forwards_pair_and_preserves_legacy_defaults_and_flags(remote_tools, all_legacy_flags: bool) -> None:
    wrapper = Path(__file__).resolve().parents[1] / 'ch-store.sh'
    generation = "/data/dated '$UNCHANGED;\n漢字" if all_legacy_flags else '/data/dated'
    env = {**remote_tools, 'NAME_SUMMARY': '1', 'HOT_L1_GENERATION': '/data/l1', 'NARROW_TARGET': 'fleet',
           'DATED_L1_GENERATION': generation, 'DATED_NAME_STORE': 'gcs_fleet', 'HOT_L2_ARTIFACT': '', 'HOT_L2_CHECK': '',
           'NARROW_RICH_NAME_INDEX': '0', 'NARROW_DIRECTORY_PARENT_INDEX': '0', 'NARROW_RICH_NAME_VARIANT': '',
           'NARROW_PLAN': 'legacy'}
    for key in ('ROOT_PLAN', 'THREADS', 'CONCURRENCY'):
        env.pop(key, None)
    if all_legacy_flags:
        env.update(HOT_L2_ARTIFACT='/data/l2', HOT_L2_CHECK='/data/check', NARROW_RICH_NAME_INDEX='1',
                   NARROW_DIRECTORY_PARENT_INDEX='1', NARROW_RICH_NAME_VARIANT='g64', NARROW_PLAN='visible',
                   ROOT_PLAN='numeric', THREADS='4', CONCURRENCY='1')
    result = subprocess.run([str(wrapper), 'serve'], env=env, text=True, capture_output=True, timeout=10)
    assert (result.returncode, result.stderr) == (0, '')
    assert json.loads(result.stdout) == [
        'run', '-d', '--name', 'serve-query', '--restart', 'unless-stopped', '--network', 'host', '-v', '/data:/data',
        '-e', 'PYTHONPATH=/data/src', '-e', 'QUERY_BOX_TOKEN=fixture-token', '--entrypoint', 'python3', 'fixture-image',
        '-u', '-m', 'dt_cloud.cli', 'serve-query', '-e', 'ch', '-p', '8080', '-c', '1' if all_legacy_flags else '2',
        '-t', '4' if all_legacy_flags else '8', '-r', 'numeric' if all_legacy_flags else 'rich', '-g', '/data/l1',
        *(['-H', '/data/l2', '-J', '/data/check'] if all_legacy_flags else []), '-L', '-G', generation, '-f', 'gcs_fleet',
        '-N', 'fleet', *(['-i', '-v', 'g64', '-j', '-k', 'visible'] if all_legacy_flags else []), 'http://localhost:8123',
    ]


@pytest.mark.parametrize("value,target,error", [
    ("1", "", "NARROW_DIRECTORY_PARENT_INDEX requires NARROW_TARGET"),
    ("invalid", "narrow_test", "NARROW_DIRECTORY_PARENT_INDEX must be 0 or 1"),
])
def test_parent_index_validation_precedes_remote_access(remote_tools, value, target, error):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    result = subprocess.run(
        [str(wrapper), "serve"],
        env={**remote_tools, "NARROW_TARGET": target, "NARROW_RICH_NAME_INDEX": "0", "NARROW_DIRECTORY_PARENT_INDEX": value},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode, result.stdout, result.stderr) == (2, "", error + "\n")


@pytest.mark.parametrize("mode", ["push", "push-src"])
def test_source_stage_does_not_copy_directories(tmp_path, mode):
    wrapper = Path(__file__).resolve().parents[1] / "ch-store.sh"
    scripts = tmp_path / "repo/job/ch-store"
    scripts.mkdir(parents=True)
    (scripts / "one.sql").write_text("SELECT 1;\n")
    (scripts / "two.yml").write_text("queries: []\n")
    (scripts / "cache").mkdir()
    copied = scripts.parent / "ch-store.sh"
    copied.write_text(wrapper.read_text())
    copied.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, source in {
        "gcloud": f"#!{sys.executable}\nimport json, sys\nif sys.argv[1] == 'compute':\n    print('127.0.0.1')\nelse:\n    print(json.dumps(sys.argv[1:]), file=sys.stderr)\n",
        # Capture the remote command; never execute push's mutations locally.
        "ssh": f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[-1]))\n",
    }.items():
        script = fake_bin / name
        script.write_text(source)
        script.chmod(0o755)
    result = subprocess.run(
        [str(copied), mode],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "USER": "wrapper-test"},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    base = "gs://oa-gcs-usage-dvx/scratch/bench/ch-store"
    expected = [
        ["storage", "rsync", "-r", "-x", ".*__pycache__.*", "cloud/src/dt_cloud", f"{base}/src/dt_cloud"],
        ["storage", "rsync", "-r", "-x", ".*__pycache__.*", "src/disk_tree", f"{base}/src/disk_tree"],
    ]
    remote = f"sudo rm -rf /data/src/dt_cloud /data/src/disk_tree && sudo gcloud storage cp -r '{base}/src/dt_cloud' '{base}/src/disk_tree' /data/src/ > /dev/null 2>&1"
    if mode == "push":
        expected.append(["storage", "cp", "job/ch-store/one.sql", "job/ch-store/two.yml", f"{base}/scripts/"])
        remote += f" && sudo gcloud storage cp '{base}/scripts/*' /data/ > /dev/null 2>&1 && sudo chmod +x /data/*.sh"
    assert [json.loads(line) for line in result.stderr.splitlines()] == expected
    assert json.loads(result.stdout) == remote + " && echo pushed"
