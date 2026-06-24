"""Curated registry of high-CPM YouTube domains for the domain scan (stage 1).

CPM is NOT available from the YouTube API — these ranges are rough US figures from public
industry data (advertiser demand varies wildly by geo/season/audience). Treat them as tiers,
not truth, and edit freely. The point is to start the search from domains already known to
monetize well, then let the live demand/supply data rank them.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Domain:
    name: str
    terms: list[str]          # representative search probes (stage 1: domain scoring)
    cpm_low: float            # approx USD CPM range (industry estimate)
    cpm_high: float
    note: str = ""
    subtopics: list[str] = field(default_factory=list)  # stage 2: niche drill-down seeds
    # Per-domain demand knee (views/day -> 0.5), calibrated from this domain's small-channel
    # scale via `python -m youtube_niche.calibrate`. None = use the global config knee.
    volume_knee_vpd: float | None = None

    @property
    def cpm_mid(self) -> float:
        return (self.cpm_low + self.cpm_high) / 2

    @property
    def cpm_tier(self) -> str:
        m = self.cpm_mid
        if m >= 20:
            return "very high"
        if m >= 12:
            return "high"
        if m >= 6:
            return "medium"
        return "low"


DOMAINS: list[Domain] = [
    Domain(
        "Personal finance / investing",
        ["personal finance", "investing for beginners", "index funds", "budgeting tips", "retirement planning"],
        12, 30, "Classic top-CPM; dense advertiser demand, but crowded.",
        subtopics=[
            # specific corners first — where gaps are likeliest
            "backdoor roth ira", "mega backdoor roth", "hsa investing", "coast fire",
            "house hacking", "tax loss harvesting", "i bonds explained",
            "treasury bills for beginners", "roth conversion ladder", "solo 401k explained",
            "covered call etf", "dividend growth investing", "529 plan explained",
            "self employed retirement", "rental property analysis", "real estate syndication",
            "annuities explained", "sequence of returns risk", "finance for nurses",
            "finance for physicians", "sinking funds budgeting", "debt snowball method",
            "high yield savings explained", "net worth tracking",
        ],
        volume_knee_vpd=100.0,  # calibrated: finance small channels' p75 ~100 views/day
    ),
    Domain(
        "Insurance",
        ["life insurance explained", "health insurance guide", "insurance for self employed",
         "term vs whole life insurance", "how to lower car insurance"],
        20, 50, "Among the highest CPMs on the platform.",
        subtopics=[
            "term life insurance for parents", "life insurance for diabetics",
            "health insurance for freelancers", "hsa vs ppo explained",
            "medicare advantage vs supplement", "long term disability insurance",
            "umbrella insurance explained", "whole life insurance cash value",
            "self employed health insurance tax deduction", "car insurance after accident",
            "renters insurance explained", "business liability insurance for consultants",
        ],
    ),
    Domain(
        "Crypto / DeFi",
        ["crypto for beginners", "defi explained", "altcoin analysis", "bitcoin investing",
         "crypto tax explained"],
        8, 25, "High CPM but demand swings with the market.",
        subtopics=[
            "crypto taxes for beginners", "defi yield farming risks", "hardware wallet setup",
            "bitcoin self custody", "ethereum staking explained", "stablecoin yield risks",
            "crypto portfolio tracking", "solana defi tutorial", "airdrop farming guide",
            "crypto security checklist", "bitcoin etf explained", "defi taxes explained",
        ],
    ),
    Domain(
        "AI / AI tools",
        ["ai tools for business", "how to use chatgpt", "ai automation", "build ai agents",
         "ai video editing"],
        6, 18, "Fast-rising interest; CPM climbing as B2B adopts.",
        subtopics=[
            "ai automation for real estate agents", "chatgpt for accountants",
            "ai agents for small business", "zapier ai automation tutorial",
            "ai customer support chatbot", "ai video repurposing workflow",
            "local business ai automation", "ai lead generation workflow",
            "claude projects for business", "chatgpt custom gpts for teams",
            "ai bookkeeping automation", "ai proposal writing workflow",
        ],
        volume_knee_vpd=230.0,  # calibrated p75 small-channel velocity, 2026-06-24
    ),
    Domain(
        "Business / make money online",
        ["start an online business", "make money online", "freelancing tips", "ecommerce 2026",
         "passive income ideas"],
        10, 25, "High CPM, very crowded — gaps are in sub-topics.",
        subtopics=[
            "productized service business", "b2b cold email agency", "freelance retainer pricing",
            "newsletter sponsorship business", "digital product validation",
            "etsy digital products taxes", "shopify subscription box business",
            "local lead generation business", "consulting offer creation",
            "micro saas ideas", "online course presell", "service business systems",
        ],
        volume_knee_vpd=136.0,  # calibrated p75 small-channel velocity, 2026-06-24
    ),
    Domain(
        "Digital marketing / SaaS",
        ["digital marketing strategy", "seo tutorial", "email marketing", "saas growth",
         "content marketing strategy"],
        10, 22, "B2B advertiser demand keeps CPM high.",
        subtopics=[
            "saas onboarding email sequence", "b2b lead scoring", "crm migration checklist",
            "linkedin ads for b2b", "product led growth metrics", "seo for dentists",
            "local seo for lawyers", "email deliverability setup", "hubspot workflow tutorial",
            "ga4 conversion tracking", "content refresh strategy", "saas churn reduction",
        ],
    ),
    Domain(
        "Real estate",
        ["real estate investing", "how to buy rental property", "real estate agent tips",
         "first time home buyer guide", "real estate market 2026"],
        10, 20, "High CPM, evergreen, regionally fragmented.",
        subtopics=[
            "house hacking fha loan", "dscr loan explained", "rental property analysis spreadsheet",
            "section 8 rental investing", "short term rental regulations", "first time home buyer grants",
            "real estate syndication fees", "cash out refinance rental", "1031 exchange explained",
            "property management for beginners", "buying duplex as first home", "subject to real estate",
        ],
    ),
    Domain(
        "Software / programming",
        ["learn python", "web development 2026", "system design interview", "devops tutorial",
         "learn javascript"],
        5, 14, "Big audience but saturated by large channels.",
        subtopics=[
            "python automation for accountants", "fastapi deployment tutorial",
            "postgres indexing explained", "system design for data engineers",
            "terraform for beginners", "github actions ci cd", "kubernetes cost optimization",
            "react server components tutorial", "python uv package manager",
            "observability for startups", "api rate limiting design", "sql query optimization",
        ],
    ),
    Domain(
        "Health / fitness",
        ["home workout plan", "nutrition for muscle", "weight loss science",
         "intermittent fasting explained", "best supplements for beginners"],
        5, 12, "YMYL; moderate CPM, trust-sensitive.",
        subtopics=[
            "strength training after 40", "high protein meal prep for beginners",
            "zone 2 cardio explained", "creatine for women", "mobility routine for desk workers",
            "walking for weight loss plan", "sleep tracking explained", "menopause strength training",
            "beginner hypertrophy program", "nutrition for night shift workers",
            "meal prep for insulin resistance", "injury prevention for runners",
        ],
    ),
]
