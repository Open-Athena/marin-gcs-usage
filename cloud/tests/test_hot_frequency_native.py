"""`native/hot_frequency.cpp` against a brute-force census: every substring of
1..K code points, once per name, summed over names' path counts, kept at ≥ T.
The native pruning (extend only windows whose prefix and suffix were hot) must
find exactly that set."""
from json import loads
from pathlib import Path
from random import Random
from shutil import which
from subprocess import run

import pytest

SOURCE = Path(__file__).parents[1] / 'src/dt_cloud/chstore/native/hot_frequency.cpp'


@pytest.fixture(scope='module')
def binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cxx = which('c++') or which('g++')
    if cxx is None:
        pytest.skip('no C++ compiler')
    out = tmp_path_factory.mktemp('native') / 'hot-frequency'
    run([cxx, '-O2', '-std=c++17', '-Wall', '-Wextra', '-Werror', str(SOURCE), '-o', str(out)], check=True)
    return out


def row_binary(rows: list[tuple[str, int]]) -> bytes:
    out = bytearray()
    for name, count in rows:
        raw = name.encode()
        n = len(raw)
        while True:
            byte = n & 0x7F
            n >>= 7
            out.append(byte | (0x80 if n else 0))
            if not n:
                break
        out += raw + count.to_bytes(8, 'little')
    return bytes(out)


def brute(rows: list[tuple[str, int]], threshold: int, max_chars: int) -> list[dict]:
    sums: dict[str, int] = {}
    for name, count in rows:
        for gram in {name[i:i + k] for k in range(1, max_chars + 1) for i in range(len(name) - k + 1)}:
            sums[gram] = sums.get(gram, 0) + count
    hot = [(len(g), g.encode(), g, s) for g, s in sums.items() if s >= threshold]
    return [{'chars': k, 'pattern': g, 'direct_matching_paths': s} for k, _, g, s in sorted(hot)]


def census(binary: Path, rows: list[tuple[str, int]], threshold: int, max_chars: int, threads: int = 3) -> list[dict]:
    done = run([str(binary), str(threshold), str(max_chars), str(threads)], input=row_binary(rows), capture_output=True, check=True)
    lines = [loads(line) for line in done.stdout.decode().splitlines()]
    assert lines[0] == {'schema': 'hot-frequency-queries-v1', 'engine': 'native', 'threshold_paths': threshold, 'max_chars': max_chars}
    assert lines[-1] == {'complete': True, 'patterns': len(lines) - 2}
    return lines[1:-1]


def test_small_exact(binary: Path) -> None:
    rows = [('model.npy', 5), ('data.npy', 3), ('zarr.json', 4), ('a.json', 1), ('nn', 2)]
    assert census(binary, rows, 6, 4) == [
        {'chars': 1, 'pattern': '.', 'direct_matching_paths': 13},
        {'chars': 1, 'pattern': 'a', 'direct_matching_paths': 8},
        {'chars': 1, 'pattern': 'd', 'direct_matching_paths': 8},
        {'chars': 1, 'pattern': 'n', 'direct_matching_paths': 15},
        {'chars': 1, 'pattern': 'o', 'direct_matching_paths': 10},
        {'chars': 1, 'pattern': 'p', 'direct_matching_paths': 8},
        {'chars': 1, 'pattern': 'y', 'direct_matching_paths': 8},
        {'chars': 2, 'pattern': '.n', 'direct_matching_paths': 8},
        {'chars': 2, 'pattern': 'np', 'direct_matching_paths': 8},
        {'chars': 2, 'pattern': 'py', 'direct_matching_paths': 8},
        {'chars': 3, 'pattern': '.np', 'direct_matching_paths': 8},
        {'chars': 3, 'pattern': 'npy', 'direct_matching_paths': 8},
        {'chars': 4, 'pattern': '.npy', 'direct_matching_paths': 8},
    ]


@pytest.mark.parametrize('seed', range(4))
def test_random_unicode_matches_brute_force(binary: Path, seed: int) -> None:
    rng = Random(seed)
    alphabet = 'ab.-_é日🙂"\\\t'  # multi-byte code points, and characters JSON must escape
    rows = sorted({''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 12))): rng.randint(1, 9) for _ in range(400)}.items())
    for threshold in (1, 40, 300):
        assert census(binary, rows, threshold, 6) == brute(rows, threshold, 6)


def raw_rows(rows: list[tuple[bytes, int]]) -> bytes:
    return b''.join(bytes([len(raw)]) + raw + count.to_bytes(8, 'little') for raw, count in rows)


def test_refuses_separators_invalid_utf8_and_pattern_cap(binary: Path) -> None:
    done = run([str(binary), '1', '3', '2'], input=row_binary([('a/b', 1)]), capture_output=True)
    assert (done.returncode, done.stderr.decode()) == (1, 'hot-frequency: basename vocabulary contains a path separator\n')
    # A lone continuation byte, an overlong '/', a surrogate, a code point past U+10FFFF.
    for bad in (b'a\x80', b'\xc0\xaf', b'\xed\xa0\x80', b'\xf4\x90\x80\x80'):
        done = run([str(binary), '1', '3', '2'], input=raw_rows([(bad, 1)]), capture_output=True)
        assert (done.returncode, done.stderr.decode()) == (1, 'hot-frequency: basename vocabulary contains invalid UTF-8\n')
    done = run([str(binary), '1', '3', '2', '2'], input=row_binary([('abc', 1)]), capture_output=True)
    assert (done.returncode, done.stderr.decode().splitlines()[-1]) == (1, 'hot-frequency: accepted-pattern cap exceeded at length 1')
