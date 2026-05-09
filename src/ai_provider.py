"""Unified AI provider abstraction: Anthropic, Google Gemini, Groq, Mistral, Cerebras."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── OpenAI-compatibele providers (Mistral, Cerebras, …) ──────────────────────
_OPENAI_COMPAT: dict[str, dict[str, str]] = {
    "mistral":  {"url": "https://api.mistral.ai/v1",  "env": "MISTRAL_API_KEY"},
    "cerebras": {"url": "https://api.cerebras.ai/v1", "env": "CEREBRAS_API_KEY"},
}

# ── Maand-grens backoff ───────────────────────────────────────────────────────
# Google reset hun spending cap op 1e van de maand in UTC. De bot draait op
# CEST (UTC+2), dus tot 02:00 CEST op de 1e kan de reset nog niet klaar zijn.
# Na een 429 op dag 1 wachten we tot de UTC-middernacht gepasseerd is.

_google_monthly_backoff_until: float = 0.0  # epoch-seconden
_GOOGLE_SPENDING_CAP_BACKOFF = 24 * 3600    # 24 uur backoff bij spending cap


def _google_is_month_boundary_429() -> bool:
    """True als het UTC dag 1 is én we de eerste 2 uur zitten — klassieke timing."""
    now_utc = datetime.now(timezone.utc)
    return now_utc.day == 1 and now_utc.hour < 2

# Beschikbare modellen per provider
PROVIDER_MODELS: dict[str, list[dict]] = {
    "anthropic": [
        {"value": "claude-opus-4-7",   "label": "Claude Opus 4.7 — beste kwaliteit (betaald)"},
        {"value": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 — goede balans (betaald)"},
        {"value": "claude-haiku-4-5",  "label": "Claude Haiku 4.5 — snel & goedkoop (betaald)"},
    ],
    "google": [
        {"value": "gemini-2.0-flash",  "label": "Gemini 2.0 Flash — snel & actueel (aanbevolen) ★"},
        {"value": "gemini-1.5-flash",  "label": "Gemini 1.5 Flash — stabiel (gratis tier)"},
        {"value": "gemini-1.5-pro",    "label": "Gemini 1.5 Pro — meest capabel (beperkt gratis)"},
    ],
    "groq": [
        {"value": "llama-3.3-70b-versatile", "label": "Llama 3.3 70B — beste kwaliteit (gratis) ★"},
        {"value": "llama-3.1-8b-instant",    "label": "Llama 3.1 8B — snelst (gratis)"},
        {"value": "mixtral-8x7b-32768",      "label": "Mixtral 8x7B (gratis)"},
    ],
    "mistral": [
        {"value": "mistral-small-latest",  "label": "Mistral Small — snel & gratis tier ★"},
        {"value": "mistral-medium-latest", "label": "Mistral Medium — goede balans"},
        {"value": "open-mistral-7b",       "label": "Mistral 7B Open — volledig gratis"},
    ],
    "cerebras": [
        {"value": "llama3.3-70b",    "label": "Llama 3.3 70B — beste kwaliteit ★"},
        {"value": "llama3.1-70b",    "label": "Llama 3.1 70B — snel"},
        {"value": "llama3.1-8b",     "label": "Llama 3.1 8B — snelst"},
    ],
}

_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-opus-4-7",
    "google":    "gemini-2.0-flash",
    "groq":      "llama-3.3-70b-versatile",
    "mistral":   "mistral-small-latest",
    "cerebras":  "llama3.3-70b",
}

_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google":    "GOOGLE_API_KEY",
    "groq":      "GROQ_API_KEY",
    "mistral":   "MISTRAL_API_KEY",
    "cerebras":  "CEREBRAS_API_KEY",
}


def get_active() -> tuple[str, str]:
    """Geeft (provider, model) terug op basis van de huidige omgevingsvariabelen."""
    provider = os.getenv("AI_PROVIDER", "anthropic").lower()
    model    = os.getenv("AI_MODEL", "").strip() or _DEFAULT_MODEL.get(provider, "")
    return provider, model


def get_configured_providers() -> list[tuple[str, str]]:
    """Geeft [(provider, model)] voor alle providers met API key EN ingeschakeld via AI_<PROVIDER>_ENABLED."""
    result = []
    for provider in ("anthropic", "google", "groq", "mistral", "cerebras"):
        if not os.getenv(_KEY_ENV[provider], "").strip():
            continue
        enabled = os.getenv(f"AI_{provider.upper()}_ENABLED", "true").lower()
        if enabled == "false":
            continue
        model = (os.getenv(f"AI_MODEL_{provider.upper()}", "").strip()
                 or _DEFAULT_MODEL[provider])
        result.append((provider, model))
    return result


def complete_for(provider: str, model: str, system: str, user: str, max_tokens: int = 2048) -> str:
    """Stuurt een verzoek naar de opgegeven provider, ongeacht de actieve configuratie."""
    if provider == "anthropic":
        return _anthropic(system, user, model, max_tokens)
    if provider == "google":
        return _google(system, user, model, max_tokens)
    if provider == "groq":
        return _groq(system, user, model, max_tokens)
    if provider in _OPENAI_COMPAT:
        return _openai_compatible(provider, system, user, model, max_tokens)
    raise ValueError(f"Onbekende AI provider: {provider!r}")


def list_google_models() -> list[dict]:
    """Geeft beschikbare Gemini-modellen terug via de live Google API."""
    from google import genai  # type: ignore
    key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        return []
    try:
        client = genai.Client(api_key=key)
        result = []
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if "generateContent" not in actions:
                continue
            name = getattr(m, "name", "") or ""
            model_id = name.replace("models/", "")
            display  = getattr(m, "display_name", None) or model_id
            result.append({"value": model_id, "label": display})
        # Sortering: nieuwste modellen (hogere versienummers) eerst
        result.sort(key=lambda x: x["value"], reverse=True)
        return result
    except Exception as exc:
        logger.warning("Kon Google modellen niet ophalen: %s", exc)
        return []


def list_groq_models() -> list[dict]:
    """Geeft beschikbare Groq-modellen terug via de live Groq API."""
    from groq import Groq  # type: ignore
    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        return []
    try:
        client = Groq(api_key=key)
        models = client.models.list()
        # Groq geeft ook embedding/whisper modellen terug — filter op chat-capable
        _SKIP = {"whisper", "distil-whisper", "playai", "allam"}
        result = []
        for m in models.data:
            mid = getattr(m, "id", "") or ""
            if any(s in mid.lower() for s in _SKIP):
                continue
            result.append({"value": mid, "label": mid})
        result.sort(key=lambda x: x["value"])
        return result
    except Exception as exc:
        logger.warning("Kon Groq modellen niet ophalen: %s", exc)
        return []


def _list_openai_compat_models(provider: str) -> list[dict]:
    """Haal modellen op via de OpenAI-compatibele /models endpoint."""
    import httpx
    cfg = _OPENAI_COMPAT[provider]
    key = os.getenv(cfg["env"], "")
    if not key:
        return []
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{cfg['url']}/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            resp.raise_for_status()
            data = resp.json()
        result = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            if not mid:
                continue
            result.append({"value": mid, "label": mid})
        result.sort(key=lambda x: x["value"], reverse=True)
        return result
    except Exception as exc:
        logger.warning("Kon %s modellen niet ophalen: %s", provider, exc)
        return []


def list_mistral_models() -> list[dict]:
    """Geeft beschikbare Mistral-modellen terug via de live Mistral API."""
    return _list_openai_compat_models("mistral")


def list_cerebras_models() -> list[dict]:
    """Geeft beschikbare Cerebras-modellen terug via de live Cerebras API."""
    return _list_openai_compat_models("cerebras")


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
    if provider in _OPENAI_COMPAT:
        return _openai_compatible(provider, system, user, model, max_tokens)
    raise ValueError(f"Onbekende AI provider: {provider!r}")


# ── Anthropic ────────────────────────────────────────────────────────────────

def _anthropic(system: str, user: str, model: str, max_tokens: int) -> str:
    import anthropic  # type: ignore

    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise EnvironmentError("ANTHROPIC_API_KEY niet ingesteld")

    client = anthropic.Anthropic(api_key=key, timeout=30.0)
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
    global _google_monthly_backoff_until
    from google import genai
    from google.genai import types  # type: ignore

    key = os.getenv("GOOGLE_API_KEY", "")
    if not key:
        raise EnvironmentError("GOOGLE_API_KEY niet ingesteld")

    # Actieve maand-grens backoff: gooi direct een herkenbare fout zodat
    # de aanroeper (ai_strategy) naar de Groq-fallback kan schakelen.
    if time.time() < _google_monthly_backoff_until:
        remaining = int(_google_monthly_backoff_until - time.time())
        raise RuntimeError(f"Google spending cap reset nog niet klaar — wacht nog {remaining}s")

    from google.genai import types as _gtypes
    client = genai.Client(
        api_key=key,
        http_options=_gtypes.HttpOptions(timeout=30_000),  # 30s in milliseconden
    )
    try:
        response = client.models.generate_content(
            model=model,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
            ),
            contents=user,
        )
        try:
            from src.database import save_google_requests
            save_google_requests(1)
        except Exception:
            pass
        return response.text
    except Exception as exc:
        exc_str = str(exc)
        if "429" in exc_str or "RESOURCE_EXHAUSTED" in exc_str or "spending cap" in exc_str.lower():
            if _google_is_month_boundary_429():
                now_utc = datetime.now(timezone.utc)
                seconds_to_wait = max(0, (2 * 3600 + 5 * 60) - (now_utc.hour * 3600 + now_utc.minute * 60 + now_utc.second))
                _google_monthly_backoff_until = time.time() + seconds_to_wait
                logger.warning(
                    "Google 429 op maandgrens (UTC dag 1, %02d:%02d UTC) — "
                    "maand-reset nog niet klaar; backoff %ds",
                    now_utc.hour, now_utc.minute, seconds_to_wait,
                )
            else:
                # Spending cap buiten maandgrens: 24u backoff zodat bot niet
                # elke cyclus opnieuw een nutteloze Google-call doet
                _google_monthly_backoff_until = time.time() + _GOOGLE_SPENDING_CAP_BACKOFF
                logger.warning(
                    "Google spending cap geraakt — 24u backoff ingesteld; "
                    "bot gebruikt Groq-fallback voor sentiment"
                )
            raise RuntimeError(f"Google rate limit (429): {exc_str[:80]}")
        if "401" in exc_str or "API_KEY_INVALID" in exc_str or "UNAUTHENTICATED" in exc_str:
            raise EnvironmentError(f"Google API key ongeldig: {exc_str[:80]}")
        raise


# ── Groq (Llama / Mixtral) ───────────────────────────────────────────────────

def _groq(system: str, user: str, model: str, max_tokens: int) -> str:
    from groq import Groq  # type: ignore

    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        raise EnvironmentError("GROQ_API_KEY niet ingesteld")

    # Groq free-tier: 6000 TPM (input + output samen). Schat ~4 chars/token.
    _TPM_SAFE = 5800
    sys_tokens  = len(system) // 4
    user_budget = max(200, _TPM_SAFE - sys_tokens - max_tokens)
    max_user_chars = user_budget * 4
    if len(user) > max_user_chars:
        user = user[:max_user_chars] + "\n[context afgekapt — tokenslimiet]"
        logger.debug("Groq prompt afgekapt tot %d tekens", max_user_chars)

    client = Groq(api_key=key, timeout=30.0)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        max_tokens=max_tokens,
    )
    try:
        if resp.usage and resp.usage.total_tokens:
            from src.database import save_groq_tokens
            save_groq_tokens(resp.usage.total_tokens)
    except Exception:
        pass
    return resp.choices[0].message.content


# ── OpenAI-compatibele providers (Mistral, Cerebras) ─────────────────────────

def _openai_compatible(provider: str, system: str, user: str, model: str, max_tokens: int) -> str:
    """Generieke adapter voor providers met een OpenAI-compatibele REST API."""
    import httpx

    cfg = _OPENAI_COMPAT[provider]
    key = os.getenv(cfg["env"], "")
    if not key:
        raise EnvironmentError(f"{cfg['env']} niet ingesteld")

    # Groq free-tier-achtige truncatie: schat ~4 chars/token, laat 600 tokens over voor output
    _CHAR_BUDGET = 20_000
    if len(user) > _CHAR_BUDGET:
        user = user[:_CHAR_BUDGET] + "\n[context afgekapt]"

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "max_tokens": max_tokens,
    }

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(f"{cfg['url']}/chat/completions", json=payload, headers=headers)
        if resp.status_code == 401:
            raise EnvironmentError(f"{provider} API key ongeldig (401)")
        if resp.status_code == 429:
            raise RuntimeError(f"{provider} rate limit bereikt (429)")
        resp.raise_for_status()
        data = resp.json()

    # Token-tracking (provider-specifiek)
    try:
        total_tokens = (data.get("usage") or {}).get("total_tokens", 0)
        if total_tokens:
            if provider == "mistral":
                from src.database import save_mistral_tokens
                save_mistral_tokens(total_tokens)
            elif provider == "cerebras":
                from src.database import save_cerebras_tokens
                save_cerebras_tokens(total_tokens)
    except Exception:
        pass

    return data["choices"][0]["message"]["content"]
