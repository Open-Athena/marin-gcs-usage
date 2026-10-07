"""Disk-backed canonical references for uncontaminated optimized repetitions."""

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

from ..bench.queryset import Case
from . import narrow_serve
from .serve import Store


def save_mismatch(
    root: Path,
    optimized: dict,
    canonical: dict,
) -> Path:
    """Preserve failed full bodies for diagnosis without replaying costly queries."""
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(mkdtemp(prefix="mismatch-", dir=root))
    for name, body in (("optimized", optimized), ("canonical", canonical)):
        with (directory / f"{name}.json").open("x") as output:
            json.dump(body, output)
            output.write("\n")
    return directory


@contextmanager
def reference_bodies(
    store: Store,
    dates: tuple[str, ...],
    prefix: str,
    cases: Sequence[Case],
    *,
    root: Path,
    previous: str | None = None,
    reference_timeout: int | None = None,
    reset: Callable[[], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> Iterator[dict[tuple[str, str], Path]]:
    """Run all canonical requests first, retaining one parsed body at a time.

    Reference files are scoped to this context and removed on success/failure.
    Timed optimized requests must execute inside the context, after it yields.
    This isolates engine phases, not caches: only explicit resets are cold.
    """
    keys = [(date, case.id) for date in dates for case in cases]
    if len(keys) != len(set(keys)):
        raise ValueError("reference cases must have unique date/query keys")
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="references-", dir=root) as directory:
        paths = {}
        for date in dates:
            for case in cases:
                if reset is not None:
                    reset()
                budget = {"reference_timeout": reference_timeout} if reference_timeout is not None else {}
                result = narrow_serve.compare_response(store, date, prefix, case.q, previous=previous, syntax=case.qs, **budget)
                path = Path(directory) / f"{len(paths)}.json"
                with path.open("w") as output:
                    json.dump(result, output)
                    output.write("\n")
                paths[date, case.id] = path
                if log is not None:
                    log(f"reference {date} / {case.id}: {result['response_s']}s")
                del result
        yield paths
