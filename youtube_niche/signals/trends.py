"""Signal F — Google Trends demand proxy (source = YouTube search).

Trends is the closest free proxy for *latent / rising* demand — distinct from the
views-of-existing-videos demand the other signals measure. It is unofficial and rate-limits
hard, so we:
  - cache successful results to disk (7-day TTL) so each unique term is fetched at most once,
  - throttle live calls (min interval between them),
  - return None on failure so confidence reflects the gap rather than crashing.
"""
from __future__ import annotations

import time

from ..util import clamp01, saturating

TRENDS_TTL = 7 * 86400  # trends move slowly; a week-old read is fine
_MIN_INTERVAL_S = 1.5   # spacing between live Trends calls (politeness vs rate limits)
_last_call = [0.0]      # module-level throttle state


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
        from pytrends.request import TrendReq
    except ImportError:
        return None, {"status": "pytrends not installed"}

    if throttle:
        wait = _MIN_INTERVAL_S - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()

    try:
        py = TrendReq(hl="en-US", tz=360, timeout=(10, 25))
        py.build_payload(terms or [term], timeframe="today 12-m", gprop="youtube", geo=geo)

        slope_score = 0.0
        level_score = 0.0
        breakout_score = 0.0
        iot = py.interest_over_time()
        if iot is not None and not iot.empty and term in iot:
            vals = [float(x) for x in iot[term]]
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
            baseline_cols = [t for t in terms[1:] if t in iot]
            baseline_vals = []
            for col in baseline_cols:
                baseline_vals.extend(float(x) for x in iot[col])
            if baseline_vals:
                baseline_mean = (sum(baseline_vals) / len(baseline_vals)) or 1.0
                level_score = saturating(own_mean / baseline_mean, 1.0)
            else:
                level_score = saturating(own_mean, 35.0)

        rising_score = 0.0
        rising_terms: list[str] = []
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
        }
    except Exception as e:
        return None, {"status": f"error: {type(e).__name__}"}
