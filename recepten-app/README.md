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
- **Aanbiedingen**: kies je supermarkt en tik aan wat er in de actie is. Je kunt
  ook tekst uit de online folder plakken; de app herkent dan zelf de producten.
- **Julièn**: leeftijd, wat Julièn niet mag, graanvrije vervangers, structuuradvies
  per leeftijd en een teller voor nitraatrijke groente.

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
