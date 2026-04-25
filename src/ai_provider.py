"""Unified AI provider abstraction: Anthropic, Google Gemini, Groq."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Beschikbare modellen per provider
PROVIDER_MODELS: dict[str, list[dict]] = {
    "anthropic": [
        {"value": "claude-opus-4-7",   "label": "Claude Opus 4.7 — beste kwaliteit (betaald)"},
        {"value": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 — goede balans (betaald)"},
        {"value": "claude-haiku-4-5",  "label": "Claude Haiku 4.5 — snel & goedkoop (betaald)"},
    ],
    "google": [
        {"value": "gemini-2.0-flash",      "label": "Gemini 2.0 Flash — aanbevolen (gratis tier)"},
        {"value": "gemini-2.0-flash-lite", "label": "Gemini 2.0 Flash Lite — snelst (gratis tier)"},
        {"value": "gemini-1.5-flash",      "label": "Gemini 1.5 Flash (gratis tier)"},
    ],
    "groq": [
        {"value": "llama-3.3-70b-versatile", "label": "Llama 3.3 70B — beste kwaliteit (gratis)"},
        {"value": "llama-3.1-8b-instant",    "label": "Llama 3.1 8B — snelst (gratis)"},
        {"value": "mixtral-8x7b-32768",      "label": "Mixtral 8x7B (gratis)"},
    ],
}

_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-opus-4-7",
    "google":    "gemini-2.0-flash",
    "groq":      "llama-3.3-70b-versatile",
}


def get_active() -> tuple[str, str]:
    """Geeft (provider, model) terug op basis van de huidige omgevingsvariabelen."""
    provider = os.getenv("AI_PROVIDER", "anthropic").lower()
    model    = os.getenv("AI_MODEL", "").strip() or _DEFAULT_MODEL.get(provider, "")
    return provider, model


def complete(system: str, user: str, max_tokens: int = 2048) -> str:
    """
    Stuurt een verzoek naar de geconfigureerde AI provider.
    Geeft de tekst-response terug als string.
    """
    provider, model = get_active()
    logger.debug("AI provider=%s model=%s max_tokens=%d", provider, model, max_tokens)

    if provider == "anthropic":
        return _anthropic(system, user, model, max_tokens)
    if provider == "google":
        return _google(system, user, model, max_tokens)
    if provider == "groq":
        return _groq(system, user, model, max_tokens)
    raise ValueError(f"Onbekende AI provider: {provider!r}")


# ── Anthropic ────────────────────────────────────────────────────────────────

def _anthropic(system: str, user: str, model: str, max_tokens: int) -> str:
    import anthropic  # type: ignore

    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise EnvironmentError("ANTHROPIC_API_KEY niet ingesteld")

    client = anthropic.Anthropic(api_key=key)
    kwargs: dict = dict(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    # Adaptive thinking voor Opus en Sonnet 4.6; Haiku ondersteunt dit niet
    if "opus" in model or "sonnet-4-6" in model:
        kwargs["thinking"] = {"type": "adaptive"}

    try:
        resp = client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        raise EnvironmentError("Anthropic API key ongeldig (401)")
    except anthropic.RateLimitError:
        raise RuntimeError("Anthropic rate limit bereikt")
    except anthropic.APIConnectionError as exc:
        raise RuntimeError(f"Geen verbinding met Anthropic API: {exc}")

    return next((b.text for b in resp.content if b.type == "text"), "")


# ── Google Gemini ─────────────────────────────────────────────────────────────

def _google(system: str, user: str, model: str, max_tokens: int) -> str:
    import google.generativeai as genai  # type: ignore

    key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        raise EnvironmentError("GOOGLE_API_KEY niet ingesteld")

    genai.configure(api_key=key)
    cfg = genai.GenerationConfig(max_output_tokens=max_tokens)
    client = genai.GenerativeModel(
        model_name=model,
        system_instruction=system,
        generation_config=cfg,
    )
    resp = client.generate_content(user)
    return resp.text


# ── Groq (Llama / Mixtral) ───────────────────────────────────────────────────

def _groq(system: str, user: str, model: str, max_tokens: int) -> str:
    from groq import Groq  # type: ignore

    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        raise EnvironmentError("GROQ_API_KEY niet ingesteld")

    client = Groq(api_key=key)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content
