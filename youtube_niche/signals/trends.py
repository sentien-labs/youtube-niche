"""Signal F — Google Trends demand proxy (source = YouTube search).

Trends is the closest free proxy for *latent / rising* demand — distinct from the
views-of-existing-videos demand the other signals measure. It is unofficial and rate-limits
hard, so we:
  - cache successful results to disk (7-day TTL) so each unique term is fetched at most once,
  - throttle live calls (min interval between them),
  - return None on failure so confidence reflects the gap rather than crashing.

Fetch backends: `pytrends` (unofficial, archived upstream April 2025, chronic 429s) is tried
first; if it fails AND a SerpApi key is configured (`SERPAPI_KEY`), we fall back to SerpApi's
`google_trends` engine (plain `requests` — no new dependency) so one upstream break doesn't
silently kill both Trends signals. Both backends normalize to the same plain list-of-floats
chronological series before any scoring math runs, so the math itself never knows which
backend produced the data.
"""
from __future__ import annotations

import os
import time

import requests

from ..util import clamp01, saturating

TRENDS_TTL = 7 * 86400  # trends move slowly; a week-old read is fine
DURABILITY_TTL = 30 * 86400  # a 5-year base barely moves week to week; a month-old read is fine
_MIN_INTERVAL_S = 1.5   # spacing between live Trends calls (politeness vs rate limits)
_last_call = [0.0]      # module-level throttle state

SERPAPI_URL = "https://serpapi.com/search.json"


def _throttle() -> None:
    wait = _MIN_INTERVAL_S - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def _pytrends_series(term: str, terms: list[str], timeframe: str, gprop: str, geo: str):
    """Build a pytrends payload; return (series for `term`, live TrendReq, iot DataFrame).

    Returns (None, None, None) on any failure (import, network, empty/missing column) so callers
    can fall back to SerpApi. The TrendReq object and the interest_over_time DataFrame are both
    returned for reuse: pytrends issues a fresh HTTP request on EVERY interest_over_time() call,
    so `_compute` must read baseline comparison columns from this same DataFrame rather than
    re-fetching, and it needs the TrendReq for `related_queries()` (a different endpoint with
    no SerpApi analog here).
    """
    try:
        from pytrends.request import TrendReq
    except ImportError:
        return None, None, None
    try:
        py = TrendReq(hl="en-US", tz=360, timeout=(10, 30))
        py.build_payload(terms or [term], timeframe=timeframe, gprop=gprop, geo=geo)
        iot = py.interest_over_time()
        vals = ([float(x) for x in iot[term]]
                if iot is not None and not iot.empty and term in iot else [])
        return (vals or None), py, iot
    except Exception:
        return None, None, None


def _serpapi_series(term: str, timeframe: str, gprop: str, geo: str, api_key: str):
    """Fetch a single-term interest-over-time series from SerpApi's google_trends engine.

    Returns a chronological list of floats, or None on any failure (network, bad payload,
    missing key). Swallows all exceptions — this is a best-effort fallback, never a hard
    dependency.
    """
    params = {
        "engine": "google_trends",
        "q": term,
        "data_type": "TIMESERIES",
        "date": timeframe,
        "api_key": api_key,
    }
    if gprop:
        params["gprop"] = gprop
    if geo:
        params["geo"] = geo
    try:
        r = requests.get(SERPAPI_URL, params=params, timeout=(10, 30))
        r.raise_for_status()
        data = r.json()
        timeline = (data.get("interest_over_time") or {}).get("timeline_data") or []
        vals: list[float] = []
        for point in timeline:
            values = point.get("values") or []
            if not values:
                continue
            v0 = values[0]
            raw = v0.get("extracted_value")
            if raw is None:
                raw = v0.get("value")
            if raw is None:
                continue
            vals.append(float(raw))
        return vals or None
    except Exception:
        return None


def _interest_series(
    term: str,
    timeframe: str,
    *,
    gprop: str,
    geo: str,
    terms: list[str] | None = None,
    throttle: bool = True,
):
    """Chronological interest series for `term`, trying pytrends then SerpApi.

    Returns (series | None, py | None, iot | None, detail). `py` is the live pytrends TrendReq
    object and `iot` its ALREADY-FETCHED interest_over_time DataFrame when the pytrends path
    succeeded — callers reuse `iot` for baseline comparison columns (re-calling
    interest_over_time() would issue a second HTTP request against the 429-fragile endpoint)
    and `py` for `related_queries()`. Both are None when pytrends failed or SerpApi served the
    request. `detail` records which backend answered (or that both failed); it never raises.
    """
    if throttle:
        _throttle()

    vals, py, iot = _pytrends_series(term, terms or [term], timeframe, gprop, geo)
    if vals is not None:
        return vals, py, iot, {"backend": "pytrends"}

    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        return None, None, None, {"backend": "none"}

    vals = _serpapi_series(term, timeframe, gprop, geo, api_key)
    if vals is not None:
        print("  [trends] pytrends failed; using SerpApi fallback")
        return vals, None, None, {"backend": "serpapi"}
    return None, None, None, {"backend": "none"}


def _durability_from_series(vals: list[float]):
    """Recent-year vs early-year average over a multi-year series -> (score in [0,1], ratio).

    Answers a different question than `trends_score`'s 12-month momentum: is the theme on a
    structurally RISING base (durable vein) or a fading/flat one (likely flash)? Scale-invariant
    per term, so it survives Trends' 0-100 normalization. ratio 1.0 (flat) -> 0.5; 2.0 -> 1.0; 0 -> 0.
    """
    if not vals or len(vals) < 8:
        return None, None
    yr = min(52, len(vals) // 2)  # ~52 weekly points = a year; shorter series -> use each half
    early = (sum(vals[:yr]) / yr) or 1.0
    recent = sum(vals[-yr:]) / yr
    ratio = recent / early
    return clamp01(0.5 + (ratio - 1.0) * 0.5), ratio


def durability_label(score: float | None) -> str:
    """Human flag for a durability score: rising base, fading base, or neither/unknown."""
    if score is None:
        return ""
    if score > 0.6:   # recent year >= ~20% above the early year
        return "📈 durable"
    if score < 0.4:   # recent year >= ~20% below the early year
        return "⚠️ fading"
    return ""


def durability_score(term, geo: str = "US", cache=None, ttl: float = DURABILITY_TTL,
                     throttle: bool = True, gprop: str = "youtube", live: bool = True):
    """Multi-year (5y) structural slope for one term. Returns (score in [0,1] or None, detail).

    Distinct from `trends_score` (12-month momentum) — this is the long-run durability filter
    that separates durable veins from one-week flashes. Cached 30 days (the base moves slowly).
    """
    clean = " ".join(str(term).split())
    ck = cache.key("durability", clean.lower(), geo, gprop) if cache is not None else None
    if cache is not None:
        hit = cache.get(ck, max_age=ttl)
        if hit is not None:
            return hit.get("score"), {**hit.get("detail", {}), "cached": True}
    if not live:
        return None, {"status": "no cached durability"}

    try:
        vals, _py, _iot, fetch_detail = _interest_series(
            clean, "today 5-y", gprop=gprop, geo=geo, throttle=throttle,
        )
        vals = vals or []
        score, ratio = _durability_from_series(vals)
        detail = {
            "status": "ok" if score is not None else "insufficient data",
            "durability_ratio": round(ratio, 2) if ratio is not None else None,
            "points": len(vals),
            "gprop": gprop,
            "backend": fetch_detail.get("backend"),
        }
        if cache is not None and score is not None:
            cache.set(ck, {"score": score, "detail": detail})
        return score, detail
    except Exception as e:
        return None, {"status": f"error: {type(e).__name__}"}


def _clean_terms(term: str, baseline_terms: list[str] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in [term, *(baseline_terms or [])]:
        clean = " ".join(str(t).split())
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            out.append(clean)
        if len(out) >= 5:  # Google Trends accepts at most five comparison terms.
            break
    return out


def trends_score(
    term: str,
    geo: str = "US",
    cache=None,
    ttl: float = TRENDS_TTL,
    throttle: bool = True,
    baseline_terms: list[str] | None = None,
):
    """Returns (score in [0,1] or None, detail). Caches successful results when `cache` is given."""
    terms = _clean_terms(term, baseline_terms)
    primary = terms[0] if terms else term
    ck = None
    if cache is not None:
        if terms[1:]:
            ck = cache.key("trends", primary.lower().strip(), geo, [t.lower() for t in terms[1:]])
        else:
            ck = cache.key("trends", primary.lower().strip(), geo)
        hit = cache.get(ck, max_age=ttl)
        if hit is not None:
            return hit.get("score"), {**hit.get("detail", {}), "cached": True}

    score, detail = _compute(primary, geo, throttle, terms)
    # Only cache real results — let transient failures (rate-limit) retry on the next run.
    if cache is not None and detail.get("status") == "ok":
        cache.set(ck, {"score": score, "detail": detail})
    return score, detail


def _compute(term: str, geo: str, throttle: bool, terms: list[str]):
    try:
        vals, py, iot, fetch_detail = _interest_series(
            term, "today 12-m", gprop="youtube", geo=geo, terms=terms, throttle=throttle,
        )
        if vals is None:
            return None, {"status": "error: no data from any backend"}

        slope_score = 0.0
        level_score = 0.0
        breakout_score = 0.0
        if len(vals) >= 8:
            half = len(vals) // 2
            first = sum(vals[:half]) or 1.0
            second = sum(vals[half:])
            ratio = second / first
            slope_score = clamp01((ratio - 0.8) / 1.2)  # 0.8x interest -> 0, 2.0x -> 1
            recent = vals[-max(4, len(vals) // 4):]
            prior = vals[:-len(recent)] or vals
            breakout_score = clamp01(((sum(recent) / len(recent)) / ((sum(prior) / len(prior)) or 1.0) - 0.9) / 1.1)

        own_mean = sum(vals) / len(vals) if vals else 0.0
        baseline_vals: list[float] = []
        # Baseline (comparison-term) columns reuse the DataFrame `_interest_series` already
        # fetched — calling py.interest_over_time() again would issue a SECOND HTTP request
        # (pytrends fetches per call), doubling pressure on the 429-fragile endpoint. SerpApi
        # is queried single-term here, so baseline comparison degrades gracefully (own_mean
        # vs a fixed knee) when pytrends didn't serve this request.
        try:
            if iot is not None and not iot.empty:
                baseline_cols = [t for t in terms[1:] if t in iot]
                for col in baseline_cols:
                    baseline_vals.extend(float(x) for x in iot[col])
        except Exception:
            pass
        if baseline_vals:
            baseline_mean = (sum(baseline_vals) / len(baseline_vals)) or 1.0
            level_score = saturating(own_mean / baseline_mean, 1.0)
        else:
            level_score = saturating(own_mean, 35.0)

        rising_score = 0.0
        rising_terms: list[str] = []
        if py is not None:
            try:
                rq = py.related_queries().get(term, {}) or {}
                rising = rq.get("rising")
                if rising is not None and not rising.empty:
                    rising_score = clamp01(len(rising) / 15.0)
                    if "query" in rising:
                        rising_terms = [str(x) for x in rising["query"].head(8).tolist()]
            except Exception:
                pass

        score = clamp01(
            0.40 * slope_score
            + 0.25 * rising_score
            + 0.20 * level_score
            + 0.15 * breakout_score
        )
        return score, {
            "status": "ok",
            "slope_score": round(slope_score, 2),
            "level_score": round(level_score, 2),
            "breakout_score": round(breakout_score, 2),
            "rising_queries": round(rising_score, 2),
            "rising_terms": rising_terms,
            "baseline_terms": terms[1:],
            "backend": fetch_detail.get("backend"),
        }
    except Exception as e:
        return None, {"status": f"error: {type(e).__name__}"}
