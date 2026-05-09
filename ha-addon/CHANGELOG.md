# Wijzigingslogboek — Bitvavo Trading Bot

## v1.18.17
- feat: Marktclassificatie LARGE / MID / ALT — badge op dashboard naast elke markt
- feat: ALT-markten krijgen automatisch strenger drempel (× ALT_THRESHOLD_MULTIPLIER, standaard 1.5)
- feat: Min. confluence +1 voor ALT-markten (minder valse signalen op illiquide coins)
- feat: ALT_MARKETS env var om specifieke markten als ALT te forceren
- feat: Instellingenpagina: ALT-drempel-multiplier + ALT-markten veld

## v1.18.16
- feat: Gemini-gate — Google/Gemini alleen aangeroepen bij |score| ≥ GEMINI_GATE_SCORE (standaard 0.5)
- perf: Primaire sentiment-pool gewijzigd naar Mistral + Groq (ruim quotum); Cerebras als fallback
- perf: Gemini-verzoeken gereduceerd van ~120/uur naar ~10–40/dag (alleen bij sterke signalen)
- feat: GEMINI_GATE_SCORE env var (standaard 0.5) om Gemini-drempel in te stellen

## v1.18.15
- fix: Mistral en Cerebras model-dropdown breder (volledige modelnaam zichtbaar)
- feat: Live model-detectie (↻) voor Mistral en Cerebras
- fix: "Risico Claude" verwijderd uit orchestrator-beschrijving (lokale manager)
- fix: Mistral en Cerebras toegevoegd aan STRATEGIE-statuswidget
- feat: /api/ai/mistral/models en /api/ai/cerebras/models API-endpoints

## v1.18.14
- feat: Mistral AI als tweede sentiment-provider — majority-vote pool (Gemini + Mistral)
- feat: Cerebras als tactische backup (Groq → Cerebras fallback-keten)
- feat: Pool van 4 sentiment-providers (Gemini, Mistral, Groq, Cerebras) met majority-vote
- feat: Generieke OpenAI-compatible adapter voor Mistral & Cerebras (één codepad)
- feat: Token-gauges op dashboard voor Gemini, Mistral en Cerebras
- feat: API-sleutel + model-instellingen voor Mistral en Cerebras in Instellingen

## v1.18.13
- fix: Google spending cap zet automatisch 24u backoff (niet alleen op dag 1 van de maand)
- feat: Google API verzoekenteller op dashboard (Gemini Verzoeken 24h, standaard limiet 1500/dag)
- feat: GOOGLE_DAILY_LIMIT env var om eigen limiet in te stellen

## v1.18.12
- fix: sentiment-prompt strenger — JSON-voorbeeld bovenaan voorkomt extra tekst van Gemini
- feat: retry-logica sentiment — max 2 pogingen bij parse-fout, 2e poging met striktere instructie
- feat: uitgebreide keywords in parser — BUY/MOON/STRENGTH → POSITIVE, SELL/WEAKNESS/DUMP → NEGATIVE
- perf: candle cache in correlatie-bewaking — van 64 naar max 8 API-calls per cyclus (1 uur TTL)

## v1.18.11
- fix: Update-knop en Logboek-link verwijderd uit web-app (behoorden er niet in thuis)
- fix: Gemini model gebruikt nu altijd het geselecteerde model als fallback (geen deprecated hardcoded naam)

## v1.18.10
- fix: Gemini modelnaam niet meer hardcoded — gebruikt altijd het geselecteerde model (geen deprecated fallback)
- fix: CHANGELOG.md wordt gekopieerd door update.sh zodat HA wijzigingslogboek werkt
- feat: Model-dropdown breder voor betere leesbaarheid van lange modelnamen
- feat: Update-knop verplaatst naar prominente gele sectie bovenaan Instellingen
- fix: hardcoded versienummer verwijderd uit addon description

## v1.18.9
- Logboek-pagina in web-UI met filter op INFO / WARNING / ERROR / DEBUG
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
