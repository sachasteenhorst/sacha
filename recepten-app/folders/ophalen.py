"""Haalt de actuele aanbiedingen op uit de online folders van supermarkten
en koppelt ze aan de producten die de recepten in de Aanbiedingskeuken
gebruiken.

Gebruik:
    python3 recepten-app/folders/ophalen.py [uitvoer.json]

Schrijft standaard naar recepten-app/aanbiedingen.json. Het resultaat is
precies het document dat de app leest uit de opslag `folders/actueel`.

Albert Heijn blokkeert geautomatiseerde verzoeken (Akamai-botbescherming);
Lidl en Plus laden hun aanbiedingen pas in de browser. Die winkels krijgen
daarom een status en blijven in de app handmatig aan te vullen.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Sleutel -> zoekwoorden. Houd gelijk aan CATALOG in index.html.
PRODUCTEN: dict[str, list[str]] = {
    "broccoli": ["broccoli"],
    "bloemkool": ["bloemkool"],
    "wortel": ["wortel", "wortels", "wortelen", "winterpeen", "bospeen", "peen"],
    "pompoen": ["pompoen", "pompoenen", "flespompoen", "butternut"],
    "courgette": ["courgette", "courgettes"],
    "zoete-aardappel": ["zoete aardappel", "zoete aardappelen", "zoete aardappels", "bataat"],
    "aardappel": ["aardappel", "aardappelen", "aardappels", "aardappeltjes", "krieltjes", "kriel"],
    "spinazie": ["spinazie"],
    "prei": ["prei", "preien"],
    "paprika": ["paprika", "paprika's", "paprikas"],
    "tomaat": ["tomaat", "tomaten", "cherrytomaten", "trostomaten", "snoeptomaten"],
    "champignons": ["champignon", "champignons", "kastanjechampignons"],
    "sperziebonen": ["sperziebonen", "sperzieboontjes", "haricots verts"],
    "doperwten": ["doperwten", "doperwtjes", "erwten", "erwtjes"],
    "rode-biet": ["rode biet", "rode bieten", "bietjes", "bieten"],
    "pastinaak": ["pastinaak", "pastinaken"],
    "ui": ["ui", "uien", "rode ui", "rode uien"],
    "appel": ["appel", "appels", "elstar", "jonagold", "goudrenet"],
    "peer": ["peer", "peren", "conference", "doyenne"],
    "banaan": ["banaan", "bananen"],
    "avocado": ["avocado", "avocado's", "avocados"],
    "blauwe-bessen": ["blauwe bessen", "blauwe bes", "bosbessen"],
    "mango": ["mango", "mango's", "mangos"],
    "aardbeien": ["aardbei", "aardbeien"],
    "kipfilet": ["kipfilet", "kipfilets", "kippenborst", "kipreepjes"],
    "kippendij": ["kippendij", "kippendijen", "kippendijfilet", "dijfilet"],
    "kalkoen": ["kalkoen", "kalkoenfilet"],
    "gehakt": ["gehakt", "rundergehakt", "slagersgehakt", "half-om-half", "half om half"],
    "zalm": ["zalm", "zalmfilet", "zalmfilets", "zalmmoten"],
    "witvis": ["kabeljauw", "kabeljauwfilet", "koolvis", "koolvisfilet", "witvis", "pollak", "heek", "schelvis"],
    "eieren": ["eieren", "scharreleieren", "vrije-uitloopeieren"],
    "yoghurt": ["yoghurt", "volle yoghurt", "griekse yoghurt"],
    "feta": ["feta"],
    "kaas": ["geraspte kaas", "kaas", "gouda"],
    "kikkererwten": ["kikkererwten"],
    "linzen": ["linzen", "rode linzen"],
    "bonen": ["kidneybonen", "zwarte bonen", "bruine bonen", "witte bonen"],
    "kokosmelk": ["kokosmelk"],
    "passata": ["passata", "tomatenblokjes", "gezeefde tomaten"],
    "pindakaas": ["pindakaas"],
    "pasta": ["pasta", "spaghetti", "penne", "fusilli", "lasagnebladen"],
    "rijst": ["rijst", "zilvervliesrijst", "basmatirijst"],
    "wraps": ["wrap", "wraps", "tortilla", "tortillas"],
    "brood": ["brood", "volkorenbrood", "volkoren brood", "hamburgerbroodjes", "bolletjes", "pita", "pitabroodjes"],
    "bloem": ["bloem", "tarwebloem", "patentbloem"],
    "spek": ["spekjes", "spekreepjes", "spekblokjes", "ontbijtspek", "bacon"],
    "stoofvlees": ["stoofvlees", "runderstoofvlees", "sukadelappen", "riblappen"],
    "room": ["kookroom", "slagroom"],
    "rode-kool": ["rode kool", "rodekool"],
    "komkommer": ["komkommer", "komkommers"],
}

# Titels met deze woorden zijn bewerkte producten of geen eten voor ons
# (bijvoorbeeld "Johma zalm salade" of "kattensnacks zalm").
UITSLUITEN = re.compile(
    r"salade|kat(ten)?|hond(en)?|snack|chips|saus|soep|\bsap\b|drink|drank|\bijs\b|koek|"
    r"taart|pizza|maaltijd|spread|smoothie|shampoo|zeep|thee|snoep|reep|wafel|cracker|"
    r"toast|borrel|kroket|frikandel|chocola|siroop|limonade|vla|pudding|dessert|"
    r"schnitzel|gerookt|haring|sushi|nuggets|pannenkoek|ontbijt|muesli|granola|"
    r"cruesli|babyvoeding|knijpfruit|parfum|wasmiddel|luier|croissant|salami|broodje|"
    r"\bham\b|beleg|vleeswaren|plakjes|spread|bami|nasi|tonight|aardappel anders",
    re.I,
)
_WOORD = "a-zà-ÿ"
_PATRONEN = {
    k: re.compile("|".join(
        rf"(?<![{_WOORD}]){re.escape(w)}(?![{_WOORD}])" for w in sorted(ws, key=len, reverse=True)
    ), re.I)
    for k, ws in PRODUCTEN.items()
}


def koppel(titel: str) -> list[str]:
    """Geeft de productsleutels terug die in een folder-titel voorkomen."""
    if UITSLUITEN.search(titel):
        return []
    keys = [k for k, p in _PATRONEN.items() if p.search(titel)]
    # "zoete aardappelen" is geen gewone aardappel
    if "zoete-aardappel" in keys and not re.search(r"(?<!zoete )aardappel", titel, re.I):
        keys.remove("aardappel")
    return keys


def haal(url: str, pogingen: int = 3, browser: bool = False) -> str:
    """Haalt een pagina op; sommige winkels weigeren af en toe (403), dan opnieuw.
    browser=True stuurt de headers van een gewone browser mee (nodig voor DekaMarkt)."""
    headers = {"User-Agent": UA}
    if browser:
        headers.update({"Accept": "text/html,application/json;q=0.9,*/*;q=0.8", "Accept-Language": "nl-NL,nl;q=0.9"})
    for poging in range(pogingen):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429, 503) or poging == pogingen - 1:
                raise
            time.sleep(10 * (poging + 1))
    raise RuntimeError("onbereikbaar")


DAGEN = ["ma", "di", "wo", "do", "vr", "za", "zo"]
MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def tm(iso_datum: str) -> str:
    """'2026-09-29' -> 't/m di 29 sep'"""
    try:
        d = datetime.strptime(iso_datum[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    return f"t/m {DAGEN[d.weekday()]} {d.day} {MAANDEN[d.month - 1]}"


def euro(v) -> str:
    return f"€ {v:.2f}".replace(".", ",") if isinstance(v, (int, float)) and v else ""


def per_kg(prijs, omschrijving: str):
    """Prijs per kilo uit bijvoorbeeld '500 gram' of '4 stuks, 1 kg'; None als dat niet kan."""
    if not isinstance(prijs, (int, float)) or not omschrijving:
        return None
    if re.search(r"bijv|\bà\b|\bper\s+\d+\b|\d+\s*(bakken|zakken|pakken|stuks)", omschrijving, re.I):
        return None  # combinatieactie (bijv. 3 bakken à 1 kilo): de kiloprijs is niet betrouwbaar te bepalen
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|kilo|gram|gr|g)\b", omschrijving, re.I)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    kg = n if m.group(2).lower() in ("kg", "kilo") else n / 1000
    return round(prijs / kg, 2) if kg > 0 else None


def jumbo_prijs(actie: str):
    """Leest een prijs uit Jumbo's actietekst: (prijs, prijs per kg)."""
    t = actie.replace("\u20ac", "").strip()
    if m := re.match(r"^(\d+) voor (\d+[.,]\d{2})$", t):
        return round(float(m.group(2).replace(",", ".")) / int(m.group(1)), 2), None
    if m := re.match(r"^voor (\d+[.,]\d{2})$", t):
        return float(m.group(1).replace(",", ".")), None
    if m := re.match(r"^(\d+[.,]\d{2}) per (\d+) ?(gram|g|kilo|kg)$", t):
        p = float(m.group(1).replace(",", "."))
        n = float(m.group(2)) * (1 if m.group(3) in ("kilo", "kg") else 0.001)
        return None, round(p / n, 2)
    return None, None


# ---------- Per winkel ----------

def jumbo() -> dict:
    s = haal("https://www.jumbo.com/aanbiedingen/nu")
    s = re.sub(r"<script.*?</script>|<style.*?</style>", "", s, flags=re.S)
    toks = [html.unescape(t).replace("​", "").strip() for t in re.split(r"<[^>]+>", s)]
    toks = [t for t in toks if t]
    periode = re.compile(r"^(ma|di|wo|do|vr|za|zo) \d+ .*t/m")
    items, geldig = [], ""
    for i, t in enumerate(toks):
        if periode.match(t) and i > 0 and i + 1 < len(toks):
            geldig = geldig or t
            prijs, kg = jumbo_prijs(toks[i + 1])
            items.append({"titel": toks[i - 1], "actie": toks[i + 1], "geldig": t,
                          "prijs": prijs, "perKg": kg, "eenheid": ""})
    return {"geldig": geldig, "items": items, "bron": "https://www.jumbo.com/aanbiedingen/nu"}


def _nuxt(s: str) -> list:
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', s, re.S)
    if not m:
        raise ValueError("geen __NUXT_DATA__ gevonden")
    return json.loads(m.group(1))


def dirk() -> dict:
    return detailresult("https://www.dirk.nl/aanbiedingen")


def dekamarkt() -> dict:
    # DekaMarkt hoort net als Dirk bij Detailresult en gebruikt dezelfde site-opzet
    return detailresult("https://www.dekamarkt.nl/aanbiedingen", browser=True)


def detailresult(bron: str, browser: bool = False) -> dict:
    a = _nuxt(haal(bron, browser=browser))
    val = lambda x: a[x] if isinstance(x, int) and 0 <= x < len(a) else x
    items, tot = [], ""
    for v in a:
        if isinstance(v, dict) and "offerId" in v and "headerText" in v and "offerPrice" in v:
            titel = val(v["headerText"])
            prijs, normaal = val(v["offerPrice"]), val(v.get("normalPrice"))
            eind = str(val(v.get("endDate")) or "")[:10]
            tot = tot or eind
            if not isinstance(titel, str):
                continue
            actie = euro(prijs)
            if isinstance(normaal, (int, float)) and normaal and isinstance(prijs, (int, float)) and normaal > prijs:
                actie += f" (was {euro(normaal)})"
            eenheid = str(val(v.get("packaging")) or "").strip().rstrip(".")
            items.append({"titel": titel, "actie": actie, "geldig": tm(eind),
                          "prijs": prijs if isinstance(prijs, (int, float)) else None,
                          "perKg": per_kg(prijs, eenheid), "eenheid": eenheid})
    return {"geldig": tm(tot), "items": items, "bron": bron}


def aldi() -> dict:
    s = haal("https://www.aldi.nl/aanbiedingen.html")
    s = s.replace('\\"', '"')
    items, tot = [], ""
    for m in re.finditer(r'"name":"([^"]{2,120})","currentPrice":\{"priceValue":([\d.]+)(.*?)"validUntil":(\d+)\}', s):
        titel, prijs, rest = html.unescape(m.group(1)), float(m.group(2)), m.group(3)
        promo = re.search(r'"promoText1":"([^"]+)"', rest)
        eind = datetime.fromtimestamp(int(m.group(4)), timezone.utc).strftime("%Y-%m-%d")
        tot = tot or eind
        actie = euro(prijs) + (f" ({promo.group(1)})" if promo else "")
        eenheid = html.unescape(re.search(r'"salesUnit":"([^"]*)"', s[max(0, m.start() - 400):m.start()] + '"salesUnit":""').group(1))
        base = re.search(r'"basePriceValue":([\d.]+),"basePriceScale":"kg"', rest)
        items.append({"titel": titel, "actie": actie, "geldig": tm(eind), "prijs": prijs,
                      "perKg": float(base.group(1)) if base else per_kg(prijs, eenheid), "eenheid": eenheid})
    return {"geldig": tm(tot), "items": items, "bron": "https://www.aldi.nl/aanbiedingen.html"}


# Vomar publiceert zijn weekfolder alleen als bladerfolder (Publitas). De tekstlaag daarvan
# staat door elkaar, dus we lezen alleen welke producten erin staan, zonder prijs.
VOMAR_FOLDER = "https://view.publitas.com/folder-deze-week"
VOMAR_UIT = re.compile(
    r"brood|bollen|meergranen|tarwe|versgesneden|flinterdun|vleeswaren|per 100 gram|salade|maaltijd|"
    r"diepvries|crème de la crème|chips|saus|pizza|soep|\bmix\b|fles|liter|ricotta|zakje|limonade",
    re.I,
)


def vomar() -> dict:
    req = urllib.request.Request(VOMAR_FOLDER, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:  # volgt de doorverwijzing naar de folder van deze week
        folder = r.geturl().rstrip("/")
    spreads = json.loads(haal(folder + "/spreads.json"))
    week = re.search(r"week-(\d+)", folder)
    items = []
    for spread in spreads:
        for page in spread.get("pages", []):
            blokken = [b.strip() for b in re.split(r"\n\s*\n", page.get("text") or "") if b.strip()]
            for i, blok in enumerate(blokken):
                titel = re.sub(r"\s+", " ", blok)
                omgeving = " ".join(blokken[max(0, i - 2):i + 2])
                if len(titel) > 80 or VOMAR_UIT.search(omgeving):
                    continue
                items.append({"titel": titel, "actie": "in de weekfolder", "geldig": f"week {week.group(1)}" if week else "",
                              "prijs": None, "perKg": None, "eenheid": ""})
    return {"geldig": f"week {week.group(1)}" if week else "", "items": items, "bron": folder + "/"}


WINKELS = {"Jumbo": jumbo, "Dirk": dirk, "Aldi": aldi, "DekaMarkt": dekamarkt, "Vomar": vomar}
NIET_AUTOMATISCH = {
    "Albert Heijn": "ah.nl blokkeert automatisch ophalen. Zet de bonus met de hand aan.",
    "Lidl": "Lidl laadt de folder pas in de browser. Zet de aanbiedingen met de hand aan.",
    "Plus": "Plus laadt de folder pas in de browser. Zet de aanbiedingen met de hand aan.",
}


VLEES = {"kipfilet", "kippendij", "kalkoen", "gehakt"}


def is_beleg(eenheid: str) -> bool:
    """Een bakje van minder dan 200 gram kip of kalkoen is broodbeleg, geen vlees om te koken."""
    m = re.fullmatch(r"\s*(\d+)\s*(g|gr|gram)\s*", eenheid, re.I)
    return bool(m) and int(m.group(1)) < 200


def main() -> None:
    uit = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "aanbiedingen.json"
    try:  # vorige uitvoer: terugval voor een winkel die nu niet lukt
        vorige = json.loads(uit.read_text(encoding="utf-8")).get("winkels", {})
    except (OSError, ValueError):
        vorige = {}
    doc = {"bijgewerkt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "winkels": {}}
    for naam, fn in WINKELS.items():
        try:
            r = fn()
        except Exception as e:  # een kapotte winkel mag de rest niet tegenhouden
            oud = vorige.get(naam, {})
            if oud.get("items"):
                doc["winkels"][naam] = {**oud, "status": "vorige",
                                        "melding": "Verversen lukte niet. Dit zijn de acties van de vorige keer."}
            else:
                doc["winkels"][naam] = {"status": "fout", "melding": "Ophalen lukte deze keer niet."}
            print(f"{naam}: mislukt ({e})", file=sys.stderr)
            continue
        gekoppeld, gezien = [], set()
        for it in r["items"]:
            for k in koppel(it["titel"]):
                if k in VLEES and is_beleg(it.get("eenheid") or ""):
                    continue
                if (k, it["titel"]) in gezien:
                    continue
                gezien.add((k, it["titel"]))
                gekoppeld.append({"k": k, "titel": it["titel"][:90], "actie": it["actie"][:60],
                                  "prijs": it.get("prijs"), "perKg": it.get("perKg"),
                                  "eenheid": (it.get("eenheid") or "")[:40]})
        doc["winkels"][naam] = {"status": "ok", "geldig": r["geldig"], "bron": r["bron"],
                                "aantal": len(r["items"]), "items": gekoppeld}
        print(f"{naam}: {len(r['items'])} acties, {len(gekoppeld)} gekoppeld", file=sys.stderr)
    for naam, melding in NIET_AUTOMATISCH.items():
        doc["winkels"][naam] = {"status": "handmatig", "melding": melding}
    uit.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(uit)


if __name__ == "__main__":
    main()
