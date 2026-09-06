"""`_d1_query` transient-error retry (the 2026-09-01 [team]-variant 401)."""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from gcs_usage import index_footer
from gcs_usage.index_footer import D1_RETRIES, _d1_query


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.cloudflare.com/...",
        code=code,
        msg="err",
        hdrs=None,
        fp=io.BytesIO(b'{"success":false,"errors":[{"code":10000,"message":"Authentication error"}]}'),
    )


class _Resp:
    def read(self) -> bytes:
        return b'{"success": true, "result": []}'


def test_d1_query_retries_transient_401(monkeypatch):
    """Two spurious 401s then success: exactly 3 attempts, no exception."""
    monkeypatch.setattr(index_footer, "D1_RETRY_SLEEP", 0.0)
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) <= 2:
            raise _http_error(401)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _d1_query("SELECT 1", acct="acct", tok="tok")
    assert calls == [
        "https://api.cloudflare.com/client/v4/accounts/acct/d1/database/" + index_footer.D1_DB_ID + "/query",
    ] * 3


def test_d1_query_persistent_401_raises_after_all_retries(monkeypatch):
    monkeypatch.setattr(index_footer, "D1_RETRY_SLEEP", 0.0)
    calls: list[int] = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _http_error(401)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as ei:
        _d1_query("SELECT 1", acct="acct", tok="tok")
    assert str(ei.value) == (
        'D1 query failed (401): {"success":false,"errors":'
        '[{"code":10000,"message":"Authentication error"}]}'
    )
    assert calls == [1] * (D1_RETRIES + 1)


def test_d1_query_non_retryable_status_fails_fast(monkeypatch):
    """A 400 (bad SQL / bad scope shape) is not transient — one attempt only."""
    monkeypatch.setattr(index_footer, "D1_RETRY_SLEEP", 0.0)
    calls: list[int] = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _http_error(400)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError):
        _d1_query("SELECT 1", acct="acct", tok="tok")
    assert calls == [1]
