from hashlib import sha256
from json import dumps, loads
from os import environ, fstat, pathsep
from pathlib import Path
from re import sub
from stat import S_ISDIR
from subprocess import run
from sys import executable
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from dt_cloud.chstore import hot_l1_publish as module
from test_chhot_l1_batch_catalog import artifact, expected_view, write


@pytest.fixture
def generations(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = iter(["b" * 32, "c" * 32, "d" * 32])
    monkeypatch.setattr(module, "uuid4", lambda: SimpleNamespace(hex=next(ids)))


def test_publish_complete_generation_hashes_exact_copies_and_pinned_reader(generations: None, tmp_path: Path) -> None:
    before, after = artifact("2026-10-04"), artifact()
    paths = (write(tmp_path / "before.json", before), write(tmp_path / "after.json", after))
    root = tmp_path / "published"
    manifest = module.publish(paths, root)
    catalog = module.load(root)
    expected = {"schema": module.SCHEMA, "complete": True, "generation": "b" * 32, "metadata": catalog.metadata(),
                "artifacts": [{"file": f"generations/{'b' * 32}/artifact-{i:04d}.json", "sha256": sha256(path.read_bytes()).hexdigest(),
                               "bytes": len(path.read_bytes())} for i, path in enumerate(paths)], "validation": module.VALIDATION,
                "prefix_proofs": [], "source_prefix_validation": module.PREFIX_UNCHECKED}
    assert manifest == expected
    assert module.pin(root) == expected
    assert (root / "current.json").read_text() == dumps(expected) + "\n"
    assert (root / "generations" / ("b" * 32) / "manifest.json").read_text() == dumps(expected) + "\n"
    assert [(root / row["file"]).read_bytes() for row in manifest["artifacts"]] == [path.read_bytes() for path in paths]
    assert catalog.view("2026-10-04", ".json") == expected_view(before)
    assert catalog.view("2026-10-05", ".npy") == expected_view(after, 2)
    paths[0].write_text("changed source\n")
    assert module.load(root).view("2026-10-04", ".json") == expected_view(before)


def test_files_and_directory_entries_are_fsynced_before_atomic_pointer(generations: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = write(tmp_path / "source.json", artifact())
    root, calls = tmp_path / "published", []
    original_fsync, original_replace = module.fsync, module.replace

    def fsync(fd: int) -> None:
        calls.append("directory" if S_ISDIR(fstat(fd).st_mode) else "file")
        original_fsync(fd)

    def replace(source: Path, target: Path) -> None:
        assert (source, target) == (root / (".current-" + "b" * 32 + ".json"), root / "current.json")
        calls.append("replace")
        original_replace(source, target)

    monkeypatch.setattr(module, "fsync", fsync)
    monkeypatch.setattr(module, "replace", replace)
    module.publish((path,), root)
    assert calls == ["file", "file", "directory", "directory", "directory", "file", "replace", "directory"]


@pytest.mark.parametrize("after_replace", [False, True])
def test_interrupted_publish_keeps_only_complete_pointer_and_all_generations(generations: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, after_replace: bool) -> None:
    before, after = artifact("2026-10-04"), artifact()
    old_source, new_source = write(tmp_path / "old.json", before), write(tmp_path / "new.json", after)
    root = tmp_path / "published"
    old = module.publish((old_source,), root)
    original_replace = module.replace

    def fail(source: Path, target: Path) -> None:
        if after_replace:
            original_replace(source, target)
        raise OSError("interrupted pointer publication")

    monkeypatch.setattr(module, "replace", fail)
    with pytest.raises(OSError) as caught:
        module.publish((new_source,), root)
    assert str(caught.value) == "interrupted pointer publication"
    accepted = module.pin(root)
    assert accepted["generation"] == ("c" * 32 if after_replace else "b" * 32)
    expected_body = after if after_replace else before
    assert module.load(root).view(expected_body["date"], ".json") == expected_view(expected_body)
    assert module.load_pinned(root, old).view("2026-10-04", ".json") == expected_view(before)
    assert sorted(path.name for path in (root / "generations").iterdir()) == ["b" * 32, "c" * 32]
    assert (root / (".current-" + "c" * 32 + ".json")).exists() is (not after_replace)


def test_concurrent_reader_pins_old_manifest_once_while_new_generation_becomes_current(generations: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    before, after = artifact("2026-10-04"), artifact()
    root = tmp_path / "published"
    module.publish((write(tmp_path / "old.json", before),), root)
    pinned = module.pin(root)
    module.publish((write(tmp_path / "new.json", after),), root)
    original_read = Path.read_bytes
    calls = []

    def read(path: Path) -> bytes:
        calls.append(path.relative_to(root).as_posix())
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    catalog = module.load_pinned(root, pinned)
    assert catalog.view("2026-10-04", ".json") == expected_view(before)
    assert calls == [f"generations/{'b' * 32}/manifest.json", f"generations/{'b' * 32}/artifact-0000.json"]


def test_hash_verified_bytes_are_exactly_the_bytes_parsed_not_a_later_file_version(generations: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    body = artifact()
    root = tmp_path / "published"
    manifest = module.publish((write(tmp_path / "source.json", body),), root)
    path = root / manifest["artifacts"][0]["file"]
    original_read = Path.read_bytes

    def read(source: Path) -> bytes:
        data = original_read(source)
        if source == path:
            source.write_text("changed after verified read\n")
        return data

    monkeypatch.setattr(Path, "read_bytes", read)
    assert module.load_pinned(root, manifest).view("2026-10-05", ".json") == expected_view(body)
    assert path.read_text() == "changed after verified read\n"


@pytest.mark.parametrize("kind", ["missing", "truncated", "hash", "symlink", "manifest"])
def test_corrupt_owned_generation_refuses_without_fallback(generations: None, tmp_path: Path, kind: str) -> None:
    root = tmp_path / "published"
    manifest = module.publish((write(tmp_path / "source.json", artifact()),), root)
    path = root / manifest["artifacts"][0]["file"]
    if kind == "missing":
        path.unlink()
    elif kind == "truncated":
        path.write_bytes(path.read_bytes()[:10])
    elif kind == "hash":
        data = path.read_bytes()
        path.write_bytes(data.replace(b'"b": 2', b'"b": 3'))
    elif kind == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "source.json")
    else:
        (path.parent / "manifest.json").write_text("{\n")
    with pytest.raises((ValueError, FileNotFoundError)):
        module.load(root)
    assert (root / "current.json").read_text() == dumps(manifest) + "\n"


@pytest.mark.parametrize("kind", ["refs", "bad-root", "duplicate-date"])
def test_invalid_batch_or_unreferenced_scan_never_replaces_prior_pointer(generations: None, tmp_path: Path, kind: str) -> None:
    root = tmp_path / "published"
    valid = write(tmp_path / "valid.json", artifact())
    accepted = module.publish((valid,), root)
    body = artifact("2026-10-04")
    if kind == "refs":
        body["validation"]["references"] = []
    elif kind == "bad-root":
        body["results"][0]["root"]["b"] = 999
    bad = write(tmp_path / "bad.json", body)
    with pytest.raises(ValueError):
        module.publish((bad, bad) if kind == "duplicate-date" else (bad,), root)
    assert module.pin(root) == accepted
    assert module.load(root).view("2026-10-05", ".json") == expected_view(artifact())
    assert sorted(path.name for path in (root / "generations").iterdir()) == ["b" * 32, "c" * 32]


def test_writer_lock_and_unowned_pointer_refusal_preserve_current_file(tmp_path: Path) -> None:
    root = tmp_path / "published"
    root.mkdir()
    source = write(tmp_path / "source.json", artifact())
    with module._writer(root):
        with pytest.raises(ValueError) as caught:
            module.publish((source,), root)
        assert str(caught.value) == "another local catalog publisher holds the writer lock"
    assert sorted(path.name for path in root.iterdir()) == [".publisher.lock"]
    (root / "current.json").write_text('{"someone-elses": "pointer"}\n')
    with pytest.raises(ValueError) as caught:
        module.publish((source,), root)
    assert str(caught.value) == "published catalog requires a complete owned generation manifest"
    assert (root / "current.json").read_text() == '{"someone-elses": "pointer"}\n'
    assert sorted(path.name for path in root.iterdir()) == [".publisher.lock", "current.json"]


def test_manifest_relative_ownership_and_missing_current_refuse(generations: None, tmp_path: Path) -> None:
    root = tmp_path / "published"
    with pytest.raises(FileNotFoundError):
        module.load(root)
    manifest = module.publish((write(tmp_path / "source.json", artifact()),), root)
    manifest["artifacts"][0]["file"] = "../source.json"
    with pytest.raises(ValueError) as caught:
        module.load_pinned(root, manifest)
    assert str(caught.value) == "published manifest contains invalid artifact ownership/hash metadata"


@pytest.mark.parametrize("proofs", [False, True])
def test_publish_cli_exact_forwarding_and_manifest_stdout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proofs: bool) -> None:
    from dt_cloud.cli import main

    calls, body = [], {"schema": module.SCHEMA, "complete": True, "generation": "fixture"}

    def publish(artifacts: tuple[Path, ...], root: Path, *, prefix_proofs: tuple[Path, ...]) -> dict:
        calls.append((artifacts, root, prefix_proofs))
        return body

    monkeypatch.setattr(module, "publish", publish)
    root, paths = tmp_path / "published", (tmp_path / "before.json", tmp_path / "after.json")
    proof_paths = (tmp_path / "before-proof.json", tmp_path / "after-proof.json")
    args = ["ch-hot-l1-publish", "-o", str(root), *map(str, paths)]
    if proofs:
        args.extend(["-p", str(proof_paths[0]), "-p", str(proof_paths[1])])
    result = CliRunner().invoke(main, args)
    assert (result.exit_code, result.stdout, result.stderr) == (0, dumps(body) + "\n", "")
    assert calls == [(paths, root, proof_paths if proofs else ())]


def test_module_publish_help_matches_imported_cli_help() -> None:
    from dt_cloud.cli import main

    project = Path(__file__).resolve().parents[2]
    imported = CliRunner().invoke(main, ["ch-hot-l1-publish", "--help"], terminal_width=80)
    completed = run(
        [executable, "-m", "dt_cloud.cli", "ch-hot-l1-publish", "--help"],
        cwd=project,
        env={**environ, "COLUMNS": "80", "PYTHONPATH": pathsep.join((str(project / "cloud/src"), str(project / "src"), environ.get("PYTHONPATH", "")))},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    def normalize(value: str) -> str:
        return sub(r"\AUsage: .*? ch-hot-l1-publish ", "Usage: <cli> ch-hot-l1-publish ", value)

    assert (imported.exit_code, imported.stderr) == (0, "")
    assert (completed.returncode, normalize(completed.stdout), completed.stderr) == (
        imported.exit_code, normalize(imported.stdout), imported.stderr,
    )


def prefix_proof(path: Path, accepted: Path) -> dict:
    data = accepted.read_bytes()
    body = loads(data)
    count = body["source_validation"]["rows"]
    proof = {"schema": "hot-l1-prefix-proof-v1", "complete": True, "prefix_closed": True, "target": body["target"],
             "date": body["date"], "snapshot_db": body["snapshot_db"], "nodes_read": count,
             "buckets": sorted([{field: bucket[field] for field in ("pre", "post", "path")} for bucket in body["results"][0]["buckets"]], key=lambda bucket: bucket["pre"]),
             "accepted_artifact": {"path": str(accepted), "sha256": sha256(data).hexdigest(), "bytes": len(data)},
             "source_contract": module.SOURCE_CONTRACT, "source_query_id": "hot_l1_prefix_" + "a" * 32,
             "native": {"schema": "hot-l1-native-prefix-v1", "complete": True, "prefix_closed": True, "nodes_read": count, "peak_stack": 3},
             "audit_s": .1}
    path.write_text(dumps(proof) + "\n")
    return proof


def test_complete_per_scan_proofs_are_copied_hashed_bound_and_loaded(generations: None, tmp_path: Path) -> None:
    before, after = artifact("2026-10-04"), artifact()
    paths = (write(tmp_path / "before.json", before), write(tmp_path / "after.json", after))
    proofs = (tmp_path / "before-proof.json", tmp_path / "after-proof.json")
    for path, proof in zip(paths, proofs, strict=True):
        prefix_proof(proof, path)
    root = tmp_path / "published"
    manifest = module.publish(paths, root, prefix_proofs=proofs)
    assert manifest["prefix_proofs"] == [
        {"file": f"generations/{'b' * 32}/prefix-proof-{i:04d}.json", "sha256": sha256(path.read_bytes()).hexdigest(), "bytes": len(path.read_bytes())}
        for i, path in enumerate(proofs)
    ]
    assert manifest["source_prefix_validation"] == {"checked": True, "description": module.SOURCE_CONTRACT}
    assert [(root / row["file"]).read_bytes() for row in manifest["prefix_proofs"]] == [path.read_bytes() for path in proofs]
    assert module.load(root).view("2026-10-04", ".json") == expected_view(before)
    assert module.load(root).view("2026-10-05", ".json") == expected_view(after)


def test_legacy_absent_proof_generation_still_loads_and_new_absence_is_explicit(generations: None, tmp_path: Path) -> None:
    body = artifact()
    root = tmp_path / "published"
    manifest = module.publish((write(tmp_path / "source.json", body),), root)
    assert manifest["source_prefix_validation"] == module.PREFIX_UNCHECKED
    manifest.pop("prefix_proofs")
    manifest.pop("source_prefix_validation")
    data = dumps(manifest) + "\n"
    (root / "current.json").write_text(data)
    (root / "generations" / manifest["generation"] / "manifest.json").write_text(data)
    assert module.load(root).view("2026-10-05", ".json") == expected_view(body)


@pytest.mark.parametrize("change", [
    lambda p: p.update(schema="wrong"), lambda p: p.update(complete=None), lambda p: p.update(prefix_closed=None),
    lambda p: p.update(target="other"), lambda p: p.update(date="2026-10-03"), lambda p: p.update(snapshot_db="other"),
    lambda p: p.update(nodes_read=True), lambda p: p["buckets"][0].update(pre=True),
    lambda p: p["accepted_artifact"].update(sha256="0" * 64), lambda p: p["accepted_artifact"].update(bytes=1),
    lambda p: p["native"].update(complete=None), lambda p: p["native"].update(prefix_closed=None),
    lambda p: p["native"].update(nodes_read=8), lambda p: p.update(source_contract="scalar values independently checked"),
])
def test_bad_or_null_true_proof_never_changes_current_or_old_pinned_reader(generations: None, tmp_path: Path, change: object) -> None:
    body = artifact()
    source, root = write(tmp_path / "source.json", body), tmp_path / "published"
    accepted = module.publish((source,), root)
    proof_path = tmp_path / "proof.json"
    proof = prefix_proof(proof_path, source)
    change(proof)
    proof_path.write_text(dumps(proof) + "\n")
    with pytest.raises(ValueError):
        module.publish((source,), root, prefix_proofs=(proof_path,))
    assert module.pin(root) == accepted
    assert module.load_pinned(root, accepted).view("2026-10-05", ".json") == expected_view(body)


@pytest.mark.parametrize("kind", ["missing-counterpart", "truncated", "duplicate"])
def test_provided_proof_set_must_be_complete_exactly_once(generations: None, tmp_path: Path, kind: str) -> None:
    before, after = write(tmp_path / "before.json", artifact("2026-10-04")), write(tmp_path / "after.json", artifact())
    root = tmp_path / "published"
    accepted = module.publish((after,), root)
    proof_path = tmp_path / "proof.json"
    prefix_proof(proof_path, after)
    if kind == "truncated":
        proof_path.write_text("{\n")
    with pytest.raises(ValueError):
        module.publish((before, after), root, prefix_proofs=(proof_path, proof_path) if kind == "duplicate" else (proof_path,))
    assert module.pin(root) == accepted
    assert module.load_pinned(root, accepted).view("2026-10-05", ".json") == expected_view(artifact())


@pytest.mark.parametrize("bindings", [False, True])
def test_loader_refuses_copied_proof_corruption_or_hash_valid_wrong_binding(generations: None, tmp_path: Path, bindings: bool) -> None:
    source, proof_path, root = tmp_path / "source.json", tmp_path / "proof.json", tmp_path / "published"
    write(source, artifact())
    proof = prefix_proof(proof_path, source)
    manifest = module.publish((source,), root, prefix_proofs=(proof_path,))
    row = manifest["prefix_proofs"][0]
    copied = root / row["file"]
    proof["accepted_artifact"]["sha256"] = "0" * 64
    data = (dumps(proof) + "\n").encode()
    copied.write_bytes(data)
    if bindings:
        row.update(sha256=sha256(data).hexdigest(), bytes=len(data))
        (copied.parent / "manifest.json").write_text(dumps(manifest) + "\n")
        (root / "current.json").write_text(dumps(manifest) + "\n")
    with pytest.raises(ValueError) as caught:
        module.load(root)
    assert str(caught.value) == ("prefix proof source/artifact/native bindings disagree with the copied batch" if bindings else
                                 "published artifact length/SHA256 disagrees with its pinned manifest")
