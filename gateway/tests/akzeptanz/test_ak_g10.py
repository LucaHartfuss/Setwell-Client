"""Massenwirkung am Gateway: was immer der Server auf down/setpoints antwortet, der Treiber bekommt nie einen Wert
ausserhalb der lokalen Grenzen, und der Regel-Worker laeuft weiter. Rund 500 feindliche Antworten insgesamt, verteilt
auf die acht Hebelsaetze des Simulationstreibers (feste Saat). Je Fall (Hebelsatz x Verteilsystem) gehen immer vollstaendig
und deterministisch hinaus: die Sonderwerte und jede Grenze um einen Schritt ueber- und unterschritten (je Hebel), jeder
Wert aus UNSINN auf allen Hebeln zugleich (darunter +-1e308), jedes fremde Feld, jeder fehlende Hebel, jede Form von
levers ohne Objekt, jeder status, jede Schema-Variante und jede Huelle. Das sind je Fall 50 bis 68 Pflichtantworten; der
Zufall fuellt nur bis JE_ANLAGE auf, die Pflichtfaelle werden nie gestrichen (Vaillant braucht mehr als JE_ANLAGE, daher
insgesamt 508). Jede Antwort geht auf einen offenen Tick. Beobachtet wird die Datei der simulierten Anlage (plant.json)
direkt nach dem Tick und nach jeder Antwort: jeder in dieser Runde geschriebene Hebel liegt in der Tabelle; eine
Antwort, die kein gueltiger Server senden duerfte (Nicht-Zahl, fremder oder fehlender Hebel, falsches Schema, falscher
status), schreibt gar nichts. Die Grenzen und Schritte stehen hier woertlich, nie aus dem Produktcode."""
import math
import random
from collections.abc import Callable
from typing import NamedTuple

import pytest
from anlagen import konfiguration

INF = math.inf
SAETZE = ("vaillant_vrc720", "weishaupt_wwp", "weishaupt_wwp_basis", "viessmann_vicare")
ALLE = [(satz, verteilsystem) for verteilsystem in ("Heizkoerper", "Fussbodenheizung") for satz in SAETZE]
#: lokale Grenzen (untere, obere) je (Hebelsatz, Verteilsystem) und Hebel (Regel 4), woertlich; der Mindestvorlauf
#: kommt wie jeder Hebel vom Server
BEREICHE = {
    ("vaillant_vrc720", "Heizkoerper"): {"curve": (0.4, 1.5), "room_setpoint": (15.0, 25.0), "heat_limit": (10.0, 23.0),
                                         "min_flow": (20.0, 30.0)},
    ("weishaupt_wwp", "Heizkoerper"): {"curve": (0.30, 1.00), "room_setpoint": (16.0, 25.0), "heat_limit": (10.0, 23.0)},
    ("weishaupt_wwp_basis", "Heizkoerper"): {"room_setpoint": (16.0, 25.0)},
    ("viessmann_vicare", "Heizkoerper"): {"curve": (0.4, 1.4), "level": (-13.0, 3.0), "room_setpoint": (15.0, 25.0)},
    ("vaillant_vrc720", "Fussbodenheizung"): {"curve": (0.10, 0.35), "room_setpoint": (15.0, 24.0),
                                              "heat_limit": (10.0, 20.0), "min_flow": (20.0, 30.0)},
    ("weishaupt_wwp", "Fussbodenheizung"): {"curve": (0.10, 0.50), "room_setpoint": (16.0, 24.0),
                                            "heat_limit": (10.0, 20.0)},
    ("weishaupt_wwp_basis", "Fussbodenheizung"): {"room_setpoint": (16.0, 24.0)},
    ("viessmann_vicare", "Fussbodenheizung"): {"curve": (0.2, 0.4), "level": (-5.0, 3.0), "room_setpoint": (15.0, 24.0)},
}
#: Schrittweite je Hebelsatz und Hebel, woertlich
SCHRITTE = {
    "vaillant_vrc720": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.1, "min_flow": 0.1},
    "weishaupt_wwp": {"curve": 0.05, "room_setpoint": 0.5, "heat_limit": 0.5},
    "weishaupt_wwp_basis": {"room_setpoint": 0.5},
    "viessmann_vicare": {"curve": 0.1, "level": 1.0, "room_setpoint": 1.0},
}
GESAMT = 500  # Groessenordnung der feindlichen Antworten (Laufzeitbudget des Ordners: 45 s); je Fall mindestens die Pflichtfaelle
JE_ANLAGE = GESAMT // len(ALLE)
SAAT = 20271031
SONDERWERTE = (math.nan, INF, -INF)
#: Werte, die kein Server senden duerfte: Extreme, falsche Typen, Text
UNSINN = (1e308, -1e308, 1.7976931348623157e308, 5e-324, -0.0, 0.0, 10**30, -(10**30), 2**63, True, False, None, "21.5",
          "NaN", "Infinity", [], [21.0], {}, {"wert": 1.0})
HUELLEN = ({}, {"fremd": 1}, {"learned": {"alles": [1, 2, 3]}}, {"reason": "x" * 400}, {"seq2": "fremd", "ts": 7})
FREMDE_FELDER = ("fremd", "heat_limit_x", "level", "min_flow")
KEIN_OBJEKT = (None, [], "alle", 5, [1.0, 2.0])
STATUS = ("ok", "skipped", "rejected", "unbekannt", None)
SCHEMA = (3, 4, "5", 5.0, None, True)


class Antwort(NamedTuple):
    """Eine feindliche Antwort ohne seq, ts und Kreis; `bauen` bekommt die Hebel der Anlage, damit eine abzulehnende
    Antwort gueltige Werte traegt, die von jedem Stand abweichen (sonst fiele ein falsch geschriebener Wert nicht auf)."""
    kategorie: str
    bauen: Callable[[dict], dict]
    ablehnen: bool  # ein gueltiger Server haette das nie gesendet: es wird nichts geschrieben


def _mitte(bereich: tuple[float, float], schritt: float) -> float:
    return round(round((bereich[0] + bereich[1]) / 2 / schritt) * schritt, 6)


def _zufallswert(rng: random.Random, untere: float, obere: float, schritt: float):
    """Ein feindlicher Wert: Sonderwert, Unsinn, Zufall weit ausserhalb oder innerhalb, knapp ueber einer Grenze, riesig."""
    weite = obere - untere
    return rng.choice((
        lambda: rng.choice(SONDERWERTE), lambda: rng.choice(UNSINN),
        lambda: rng.uniform(untere - 5 * weite, obere + 5 * weite), lambda: rng.uniform(untere, obere),
        lambda: untere - rng.choice((1, 2, 3)) * schritt, lambda: obere + rng.choice((1, 2, 3)) * schritt,
        lambda: rng.choice((-1, 1)) * 10 ** rng.randint(3, 300) * rng.random(),
    ))()


def _ist_zahl(wert) -> bool:
    """Ein int oder float, kein bool, endlich: nur das darf als Hebelwert ankommen."""
    return isinstance(wert, (int, float)) and not isinstance(wert, bool) and math.isfinite(wert)


def _typ_kategorie(nr: int, hebel: str | None) -> str:
    """Zahlen gehen auf allen Hebeln zugleich hinaus, Nicht-Zahlen je Hebel einzeln."""
    return f"typ:{nr}" if hebel is None else f"typ:{nr}:{hebel}"


def _kategorien(satz: str, verteilsystem: str) -> set[str]:
    """Alles, was je Fall mindestens einmal hinausgehen muss (gegen kuenftiges Kuerzen der Antworten)."""
    hebel = tuple(BEREICHE[(satz, verteilsystem)])
    return ({f"grenze:{name}:{art}" for name in hebel for art in ("unter", "ueber")}
            | {f"sonder:{name}:{wert}" for name in hebel for wert in SONDERWERTE}
            | {_typ_kategorie(nr, name) for nr, wert in enumerate(UNSINN) for name in (hebel if not _ist_zahl(wert) else (None,))}
            | {f"fremd:{name}" for name in FREMDE_FELDER}
            | {f"fehlt:{name}" for name in hebel} | {f"kein_objekt:{nr}" for nr in range(len(KEIN_OBJEKT))}
            | {f"status:{wert!r}" for wert in STATUS} | {f"schema:{wert!r}" for wert in SCHEMA}
            | {f"huelle:{nr}" for nr in range(len(HUELLEN))})


def _pflichtfaelle(satz: str, verteilsystem: str) -> list[Antwort]:
    """Je Fall alle Huell- und Typkategorien, deterministisch; sie haben Vorrang vor dem Zufallsanteil."""
    bereiche, schritte = BEREICHE[(satz, verteilsystem)], SCHRITTE[satz]
    hebel = tuple(bereiche)
    mitte = {name: _mitte(bereiche[name], schritte[name]) for name in hebel}

    def abweichend(stand: dict) -> dict:
        """Gueltige Werte (je Hebel eine Grenze), die vom jetzigen Stand der Anlage abweichen."""
        return {name: bereiche[name][1] if stand[name] != bereiche[name][1] else bereiche[name][0] for name in hebel}

    faelle = []
    for name in hebel:
        untere, obere = bereiche[name]
        for art, wert in (("unter", untere - schritte[name]), ("ueber", obere + schritte[name])):  # genau ein Schritt
            faelle.append(Antwort(f"grenze:{name}:{art}",
                                  lambda stand, name=name, wert=wert: {"status": "ok", "levers": {**mitte, name: wert}},
                                  False))
        for wert in SONDERWERTE:
            faelle.append(Antwort(f"sonder:{name}:{wert}",
                                  lambda stand, name=name, wert=wert: {"status": "ok",
                                                                       "levers": {**abweichend(stand), name: wert}},
                                  True))
    for nr, wert in enumerate(UNSINN):
        if _ist_zahl(wert):  # derselbe Zahlenwert auf allen Hebeln: er wird je Hebel begrenzt
            faelle.append(Antwort(_typ_kategorie(nr, None),
                                  lambda stand, wert=wert: {"status": "ok", "levers": dict.fromkeys(hebel, wert)},
                                  False))
            continue
        for name in hebel:  # eine Nicht-Zahl auf genau einem Hebel (die uebrigen gueltig): die Antwort ist ungueltig
            faelle.append(Antwort(_typ_kategorie(nr, name),
                                  lambda stand, name=name, wert=wert: {"status": "ok", "levers": {
                                      **abweichend(stand), name: wert}},
                                  True))
    for name in FREMDE_FELDER:
        faelle.append(Antwort(f"fremd:{name}",
                              lambda stand, name=name: {"status": "ok", "levers": {**abweichend(stand), name: 21.5}},
                              name not in hebel))
    for name in hebel:
        faelle.append(Antwort(f"fehlt:{name}",
                              lambda stand, name=name: {"status": "ok", "levers": {
                                  k: v for k, v in abweichend(stand).items() if k != name}}, True))
    for nr, form in enumerate(KEIN_OBJEKT):
        faelle.append(Antwort(f"kein_objekt:{nr}", lambda stand, form=form: {"status": "ok", "levers": form}, True))
    for wert in STATUS:
        faelle.append(Antwort(f"status:{wert!r}",
                              lambda stand, wert=wert: {"status": wert, "levers": abweichend(stand)},
                              wert not in ("ok", "skipped")))
    for wert in SCHEMA:  # gueltig ist nur das ganzzahlige 5
        faelle.append(Antwort(f"schema:{wert!r}",
                              lambda stand, wert=wert: {"schema": wert, "status": "ok", "levers": abweichend(stand)},
                              True))
    for nr, huelle in enumerate(HUELLEN):
        faelle.append(Antwort(f"huelle:{nr}",
                              lambda stand, huelle=huelle: {"status": "ok", "levers": mitte, **huelle}, False))
    return faelle


def _antworten(satz: str, verteilsystem: str, rng: random.Random) -> list[Antwort]:
    """Alle Pflichtfaelle, danach zufaellige bis JE_ANLAGE (mehr Pflichtfaelle: keine zufaelligen), gemischt."""
    bereiche, schritte = BEREICHE[(satz, verteilsystem)], SCHRITTE[satz]
    hebel = tuple(bereiche)
    antworten = _pflichtfaelle(satz, verteilsystem)
    for _ in range(JE_ANLAGE - len(antworten)):
        levers = {name: _zufallswert(rng, *bereiche[name], schritte[name]) for name in hebel}
        form = rng.random()
        if form < 0.1 and len(levers) > 1:
            del levers[rng.choice(sorted(levers))]  # ein Hebel fehlt
        elif form < 0.2:
            levers[rng.choice(FREMDE_FELDER)] = rng.choice(UNSINN)  # fremdes Feld
        elif form < 0.25:
            levers = rng.choice(KEIN_OBJEKT)  # gar kein Objekt
        antwort = {"status": rng.choice(("ok", "ok", "ok", "skipped", "rejected", "unbekannt", None)), "levers": levers}
        antwort.update(rng.choice(HUELLEN))
        if rng.random() < 0.1:
            antwort["schema"] = rng.choice(SCHEMA)
        antworten.append(Antwort("zufall", lambda stand, antwort=antwort: antwort, False))
    rng.shuffle(antworten)
    return antworten


def _stand(welt) -> tuple[dict, dict]:
    """Hebel der simulierten Anlage und der Zeitpunkt ihres letzten Schreibens (Datei, schreibt nur die Laufzeit)."""
    stand = welt.anlagenstand()
    return stand["hebel"], stand["geschrieben_um"]


def _geschrieben(welt, wo: str, zuletzt: dict, bereiche: dict) -> tuple[dict, dict]:
    """Die seit `zuletzt` geschriebenen Hebel (Name -> Wert), jeder in der Tabelle; dazu der neue Schreibstand."""
    hebel, um = _stand(welt)
    neu = {}
    for name, wert in hebel.items():
        if um.get(name) != zuletzt.get(name):
            assert name in bereiche, f"{wo}: Schreibvorgang auf {name}"
            untere, obere = bereiche[name]
            assert isinstance(wert, float) and untere <= wert <= obere, f"{wo}: {name} = {wert!r}"
            neu[name] = wert
    return neu, dict(um)


def _offener_tick(welt, beantwortet: set[str]) -> str:
    """Der Tick des naechsten Tages (warm: kein Boost), dessen Folgenummer, auf die noch nichts kam."""
    seq = welt.neuer_tag_mit_tick(22.0)
    assert seq not in beantwortet, "kein offener Tick"
    return seq


@pytest.mark.akzeptanz("AK-G10-01")
@pytest.mark.parametrize("satz,verteilsystem", ALLE, ids=[f"{s}-{v}" for s, v in ALLE])
def test_ak_g10_01_feindliche_antworten_bleiben_in_den_grenzen(welt, satz, verteilsystem):
    bereiche = BEREICHE[(satz, verteilsystem)]
    rng = random.Random(SAAT + ALLE.index((satz, verteilsystem)))
    antworten = _antworten(satz, verteilsystem, rng)
    pflicht = _kategorien(satz, verteilsystem)
    assert len(antworten) >= max(JE_ANLAGE, len(pflicht))  # die Pflichtfaelle werden nie gekuerzt
    welt.starten()
    welt.verbinden()
    welt.einrichten(1, konfiguration(satz, verteilsystem))
    welt.bis_nach_tagestick(22.0)  # warm: kein Boost, kein Notfall-Boost
    beantwortet: set[str] = set()
    gesendet: set[str] = set()
    geschrieben: dict[str, list[float]] = {name: [] for name in bereiche}
    _, zuletzt = _stand(welt)
    for nr, eintrag in enumerate(antworten):
        seq = _offener_tick(welt, beantwortet)
        beantwortet.add(seq)
        _, zuletzt = _geschrieben(welt, f"Tagesrunde vor Antwort {nr}", zuletzt, bereiche)  # Schreiben im Tick selbst
        antwort = eintrag.bauen(_stand(welt)[0])
        welt.link.empfangen("down/setpoints", {"schema": 5, "kreis": 1, "seq": seq, "ts": welt.uhr.wand().isoformat(),
                                               **antwort})  # verarbeitet die folgende Runde (zwei Sekunden spaeter)
        welt.runde(2)
        neu, zuletzt = _geschrieben(welt, f"Antwort {nr} ({eintrag.kategorie})", zuletzt, bereiche)
        if eintrag.ablehnen:
            assert not neu, f"Antwort {nr} ({eintrag.kategorie}) haette nichts schreiben duerfen: {neu}"
        for name, wert in neu.items():
            geschrieben[name].append(wert)
        gesendet.add(eintrag.kategorie)
    assert pflicht <= gesendet, sorted(pflicht - gesendet)  # jede Huell- und Typkategorie kam je Fall an
    for name, (untere, obere) in bereiche.items():  # die Antworten ueber den Grenzen kamen an und wurden genau begrenzt
        assert min(geschrieben[name]) == untere and max(geschrieben[name]) == obere, (name, set(geschrieben[name]))
    # der Regel-Worker laeuft weiter: eine gueltige Antwort auf den naechsten offenen Tick wird geschrieben
    seq = _offener_tick(welt, beantwortet)
    ziel = {name: _mitte(bereiche[name], SCHRITTE[satz][name]) for name in bereiche}
    welt.vom_server("down/setpoints", {"schema": 5, "kreis": 1, "seq": seq, "ts": welt.uhr.wand().isoformat(),
                                       "status": "ok", "reason": None, "levers": ziel, "learned": {}})
    welt.runde(2)
    assert {name: wert for name, wert in welt.anlagenstand()["hebel"].items() if name in ziel} == ziel
