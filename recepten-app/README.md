# Aanbiedingskeuken

Een webapp die elke dag een gezond recept laat zien, gekozen op basis van wat
er deze week in de aanbieding is bij de supermarkt. De app is voor elk gezin:
je stelt zelf in hoeveel volwassenen en kinderen er mee-eten, en je kunt baby's
toevoegen met wat ze wel en niet mogen. Voor elke baby staat bij elk recept hoe
je een aparte babyportie maakt.

Open `index.html` in een browser. Er is geen server of installatie nodig.
Alles wat je instelt, blijft bewaard in de browser.

## Op je telefoon installeren

De app is een installeerbare web-app (PWA): `manifest.webmanifest` geeft naam,
kleuren en iconen, `sw.js` zorgt dat de app ook zonder bereik opent. De app
staat online via GitHub Pages:

**https://sachasteenhorst.github.io/sacha/recepten-app/**

(eenmalig aanzetten: GitHub → Settings → Pages → *Deploy from a branch* →
branch `claude/intelligent-wozniak-1j5v2q`, map `/ (root)` → Save.)

- **iPhone (Safari):** open de link → deelknop → *Zet op beginscherm*.
- **Android (Chrome):** open de link → menu ⋮ → *App installeren* (of de
  melding onderin).

In de geïnstalleerde app worden boodschappenlijst, kaarten, gezin en filters op
het apparaat zelf bewaard. De folders komen uit `aanbiedingen.json`; de
wekelijkse taak werkt dat bestand bij en pusht het naar deze branch.

## Onderdelen

- **Vandaag**: het recept van de dag, de producten uit de aanbieding die erin
  zitten, de bereiding en, als je een baby hebt toegevoegd, een apart stappenplan
  voor de baby met een tussendoortje erbij. Met "Ander recept" wissel je het gerecht.
- **Week**: het weekmenu en een boodschappenlijst om af te vinken. De
  hoeveelheden passen zich aan je gezin aan.
- **Aanbiedingen**: een overzicht van alles wat in jouw winkels in de
  aanbieding is. Tik op een product om te zien waar het het goedkoopst is.

Bovenaan kies je van welke winkels je de aanbiedingen wilt zien (Jumbo, Dirk,
Aldi, Vomar, DekaMarkt, Albert Heijn, Lidl, Plus). Bij elk ingrediënt staat de goedkoopste actie
in jouw winkels; tik erop voor de vergelijking met alle winkels, op prijs per
kilo als die bekend is. Aanbiedingen van Albert Heijn, Lidl en Plus voeg je
zelf toe onder "Zelf aanbiedingen toevoegen".
- **Recepten**: je favorieten (tik bij een recept op het hartje), filters en
  alle recepten. Vink allergieën aan (gluten, melk, ei, vis, pinda, noten, soja,
  selderij, kokos) en kies per soort gerecht (vega, vis, kip, vlees, ei) tussen
  niet, normaal of vaker. Het weekmenu en "Ander recept" houden zich aan de
  filters; voorkeuren en favorieten komen vaker voor. Ook deze instellingen
  worden bij je account bewaard.
- **Gezin**: het aantal volwassenen en kinderen (een kind telt als ongeveer 0,6
  portie) en je baby's. Per baby vul je naam en geboortedatum in en tik je aan
  wat de baby niet mag: granen en brood, gluten, zuivel, ei, vis, vlees, pinda en
  noten, peulvruchten, kokos of nitraatrijke groente. Met een baby erbij zie je
  ook de algemene regels voor baby's, structuuradvies per leeftijd en een teller
  voor nitraatrijke groente. Het gezin wordt bij je account bewaard.
- **Lijst**: je boodschappenlijst. Met één tik zet je een gerecht of het hele
  weekmenu erop. De lijst is gegroepeerd per winkel (waar het product in de
  aanbieding is, met de actie erbij) en toont per product en per winkel wat het
  ongeveer kost. Losse producten voeg je zelf toe. De lijst wordt net als de
  kaarten bij je account bewaard.
- **Kosten per gerecht**: onder de ingrediënten staat wat het gerecht ongeveer
  kost, per ingrediënt uitgesplitst. Stel een budget per maaltijd in en kies
  goedkopere alternatieven (koolvis in plaats van zalm, de helft van het gehakt
  vervangen door linzen, kaas weglaten, enzovoort), of laat de app het gerecht
  zelf aanpassen tot het binnen je budget valt. Prijzen zijn de actieprijs per
  kilo waar die bekend is, en anders een richtprijs (`EST` in `index.html`).
- **Kaarten**: je spaarkaarten (Bonuskaart, Extra's en andere) met een grote
  streepjescode om bij de kassa te laten scannen. De kaarten staan in je
  privédeel van de app-opslag (`data/users/<jouw id>/kaarten`): alleen jij ziet
  ze, ook als je de app deelt, en ze staan op elk apparaat waarop je bent
  ingelogd. Een kopie op het apparaat zorgt dat ze ook zonder bereik werken.
  Echt koppelen aan een winkelaccount kan niet; supermarkten bieden daar geen
  openbare koppeling voor.

## Folders

`folders/ophalen.py` haalt de actuele aanbiedingen op en koppelt elke
folder-titel aan een product uit de recepten, bijvoorbeeld
"Hollandse broccoli € 0,99 (was € 1,15)" bij Dirk aan `broccoli`. Bij elk
ingrediënt toont de app daarna de goedkoopste actie, en per product een
vergelijking tussen winkels. Het script leest daarvoor ook de prijs, de
verpakking en waar mogelijk de prijs per kilo uit.

| Winkel | Hoe | Opmerking |
| --- | --- | --- |
| Jumbo | weekaanbiedingen-pagina | alleen de eerste ± 44 acties; weigert soms (403), dan blijven de vorige acties staan |
| Dirk | Nuxt-gegevens in de pagina | alle acties, met actie- en normale prijs |
| Aldi | productgegevens in de pagina | prijs, korting en geldigheid |
| DekaMarkt | zelfde opzet als Dirk (Detailresult) | alle acties, met actie- en normale prijs; vraagt browser-headers |
| Vomar | tekstlaag van de Publitas-bladerfolder | alleen welke producten in de folder staan, zonder prijs (de tekstlaag staat door elkaar) |
| Albert Heijn | niet automatisch | ah.nl blokkeert scripts (Akamai) |
| Lidl, Plus | niet automatisch | de folder laadt pas in de browser |

```
python3 recepten-app/folders/ophalen.py            # schrijft recepten-app/aanbiedingen.json
```

De gepubliceerde app leest de folders uit zijn gedeelde opslag (document
`folders/actueel`). Een wekelijkse taak draait het script op maandag en
woensdag en schrijft het resultaat in die opslag. Open je `index.html` via een
webserver, dan leest de app `aanbiedingen.json` in dezelfde map.

Websites van supermarkten veranderen af en toe. Geeft een winkel ineens
"Mislukt", dan moet de bijbehorende functie in `ophalen.py` worden aangepast.

## Hoe recepten worden gekozen

1. Elk recept krijgt punten voor ieder product uit de aanbieding.
2. Het weekmenu wordt dag voor dag gevuld met het best scorende recept, met
   deze regels:
   - niet twee dagen achter elkaar hetzelfde soort gerecht;
   - minstens één keer vis per week;
   - hooguit twee keer nitraatrijke groente (spinazie, rode biet), en nooit op
     twee dagen achter elkaar.
3. Elke nieuwe week of andere aanbieding geeft een nieuw menu.

## Babyportie

- In de recepten staat `{baby}` waar de naam van de baby komt. Zonder baby
  verdwijnen die zinnen uit de bereiding.
- Een ingrediënt dat een baby niet mag, krijgt in de app het label
  "niet voor <naam>". Bevat een gerecht iets wat een baby niet mag (zoals vis),
  dan staat er een waarschuwing bij de babyportie.
- Stappen voor een graanvrije babyportie ("Geen rijst: …") verschijnen alleen
  voor baby's die geen granen of gluten mogen.
- De babyportie gaat eruit vóór zout, bouillon, ketjap, currypasta of chili.
- Allergenen zoals vis, ei, pinda en melk staan per recept vermeld.

Het advies van het consultatiebureau of de arts gaat altijd voor.

## Recepten toevoegen

Recepten staan in de lijst `RECIPES` in `index.html`. Ingrediënten koppel je
via `I(hoeveelheid, eenheid, naam, productsleutel)` aan de producten in
`CATALOG`, zodat de app ze met de aanbiedingen kan matchen.
