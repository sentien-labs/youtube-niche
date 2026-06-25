"""LLM signals (E comment-demand mining, G depth scoring) with pluggable backends.

Backends:
  - AnthropicBackend : the `anthropic` SDK (needs ANTHROPIC_API_KEY)
  - CodexCliBackend  : shells out to `codex exec` (OpenAI; uses its own auth)  [verified]
  - ClaudeCliBackend : shells out to `claude -p` (Anthropic CLI auth)
  - AgyCliBackend    : shells out to `agy -p` (Google/Gemini CLI auth)
  - GrokCliBackend   : shells out to `grok -p` (xAI/Grok CLI auth)

Every backend exposes `complete_json(system, user, tier)` and returns parsed JSON or None.
The whole thing degrades gracefully: no working backend -> LLM.enabled is False -> signals skip.

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

LLM_PROVIDERS = ["auto", "anthropic", "codex", "claude", "agy", "grok"]


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
class LLM:
    def __init__(self, backend=None):
        self.backend = backend
        self.enabled = bool(backend and getattr(backend, "available", False))

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
        out = self.backend.complete_json(system, user, tier="cheap", max_tokens=512)
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
        out = self.backend.complete_json(system, user, tier="quality", max_tokens=256)
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
        out = self.backend.complete_json(system, user, tier="quality", max_tokens=1024)
        if not out:
            return None
        topics = [t.strip() for t in out.get("topics", []) if isinstance(t, str) and t.strip()]
        return topics[:max_niches] or None


def make_llm(cfg) -> LLM:
    """Build an LLM from cfg.llm_provider."""
    provider = (getattr(cfg, "llm_provider", "auto") or "auto").lower()

    grok_default_model = getattr(cfg, "grok_model", None)
    grok_models = {
        "cheap": getattr(cfg, "grok_comment_model", None) or grok_default_model,
        "quality": getattr(cfg, "grok_quality_model", None) or grok_default_model,
    }

    builders = {
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

    if provider != "auto" and provider not in builders:
        print(f"  [llm] unknown provider {provider!r}; using auto")
        provider = "auto"

    if provider == "auto":
        # Prefer the SDK if a key is present, else the verified codex CLI.
        anthro = builders["anthropic"]()
        return LLM(anthro if anthro.available else builders["codex"]())
    return LLM(builders[provider]())


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
