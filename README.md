# Fietsenwinkel administratie -- afschriften/facturen matcher

Een los tool naast Basecone dat:

1. banktransacties ophaalt via de **Basecone API**,
2. factuur-PDF's ophaalt uit je **mailbox** (IMAP),
3. ze automatisch aan elkaar **matcht** op bedrag/datum/factuurnummer,
4. en in een **webdashboard** laat zien welke afschriften nog geen factuur
   hebben (en andersom) -- zodat jij dat oplost voordat je accountant na
   een paar maanden met vraagposten komt.

## Hoe het matcht

- **Factuurnummer gevonden in omschrijving** -> automatisch bevestigd (hoge
  zekerheid).
- **Bedrag komt exact overeen + factuurdatum binnen het tijdvenster**
  (standaard 60 dagen) -> als "te bevestigen" suggestie in het dashboard;
  jij klikt op bevestigen of afwijzen.
- **Geen van beide** -> blijft open staan als niet-gekoppeld, tot jij het
  handmatig koppelt of als "geen factuur nodig" markeert (bijv. bankkosten,
  privé-opname).

PDF-tekstherkenning is heuristisch (regex op de geëxtraheerde tekst). Niet
elke factuur-layout wordt goed herkend -- velden die niet gevonden worden
blijven leeg, de factuur verschijnt dan gewoon met minder gegevens in het
dashboard zodat je het zelf kan aanvullen of matchen.

## Belangrijk: verifieer de Basecone API-aannames

Ik heb `app/basecone_client.py` gebouwd op de gangbare OAuth2
client-credentials flow en een REST-endpoint voor bankafschriftregels,
maar de **exacte** endpoint-paden en JSON-veldnamen verschillen per account
en API-versie. Voordat dit echt gaat draaien:

1. Vraag Basecone API-toegang aan (via Wolters Kluwer/Basecone
   support/developer portal) -- je krijgt een `client_id`, `client_secret`
   en documentatie.
2. Vergelijk die documentatie met `BASECONE_TOKEN_URL`,
   `BASECONE_API_BASE_URL` en `BASECONE_BANK_TRANSACTIONS_PATH` in
   `.env` / `app/config.py`.
3. Vergelijk de JSON-veldnamen in de documentatie met `FIELD_MAP` bovenin
   `app/basecone_client.py` en pas die aan indien de velden anders heten.

Zonder kloppende gegevens geeft de sync-stap een duidelijke foutmelding in
het dashboard (i.p.v. gewoon stil te falen).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# vul .env in: Basecone client_id/secret/administratie-id,
# IMAP-gegevens (gebruik een APP-WACHTWOORD, niet je gewone wachtwoord),
# en verander DASHBOARD_USERNAME/DASHBOARD_PASSWORD.
```

### Draaien

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/dashboard` (log in met je `DASHBOARD_USERNAME`/
`DASHBOARD_PASSWORD`). Bij het opstarten wordt de database aangemaakt
(SQLite-bestand in `./data/`) en start een achtergrondtaak die elke
`SYNC_INTERVAL_MINUTES` automatisch nieuwe afschriften/facturen ophaalt en
matcht. Met de "Sync nu"-knop in het dashboard kun je dit ook direct
triggeren.

### Testen

```bash
python3 -m pytest tests/ -v
```

## Beveiliging

- Het dashboard toont financiële gegevens en is beveiligd met HTTP Basic
  Auth (`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`). **Zet dit nooit
  onbeveiligd op internet** -- draai het achter HTTPS (bijv. via een
  reverse proxy als Caddy/nginx) als je het buiten je eigen netwerk
  bereikbaar wilt maken.
- `.env` (met wachtwoorden/API-keys) en de `data/`-map (database +
  gedownloade factuur-PDF's) staan in `.gitignore` en horen nooit gecommit
  te worden.
- Gebruik voor IMAP een app-specifiek wachtwoord (Gmail/Outlook/etc. bieden
  dit aan), zodat je hoofdwachtwoord nergens hoeft te staan.

## Projectstructuur

```
app/
  config.py          instellingen uit .env
  db.py               SQLAlchemy setup
  models.py           Transaction / Invoice / Match
  basecone_client.py   ophalen banktransacties via Basecone API
  email_client.py      ophalen + parsen factuur-PDF's via IMAP
  matcher.py           matching-logica
  sync.py              orchestreert een volledige sync-ronde
  scheduler.py          periodieke achtergrondtaak
  main.py               FastAPI-dashboard
templates/, static/      dashboard front-end
tests/                    unit tests voor de matcher
```
