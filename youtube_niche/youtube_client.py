"""Thin, quota-aware, cached wrapper over the YouTube Data API v3.

Design rules:
- Track search.list calls separately from the general unit pool.
- videos/channels/commentThreads cost 1 general unit (batched 50/call).
- Cache every response to disk so re-runs don't re-spend quota.
- Track units spent per day and refuse calls that would exceed the budget.
"""
from __future__ import annotations

import datetime as dt
import time

import requests

from .cache import Cache

API = "https://www.googleapis.com/youtube/v3"


class QuotaExceeded(RuntimeError):
    """Raised when a call would exceed the day's unit budget, or Google reports quota exhaustion."""


class CacheMiss(RuntimeError):
    """Raised in cache-only mode when a requested response is not already cached."""


class APIError(RuntimeError):
    """Non-retryable client error (e.g. comments disabled, forbidden, not found)."""


class YouTubeClient:
    def __init__(
        self,
        auth,
        cache: Cache,
        daily_quota: int = 10000,
        reserve: int = 200,
        cache_ttl: float = 7 * 86400,
        verbose: bool = True,
        daily_search_limit: int = 100,
        cache_only: bool = False,
    ):
        if auth is None and not cache_only:
            raise ValueError("An auth strategy (ApiKeyAuth or OAuthAuth) is required.")
        self.auth = auth
        self.cache = cache
        self.daily_quota = daily_quota
        self.reserve = reserve
        self.cache_ttl = cache_ttl
        self.verbose = verbose
        self.daily_search_limit = daily_search_limit
        self.cache_only = cache_only
        self.session = requests.Session()
        self.cache.conn.execute(
            "CREATE TABLE IF NOT EXISTS quota (day TEXT PRIMARY KEY, units INTEGER, search_calls INTEGER DEFAULT 0)"
        )
        cols = {r[1] for r in self.cache.conn.execute("PRAGMA table_info(quota)").fetchall()}
        if "search_calls" not in cols:
            self.cache.conn.execute("ALTER TABLE quota ADD COLUMN search_calls INTEGER DEFAULT 0")
        self.cache.conn.commit()

    # ------------------------------------------------------------------ quota
    def _today(self) -> str:
        return dt.date.today().isoformat()

    def units_spent(self) -> int:
        row = self.cache.conn.execute(
            "SELECT units FROM quota WHERE day=?", (self._today(),)
        ).fetchone()
        return row[0] if row else 0

    def search_calls_used(self) -> int:
        row = self.cache.conn.execute(
            "SELECT search_calls FROM quota WHERE day=?", (self._today(),)
        ).fetchone()
        return row[0] if row else 0

    def units_remaining(self) -> int:
        return self.daily_quota - self.reserve - self.units_spent()

    def search_calls_remaining(self) -> int:
        return self.daily_search_limit - self.search_calls_used()

    def _charge(self, endpoint: str, units: int) -> None:
        cur = self.units_spent()
        cur_search = self.search_calls_used()
        if endpoint == "search":
            cur_search += 1
        else:
            cur += max(units, 1)
        self.cache.conn.execute(
            "INSERT OR REPLACE INTO quota (day, units, search_calls) VALUES (?, ?, ?)",
            (self._today(), cur, cur_search),
        )
        self.cache.conn.commit()

    def _guard(self, endpoint: str, units: int) -> None:
        if endpoint == "search":
            if self.search_calls_remaining() < 1:
                raise QuotaExceeded(
                    f"Need 1 search call but only {self.search_calls_remaining()} remain today."
                )
            return
        if self.units_remaining() < max(units, 1):
            raise QuotaExceeded(
                f"Need {units} units but only {self.units_remaining()} remain in today's budget."
            )

    # -------------------------------------------------------------- transport
    def _get(self, endpoint: str, params: dict, cost: int) -> dict:
        # Cache key excludes the API key so a re-run with a rotated key still hits cache.
        ck = self.cache.key("yt", endpoint, {k: v for k, v in sorted(params.items())})
        cached = self.cache.get(ck, max_age=self.cache_ttl)
        if cached is not None:
            return cached
        if self.cache_only:
            raise CacheMiss(f"cache miss for YouTube {endpoint}: {params}")

        url = f"{API}/{endpoint}"
        for attempt in range(4):
            self._guard(endpoint, cost)
            req_params = dict(params)
            headers: dict = {}
            self.auth.apply(req_params, headers)  # adds ?key= or Authorization: Bearer
            r = self.session.get(url, params=req_params, headers=headers, timeout=30)

            # A 401 we're about to refresh+retry was rejected pre-billing — don't charge it.
            if r.status_code == 401 and hasattr(self.auth, "invalidate") and attempt == 0:
                self.auth.invalidate()
                continue

            reason = self._reason(r) if r.status_code != 200 else ""
            is_quota = r.status_code in (403, 429) and (
                "quota" in reason.lower() or "dailylimit" in reason.lower()
            )
            # Rate-limits (429) and server errors (5xx) are NOT billed by Google and are
            # retryable — retry WITHOUT charging so a flaky API can't burn the daily budget.
            if not is_quota and r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if not is_quota and 500 <= r.status_code < 600:
                time.sleep(2 * (attempt + 1))
                continue

            # Terminal outcome (success, or a billed/non-retryable error): charge exactly once.
            self._charge(endpoint, cost)
            if r.status_code == 200:
                data = r.json()
                self.cache.set(ck, data)
                return data
            if is_quota:
                raise QuotaExceeded(f"Google reports quota exhausted: {reason}")
            if r.status_code == 401:
                raise APIError(f"auth failed (401): {reason}")
            if r.status_code in (403, 404):
                # commentsDisabled, insufficientPermissions, notFound, forbidden: not retryable
                raise APIError(reason)
            r.raise_for_status()
        raise RuntimeError(f"YouTube API failed after retries: {endpoint}")

    @staticmethod
    def _reason(r: requests.Response) -> str:
        try:
            return r.json()["error"]["errors"][0]["reason"]
        except Exception:
            return r.text[:200]

    # -------------------------------------------------------------- endpoints
    def search(
        self,
        q: str,
        max_results: int = 20,
        order: str = "relevance",
        published_after: str | None = None,
        published_before: str | None = None,
        region: str | None = None,
        relevance_language: str | None = None,
    ) -> dict:
        params = {
            "part": "snippet",
            "q": q,
            "type": "video",
            "maxResults": min(max_results, 50),
            "order": order,
        }
        if published_after:
            params["publishedAfter"] = published_after
        if published_before:
            params["publishedBefore"] = published_before
        if region:
            params["regionCode"] = region
        if relevance_language:
            params["relevanceLanguage"] = relevance_language
        return self._get("search", params, cost=100)

    def videos(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), 50):
            chunk = ids[i : i + 50]
            data = self._get(
                "videos",
                {"part": "statistics,snippet,contentDetails", "id": ",".join(chunk)},
                cost=1,
            )
            for item in data.get("items", []):
                out[item["id"]] = item
        return out

    def channels(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), 50):
            chunk = ids[i : i + 50]
            data = self._get(
                "channels",
                {"part": "statistics,snippet", "id": ",".join(chunk)},
                cost=1,
            )
            for item in data.get("items", []):
                out[item["id"]] = item
        return out

    def comment_threads(self, video_id: str, pages: int = 2, per_page: int = 100) -> list[dict]:
        items: list[dict] = []
        page_token: str | None = None
        for _ in range(pages):
            params = {
                "part": "snippet",
                "videoId": video_id,
                "maxResults": min(per_page, 100),
                "order": "relevance",
                "textFormat": "plainText",
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                data = self._get("commentThreads", params, cost=1)
            except APIError:
                break  # comments disabled / not found — just no demand signal here
            items.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items
