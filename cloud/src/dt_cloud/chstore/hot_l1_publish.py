"""Single-node immutable local catalog generations and pinned verified readers.

The explicit private filesystem and operator-supplied oracle declarations are
trusted. Hashes detect artifact corruption, not a malicious replacement of the
manifest. No old or interrupted generation is deleted, and no scan fallback
exists. Files inside a published generation must remain immutable.
"""

from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from hashlib import sha256
from json import dumps, loads
from math import isfinite
from os import O_DIRECTORY, O_RDONLY, close, fsync, open as open_fd, replace
from pathlib import Path
from re import fullmatch
from typing import Iterator
from uuid import uuid4

from .hot_l1_batch_catalog import HotL1BatchCatalog
from .hot_l1_catalog import _unique_object
from .hot_l1_prefix_audit import SOURCE_CONTRACT

SCHEMA = "hot-l1-published-generation-v1"
VALIDATION = "operator-trusted batch references; not an independent full-catalog scan"
PREFIX_UNCHECKED = {"checked": False, "description": "not supplied; per-scan immediate-parent presence unverified"}


def _fsync_dir(path: Path) -> None:
    fd = open_fd(path, O_RDONLY | O_DIRECTORY)
    try:
        fsync(fd)
    finally:
        close(fd)


def _owned(root: Path, relative: str) -> Path:
    path = root
    for part in relative.split("/"):
        if not part or part in (".", ".."):
            raise ValueError("published paths must be owned relative generation files")
        path = path / part
        if path.is_symlink():
            raise ValueError("published generation paths must not be symlinks")
    return path


def _manifest(value: object) -> dict:
    required = {"schema", "complete", "generation", "metadata", "artifacts", "validation"}
    optional = {"prefix_proofs", "source_prefix_validation"}
    if (not isinstance(value, dict) or not required <= set(value) <= required | optional or
            set(value) & optional not in (set(), optional) or
            value["schema"] != SCHEMA or value["complete"] is not True or value["validation"] != VALIDATION or
            not isinstance(value["generation"], str) or fullmatch(r"[0-9a-f]{32}", value["generation"]) is None or
            not isinstance(value["metadata"], dict) or not isinstance(value["artifacts"], list) or not value["artifacts"]):
        raise ValueError("published catalog requires a complete owned generation manifest")
    proofs = value.get("prefix_proofs", [])
    if not isinstance(proofs, list):
        raise ValueError("published prefix proofs must be a descriptor list")
    if "source_prefix_validation" in value:
        expected = {"checked": True, "description": SOURCE_CONTRACT} if proofs else PREFIX_UNCHECKED
        actual = value["source_prefix_validation"]
        if (not isinstance(actual, dict) or set(actual) != set(expected) or actual.get("checked") is not expected["checked"] or
                actual.get("description") != expected["description"]):
            raise ValueError("published source-prefix validation disagrees with its declared proofs")
    files = set()
    for row, role in [(row, "artifact") for row in value["artifacts"]] + [(row, "prefix-proof") for row in proofs]:
        if (not isinstance(row, dict) or set(row) != {"file", "sha256", "bytes"} or not isinstance(row["file"], str) or
                fullmatch(r"generations/" + value["generation"] + "/" + role + r"-[0-9]{4,}\.json", row["file"]) is None or
                row["file"] in files or not isinstance(row["sha256"], str) or fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None or
                type(row["bytes"]) is not int or row["bytes"] <= 0):
            raise ValueError("published manifest contains invalid artifact ownership/hash metadata")
        files.add(row["file"])
    return value


def _read_manifest(path: Path) -> dict:
    return _manifest(loads(path.read_bytes(), object_pairs_hook=_unique_object))


def _require_references(catalog: HotL1BatchCatalog) -> None:
    if any(not snapshot.references for snapshot in catalog._snapshots.values()):
        raise ValueError("publication requires at least one trusted independent reference per scan")


def _prefix_proofs(proofs: list[bytes], batches: list[bytes]) -> dict:
    if not proofs:
        return dict(PREFIX_UNCHECKED)
    by_date = {body["date"]: (body, data) for data in batches for body in [loads(data, object_pairs_hook=_unique_object)]}
    seen = set()
    for data in proofs:
        proof = loads(data, object_pairs_hook=_unique_object)
        if (not isinstance(proof, dict) or set(proof) != {"schema", "complete", "prefix_closed", "target", "date", "snapshot_db", "nodes_read", "buckets", "accepted_artifact", "source_contract", "source_query_id", "native", "audit_s"} or
                proof["schema"] != "hot-l1-prefix-proof-v1" or proof["complete"] is not True or proof["prefix_closed"] is not True or
                proof["source_contract"] != SOURCE_CONTRACT or not isinstance(proof["date"], str) or proof["date"] not in by_date or proof["date"] in seen or
                not isinstance(proof["source_query_id"], str) or fullmatch(r"hot_l1_prefix_[0-9a-f]{32}", proof["source_query_id"]) is None or
                type(proof["audit_s"]) not in (int, float) or not isfinite(proof["audit_s"]) or proof["audit_s"] < 0):
            raise ValueError("publication requires complete valid bound prefix proofs")
        body, accepted = by_date[proof["date"]]
        bounds = sorted([{field: row[field] for field in ("pre", "post", "path")} for row in body["results"][0]["buckets"]], key=lambda row: row["pre"])
        artifact, native = proof["accepted_artifact"], proof["native"]
        if (proof["target"] != body["target"] or proof["snapshot_db"] != body["snapshot_db"] or
                type(proof["nodes_read"]) is not int or proof["nodes_read"] != body["source_validation"]["rows"] or
                dumps(proof["buckets"], sort_keys=True) != dumps(bounds, sort_keys=True) or
                not isinstance(artifact, dict) or set(artifact) != {"path", "sha256", "bytes"} or not isinstance(artifact["path"], str) or not artifact["path"] or
                artifact["sha256"] != sha256(accepted).hexdigest() or type(artifact["bytes"]) is not int or artifact["bytes"] != len(accepted) or
                not isinstance(native, dict) or set(native) != {"schema", "complete", "prefix_closed", "nodes_read", "peak_stack"} or
                native["schema"] != "hot-l1-native-prefix-v1" or native["complete"] is not True or native["prefix_closed"] is not True or
                type(native["nodes_read"]) is not int or native["nodes_read"] != proof["nodes_read"] or
                type(native["peak_stack"]) is not int or not 1 <= native["peak_stack"] <= native["nodes_read"]):
            raise ValueError("prefix proof source/artifact/native bindings disagree with the copied batch")
        seen.add(proof["date"])
    if seen != set(by_date):
        raise ValueError("prefix proofs must cover every published scan exactly once")
    return {"checked": True, "description": SOURCE_CONTRACT}


def _verified_files(root: Path, descriptors: list[dict]) -> list[bytes]:
    blobs = []
    for row in descriptors:
        data = _owned(root, row["file"]).read_bytes()
        if len(data) != row["bytes"] or sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("published artifact length/SHA256 disagrees with its pinned manifest")
        blobs.append(data)
    return blobs


def pin(root: Path) -> dict:
    """Read the atomic current pointer once; later loading never rereads it."""
    root = root.resolve()
    return _read_manifest(_owned(root, "current.json"))


def load_pinned(root: Path, manifest: dict) -> HotL1BatchCatalog:
    """Hash and parse the same bytes from exactly the pinned owned generation."""
    root, manifest = root.resolve(), _manifest(manifest)
    generation = "generations/" + manifest["generation"]
    if _read_manifest(_owned(root, generation + "/manifest.json")) != manifest:
        raise ValueError("published pointer disagrees with its immutable generation manifest")
    blobs = _verified_files(root, manifest["artifacts"])
    catalog = HotL1BatchCatalog.from_bytes(blobs)
    _require_references(catalog)
    if catalog.metadata() != manifest["metadata"]:
        raise ValueError("published catalog metadata disagrees with its pinned artifacts")
    prefix_validation = _prefix_proofs(_verified_files(root, manifest.get("prefix_proofs", [])), blobs)
    if manifest.get("source_prefix_validation", PREFIX_UNCHECKED) != prefix_validation:
        raise ValueError("published source-prefix validation disagrees with verified proofs")
    return catalog


def load(root: Path) -> HotL1BatchCatalog:
    return load_pinned(root, pin(root))


@contextmanager
def _writer(root: Path) -> Iterator[None]:
    with _owned(root, ".publisher.lock").open("a+b") as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            raise ValueError("another local catalog publisher holds the writer lock") from None
        try:
            yield
        finally:
            flock(lock, LOCK_UN)


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as output:
        output.write(data)
        output.flush()
        fsync(output.fileno())


def _copy(source: Path, root: Path, relative: str) -> tuple[dict, bytes]:
    destination, digest, size = root / relative, sha256(), 0
    with source.open("rb") as input_file, destination.open("xb") as output:
        while chunk := input_file.read(1 << 20):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        output.flush()
        fsync(output.fileno())
    data = destination.read_bytes()
    if len(data) != size or sha256(data).hexdigest() != digest.hexdigest():
        raise ValueError("copied publication artifact changed before validation")
    return {"file": relative, "sha256": digest.hexdigest(), "bytes": size}, data


def publish(
    artifacts: tuple[Path, ...],
    root: Path,
    *,
    prefix_proofs: tuple[Path, ...] = (),
) -> dict:
    """Publish exactly these scans; unrelated current.json files are refused.

    Failure before replacement leaves the prior pointer untouched. Failure
    afterward may leave the new complete generation visible; never roll back
    a pointer that a concurrent reader could already have pinned.
    """
    if not artifacts:
        raise ValueError("publication requires explicit completed batch artifacts")
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    with _writer(root):
        current = _owned(root, "current.json")
        if current.exists():
            load_pinned(root, _read_manifest(current))
        generations = _owned(root, "generations")
        generations.mkdir(exist_ok=True)
        generation = uuid4().hex
        directory = generations / generation
        directory.mkdir()
        descriptors, blobs = [], []
        for i, source in enumerate(artifacts):
            relative = f"generations/{generation}/artifact-{i:04d}.json"
            descriptor, data = _copy(source, root, relative)
            descriptors.append(descriptor)
            blobs.append(data)
        catalog = HotL1BatchCatalog.from_bytes(blobs)
        _require_references(catalog)
        proof_descriptors, proof_blobs = [], []
        for i, source in enumerate(prefix_proofs):
            descriptor, data = _copy(source, root, f"generations/{generation}/prefix-proof-{i:04d}.json")
            proof_descriptors.append(descriptor)
            proof_blobs.append(data)
        prefix_validation = _prefix_proofs(proof_blobs, blobs)
        manifest = _manifest({"schema": SCHEMA, "complete": True, "generation": generation, "metadata": catalog.metadata(),
                              "artifacts": descriptors, "validation": VALIDATION, "prefix_proofs": proof_descriptors,
                              "source_prefix_validation": prefix_validation})
        encoded = (dumps(manifest) + "\n").encode()
        _write(directory / "manifest.json", encoded)
        _fsync_dir(directory)
        _fsync_dir(generations)
        _fsync_dir(root)
        staged = root / (".current-" + generation + ".json")
        _write(staged, encoded)
        replace(staged, current)
        _fsync_dir(root)
        return manifest
