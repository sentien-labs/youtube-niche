"""Signal G — content-quality / depth gap.

Pull transcripts of the top-ranking videos and ask the LLM how thorough they are. Thin top
content is a real opening even when view counts look healthy. quality_gap = 1 - avg_depth.
"""
from __future__ import annotations

from ..transcript import fetch_transcript
from ..util import clamp01


def quality_gap_score(videos: list[dict], topic: str, llm, max_videos: int = 3):
    """Returns (gap in [0,1] or None, detail). None when no LLM or no transcripts could be scored."""
    if llm is None or not getattr(llm, "enabled", False):
        return None, {"status": "llm disabled"}

    depths = []
    per_video = []
    for v in videos[:max_videos]:
        tr = fetch_transcript(v["video_id"])
        if not tr:
            per_video.append({"video_id": v["video_id"], "depth": None, "note": "no transcript"})
            continue
        res = llm.score_depth(topic, tr)
        if res is None:
            per_video.append({"video_id": v["video_id"], "depth": None, "note": "score failed"})
            continue
        depths.append(res["depth"])
        per_video.append(
            {"video_id": v["video_id"], "depth": res["depth"], "reason": res.get("reason", "")}
        )

    if not depths:
        return None, {"status": "no transcripts scored", "videos": per_video}

    avg_depth = sum(depths) / len(depths)
    return clamp01(1.0 - avg_depth), {
        "status": "ok",
        "avg_depth": round(avg_depth, 2),
        "videos": per_video,
    }
