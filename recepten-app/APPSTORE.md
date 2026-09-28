# Aanbiedingskeuken in de App Store: checklist voor later

Status: **bewaard voor later.** Eerst wordt de app verder uitgewerkt. Deze lijst
beschrijft wat er nodig is zodra de app helemaal naar wens is.

## Route

De app is nu een installeerbare web-app (PWA). Voor de App Store (en eventueel
Google Play) verpakken we dezelfde code in een echte app met
[Capacitor](https://capacitorjs.com). De bestaande `index.html` blijft de basis;
Capacitor voegt een iOS- en Android-project toe.

## Nodig van jou

- [ ] **Apple Developer Program**: € 99 per jaar, op naam van jou of je bedrijf.
      Aanmelden via developer.apple.com. Bij een bedrijf is een D-U-N-S-nummer
      nodig (gratis, duurt een paar dagen).
- [ ] Optioneel **Google Play Console**: eenmalig $ 25.
- [ ] Een **Mac met Xcode**, of een cloud-bouwdienst (bijvoorbeeld Codemagic of
      Ionic Appflow) als er geen Mac is.
- [ ] Een **iPhone** om de app vóór het insturen te testen (via TestFlight).

## Aanpassen aan de app vóór het insturen

- [ ] **Eigen opslag voor accounts.** De synchronisatie tussen apparaten
      (boodschappenlijst, kaarten, gezin, favorieten) gebruikt nu de opslag van
      claude.ai. Die werkt niet in een losse app. Kies: alles op het toestel
      bewaren (eenvoudig), of een eigen backend met inloggen (bijvoorbeeld
      Supabase of Firebase).
- [ ] **Folders ophalen via een eigen server.** Nu doet een wekelijkse
      Claude-taak dat. Voor een app in de winkel is een eigen, betrouwbare
      bron nodig (bijvoorbeeld een GitHub Action of een kleine server die
      `ophalen.py` draait en `aanbiedingen.json` publiceert).
- [ ] **Toestemming van supermarkten.** De app leest folders van de websites
      van Jumbo, Dirk, Aldi, DekaMarkt en Vomar. Voor een openbare app is het
      verstandig de gebruiksvoorwaarden na te lezen, of samen te werken met een
      folderdienst met een officiële koppeling. Apple kan vragen of je de
      rechten hebt op getoonde merken en inhoud.
- [ ] **Winkelbadges.** Nu zijn het eigen badges in winkelkleuren, geen
      officiële logo's. Zo laten, of toestemming vragen voor echte logo's.
- [ ] **Meer dan een website.** Apple wijst apps af die alleen een verpakte
      website zijn (richtlijn 4.2). Goede kandidaten om de app "echt" te maken:
      - de camera gebruiken om de streepjescode van een spaarkaart te scannen;
      - meldingen ("nieuwe folders", "vandaag in de aanbieding: …");
      - een widget met het recept van de dag;
      - spaarkaarten in Apple Wallet zetten.
- [ ] **Babyadvies.** De disclaimer over babyvoeding blijft zichtbaar. Controleer
      de teksten eventueel met een diëtist of het consultatiebureau.

## Nodig voor de winkelpagina

- [ ] Appnaam (bijvoorbeeld "Aanbiedingskeuken") en een ondertitel.
- [ ] Beschrijving, trefwoorden en categorie (Eten en drinken).
- [ ] Schermafbeeldingen voor iPhone (6,7" en 6,1") en eventueel iPad.
- [ ] Een **privacybeleid** op een openbare webpagina, en de privacyvragen in App
      Store Connect ("welke gegevens verzamel je?").
- [ ] Een support-URL of een e-mailadres voor vragen.
- [ ] Leeftijdsclassificatie (vragenlijst in App Store Connect).
- [ ] Het app-icoon in 1024×1024 (bron staat in `icons/`).

## Stappenplan als het zover is

1. Openstaande punten hierboven beslissen (opslag, folderbron, extra functies).
2. Capacitor toevoegen en de iOS- en Android-projecten genereren.
3. Native functies bouwen (bijvoorbeeld camera-scan en meldingen).
4. Testen via TestFlight met een paar gezinnen.
5. Winkelpagina, privacybeleid en schermafbeeldingen klaarzetten.
6. Insturen ter beoordeling (meestal 1 tot 3 dagen).
