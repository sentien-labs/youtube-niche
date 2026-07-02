"""LLM signals (E comment-demand mining, G depth scoring) with pluggable backends.

Backends:
  - AnthropicBackend : the `anthropic` SDK (needs ANTHROPIC_API_KEY)
  - CodexCliBackend  : shells out to `codex exec` (OpenAI; uses its own auth)  [verified]
  - ClaudeCliBackend : shells out to `claude -p` (Anthropic CLI auth)
  - AgyCliBackend    : shells out to `agy -p` (Google/Gemini CLI auth)
  - GrokCliBackend   : shells out to `grok -p` (xAI/Grok CLI auth)

Every backend exposes `complete_json(system, user, tier)` and returns parsed JSON or None.
The whole thing degrades gracefully: no working backend -> LLM.enabled is False -> signals skip.

Failover: `LLM` tries an ordered chain of backends — the configured/primary one first, then the
remaining available providers in a fixed order (agy -> codex -> claude -> grok -> anthropic) — so a
backend that silently returns empty output (as `agy` did on 2026-06-30, exit 0 with empty stdout)
doesn't take the whole run down with it. `LLM.enabled` is chain-aware: a configured-but-missing
primary binary with working fallbacks still counts as enabled (the user asked for an LLM and one
exists), while a keyless/binary-less machine stays disabled (quiet keyword path). Opt out with env
`LLM_FALLBACK=0`. See `LLM._call_chain`.

Important boundary: the Grok CLI backend is only an LLM/reasoning backend. It does not provide
native X/Twitter data or xAI's API `x_search` tool. If X momentum becomes a scoring signal, keep it
as a separate API-backed signal module rather than routing it through this generic LLM layer.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable

LLM_PROVIDERS = ["auto", "anthropic", "codex", "claude", "agy", "grok"]

# Fallback order when the primary backend fails: agy/codex/claude are the CLI backends most
# likely to be logged in already; grok often needs separate setup; anthropic needs an API key
# (usage-billed, so it's last — a deliberate cost/reliability tradeoff, not a quality judgment).
_FALLBACK_ORDER = ["agy", "codex", "claude", "grok", "anthropic"]


# --------------------------------------------------------------------- backends
class AnthropicBackend:
    name = "anthropic"

    def __init__(self, api_key: str | None, cheap_model: str, quality_model: str):
        self.models = {"cheap": cheap_model, "quality": quality_model}
        self._client = None
        self.available = False
        if api_key:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=api_key)
                self.available = True
            except ImportError:
                pass

    def complete_json(self, system: str, user: str, tier: str = "cheap", max_tokens: int = 512):
        if not self.available:
            return None
        try:
            msg = self._client.messages.create(
                model=self.models[tier],
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            return _extract_json(text)
        except Exception as e:
            print(f"  [llm:anthropic] {type(e).__name__}: {e}")
            return None


class CodexCliBackend:
    """`codex exec` — writes the final message to a file via -o (parse-clean, no echo/preamble)."""

    name = "codex"

    def __init__(self, bin: str = "codex", model: str | None = None, efforts: dict | None = None, timeout: int = 240):
        self.bin = bin
        self.model = model
        self.efforts = efforts or {"cheap": "low", "quality": "low"}
        self.timeout = timeout
        self.available = shutil.which(bin) is not None
        # Run in an empty dir so the agentic CLI has no project to explore — just answers.
        self.workdir = tempfile.mkdtemp(prefix="yn-codex-") if self.available else None

    def complete_json(self, system: str, user: str, tier: str = "cheap", max_tokens=None):
        if not self.available:
            return None
        prompt = f"{system}\n\n{user}"
        fd, path = tempfile.mkstemp(suffix=".json", dir=self.workdir)
        os.close(fd)
        cmd = [
            self.bin, "exec", "--skip-git-repo-check", "--ephemeral",
            "-c", f"model_reasoning_effort={self.efforts.get(tier, 'low')}",
            "-o", path,
        ]
        if self.model:
            cmd += ["-m", self.model]
        cmd.append(prompt)
        try:
            subprocess.run(
                cmd, cwd=self.workdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=self.timeout, check=False,
            )
            with open(path) as f:
                return _extract_json(f.read())
        except Exception as e:
            print(f"  [llm:codex] {type(e).__name__}: {e}")
            return None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class _StdoutCliBackend:
    """Shared impl for CLIs that print the answer to stdout (`claude -p`, `agy -p`, `grok -p`)."""

    name = "stdout-cli"
    flag = "-p"

    def __init__(
        self,
        bin: str,
        models: dict | None = None,
        timeout: int = 240,
        extra_args: list[str] | None = None,
    ):
        self.bin = bin
        self.models = models or {}
        self.timeout = timeout
        self.extra_args = extra_args or []
        self.available = shutil.which(bin) is not None
        # Empty cwd: these CLIs are agentic and will explore the project otherwise.
        self.workdir = tempfile.mkdtemp(prefix="yn-cli-") if self.available else None

    def complete_json(self, system: str, user: str, tier: str = "cheap", max_tokens=None):
        if not self.available:
            return None
        prompt = self._format_prompt(system, user)
        cmd = [self.bin, *self.extra_args, self.flag, prompt]
        model = self.models.get(tier)
        if model:
            cmd += ["--model", model]
        try:
            r = subprocess.run(
                cmd, cwd=self.workdir, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=self.timeout,
            )
            return _extract_json(r.stdout)
        except Exception as e:
            print(f"  [llm:{self.name}] {type(e).__name__}: {e}")
            return None

    def _format_prompt(self, system: str, user: str) -> str:
        return f"{system}\n\n{user}"


class ClaudeCliBackend(_StdoutCliBackend):
    name = "claude"

    def __init__(self, bin: str = "claude", models: dict | None = None, timeout: int = 240):
        super().__init__(bin, models, timeout)


class AgyCliBackend(_StdoutCliBackend):
    name = "agy"

    def __init__(self, bin: str = "agy", models: dict | None = None, timeout: int = 300):
        super().__init__(bin, models, timeout)


class GrokCliBackend(_StdoutCliBackend):
    name = "grok"

    def __init__(self, bin: str = "grok", models: dict | None = None, timeout: int = 300):
        super().__init__(
            bin,
            models,
            timeout,
            extra_args=[
                "--no-memory",
                "--no-auto-update",
                "--disable-web-search",
                "--no-subagents",
                "--no-plan",
                "--permission-mode",
                "bypassPermissions",
            ],
        )
        if self.available and self.workdir:
            self.extra_args += ["--cwd", self.workdir]

    def _format_prompt(self, system: str, user: str) -> str:
        # Grok CLI's single-turn mode can emit empty stdout for multi-line prompts; a flattened
        # prompt with this prefix reliably preserves parseable JSON output.
        compact_system = re.sub(r"\s+", " ", system).strip()
        compact_user = re.sub(r"\s+", " ", user).strip()
        return f"Reply only with JSON: System: {compact_system} User: {compact_user}"


# ------------------------------------------------------------------------- LLM
def _fallback_enabled() -> bool:
    """Env opt-out: LLM_FALLBACK=0 disables the chain (primary backend only). Default on."""
    val = os.environ.get("LLM_FALLBACK")
    if val is None:
        return True
    return val.strip().lower() not in {"0", "false", "no", "off"}


class LLM:
    def __init__(self, backend=None, fallback_builders: list[tuple[str, Callable]] | None = None):
        """
        backend: the primary backend (already constructed; may be unavailable).
        fallback_builders: ordered [(name, zero-arg builder)] tried, lazily, after the primary
            fails. Each builder is only invoked if that fallback is actually needed.

        `enabled` is chain-aware: True when the primary is usable OR when fallbacks exist and
        the chain is on (LLM_FALLBACK != 0). make_llm pre-filters fallback_builders by
        availability, so a machine with no keys and no CLI binaries still gets enabled=False
        (callers like winners.discover_niches keep their quiet keyword path), while a machine
        whose CONFIGURED provider binary is missing but has working alternatives stays enabled
        and fails over instead of silently degrading (the 2026-06-30 incident class). Evaluated
        at construction time — the env doesn't change mid-run.
        """
        self.backend = backend
        self._fallback_builders = list(fallback_builders or [])
        self._fallback_cache: dict[str, object] = {}
        self.last_provider: str | None = None
        self.enabled = bool(backend and getattr(backend, "available", False)) or (
            bool(self._fallback_builders) and _fallback_enabled()
        )

    def _chain(self):
        """Yield available backends in try-order: primary first, then lazily-built fallbacks."""
        if self.backend is not None and getattr(self.backend, "available", False):
            yield self.backend
        if not _fallback_enabled():
            return
        for name, builder in self._fallback_builders:
            if self.backend is not None and getattr(self.backend, "name", None) == name:
                continue  # already tried as primary
            backend = self._fallback_cache.get(name)
            if backend is None:
                backend = builder()
                self._fallback_cache[name] = backend
            if getattr(backend, "available", False):
                yield backend

    def _call_chain(self, method_name: str, *args, **kwargs):
        """Call `method_name(*args, **kwargs)` on each backend in the chain until one returns a
        non-empty result. Records `last_provider` on success.

        Diagnostics contract (the daily health check greps these lines, so they must never lie):
          - a fallback is announced only at the moment it is actually about to be called
            ("falling back to <name>"), so the name is always real — never a provider that was
            already tried or one that is unavailable;
          - every empty/failed hop gets its own warning, with no claim about what comes next
            (the old "trying <next>" phrasing could name an already-consumed provider);
          - exhaustion ends with the "all providers" line.
        """
        tried_any = False
        for backend in self._chain():
            if tried_any:
                print(f"  [llm] falling back to {backend.name}")
            tried_any = True
            result = getattr(backend, method_name)(*args, **kwargs)
            if result:
                self.last_provider = backend.name
                return result
            print(f"  [llm] WARNING: {backend.name} returned empty")
        if tried_any:
            print("  [llm] WARNING: all providers returned empty/failed")
        return None

    def classify_comment_demand(self, comments: list[str]):
        """Label which comments express UNMET viewer demand. Returns [{text, is_request}] or None."""
        if not self.enabled or not comments:
            return None
        sample = comments[:60]
        numbered = "\n".join(f"{i}. {c[:200]}" for i, c in enumerate(sample))
        system = (
            "You analyze YouTube comments to find UNMET VIEWER DEMAND: comments asking for a "
            "video/topic that isn't covered, expressing confusion the video left unresolved, or "
            "requesting a tutorial/explanation. Generic praise or off-topic chatter is NOT demand. "
            "Reply ONLY with JSON."
        )
        user = (
            f"Comments:\n{numbered}\n\n"
            'Return JSON: {"requests": [<indices of comments that express unmet demand>]}'
        )
        out = self._call_chain("complete_json", system, user, tier="cheap", max_tokens=512)
        if not out:
            return None
        idx = {i for i in out.get("requests", []) if isinstance(i, int)}
        return [{"text": sample[i], "is_request": i in idx} for i in range(len(sample))]

    def score_depth(self, topic: str, transcript: str):
        """Score how thorough/high-quality a video is from its transcript. Returns {depth, reason} or None."""
        if not self.enabled or not transcript:
            return None
        snippet = transcript[:8000]
        system = (
            "You judge how thorough and high-quality a YouTube video is for its topic, from its "
            "transcript alone. 0.0 = shallow, clickbait, thin, or padding; 1.0 = comprehensive, "
            "expert, the definitive treatment. Reply ONLY with JSON."
        )
        user = (
            f'Topic: "{topic}"\nTranscript (may be truncated):\n{snippet}\n\n'
            'Return JSON: {"depth": <0.0-1.0>, "reason": "<one short sentence>"}'
        )
        out = self._call_chain("complete_json", system, user, tier="quality", max_tokens=256)
        if not out:
            return None
        try:
            depth = float(out.get("depth"))
        except (TypeError, ValueError):
            return None
        return {"depth": max(0.0, min(1.0, depth)), "reason": str(out.get("reason", ""))}

    def extract_niches(self, titles: list[str], max_niches: int = 20):
        """Read searchable niche topics off the titles of breakout videos. Returns [str] or None."""
        if not self.enabled or not titles:
            return None
        numbered = "\n".join(f"- {t[:120]}" for t in titles[:80])
        system = (
            "You are a YouTube niche strategist. You are given titles of videos that recently "
            "earned high views from SMALL channels — i.e. proven, winnable demand. Identify the "
            "specific, searchable niche TOPICS they represent: each is the phrase a viewer would "
            "type into YouTube search (2-5 words), specific not generic ('backdoor roth ira', not "
            "'investing'). Merge near-duplicates. Reply ONLY with JSON."
        )
        user = (
            f"Breakout video titles:\n{numbered}\n\n"
            f'Return JSON: {{"topics": [<up to {max_niches} specific niche search phrases>]}}'
        )
        out = self._call_chain("complete_json", system, user, tier="quality", max_tokens=1024)
        if not out:
            return None
        topics = [t.strip() for t in out.get("topics", []) if isinstance(t, str) and t.strip()]
        return topics[:max_niches] or None

    def hypothesis_statement(
        self,
        topic: str,
        matching_titles: list[str],
        comment_questions: list[str] | None = None,
    ) -> str | None:
        """One-sentence "I help [X] do/overcome [Y]" positioning hypothesis for a niche.

        X = a specific audience, Y = a concrete felt outcome or pain — grounded in the breakout
        titles that actually matched this niche (+ up to 5 real viewer questions, when available).
        No mechanism/method clause ("with Z") and never "everyone": this is a promise a creator
        could say out loud in one breath, not a content plan. Report-only, nice-to-have — callers
        must degrade silently on any failure (see winners.py), same spirit as the other LLM signals
        but with no health-check warnings, since a missing hypothesis is not an outage.

        Routed through the same failover chain as every other signal here (`_call_chain`). Returns
        the hypothesis string, or None if the LLM is disabled, every backend fails, or the reply
        doesn't parse into a valid "I help ..." sentence under 160 chars.
        """
        if not self.enabled or not topic or not matching_titles:
            return None
        titles = "\n".join(f"- {t[:120]}" for t in matching_titles[:8])
        system = (
            "You are a YouTube positioning strategist. Given a niche topic and titles of breakout "
            "videos that just proved demand for it, write ONE hypothesis sentence a creator could "
            "say out loud in one breath: \"I help [X] do/overcome [Y]\" where X is a SPECIFIC "
            "audience (never 'everyone') and Y is a CONCRETE felt outcome or pain — not a mechanism "
            "or method. Do NOT add a 'with <tool/method>' clause. Reply ONLY with JSON."
        )
        user_parts = [f'Niche topic: "{topic}"', f"Breakout video titles:\n{titles}"]
        questions = [q.strip() for q in (comment_questions or []) if isinstance(q, str) and q.strip()]
        if questions:
            numbered_q = "\n".join(f"- {q[:160]}" for q in questions[:5])
            user_parts.append(f"Real viewer questions:\n{numbered_q}")
        user_parts.append('Return JSON: {"hypothesis": "I help ... "}')
        user = "\n\n".join(user_parts)
        out = self._call_chain("complete_json", system, user, tier="cheap", max_tokens=200)
        if not out:
            return None
        hypothesis = out.get("hypothesis")
        if not isinstance(hypothesis, str):
            return None
        hypothesis = hypothesis.strip()
        if not hypothesis.lower().startswith("i help "):
            return None
        if len(hypothesis) >= 160:
            return None
        return hypothesis


def _is_available(name: str, cfg) -> bool:
    """Cheap pre-construction availability probe, so we don't build backends we'll never use.
    CLI backends: binary on PATH. anthropic: an API key is set."""
    if name == "anthropic":
        return bool(getattr(cfg, "anthropic_api_key", None))
    bins = {"codex": getattr(cfg, "codex_bin", "codex") or "codex", "claude": "claude",
            "agy": "agy", "grok": "grok"}
    bin_name = bins.get(name)
    return bool(bin_name and shutil.which(bin_name))


def _provider_builders(cfg) -> dict[str, Callable]:
    grok_default_model = getattr(cfg, "grok_model", None)
    grok_models = {
        "cheap": getattr(cfg, "grok_comment_model", None) or grok_default_model,
        "quality": getattr(cfg, "grok_quality_model", None) or grok_default_model,
    }
    return {
        "anthropic": lambda: AnthropicBackend(
            cfg.anthropic_api_key, cfg.llm_comment_model, cfg.llm_quality_model
        ),
        "codex": lambda: CodexCliBackend(bin=getattr(cfg, "codex_bin", "codex")),
        "claude": lambda: ClaudeCliBackend(),
        "agy": lambda: AgyCliBackend(
            models={"cheap": "Gemini 3.5 Flash (Low)", "quality": "Gemini 3.1 Pro (Low)"}
        ),
        "grok": lambda: GrokCliBackend(models={k: v for k, v in grok_models.items() if v}),
    }


def make_llm(cfg) -> LLM:
    """Build an LLM from cfg.llm_provider, with an ordered failover chain behind it.

    Primary = the explicitly configured provider (or auto's pick, unchanged: anthropic SDK if a
    key is present, else the codex CLI). Fallbacks = the remaining providers in fixed order
    agy -> codex -> claude -> grok -> anthropic, filtered to those actually available, and
    excluding whichever one is already primary. Fallback backends are NOT constructed here —
    only their (name, builder) pairs are recorded; `LLM._chain` builds one lazily the first time
    it's actually needed. Disable the whole chain with env LLM_FALLBACK=0.
    """
    provider = (getattr(cfg, "llm_provider", "auto") or "auto").lower()
    builders = _provider_builders(cfg)

    if provider != "auto" and provider not in builders:
        print(f"  [llm] unknown provider {provider!r}; using auto")
        provider = "auto"

    if provider == "auto":
        # Prefer the SDK if a key is present, else the verified codex CLI.
        anthro = builders["anthropic"]()
        primary_name = "anthropic" if anthro.available else "codex"
        primary = anthro if anthro.available else builders["codex"]()
    else:
        primary_name = provider
        primary = builders[provider]()

    fallback_builders = [
        (name, builders[name]) for name in _FALLBACK_ORDER
        if name != primary_name and _is_available(name, cfg)
    ]
    return LLM(primary, fallback_builders=fallback_builders)


def _extract_json(text: str | None):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None
