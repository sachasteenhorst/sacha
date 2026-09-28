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
  zekerheid). Staan er **meerdere** factuurnummers in dezelfde omschrijving
  (bijv. een Kruitbosch/Accell-incasso die in één keer meerdere facturen
  afschrijft: "... VFNL002275878,VFNL002277060") dan worden ze allemaal in
  één koppeling meegenomen. Het totaal hoeft niet precies te kloppen -- een
  kleine **betalingskorting** wordt geaccepteerd (standaard tot 3% of
  EUR 25, wat van de twee kleiner is; instelbaar via
  `PAYMENT_DISCOUNT_PERCENT`/`PAYMENT_DISCOUNT_MAX_CENTS`) en het verschil
  wordt getoond in de kolom "Korting" bij "Recent gekoppeld". Noemt de
  omschrijving geen enkel volledig factuurnummer (Accell's incasso's
  bevatten bijvoorbeeld alleen de **laatste 5 cijfers** van elk
  factuurnummer, soms met een per ongeluk ingevoegde spatie middenin door
  een PDF-regelafbreking), dan wordt daar ook op gezocht -- maar alleen
  binnen facturen van dezelfde leverancier als de tegenpartij, en met
  dezelfde totaal/korting-controle, zodat een toevallige cijferovereenkomst
  nooit een verkeerde koppeling kan maken. Voor ENRA-rekeningcourantoverzichten
  (die geen eigen "totaalbedrag" hebben) wordt de referentie + het bedrag
  gehaald uit de "Saldo RC &lt;datum&gt; Agentnr. &lt;nr&gt;"-regel, die
  letterlijk ook in de omschrijving van de bijbehorende bankbijschrijving
  staat.
- **Bedrag herkennen**: het eindtotaal wordt gezocht op een label als
  "Totaal", "Totaalbedrag", "Te betalen" of "Factuurbedrag incl. btw", met
  of zonder euroteken ervoor. Sommige leveranciers zetten het euroteken
  er juist ACHTER (Mobility Services: "Totaal 4.507,08 €") of ertussenin
  vóór "incl. btw" (Vlechtservice.nl: "TOTAAL € INCL. BTW 247,99") -- een
  kaal `Totaal`-label (zonder "incl. btw" erachter) wordt ook herkend,
  mits het niet per ongeluk op "Subtotaal" matcht.
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
  rekening storten in plaats van innen (standaard ENRA, HelloRider en
  Mobility Services, instelbaar via `INCOMING_SUPPLIERS`) worden apart
  herkend. Mobility Services (het Lease a Bike-platform) stuurt geen
  inkoopfactuur maar een "factuur" die eigenlijk onze eigen verkoop aan
  leasemaatschappij VWPFS is (het IBAN in het document is onze eigen
  rekening) -- die telt dus als inkomend en matcht met de bijschrijvingen
  van "VWPFS B.V.".
- **Geen van bovenstaande** -> blijft open staan, tot jij het handmatig
  koppelt (aan één of meerdere facturen tegelijk), als "geen factuur
  nodig" markeert (bankkosten, privé-opname), of als "bon staat in
  Basecone" aanvinkt (kassabon al gefotografeerd met de Basecone-app --
  apart bijgehouden van "geen factuur nodig").

PDF-tekstherkenning is heuristisch (regex op de geëxtraheerde tekst). Niet
elke factuur-layout wordt goed herkend -- velden die niet gevonden worden
blijven leeg, de factuur verschijnt dan gewoon met minder gegevens in het
dashboard zodat je het zelf kan aanvullen of matchen. Documenten die
duidelijk geen factuur zijn (een document dat zelf de algemene voorwaarden
IS, een bankrekening-wijzigingsbericht, een pakbon zonder bedrag) worden
automatisch herkend en genegeerd, zodat ze niet als "openstaande factuur"
blijven hangen -- maar een factuur die in de footer alleen even *verwijst*
naar "onze algemene voorwaarden" (zoals Accell op praktisch elk document
doet) telt niet mee, anders zou elke Accell-factuur ten onrechte genegeerd
worden. Hetzelfde geldt voor een **verkoopfactuur die je zelf verstuurd
hebt** (bijv. gearchiveerd of CC'd naar info@) -- die komt nooit als
openstaande inkoopfactuur in het dashboard te staan. Herkenning gaat op het
afzenderadres (zelfde domein als je mailbox-adres) en op de bedrijfsnaam
uit `OWN_COMPANY_NAMES` (zie `.env.example`).

Automatisch genegeerd is niet hetzelfde als **handmatig** genegeerd (de
"Negeren"-knop bij een factuur): alleen dat laatste onthoudt het
dashboard permanent. Herkent `scripts/reparse_invoices.py` een eerder
automatisch genegeerd document later alsnog als een echte factuur/
specificatie (bijv. na een verbetering zoals de Accell-fix hierboven), dan
wordt het vanzelf heropend -- een document dat je zelf op "Negeren" hebt
gezet, blijft altijd met rust.

Het factuurnummer wordt ook uit de **bestandsnaam** gehaald als de tekst in
de PDF een incassant-ID of klantnummer oplevert in plaats van het echte
nummer (een bekend Kruitbosch-euvel: de PDF-layout zet het incassant-ID
"306228" direct na het "Factuurnummer"-label neer, terwijl het echte
nummer "VFNL002280953" gewoon in de bestandsnaam staat).

Dezelfde factuur komt soms **twee keer** binnen -- eenmaal per mailbox als
je meerdere mailboxen scant (bijv. `facturen@` én `info@` krijgen 'm beide
als losse e-mail). Dat wordt automatisch herkend (op een hash van de
PDF-inhoud, of anders leverancier+factuurnummer+bedrag) en de tweede kopie
wordt overgeslagen, zowel bij een nieuwe sync als door
`scripts/reparse_invoices.py` op bestaande dubbelen.

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

- De matcher krijgt **eerst** de kans op elke betaling, bij elke upload en
  elke sync -- een regel handelt alleen af wat daarna nog openstaat. Dat
  voorkomt dat een regel (bijv. "Omzet lease VWPFS") een betaling afvangt
  die eigenlijk gewoon aan een echte factuur gekoppeld had moeten worden.
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

## Basecone doorsturen

Een factuur "gekoppeld" hebben in dit dashboard is niet hetzelfde als "de
boekhouder kan het zien" -- daarvoor moet de e-mail ook echt doorgestuurd
zijn naar het Basecone-adres (`BASECONE_FORWARD_ADDRESS`, standaard
`vdlg.161024@mailvanderlaangroep.nl`). Elke factuur/creditnota/specificatie
heeft daarom een status **In Basecone: ja/nee/onbekend**:

- **ja** -- op drie manieren vastgesteld: de leverancier had het
  Basecone-adres zelf al aan/cc/bcc gezet, jij hebt 'm handmatig al eens
  doorgestuurd vanuit Outlook vóór deze functie bestond (herkend via
  Verzonden items, zie hieronder), of de app heeft 'm zelf verstuurd
  (automatisch of via de knop).
- **onbekend/nee** -- staat in het dashboard onder **"Niet naar
  Basecone"** (met een selectievakje per rij + knop "Stuur geselecteerde
  naar Basecone", en een knop per rij) of, als het factuurnummer/bedrag
  niet goed herkend is, onder **"Controleer eerst"**.

**Automatisch doorsturen** (`AUTO_FORWARD_BASECONE=true`, standaard uit):
bij elke sync stuurt de app zelf elk nieuw, herkenbaar document (heeft een
factuurnummer én een bedrag) dat nog niet in Basecone staat door -- als een
**nieuw bericht** (niet de hele originele e-mail doorgestuurd) met alleen de
factuur-PDF als bijlage, vanuit de mailbox waar het binnenkwam
(`Mail.Send.Shared`) of anders vanuit jouw eigen account (`Mail.Send`).
Nooit meer dan `AUTO_FORWARD_MAX_PER_SYNC` (standaard 50) per sync, en
nooit tweemaal hetzelfde document (de "ja"-status is definitief, ook na een
reparse). Alleen documenten ontvangen op/na `AUTO_FORWARD_FROM_DATE`
komen hiervoor in aanmerking -- staat die leeg, dan onthoudt de app zelf de
datum waarop je `AUTO_FORWARD_BASECONE` voor het eerst aanzette
(`data/auto_forward_since.txt`). Alles van dáárvoor ("backlog") wordt bij
elke sync vergeleken met **Verzonden items** (van elke geconfigureerde
mailbox én van jouw eigen account) op hetzelfde onderwerp
(RE/FW/Doorst.-prefixes genegeerd) of dezelfde bijlagenaam -- vind je zo'n
al verstuurd bericht, dan wordt de factuur op "ja" gezet zonder opnieuw te
versturen; de rest blijft in "Niet naar Basecone" staan zodat jij 'm bewust
verstuurt.

**Benodigde Entra-permissie**: `Mail.Send` (versturen als jezelf) en/of
`Mail.Send.Shared` (versturen namens een gedeelde mailbox) -- zie de
Azure AD-sectie hieronder voor de exacte stappen en dat je daarna opnieuw
moet inloggen met `scripts/graph_login.py`. Zonder die permissie blijft al
het andere gewoon werken; de doorstuur-knop toont dan een duidelijke
melding in plaats van te crashen.

## Vraagposten-pagina

De pagina **`/vraagposten`** (knop bovenin) is het uiteindelijke doel van
deze tool in één lijst: alles wat je accountant nog een vraag zou opleveren
-- betalingen zonder bewijsstuk, én facturen die wel gekoppeld zijn maar
nog niet naar Basecone zijn gestuurd. Gesorteerd op leeftijd (oudste
eerst), dan op bedrag. Per rij een concrete voorgestelde actie:

- **Mogelijke factuur (zelfde leverancier, bedrag wijkt af) -- controleren**
  -- er staat een openstaande factuur van dezelfde leverancier, maar het
  bedrag klopt niet (dus niet automatisch gematcht).
- **Terugkerende betaling zonder factuur -- regel maken** -- deze
  tegenpartij komt vaker terug zonder ooit een factuur te krijgen.
- **Privé/kleine uitgave -- bon in Basecone** -- een klein eenmalig bedrag,
  waarschijnlijk een kleine contante/pin-uitgave.
- **Geen factuur in mail -- opvragen bij leverancier** -- niets van dit
  alles; een kant-en-klare `mailto:`-link met onderwerp en tekst staat
  klaar om de leverancier om een kopie te vragen.
- **Factuur gevonden maar nog niet in Basecone -- doorsturen** -- al
  gekoppeld, maar de boekhouder ziet 'm nog niet.
- **Incasso mist N factu(u)r(en) (eindigend op ...) -- opvragen bij
  leverancier** -- specifiek voor incasso's die (zoals Accell/Kruitbosch)
  alleen de laatste 5 cijfers van elk factuurnummer in de omschrijving
  noemen: als één van die referenties nergens bij een ontvangen factuur
  van diezelfde leverancier hoort, staat híer precies welk(e)
  factuurnummer(s) ontbreken (op de laatste 5 cijfers na) -- inclusief een
  kant-en-klare `mailto:`-link om die specifieke factu(u)r(en) bij de
  leverancier op te vragen, in plaats van "er klopt iets niet" te moeten
  uitzoeken.

Rechtsboven staat een **CSV-export** (`/vraagposten/export.csv`) om de
lijst door te nemen of naar de boekhouder te sturen.

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
2. **Authentication → Allow public client flows → Yes → Save.** Dit is een
   **public client** app (device code login, zie hieronder) -- er is geen
   client secret nodig en Azure AD wijst er ook een af op deze app.
3. **API permissions → Add a permission → Microsoft Graph → Delegated
   permissions** → voeg toe:
   - `Mail.Read.Shared` -- om facturen te kunnen lezen (verplicht).
   - `Mail.Send` -- om zelf te versturen namens de ingelogde gebruiker
     ("me"), nodig voor de **"Stuur naar Basecone"**-knop en automatisch
     doorsturen (`AUTO_FORWARD_BASECONE`). Optioneel: zonder deze permissie
     werkt de rest van de app gewoon door, alleen versturen naar Basecone
     lukt dan niet (de knop toont een duidelijke foutmelding).
   - `Mail.Send.Shared` -- om te versturen namens een gedeelde mailbox
     (`facturen@`/`info@`) in plaats van "me". Ook optioneel, ook alleen
     voor het versturen naar Basecone.

   Deze drie zijn allemaal **gedelegeerd**, niet de toepassings-variant:
   die laatste mag bij *elke* mailbox in de hele tenant en vereist een
   Globale beheerder om goed te keuren, terwijl de gedelegeerde versies
   geen beheerderstoestemming nodig hebben (te zien aan "Nee" in de kolom
   "Beheerderstoestemming vereist") -- een gewone gebruiker keurt ze zelf
   goed tijdens het inloggen in de volgende stap.

   Vul daarna `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID` en `GRAPH_MAILBOX` (het
   mailadres van de gedeelde mailbox, bijv. `facturen@jouwfietsenwinkel.nl`)
   in via `.env`.

   `GRAPH_MAILBOX` mag ook een kommagescheiden lijst zijn, bijv.
   `facturen@jouwfietsenwinkel.nl,info@jouwfietsenwinkel.nl`, als facturen
   op meer dan één adres binnenkomen. Het **eerste** adres in de lijst
   wordt als factuur-gewijd beschouwd (alle PDF-bijlagen tellen mee); voor
   ieder adres daarna wordt alleen een PDF meegenomen als de bestandsnaam
   of onderwerp op een factuur/creditnota/specificatie lijkt, zodat een
   algemene inbox niet volloopt met nieuwsbrieven-als-"factuur". Een
   e-mail die op meerdere van die adressen tegelijk binnenkomt (bijv.
   CC'd) wordt niet dubbel opgeslagen.

   **Mail.Send(.Shared) later toegevoegd?** Draai `scripts/graph_login.py`
   opnieuw (zie hieronder) -- pas dan wordt er ook echt om die permissie
   gevraagd. Tot die tweede keer inloggen blijft alles gewoon
   read-only werken; er gaat nooit iets stuk door deze permissie later toe
   te voegen.

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
  basecone_forward.py   Basecone-doorstuurcontrole + versturen (sendMail via Graph)
  vraagposten.py         bouwt de Vraagposten-lijst + voorgestelde acties
  sync.py              orchestreert e-mailsync + bankupload, draait de matcher
  sync_state.py         laatste-sync-info in geheugen, voor de statusbalk
  scheduler.py          periodieke achtergrondtaak (e-mail)
  main.py               FastAPI-dashboard
templates/, static/      dashboard front-end
scripts/                  eenmalige/onderhoudsscripts (login, reparse)
tests/                    unit tests
```
