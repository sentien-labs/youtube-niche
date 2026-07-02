"""Offline tests for the SerpApi fallback in youtube_niche/signals/trends.py.

pytrends is unofficial and its upstream repo is dead (archived April 2025, chronic 429s), so
one upstream break used to silently kill both Trends signals (12-month momentum + 5-year
durability). These tests exercise the SerpApi fallback path entirely offline: no real network
call is made anywhere. `requests.get` is monkeypatched to a fake that raises if it's ever called
with a live URL, so a stray real request would fail loudly rather than hang.

Run: ./venv/bin/python -m pytest tests/test_trends_fallback.py -q
"""
from __future__ import annotations

import tempfile

import pytest

from youtube_niche.cache import Cache
from youtube_niche.signals import trends as trends_mod
from youtube_niche.signals.trends import _serpapi_series, durability_score, trends_score


class _FakeResponse:
    """Minimal stand-in for requests.Response — just enough for the SerpApi parser."""

    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def _canned_serpapi_payload(values: list[float]) -> dict:
    """A realistic SerpApi google_trends TIMESERIES payload shape."""
    return {
        "interest_over_time": {
            "timeline_data": [
                {
                    "date": f"day {i}",
                    "values": [{"extracted_value": v, "value": str(int(v))}],
                }
                for i, v in enumerate(values)
            ]
        }
    }


# --- SerpApi payload parser: realistic canned dict, incl. extracted_value / value variants ---


def test_serpapi_series_parses_extracted_value(monkeypatch):
    payload = _canned_serpapi_payload([10.0, 20.0, 30.0])

    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        return _FakeResponse(payload)

    monkeypatch.setattr(trends_mod.requests, "get", fake_get)

    vals = _serpapi_series("solar generators", "today 12-m", "youtube", "US", "fake-key")
    assert vals == [10.0, 20.0, 30.0]
    # Sanity-check the request shape (engine/data_type/date/api_key wired through).
    assert len(calls) == 1
    url, params, timeout = calls[0]
    assert url == trends_mod.SERPAPI_URL
    assert params["engine"] == "google_trends"
    assert params["q"] == "solar generators"
    assert params["data_type"] == "TIMESERIES"
    assert params["date"] == "today 12-m"
    assert params["gprop"] == "youtube"
    assert params["geo"] == "US"
    assert params["api_key"] == "fake-key"
    assert timeout == (10, 30)


def test_serpapi_series_falls_back_to_value_when_extracted_value_missing(monkeypatch):
    """Some timeline points only carry a string `value` (no `extracted_value`)."""
    payload = {
        "interest_over_time": {
            "timeline_data": [
                {"values": [{"value": "5"}]},
                {"values": [{"value": "15"}]},
                {"values": [{"extracted_value": 25}]},
            ]
        }
    }
    monkeypatch.setattr(trends_mod.requests, "get", lambda *a, **k: _FakeResponse(payload))
    vals = _serpapi_series("term", "today 5-y", "youtube", "US", "fake-key")
    assert vals == [5.0, 15.0, 25.0]


def test_serpapi_series_returns_none_on_empty_timeline(monkeypatch):
    payload = {"interest_over_time": {"timeline_data": []}}
    monkeypatch.setattr(trends_mod.requests, "get", lambda *a, **k: _FakeResponse(payload))
    assert _serpapi_series("term", "today 12-m", "youtube", "US", "fake-key") is None


def test_serpapi_series_returns_none_on_malformed_payload(monkeypatch):
    """Missing interest_over_time entirely, or a non-dict values entry -> None, never raises."""
    monkeypatch.setattr(trends_mod.requests, "get", lambda *a, **k: _FakeResponse({}))
    assert _serpapi_series("term", "today 12-m", "youtube", "US", "fake-key") is None


def test_serpapi_series_returns_none_on_request_exception(monkeypatch):
    def fake_get(*a, **k):
        raise ConnectionError("no network")

    monkeypatch.setattr(trends_mod.requests, "get", fake_get)
    assert _serpapi_series("term", "today 12-m", "youtube", "US", "fake-key") is None


def test_serpapi_series_returns_none_on_http_error(monkeypatch):
    monkeypatch.setattr(
        trends_mod.requests, "get", lambda *a, **k: _FakeResponse({}, status=429)
    )
    assert _serpapi_series("term", "today 12-m", "youtube", "US", "fake-key") is None


# --- pytrends raises + SerpApi configured -> fallback engages and a score is computed ---


def test_durability_score_falls_back_to_serpapi_when_pytrends_fails(monkeypatch):
    """pytrends path fails, SERPAPI_KEY is set -> SerpApi answers and durability is scored."""
    monkeypatch.setenv("SERPAPI_KEY", "fake-key")
    monkeypatch.setattr(trends_mod, "_pytrends_series", lambda *a, **k: (None, None, None))

    rising = [10.0] * 52 + [30.0] * 52  # recent year 3x the early year -> durable
    payload = _canned_serpapi_payload(rising)
    monkeypatch.setattr(trends_mod.requests, "get", lambda *a, **k: _FakeResponse(payload))

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/c.sqlite")
        score, detail = durability_score(
            "solar generators", geo="US", cache=cache, throttle=False,
        )
        assert score == pytest.approx(1.0)
        assert detail["status"] == "ok"
        assert detail["backend"] == "serpapi"
        assert detail["durability_ratio"] == pytest.approx(3.0)
        cache.close()


def test_trends_score_falls_back_to_serpapi_when_pytrends_fails(monkeypatch):
    """Same fallback wiring for the 12-month momentum signal (not just durability)."""
    monkeypatch.setenv("SERPAPI_KEY", "fake-key")
    monkeypatch.setattr(trends_mod, "_pytrends_series", lambda *a, **k: (None, None, None))

    # Rising slope across the 12-month window so slope/breakout scores are > 0.
    series = [10.0] * 6 + [40.0] * 6
    payload = _canned_serpapi_payload(series)
    monkeypatch.setattr(trends_mod.requests, "get", lambda *a, **k: _FakeResponse(payload))

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/c.sqlite")
        score, detail = trends_score("solar generators", geo="US", cache=cache, throttle=False)
        assert detail["status"] == "ok"
        assert detail["backend"] == "serpapi"
        assert score is not None and score > 0.0
        # No comparison terms were passed and pytrends never ran, so baseline/rising features
        # are unavailable via SerpApi and degrade to their no-signal defaults, not an exception.
        assert detail["rising_queries"] == 0.0
        assert detail["rising_terms"] == []
        cache.close()


# --- pytrends happy path: exactly ONE interest_over_time() fetch per momentum computation ---


def test_pytrends_momentum_fetch_calls_interest_over_time_exactly_once(monkeypatch):
    """Baseline columns must reuse the already-fetched DataFrame.

    pytrends issues a fresh HTTP request on every interest_over_time() call, and its 429
    fragility is the whole reason the fallback exists — so the momentum happy path must make
    exactly one fetch, with baseline comparison columns read from that same DataFrame.
    """
    pd = pytest.importorskip("pandas")
    pytrends_request = pytest.importorskip("pytrends.request")

    calls = {"iot": 0}
    frame = pd.DataFrame({
        "solar generators": [20.0] * 12,
        "personal finance": [80.0] * 12,
    })

    class FakeTrendReq:
        def __init__(self, *a, **k):
            pass

        def build_payload(self, terms, timeframe=None, gprop=None, geo=None):
            pass

        def interest_over_time(self):
            calls["iot"] += 1
            return frame

        def related_queries(self):
            return {}

    monkeypatch.setattr(pytrends_request, "TrendReq", FakeTrendReq)
    # SerpApi (and any other HTTP) must never be touched on the pytrends happy path.
    monkeypatch.setattr(
        trends_mod.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HTTP expected")),
    )

    score, detail = trends_score(
        "solar generators", geo="US", cache=None, throttle=False,
        baseline_terms=["personal finance"],
    )
    assert detail["status"] == "ok"
    assert detail["backend"] == "pytrends"
    assert calls["iot"] == 1  # the regression guard: one fetch, reused for baseline columns
    # level_score proves the baseline column was read from the carried DataFrame:
    # own mean 20 vs baseline mean 80 -> ratio 0.25 -> saturating(0.25, 1.0) = 0.2
    # (without the baseline it would degrade to saturating(20, 35) ~= 0.36).
    assert detail["level_score"] == 0.2
    assert detail["baseline_terms"] == ["personal finance"]
    assert score is not None and score > 0.0


# --- both backends fail -> graceful None/degraded result, no exception ---


def test_durability_score_both_backends_fail_returns_none(monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "fake-key")
    monkeypatch.setattr(trends_mod, "_pytrends_series", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(
        trends_mod.requests, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError())
    )

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/c.sqlite")
        score, detail = durability_score("obscure term", geo="US", cache=cache, throttle=False)
        assert score is None
        assert detail["status"] == "insufficient data"
        cache.close()


def test_trends_score_both_backends_fail_returns_none(monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "fake-key")
    monkeypatch.setattr(trends_mod, "_pytrends_series", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(
        trends_mod.requests, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError())
    )

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/c.sqlite")
        score, detail = trends_score("obscure term", geo="US", cache=cache, throttle=False)
        assert score is None
        assert detail["status"] == "error: no data from any backend"
        cache.close()


def test_durability_score_no_pytrends_installed_and_no_serpapi_key(monkeypatch):
    """Belt-and-suspenders: even a raw ImportError-shaped failure degrades cleanly."""
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    monkeypatch.setattr(trends_mod, "_pytrends_series", lambda *a, **k: (None, None, None))

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/c.sqlite")
        score, detail = durability_score("obscure term", geo="US", cache=cache, throttle=False)
        assert score is None
        assert detail["status"] == "insufficient data"
        cache.close()


# --- no SERPAPI_KEY configured + pytrends failing -> SerpApi never called at all ---


def test_no_serpapi_key_means_serpapi_never_called(monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    monkeypatch.setattr(trends_mod, "_pytrends_series", lambda *a, **k: (None, None, None))

    calls = []
    monkeypatch.setattr(
        trends_mod.requests, "get", lambda *a, **k: calls.append((a, k)) or _FakeResponse({})
    )

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/c.sqlite")
        score, detail = trends_score("obscure term", geo="US", cache=cache, throttle=False)
        assert score is None
        assert calls == []  # no HTTP request was made — SerpApi path was never entered
        cache.close()


def test_no_serpapi_key_means_durability_serpapi_never_called(monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    monkeypatch.setattr(trends_mod, "_pytrends_series", lambda *a, **k: (None, None, None))

    calls = []
    monkeypatch.setattr(
        trends_mod.requests, "get", lambda *a, **k: calls.append((a, k)) or _FakeResponse({})
    )

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/c.sqlite")
        score, detail = durability_score("obscure term", geo="US", cache=cache, throttle=False)
        assert score is None
        assert calls == []
        cache.close()


# --- cache hits never touch the network, and never print the fallback line ---


def test_cached_durability_result_skips_fetch_entirely(monkeypatch, capsys):
    monkeypatch.setenv("SERPAPI_KEY", "fake-key")

    def boom(*a, **k):
        raise AssertionError("fetch should not be called on a cache hit")

    monkeypatch.setattr(trends_mod, "_pytrends_series", boom)
    monkeypatch.setattr(trends_mod.requests, "get", boom)

    with tempfile.TemporaryDirectory() as d:
        cache = Cache(f"{d}/c.sqlite")
        cache.set(
            cache.key("durability", "cached term", "US", "youtube"),
            {"score": 0.8, "detail": {"status": "ok", "backend": "serpapi"}},
        )
        score, detail = durability_score("cached term", geo="US", cache=cache)
        assert score == 0.8 and detail["cached"] is True
        cache.close()

    # No fallback line printed for a cache hit — only on a live fetch.
    assert "[trends]" not in capsys.readouterr().out
