# Fietsenwinkel administratie -- afschriften/facturen matcher

Een los tool naast Basecone dat:

1. banktransacties ophaalt via de **Basecone API**,
2. factuur-PDF's ophaalt uit je **mailbox** (Microsoft Graph API),
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

## Azure AD app-registratie voor de mailbox (Microsoft Graph)

Microsoft 365 staat standaard geen klassieke IMAP-login (gebruikersnaam +
wachtwoord/app-wachtwoord) meer toe voor zakelijke mailboxen. In plaats
daarvan registreert een beheerder een eigen "app" in Azure AD / Entra die
via OAuth2 mag meelezen. Iemand met beheerdersrechten in het Microsoft 365
beheercentrum moet dit doen:

1. **Azure Portal / Entra admin center → App registrations → New
   registration.** Naam bijv. "Fietsenwinkel factuur-tool", "Accounts in
   this organizational directory only" (single tenant). Noteer na het
   aanmaken de **Application (client) ID** en **Directory (tenant) ID**.
2. **Certificates & secrets → New client secret.** Kopieer de waarde
   meteen (die is later niet meer zichtbaar) -- dit is de **Client
   secret**.
3. **API permissions → Add a permission → Microsoft Graph → Delegated
   permissions → `Mail.Read.Shared` → Add permissions.**

   We gebruiken bewust de **gedelegeerde** `Mail.Read.Shared` in plaats van
   de toepassings-variant van `Mail.Read`: die laatste mag bij *elke*
   mailbox in de hele tenant en vereist een Globale beheerder om goed te
   keuren. `Mail.Read.Shared` vereist geen beheerderstoestemming (te zien
   aan "Nee" in de kolom "Beheerderstoestemming vereist") -- een gewone
   gebruiker keurt 'm zelf goed tijdens het inloggen in de volgende stap.

   Vul daarna `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` en
   `GRAPH_MAILBOX` (het mailadres van de gedeelde mailbox, bijv.
   `info@jouwfietsenwinkel.nl`) in via `.env`.

## Eenmalig inloggen (device code flow)

Omdat we een gedelegeerde permissie gebruiken (in plaats van een
onbemand/"headless" app-only permissie), moet één keer een mens inloggen om
een refresh-token te genereren. Dat hoeft geen beheerder te zijn -- gewoon
iemand die de gedeelde mailbox (`GRAPH_MAILBOX`) al kan openen in zijn/haar
eigen Outlook (dat betekent dat diegene daar al "Full Access"-rechten op
heeft).

```bash
python3 scripts/graph_login.py
```

Dit toont een korte code en een link (`https://microsoft.com/devicelogin`).
Open die link in een willekeurige browser, voer de code in, log in en
bevestig. Het script slaat daarna een refresh-token op in
`data/graph_refresh_token.txt` (staat in `.gitignore`, wordt nooit
gecommit). De achtergrondtaak ververst dit token zelf steeds automatisch;
je hoeft dit script alleen opnieuw te draaien als het ooit verloopt of
wordt ingetrokken (bijv. na een wachtwoordwijziging).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# vul .env in: Basecone client_id/secret/administratie-id,
# Graph-gegevens (zie hierboven),
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

## Overdracht aan een hostingpartij

Dit hoeft niet ontwikkeld te worden -- het staat al klaar. Wat een
hostingpartij nodig heeft om dit blijvend te laten draaien:

1. **Deze repository** (bevat een `Dockerfile`):
   ```bash
   docker build -t fietsenwinkel-admin .
   docker run -d \
     --name fietsenwinkel-admin \
     -p 8000:8000 \
     --env-file .env \
     -v fietsenwinkel-admin-data:/app/data \
     fietsenwinkel-admin
   ```
   De `-v ...:/app/data` regel is belangrijk: daar staan de database, de
   gedownloade factuur-PDF's en het Microsoft-inlogtoken. Zonder een
   persistent volume raakt dat alles kwijt bij elke herstart.
2. **Een ingevuld `.env`-bestand** (niet in git -- apart aanleveren) met
   daarin de Basecone- en Graph-gegevens uit deze README.
3. **Een reverse proxy met HTTPS** ervoor (bijv. Caddy, nginx of Traefik) --
   dit draait zelf alleen platte HTTP op poort 8000, en het dashboard bevat
   financiële gegevens achter een wachtwoord dat niet onversleuteld over
   internet mag.
4. Het **eenmalige Microsoft-inlogbestand**
   (`data/graph_refresh_token.txt`) kan gewoon worden meegenomen naar de
   nieuwe server (in het volume hierboven) -- dan hoeft niemand de
   Azure-inlogstappen opnieuw te doorlopen.

## Beveiliging

- Het dashboard toont financiële gegevens en is beveiligd met HTTP Basic
  Auth (`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`). **Zet dit nooit
  onbeveiligd op internet** -- draai het achter HTTPS (bijv. via een
  reverse proxy als Caddy/nginx) als je het buiten je eigen netwerk
  bereikbaar wilt maken.
- `.env` (met wachtwoorden/API-keys) en de `data/`-map (database +
  gedownloade factuur-PDF's) staan in `.gitignore` en horen nooit gecommit
  te worden.
- De Graph-app heeft alleen leestoegang (`Mail.Read.Shared`, gedelegeerd),
  beperkt tot mailboxen waar de ingelogde gebruiker zelf al toegang toe
  heeft -- niet je hele Microsoft 365-tenant. Bewaar
  `data/graph_refresh_token.txt` net zo zorgvuldig als een wachtwoord: wie
  dat refresh-token heeft, kan namens de ingelogde gebruiker bij die
  mailbox.

## Projectstructuur

```
app/
  config.py          instellingen uit .env
  db.py               SQLAlchemy setup
  models.py           Transaction / Invoice / Match
  basecone_client.py   ophalen banktransacties via Basecone API
  email_client.py      ophalen + parsen factuur-PDF's via Microsoft Graph
  matcher.py           matching-logica
  sync.py              orchestreert een volledige sync-ronde
  scheduler.py          periodieke achtergrondtaak
  main.py               FastAPI-dashboard
templates/, static/      dashboard front-end
tests/                    unit tests voor de matcher
```
