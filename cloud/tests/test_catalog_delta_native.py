"""`native/catalog_delta.cpp` against a brute-force first-hit sum: for every
literal and bucket, Σ sign·(size, n_files) over rows whose name contains the
literal and whose parent path doesn't — the same rule as `mega_names.answer`."""
from json import loads
from pathlib import Path
from random import Random
from shutil import which
from subprocess import run

import pytest

SOURCE = Path(__file__).parents[1] / 'src/dt_cloud/chstore/native/catalog_delta.cpp'


@pytest.fixture(scope='module')
def binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cxx = which('c++') or which('g++')
    if cxx is None:
        pytest.skip('no C++ compiler')
    out = tmp_path_factory.mktemp('native') / 'catalog-delta'
    run([cxx, '-O2', '-std=c++17', '-Wall', '-Wextra', '-Werror', str(SOURCE), '-o', str(out)], check=True)
    return out


def string(value: str) -> bytes:
    raw = value.encode()
    n, out = len(raw), bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + raw


def rows_binary(rows: list[tuple[str, str, int, int, int]]) -> bytes:
    return b''.join(string(lpath) + string(bucket) + sign.to_bytes(1, 'little', signed=True) +
                    size.to_bytes(8, 'little', signed=True) + files.to_bytes(8, 'little', signed=True)
                    for lpath, bucket, sign, size, files in rows)


def native(binary: Path, tmp: Path, terms: list[str], rows: list[tuple]) -> dict:
    (tmp / 'terms').write_bytes(b''.join(string(t) for t in terms))
    done = run([str(binary), str(tmp / 'terms')], input=rows_binary(rows), capture_output=True, check=True)
    lines = done.stdout.decode().splitlines()
    header, footer = loads(lines[0]), loads(lines[-1])
    assert header['schema'] == 'catalog-delta-v1'
    assert footer == {'complete': True, 'rows': len(rows)}
    out = {}
    for line in lines[1:-1]:
        t, b, size, files = line.split('\t')
        out[terms[int(t)], header['buckets'][int(b)]] = (int(size), int(files))
    return out


def brute(terms: list[str], rows: list[tuple]) -> dict:
    out: dict[tuple[str, str], list[int]] = {}
    for lpath, bucket, sign, size, files in rows:
        parent, _, name = lpath.rpartition('/')
        for t in terms:
            if t in name and t not in parent:
                acc = out.setdefault((t, bucket), [0, 0])
                acc[0] += sign * size
                acc[1] += sign * files
    return {key: tuple(v) for key, v in out.items() if v != [0, 0]}


def test_fixed_rows(binary: Path, tmp_path: Path) -> None:
    rows = [
        ('b1', 'b1', 1, 100, 3),
        ('b1/ckpt', 'b1', 1, 60, 2),
        ('b1/ckpt/ckpt-1.bin', 'b1', 1, 50, 1),   # covered by its parent `ckpt`
        ('b1/ckpt/x.bin', 'b1', 1, 10, 1),
        ('b1/other/ckpt.bin', 'b1', 1, 40, 1),
        ('b2/aa/aaa', 'B2', -1, 7, 1),            # a closed version; `a` covered, `aaa` a first hit
    ]
    terms = ['ckpt', 'bin', '.bin', 'b1', 'aaa', 'a', 'zz']
    assert native(binary, tmp_path, terms, rows) == {
        ('b1', 'b1'): (100, 3),
        ('ckpt', 'b1'): (100, 3),
        ('bin', 'b1'): (100, 3),
        ('.bin', 'b1'): (100, 3),
        ('aaa', 'B2'): (-7, -1),
    }


def test_random_rows_equal_brute_force(binary: Path, tmp_path: Path) -> None:
    rng = Random(7)
    alphabet = 'abcAB-_.é字'
    segments = [''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 6))) for _ in range(40)]
    rows = []
    for _ in range(3000):
        bucket = rng.choice(['bk1', 'bk2', 'Bk3'])
        path = '/'.join([bucket] + [rng.choice(segments) for _ in range(rng.randint(0, 4))])
        rows.append((path.lower(), bucket, rng.choice([1, -1]), rng.randint(0, 10 ** 12), rng.randint(0, 9)))
    rows.sort(key=lambda r: (r[0].count('/'), r[0].encode()))
    names = {r[0].rpartition('/')[2] for r in rows}
    terms = sorted({n[i:i + k] for n in names for k in (1, 2, 3, 5) for i in range(len(n) - k + 1)} - {''})
    terms = [t for t in terms if '/' not in t]
    assert len(terms) == 156
    assert native(binary, tmp_path, terms, rows) == brute(terms, rows)
