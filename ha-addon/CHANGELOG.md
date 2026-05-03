# Wijzigingslogboek — Bitvavo Trading Bot

## v1.18.10
- fix: CHANGELOG.md wordt nu ook gekopieerd door update.sh (HA wijzigingslogboek werkt nu correct)
- fix: hardcoded versienummer verwijderd uit addon description

## v1.18.9
- Logboek-pagina in web-UI met filter op INFO / WARNING / ERROR / DEBUG
- Update-knop in Instellingen die `update.sh` op de achtergrond uitvoert
- Log-buffer (laatste 500 regels) live zichtbaar zonder SSH

## v1.18.8
- Live Google-modellen worden automatisch opgehaald bij openen van Instellingen
- Live Groq-modellen ophalen via ↻ knop + auto-fetch bij laden
- Aanbevolen modellen gemarkeerd met ★ in beide dropdowns

## v1.18.7
- `gemini-1.5-flash` vervangen door `gemini-2.0-flash` als light model default
  (was 404 NOT_FOUND op v1beta API, veroorzaakte dubbele Groq-fallback)

## v1.18.6
- Lokale risicomanager vervangt Anthropic/Claude als risk-provider (0 API calls)
- Token-budget gehalveerd: max_tokens 160 → 80 (tactisch), 160 → 50 (sentiment)
- Marktadviseur: limit 40 → 20 markten, max_tokens 1024 → 400

## v1.18.5
- Model-degradatie: licht model bij lage confluentiesscore, zwaar bij hoog
- Technische confluentiesscore (0–5) als gate vóór AI-aanroepen
- Sentimentcache met 20-minuten TTL per markt

## v1.18.4
- SMA200 trendfilter als vroege gatekeeper (geen API-calls bij bearish trend)
- Sentiment caching: voorkomt dubbele Gemini/Groq calls per cyclus
- ValueError sleep-fix voor Bitvavo tijdzone-mismatch
