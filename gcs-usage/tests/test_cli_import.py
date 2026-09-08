"""The CLI must import without the `disk_tree` engine installed: the Healthcheck
workflow (`.github/workflows/health.yml`) syncs only `gcs-usage/` and runs
`gcs-usage healthcheck`; the engine is a runtime import of the commands that
aggregate listings, never a module-load dependency."""
import subprocess
import sys


def test_cli_imports_without_disk_tree() -> None:
    code = (
        "import sys; sys.modules['disk_tree'] = None; sys.modules['disk_tree.listing'] = None\n"
        "import gcs_usage.cli; print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert (r.returncode, r.stdout.strip(), r.stderr.strip().splitlines()[-1:] ) == (0, "ok", [])
