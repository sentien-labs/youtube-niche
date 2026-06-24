"""Transcript fetch for signal G. Tolerant of youtube-transcript-api version differences."""
from __future__ import annotations


def fetch_transcript(video_id: str, languages: tuple[str, ...] = ("en",)) -> str | None:
    """Return the full transcript text, or None if unavailable (no captions, blocked, etc.)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None

    # youtube-transcript-api >= 1.0: instance .fetch() returning snippet objects.
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=list(languages))
        return " ".join(seg.text for seg in fetched).strip() or None
    except Exception:
        pass

    # Older versions: static .get_transcript() returning list of dicts.
    try:
        segs = YouTubeTranscriptApi.get_transcript(video_id, languages=list(languages))
        return " ".join(s["text"] for s in segs).strip() or None
    except Exception:
        return None
