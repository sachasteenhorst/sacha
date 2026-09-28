# Fietsenwinkel administratie -- afschriften/facturen matcher

Doel: elke transactie op de zakelijke Rabobank-rekening moet gekoppeld zijn
aan een bewijsstuk (factuur, creditnota of bon). Deze tool:

1. leest **bankafschriften** die je zelf uploadt (Rabobank CSV, CAMT.053 of
   MT940 -- zie hieronder), **niet** via Basecone (Basecone heeft alleen
   inkoop/verkoopboekingen en documenten, geen bankafschriftregels -- geen
   bruikbare bron voor de rekening),
2. haalt factuur-PDF's op uit je **mailbox(en)** (Microsoft Graph API),
3. matcht ze automatisch op **bedrag/datum/factuurnummer**, met oog voor de
   **richting** van het geld en voor **één betaling die meerdere documenten
   dekt**,
4. en laat in een **webdashboard** zien wat nog geen bewijsstuk heeft (en
   andersom) -- zodat jij dat oplost voordat je accountant met vraagposten
   komt.

## Hoe het matcht

- **Factuurnummer gevonden in omschrijving** -> automatisch bevestigd (hoge
  zekerheid).
- **Bedrag komt exact overeen + datum binnen het tijdvenster** (standaard
  60 dagen) -> als "te bevestigen" suggestie in het dashboard; jij klikt op
  Klopt/Klopt niet. Bij meerdere kandidaten telt ook mee hoe goed de naam
  van de tegenpartij bij de leveranciersnaam past.
- **Eén specificatie, meerdere facturen**: een document als Accell's
  "Specificatie automatische incasso" noemt meerdere factuurnummers -- als
  dat document matcht, worden die facturen (als ze ook los binnenkwamen)
  in dezelfde koppeling meegenomen. Je bevestigt/wijst de hele groep in één
  keer af.
- **Combinatie van facturen**: geen enkel document matcht alleen, maar een
  paar openstaande facturen van dezelfde leverancier tellen precies op tot
  het afgeschreven bedrag -> ook als groep-suggestie.
- **Richting**: een bijschrijving (geld erbij) matcht nooit met een
  gewone leveranciersfactuur, en andersom. Leveranciers die geld op de
  rekening storten in plaats van innen (standaard ENRA en HelloRider,
  instelbaar via `INCOMING_SUPPLIERS`) worden apart herkend.
- **Geen van bovenstaande** -> blijft open staan, tot jij het handmatig
  koppelt (aan één of meerdere facturen tegelijk), als "geen factuur
  nodig" markeert (bankkosten, privé-opname), of als "bon staat in
  Basecone" aanvinkt (kassabon al gefotografeerd met de Basecone-app --
  apart bijgehouden van "geen factuur nodig").

PDF-tekstherkenning is heuristisch (regex op de geëxtraheerde tekst). Niet
elke factuur-layout wordt goed herkend -- velden die niet gevonden worden
blijven leeg, de factuur verschijnt dan gewoon met minder gegevens in het
dashboard zodat je het zelf kan aanvullen of matchen. Documenten die
duidelijk geen factuur zijn (algemene voorwaarden, een
bankrekening-wijzigingsbericht, een pakbon zonder bedrag) worden
automatisch herkend en genegeerd, zodat ze niet als "openstaande factuur"
blijven hangen.

## Regels: automatisch afhandelen

Niet elke betaling krijgt ooit een factuur of bon -- bankkosten, een
overboeking tussen je eigen rekeningen, een pinbetaling-afrekening die
gewoon omzet is. Daarvoor is er de **Regels-pagina** (`/regels`, ook een
knop bovenin het dashboard).

Een regel bestaat uit kenmerken die allemaal moeten kloppen (AND): naam van
de tegenpartij (bevat), IBAN van de tegenpartij (exact), omschrijving
(bevat), Rabobank-transactiecode (bijv. `db`/`tb`/`ba`/`ok`, alleen bij een
CSV-upload beschikbaar) en richting (bij/af). Een regel zonder één ingevuld
kenmerk matcht nooit iets, zodat er geen regel per ongeluk alles pakt. Elke
regel wijst een betaling toe aan "geen factuur nodig" of "omzet".

- Regels worden toegepast **voordat** de matcher draait, bij elke upload en
  elke sync.
- Bij elke betaling zonder factuur staat een knop **"Maak regel"** die een
  nieuwe regel voorinvult met de tegenpartij/IBAN/code/richting van die
  betaling. Na opslaan wordt de regel **direct** toegepast op alle
  openstaande betalingen die eraan voldoen.
- Betalingen die door een regel zijn afgehandeld staan apart in het
  dashboard onder **"Automatisch afgehandeld"**, met welke regel het deed.
  Klopt het niet? Klik op **"Terugzetten"** om die ene betaling terug te
  zetten naar "zonder factuur".
- Een regel **uitzetten** (in plaats van verwijderen) laat al afgehandelde
  betalingen met rust; een regel **verwijderen** zet alle betalingen die
  hij had afgehandeld terug naar "zonder factuur".
- De teller "Betalingen zonder factuur" telt alleen wat nog echt een
  bewijsstuk nodig heeft (regel-afgehandelde betalingen tellen niet mee).
  Onderaan het dashboard staat een tabel **"Voortgang per maand"**: per
  maand het percentage betalingen dat is afgehandeld (gekoppeld aan een
  factuur, door een regel afgehandeld, bon in Basecone, of genegeerd als
  "geen factuur nodig") -- zo zie je in één oogopslag hoe ver je bent.

Standaard staan er een paar regels klaar (uit/aan te zetten of te
verwijderen op de Regels-pagina, ze komen niet terug na verwijderen):

- Rabo Smart Pay / Rabobank Smart Pay (bijschrijving) -> omzet
- Stichting Pay.nl Clearing -> omzet
- Rabobank + "Kosten"/"Provisie"/"Rente" in de omschrijving -> geen factuur
  nodig
- Transactiecode `tb` (overboeking tussen eigen rekeningen) -> geen factuur
  nodig
- Belastingdienst -> geen factuur nodig

Alles wat niet onder deze standaardregels valt (huur, verzekering, etc.)
laat je zelf via "Maak regel" lopen, zodat je het bewust kiest.

## Bankafschriften uploaden

Basecone is voor deze rekening geen bruikbare bron (zie hierboven) -- je
upload je Rabobank-afschriften zelf via de knop **"Bankafschrift
uploaden"** bovenaan het dashboard. Drie formaten worden ondersteund:

- **Rabobank CSV** -- in Rabo Internetbankieren: **Downloads en
  documenten → Transacties downloaden**, kies CSV en de gewenste periode.
- **CAMT.053** (.xml) -- het ISO 20022-formaat, ook te downloaden vanuit
  Rabo Internetbankieren (Downloads en documenten → Transacties
  downloaden → CAMT.053).
- **MT940 Structured** (.swi) -- idem, kies MT940 Structured.

Bij het uploaden worden regels die je al eerder hebt geüpload automatisch
overgeslagen (op basis van een stabiele sleutel per regel, niet het
bestand als geheel) -- een bestand met overlappende periodes opnieuw
uploaden is dus altijd veilig. Na het uploaden draait de matcher meteen.

**Let op:** de drie parsers (`app/bank_import.py`) zijn gebouwd volgens de
gepubliceerde formaatspecificaties, maar nog niet tegen een echt
gedownload Rabobank-bestand geprobeerd -- alleen tegen handgemaakte
testbestanden. Upload gerust een eerste echt bestand; als het misgaat
toont de foutmelding precies welke kolom/tag niet gevonden werd, meestal
in een paar regels te verhelpen (dezelfde aanpak waarmee de
Graph-koppeling eerder ook werkend is gekregen).

Wil je Basecone later alsnog ergens voor gebruiken (documenten/boekingen,
niet de bankafschriften) dan kan dat via `BASECONE_ENABLED=true` en de
overige `BASECONE_*`-instellingen -- de endpoint-paden en veldnamen in
`app/basecone_client.py` zijn een aanname op basis van de gangbare OAuth2
client-credentials flow en moeten dan geverifieerd worden tegen de
documentatie die je van Basecone krijgt.

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
   `facturen@jouwfietsenwinkel.nl`) in via `.env`.

   `GRAPH_MAILBOX` mag ook een kommagescheiden lijst zijn, bijv.
   `facturen@jouwfietsenwinkel.nl,info@jouwfietsenwinkel.nl`, als facturen
   op meer dan één adres binnenkomen. Het **eerste** adres in de lijst
   wordt als factuur-gewijd beschouwd (alle PDF-bijlagen tellen mee); voor
   ieder adres daarna wordt alleen een PDF meegenomen als de bestandsnaam
   of onderwerp op een factuur/creditnota/specificatie lijkt, zodat een
   algemene inbox niet volloopt met nieuwsbrieven-als-"factuur". Een
   e-mail die op meerdere van die adressen tegelijk binnenkomt (bijv.
   CC'd) wordt niet dubbel opgeslagen.

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
# vul .env in: Graph-gegevens (zie hierboven), eventueel INCOMING_SUPPLIERS
# als er meer leveranciers geld op de rekening storten dan ENRA/HelloRider,
# en verander DASHBOARD_USERNAME/DASHBOARD_PASSWORD.
# BASECONE_* alleen invullen als je BASECONE_ENABLED=true zet (zie hierboven).
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

### Bestaande facturen opnieuw verwerken

Na een verbetering aan de tekstherkenning in `app/email_client.py` (bedrag/
factuurnummer/leverancier/richting/documentsoort) hoeven facturen die al in
de database staan niet opnieuw opgehaald te worden -- dit script leest ze
gewoon opnieuw uit het al opgeslagen PDF-bestand:

```bash
python3 scripts/reparse_invoices.py
```

Laat status en koppelingen die je al gemaakt hebt met rust; alleen een
document dat nog gewoon openstond (UNMATCHED) en nu als "geen factuur"
wordt herkend, wordt op genegeerd gezet. Rapporteert aan het eind hoeveel
er nog zonder bedrag/factuurnummer zijn.

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
   daarin de Graph-gegevens (en eventueel Basecone-gegevens, alleen als
   `BASECONE_ENABLED=true`) uit deze README.
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
  db.py               SQLAlchemy setup + startup-migratie voor nieuwe kolommen
  models.py           Transaction / Invoice / Match / Rule, Direction, DocumentKind
  bank_import.py       Rabobank CSV / CAMT.053 / MT940-parsers
  basecone_client.py   optioneel: ophalen documenten/boekingen via Basecone API
  email_client.py      ophalen + parsen factuur-PDF's via Microsoft Graph
  matcher.py           matching-logica (richting, specificaties, combinaties)
  rules.py              regels die betalingen automatisch afhandelen (voor de matcher draait)
  sync.py              orchestreert e-mailsync + bankupload, draait de matcher
  sync_state.py         laatste-sync-info in geheugen, voor de statusbalk
  scheduler.py          periodieke achtergrondtaak (e-mail)
  main.py               FastAPI-dashboard
templates/, static/      dashboard front-end
scripts/                  eenmalige/onderhoudsscripts (login, reparse)
tests/                    unit tests
```
