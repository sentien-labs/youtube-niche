"""Signal E — comment-demand mining.

Find literal demand requests ("please make a video on X", "can someone explain Y") in the
comments of top videos. Keyword pre-filter is free; an LLM (if available) refines it.
"""
from __future__ import annotations

from ..util import saturating

REQUEST_HINTS = (
    "make a video",
    "please make",
    "can you make",
    "could you make",
    "do a video",
    "wish someone",
    "anyone know",
    "how do i",
    "how do you",
    "can someone explain",
    "please explain",
    "need a tutorial",
    "is there a video",
    "where can i learn",
    "help me understand",
    "i don't understand",
    "still confused",
)


def keyword_demand(comments: list[str]) -> list[str]:
    return [c for c in comments if any(h in c.lower() for h in REQUEST_HINTS)]


def comment_demand_score(comments: list[str], llm=None, knee: float = 8.0):
    """comments: list of comment strings. Returns (score in [0,1], detail)."""
    if not comments:
        return 0.0, {"n_comments": 0, "n_requests": 0, "method": "none", "examples": []}

    kw_hits = keyword_demand(comments)
    requests_found = kw_hits
    method = "keyword"

    if llm is not None and getattr(llm, "enabled", False):
        # Feed keyword hits + a sample of the rest, so the LLM can both confirm and discover.
        sample = list(dict.fromkeys(kw_hits + comments[:40]))
        labeled = llm.classify_comment_demand(sample)
        if labeled is not None:
            requests_found = [x["text"] for x in labeled if x["is_request"]]
            method = "llm"

    n_req = len(requests_found)
    return saturating(n_req, knee), {
        "n_comments": len(comments),
        "n_requests": n_req,
        "method": method,
        "examples": requests_found[:5],
    }
