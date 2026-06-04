"""LLM abstraction.

A tiny interface so swapping providers is one file. Two implementations
ship out of the box:

  - `AnthropicLLM` — Claude (Sonnet/Opus/Haiku). Has prompt caching.
  - `GeminiLLM`    — Google Gemini (2.5 Flash / 2.5 Pro). Free tier
                     available at https://aistudio.google.com/apikey.

`build_llm` picks the provider by inspecting the model name prefix
(`claude-*` → Anthropic, `gemini-*` → Gemini). Adding OpenAI is one more
dataclass below.

Why an interface and not just one SDK call directly?
- Reviewers can swap providers without rewriting stage code.
- Tests can inject a fake LLM that returns canned responses.
- The Tracer attaches once here, so every stage gets free instrumentation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.tracing import Tracer


# Pricing per 1M tokens. Used for cost tracking in traces only.
# Free-tier models keep zeros so the cost line stays accurate.
_PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00, "cache_read": 0.30},
    "claude-opus-4-7":            {"input": 15.00, "output": 75.00, "cache_read": 1.50},
    "claude-haiku-4-5-20251001":  {"input": 1.00,  "output": 5.00,  "cache_read": 0.10},
    # Gemini paid-tier prices, in case you upgrade. Free tier = $0.
    "gemini-2.5-flash":           {"input": 0.30,  "output": 2.50,  "cache_read": 0.075},
    "gemini-2.5-pro":             {"input": 1.25,  "output": 10.00, "cache_read": 0.31},
    "gemini-2.0-flash":           {"input": 0.10,  "output": 0.40,  "cache_read": 0.025},
}


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    cost_usd: float = 0.0
    raw: Any = None


class LLM(Protocol):
    """The interface every stage talks to.

    `system` is the system prompt (cached when the provider supports it).
    `messages` is a list of {"role": "user"|"assistant", "content": str}.
    """

    def complete(
        self,
        *,
        stage: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse: ...


# ─── Anthropic ────────────────────────────────────────────────────────────

@dataclass
class AnthropicLLM:
    model: str
    tracer: Tracer
    client: Any = field(default=None, repr=False)

    # Class-level so the pause persists across the multiple AnthropicLLM
    # instances a single run might create. Tracks the wall-clock time of
    # the most recent Anthropic call.
    _last_call_at: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        try:
            from anthropic import Anthropic  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "anthropic SDK not installed. `pip install anthropic`."
            ) from e
        self.client = Anthropic()  # picks up ANTHROPIC_API_KEY from env

    def _maybe_sleep_for_rate_limit(self) -> None:
        """Pause if the previous Anthropic call was very recent.

        Claude Sonnet free-tier accounts allow only 30k input tokens per
        minute. Our Edit calls are ~10k tokens each, so 3 back-to-back
        Edits can exceed the budget and trigger 429s / socket drops.
        Sleeping 60s between calls keeps any run well under the limit at
        the cost of slower wall-clock time per run. Good trade for
        reliability on a tightly-rate-limited account.
        """
        import time
        if self._last_call_at == 0.0:
            return
        elapsed = time.time() - self._last_call_at
        wait = 60.0 - elapsed
        if wait > 0:
            self.tracer.event("rate_limit.pause", {"seconds": round(wait, 1)})
            time.sleep(wait)

    def complete(
        self, *, stage: str, system: str, messages: list[dict[str, Any]],
        max_tokens: int = 4096, temperature: float = 0.0,
    ) -> LLMResponse:
        self._maybe_sleep_for_rate_limit()
        system_blocks = [{
            "type": "text", "text": system,
            "cache_control": {"type": "ephemeral"},
        }]
        resp = self.client.messages.create(
            model=self.model, system=system_blocks, messages=messages,
            max_tokens=max_tokens, temperature=temperature,
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        usage = resp.usage
        in_tok = getattr(usage, "input_tokens", 0)
        out_tok = getattr(usage, "output_tokens", 0)
        cached = (getattr(usage, "cache_read_input_tokens", 0)
                  + getattr(usage, "cache_creation_input_tokens", 0))
        cost = _estimate_cost(self.model, in_tok, out_tok, cached)
        self.tracer.llm_call(
            stage=stage, model=self.model,
            input_tokens=in_tok, output_tokens=out_tok,
            cached_tokens=cached, cost_usd=cost,
            prompt=messages[-1]["content"] if messages and isinstance(messages[-1]["content"], str) else "",
            response=text,
        )
        import time
        AnthropicLLM._last_call_at = time.time()
        return LLMResponse(text=text, input_tokens=in_tok, output_tokens=out_tok,
                           cached_tokens=cached, cost_usd=cost, raw=resp)


# ─── OpenAI-compatible (NVIDIA NIM, OpenRouter, DeepSeek direct, OpenAI) ──

@dataclass
class OpenAICompatibleLLM:
    """One adapter for any OpenAI-API-compatible endpoint.

    NVIDIA NIM, OpenRouter, DeepSeek's direct API, and OpenAI itself all
    speak the same wire protocol. This class is parameterised so we can
    point at any of them. The factory below picks the right base_url +
    env var based on the model name prefix.

    `extra_body` forwards through to the SDK so backend-specific knobs
    (DeepSeek's `chat_template_kwargs.thinking`, OpenRouter's transforms,
    etc.) work without modifying the call sites.
    """
    model: str
    tracer: Tracer
    base_url: str
    api_key_env: str
    provider_label: str = "openai-compat"
    extra_body: dict[str, Any] | None = None
    client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "openai SDK not installed. `pip install openai`."
            ) from e
        key = os.environ.get(self.api_key_env)
        if not key:
            raise RuntimeError(
                f"{self.api_key_env} not set. Required for {self.provider_label} "
                f"(model {self.model})."
            )
        self.client = OpenAI(base_url=self.base_url, api_key=key)

    def complete(
        self, *, stage: str, system: str, messages: list[dict[str, Any]],
        max_tokens: int = 4096, temperature: float = 0.0,
    ) -> LLMResponse:
        # Combine system + messages — OpenAI puts system in the messages list.
        full_messages = [{"role": "system", "content": system}, *messages]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": full_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": {"type": "json_object"},  # all our stages emit JSON
        }
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        resp = self.client.chat.completions.create(**kwargs)

        text = resp.choices[0].message.content or ""
        usage = resp.usage
        in_tok = getattr(usage, "prompt_tokens", 0) or 0
        out_tok = getattr(usage, "completion_tokens", 0) or 0
        # OpenAI-compat usage doesn't have a standard cached-tokens field.
        cached = 0
        cost = _estimate_cost(self.model, in_tok, out_tok, cached)
        self.tracer.llm_call(
            stage=stage, model=self.model,
            input_tokens=in_tok, output_tokens=out_tok,
            cached_tokens=cached, cost_usd=cost,
            prompt=messages[-1]["content"] if messages and isinstance(messages[-1]["content"], str) else "",
            response=text,
        )
        return LLMResponse(text=text, input_tokens=in_tok, output_tokens=out_tok,
                           cached_tokens=cached, cost_usd=cost, raw=resp)


# ─── Gemini ───────────────────────────────────────────────────────────────

@dataclass
class GeminiLLM:
    """Google Gemini via the google-genai SDK.

    Free tier is available at https://aistudio.google.com/apikey (no card
    required). Reads the key from $GEMINI_API_KEY or $GOOGLE_API_KEY.

    Gemini has implicit context caching on the paid tier for prompts >=
    ~32k tokens. On the free tier there is no automatic cache, but the
    free per-day budget is generous enough that this doesn't matter for
    a take-home demo.
    """
    model: str
    tracer: Tracer
    client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            from google import genai  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "google-genai SDK not installed. `pip install google-genai`."
            ) from e
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise RuntimeError("Set GEMINI_API_KEY or GOOGLE_API_KEY in your environment.")
        self.client = genai.Client(api_key=key)

    def complete(
        self, *, stage: str, system: str, messages: list[dict[str, Any]],
        max_tokens: int = 4096, temperature: float = 0.0,
    ) -> LLMResponse:
        # Gemini's API takes `contents` as a flat list of messages. We map
        # our {role,content} pairs to its expected shape. There's no
        # separate "system" field on the contents list — instead it goes
        # in the GenerationConfig as `system_instruction`.
        contents = []
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            text = m["content"] if isinstance(m["content"], str) else str(m["content"])
            contents.append({"role": role, "parts": [{"text": text}]})

        from google.genai import types as gtypes  # type: ignore
        # Gemini 2.5 series defaults to "thinking" ON, which spends output
        # tokens before the visible response starts. We need predictable
        # structured JSON, not chain-of-thought, so we disable thinking and
        # rely on the response_mime_type to enforce valid JSON. This also
        # makes runs noticeably faster and cheaper.
        thinking_cfg = None
        if self.model.startswith("gemini-2.5"):
            try:
                thinking_cfg = gtypes.ThinkingConfig(thinking_budget=0)
            except (AttributeError, TypeError):
                thinking_cfg = None  # older SDK without thinking_config
        cfg = gtypes.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
            thinking_config=thinking_cfg,
        )
        resp = self.client.models.generate_content(
            model=self.model, contents=contents, config=cfg,
        )

        text = getattr(resp, "text", "") or ""
        usage = getattr(resp, "usage_metadata", None)
        # Gemini returns None (not 0) when a count is absent — coerce.
        in_tok = (getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
        out_tok = (getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
        cached = (getattr(usage, "cached_content_token_count", 0) or 0) if usage else 0
        cost = _estimate_cost(self.model, in_tok, out_tok, cached)
        self.tracer.llm_call(
            stage=stage, model=self.model,
            input_tokens=in_tok, output_tokens=out_tok,
            cached_tokens=cached, cost_usd=cost,
            prompt=messages[-1]["content"] if messages and isinstance(messages[-1]["content"], str) else "",
            response=text,
        )
        return LLMResponse(text=text, input_tokens=in_tok, output_tokens=out_tok,
                           cached_tokens=cached, cost_usd=cost, raw=resp)


# ─── Factory ──────────────────────────────────────────────────────────────

def _estimate_cost(model: str, in_tok: int, out_tok: int, cached_tok: int) -> float:
    p = _PRICING_USD_PER_MTOK.get(model)
    if not p:
        return 0.0
    return (
        (in_tok - cached_tok) * p["input"]
        + cached_tok * p["cache_read"]
        + out_tok * p["output"]
    ) / 1_000_000


def build_llm(*, model: str, tracer: Tracer) -> LLM:
    """Pick a provider by inspecting the model name prefix.

    - `claude-*`       → Anthropic   (needs ANTHROPIC_API_KEY)
    - `gemini-*`       → Gemini      (needs GEMINI_API_KEY or GOOGLE_API_KEY)
    - `deepseek-ai/*`  → NVIDIA NIM  (needs NVIDIA_API_KEY)  [OpenAI-compatible]
    - `gpt-*`          → OpenAI      (needs OPENAI_API_KEY)  [OpenAI-compatible]

    Adding a fourth provider is one branch + (for OpenAI-compatible ones)
    one OpenAICompatibleLLM construction.
    """
    if model.startswith("claude-"):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Either set it, or pick a Gemini "
                "model (e.g. --model gemini-2.5-flash) and set GEMINI_API_KEY. "
                "Free Gemini key: https://aistudio.google.com/apikey"
            )
        return AnthropicLLM(model=model, tracer=tracer)

    if model.startswith("gemini-"):
        return GeminiLLM(model=model, tracer=tracer)

    if model.startswith("deepseek-ai/"):
        # NVIDIA NIM hosts deepseek models. Disable DeepSeek's "thinking"
        # mode for structured-JSON output (matches the user-supplied
        # reference snippet).
        return OpenAICompatibleLLM(
            model=model, tracer=tracer,
            base_url="https://integrate.api.nvidia.com/v1",
            api_key_env="NVIDIA_API_KEY",
            provider_label="NVIDIA NIM",
            extra_body={"chat_template_kwargs": {"thinking": False}},
        )

    if model.startswith("groq/"):
        # Groq's API is OpenAI-compatible but expects the bare model name
        # (no `groq/` prefix). Strip it before sending.
        return OpenAICompatibleLLM(
            model=model[len("groq/"):],
            tracer=tracer,
            base_url="https://api.groq.com/openai/v1",
            api_key_env="GROQ_API_KEY",
            provider_label="Groq",
        )

    if model.startswith("gpt-"):
        return OpenAICompatibleLLM(
            model=model, tracer=tracer,
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            provider_label="OpenAI",
        )

    raise ValueError(
        f"unknown model '{model}'. Use a name starting with "
        f"'claude-', 'gemini-', 'deepseek-ai/', 'groq/', or 'gpt-'."
    )
