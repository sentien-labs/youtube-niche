"""Configuration: API keys (from env/.env), quota budget, scan params, thresholds, weights."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency). Does not override already-exported vars."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Weights:
    """Scoring weights. Optional raw-score axes renormalize; confidence records missing evidence."""

    # top-level opportunity axes
    demand: float = 0.35
    supply_gap: float = 0.30
    monetization: float = 0.20
    quality_gap: float = 0.15
    # demand sub-weights — demand is absolute interest ONLY. The outlier ratio is excluded
    # (it's beatability, not demand). `newcomer_volume` = what small channels actually achieve
    # here, the realistic ceiling for a new creator; `trends` = latent/rising interest.
    volume: float = 0.25          # overall median view velocity
    newcomer_volume: float = 0.22  # views/day small channels get here (newcomer ceiling)
    p75_volume: float = 0.15      # top-of-niche ceiling
    recent_demand: float = 0.15   # proven-recent successful uploads
    trends: float = 0.10          # rising/latent interest (cached, reliable)
    comment_demand: float = 0.05
    external_demand: float = 0.08  # optional imported keyword-volume/RPM provider data
    outlier: float = 0.0  # unused in demand; kept for reference
    # supply-gap sub-weights
    competition: float = 0.35
    authority: float = 0.15
    recent_supply: float = 0.25
    small_channel: float = 0.20
    supply_age: float = 0.05


@dataclass
class Config:
    # YouTube auth: an API key OR OAuth credential paths. API key wins if both are set.
    youtube_api_key: str | None = None
    youtube_oauth_client_secret: str | None = None
    youtube_oauth_token: str | None = None
    anthropic_api_key: str | None = None

    # --- quota (search calls/day + general units/day) ---
    daily_quota_units: int = 10000
    daily_search_limit: int = 100
    quota_reserve: int = 200  # never spend below this, leaves headroom

    # --- per-topic scan params ---
    top_n: int = 30          # search results scanned per seed
    enrich_n: int = 30       # of those, how many to pull stats for (<= 50)
    query_samples: int = 1   # search-query variants per topic; >1 reduces single-search noise
    comment_videos: int = 5  # top videos to mine comments from (signal E)
    comment_pages: int = 2   # comment-thread pages per video
    quality_videos: int = 3  # top videos to depth-score (signal G)

    # --- thresholds ---
    small_channel_subs: int = 50000  # "beatable" channel size (signal D)
    outlier_knee: float = 1.0        # views/subs ratio that maps to 0.5 (signal A)
    competition_knee: float = 30.0   # credible-result count knee (signal C)
    age_knee_days: float = 365.0     # median-age knee (signal B)
    min_view_floor: int = 1000       # ignore near-zero-view noise
    volume_knee_vpd: float = 500.0
    recent_days: int = 180
    recent_success_knee: float = 4.0
    recent_supply_knee: float = 8.0
    min_small_channel_vpd: float = 50.0
    min_relevant_results: int = 3
    cpm_full_scale: float = 40.0

    # --- winners-first discovery ---
    winner_recent_days: int = 180      # only count videos published this recently as "breakouts"
    winner_min_vpd: float = 100.0      # min views/day for a small-channel video to count as a breakout
    winner_max_per_term: int = 8       # cap breakouts kept per seed term

    # --- locale ---
    region_code: str = "US"
    relevance_language: str = "en"
    trends_geo: str = "US"

    # --- LLM provider for signals E, G ---
    # 'auto' = anthropic SDK if ANTHROPIC_API_KEY set, else the codex CLI.
    # Or force: 'anthropic' | 'codex' | 'claude' | 'agy' (CLIs use their own auth).
    llm_provider: str = "auto"
    codex_bin: str = "codex"
    # Models used only by the anthropic SDK backend:
    llm_comment_model: str = "claude-haiku-4-5-20251001"
    llm_quality_model: str = "claude-sonnet-4-6"

    # --- seed expansion ---
    max_seeds: int = 20
    alphabet_soup: bool = False

    # --- feature toggles ---
    use_trends: bool = True
    use_llm: bool = True

    # --- io ---
    cache_path: str = ".cache/youtube_niche.sqlite"
    out_dir: str = "out"
    cache_only: bool = False
    keyword_metrics_csv: str | None = None

    weights: Weights = field(default_factory=Weights)

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        load_dotenv()
        cfg = cls(
            youtube_api_key=os.environ.get("YOUTUBE_API_KEY"),
            youtube_oauth_client_secret=os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET"),
            youtube_oauth_token=os.environ.get("YOUTUBE_OAUTH_TOKEN"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )
        cfg.llm_provider = os.environ.get("LLM_PROVIDER", cfg.llm_provider)
        cfg.daily_search_limit = _env_int("YOUTUBE_DAILY_SEARCH_LIMIT", cfg.daily_search_limit)
        cfg.region_code = os.environ.get("YOUTUBE_REGION_CODE", cfg.region_code)
        cfg.relevance_language = os.environ.get("YOUTUBE_RELEVANCE_LANGUAGE", cfg.relevance_language)
        cfg.trends_geo = os.environ.get("TRENDS_GEO", cfg.trends_geo)
        cfg.volume_knee_vpd = _env_float("VOLUME_KNEE_VPD", cfg.volume_knee_vpd)
        cfg.query_samples = _env_int("QUERY_SAMPLES", cfg.query_samples)
        cfg.cache_only = _env_bool("CACHE_ONLY", cfg.cache_only)
        cfg.keyword_metrics_csv = os.environ.get("KEYWORD_METRICS_CSV")
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        cfg.query_samples = max(1, int(cfg.query_samples))
        return cfg

    def per_topic_unit_estimate(self) -> int:
        """Rough non-search units one topic costs: videos + channels + comment pages."""
        return (2 * self.query_samples) + (self.comment_videos * self.comment_pages)

    def per_topic_search_estimate(self) -> int:
        return self.query_samples
