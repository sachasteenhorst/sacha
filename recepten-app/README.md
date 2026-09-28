# Aanbiedingskeuken

Een webapp die elke dag een gezond recept laat zien, gekozen op basis van wat
er deze week in de aanbieding is bij de supermarkt. Bij elk recept staat
hoe je een portie maakt voor **Julièn** (8 maanden, geen brood en geen granen).

Open `index.html` in een browser. Er is geen server of installatie nodig.
Alles wat je instelt, blijft bewaard in de browser.

## Onderdelen

- **Vandaag**: het recept van de dag, de producten uit de aanbieding die erin
  zitten, de bereiding en een apart stappenplan voor Julièn. Er komt ook elke dag
  een graanvrij tussendoortje bij. Met "Ander recept" wissel je het gerecht.
- **Week**: het weekmenu en een boodschappenlijst om af te vinken. De
  hoeveelheden passen zich aan het aantal volwassenen aan, met een portie voor
  Julièn inbegrepen.
- **Aanbiedingen**: een overzicht van alles wat in jouw winkels in de
  aanbieding is. Tik op een product om te zien waar het het goedkoopst is.

Bovenaan kies je van welke winkels je de aanbiedingen wilt zien (Jumbo, Dirk,
Aldi, Albert Heijn, Lidl, Plus). Bij elk ingrediënt staat de goedkoopste actie
in jouw winkels; tik erop voor de vergelijking met alle winkels, op prijs per
kilo als die bekend is. Aanbiedingen van Albert Heijn, Lidl en Plus voeg je
zelf toe onder "Zelf aanbiedingen toevoegen".
- **Julièn**: leeftijd, wat Julièn niet mag, graanvrije vervangers, structuuradvies
  per leeftijd en een teller voor nitraatrijke groente.
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

## Regels voor Julièn

- Brood, pasta, rijst, wraps en andere granen horen alleen bij de volwassenen.
  Ze staan in de app als "niet voor Julièn" gemarkeerd. Julièn krijgt aardappel,
  zoete aardappel, pastinaak, pompoen of peulvruchten.
- De portie van Julièn gaat eruit vóór zout, bouillon, ketjap, currypasta of
  chili. Die bevatten zout en soms tarwe.
- Allergenen zoals vis, ei, pinda en melk staan per recept vermeld.

Het advies van het consultatiebureau of de arts gaat altijd voor.

## Recepten toevoegen

Recepten staan in de lijst `RECIPES` in `index.html`. Ingrediënten koppel je
via `I(hoeveelheid, eenheid, naam, productsleutel)` aan de producten in
`CATALOG`, zodat de app ze met de aanbiedingen kan matchen.
