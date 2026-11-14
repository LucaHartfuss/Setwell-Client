"""Massenwirkung am Gateway: was immer der Server auf down/setpoints antwortet, der Treiber bekommt nie einen Wert
ausserhalb der lokalen Grenzen, und der Regel-Worker laeuft weiter. 200 feindliche Antworten insgesamt, verteilt auf
die acht Hebelsaetze des Simulationstreibers (feste Saat), je Satz vollstaendig die Sonderwerte und jede Grenze um einen
Schritt ueber- und unterschritten. Jede Antwort geht auf einen offenen Tick. Beobachtet wird die Datei der simulierten
Anlage (plant.json) nach jeder Antwort: jeder in dieser Runde geschriebene Hebel liegt in der Tabelle. Die Grenzen und
Schritte stehen hier woertlich, nie aus dem Produktcode."""
import math
import random

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
GESAMT = 200  # feindliche Antworten insgesamt (Laufzeitbudget des Ordners: 30 s; je Satz mindestens die Pflichtfaelle)
JE_ANLAGE = GESAMT // len(ALLE)
SAAT = 20271031
SONDERWERTE = (math.nan, INF, -INF)
#: Werte, die kein Server senden duerfte: Extreme, falsche Typen, Text
UNSINN = (1e308, -1e308, 1.7976931348623157e308, 5e-324, -0.0, 0.0, 10**30, -(10**30), 2**63, True, False, None, "21.5",
          "NaN", "Infinity", [], [21.0], {}, {"wert": 1.0})
HUELLEN = ({}, {"fremd": 1}, {"learned": {"alles": [1, 2, 3]}}, {"reason": "x" * 400}, {"seq2": "fremd", "ts": 7})


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


def _antworten(satz: str, verteilsystem: str, rng: random.Random) -> list[dict]:
    """Genau JE_ANLAGE Antworten (ohne seq und ts): erst die Pflichtfaelle, dann zufaellige, immer genau die Hebel des
    Satzes oder ein davon abweichender Satz."""
    bereiche, schritte = BEREICHE[(satz, verteilsystem)], SCHRITTE[satz]
    hebel = tuple(bereiche)
    mitte = {name: _mitte(bereiche[name], schritte[name]) for name in hebel}
    pflicht = []
    for name in hebel:
        untere, obere = bereiche[name]
        for wert in (*SONDERWERTE, untere - schritte[name], obere + schritte[name]):
            pflicht.append({"status": "ok", "levers": {**mitte, name: wert}})
    zufall = []
    while len(pflicht) + len(zufall) < JE_ANLAGE:
        levers = {name: _zufallswert(rng, *bereiche[name], schritte[name]) for name in hebel}
        form = rng.random()
        if form < 0.1 and len(levers) > 1:
            del levers[rng.choice(sorted(levers))]  # ein Hebel fehlt
        elif form < 0.2:
            levers[rng.choice(("fremd", "heat_limit_x", "level", "min_flow"))] = rng.choice(UNSINN)  # fremdes Feld
        elif form < 0.25:
            levers = rng.choice((None, [], "alle", 5, [1.0, 2.0]))  # gar kein Objekt
        antwort = {"status": rng.choice(("ok", "ok", "ok", "skipped", "rejected", "unbekannt", None)), "levers": levers}
        antwort.update(rng.choice(HUELLEN))
        if rng.random() < 0.1:
            antwort["schema"] = rng.choice((3, 4, "5", 5.0, None, True))
        zufall.append(antwort)
    return pflicht + zufall


def _stand(welt) -> tuple[dict, dict]:
    """Hebel der simulierten Anlage und der Zeitpunkt ihres letzten Schreibens (Datei, schreibt nur die Laufzeit)."""
    stand = welt.anlagenstand()
    return stand["hebel"], stand["geschrieben_um"]


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
    assert len(antworten) == JE_ANLAGE
    welt.starten()
    welt.verbinden()
    welt.einrichten(1, konfiguration(satz, verteilsystem))
    welt.bis_nach_tagestick(22.0)  # warm: kein Boost, kein Notfall-Boost
    beantwortet: set[str] = set()
    geschrieben: dict[str, list[float]] = {name: [] for name in bereiche}
    _, zuletzt = _stand(welt)
    for nr, antwort in enumerate(antworten):
        seq = _offener_tick(welt, beantwortet)
        beantwortet.add(seq)
        welt.link.empfangen("down/setpoints", {"schema": 5, "kreis": 1, "seq": seq, "ts": welt.uhr.wand().isoformat(),
                                               **antwort})  # verarbeitet die folgende Runde (zwei Sekunden spaeter)
        welt.runde(2)
        hebel, um = _stand(welt)
        for name, wert in hebel.items():
            if um.get(name) != zuletzt.get(name):  # in dieser Runde geschrieben
                assert name in bereiche, f"Antwort {nr}: Schreibvorgang auf {name}"
                geschrieben[name].append(wert)
                untere, obere = bereiche[name]
                assert isinstance(wert, float) and untere <= wert <= obere, f"Antwort {nr}: {name} = {wert!r}"
        zuletzt = dict(um)
    for name, (untere, obere) in bereiche.items():  # die Antworten ueber den Grenzen kamen an und wurden genau begrenzt
        assert min(geschrieben[name]) == untere and max(geschrieben[name]) == obere, (name, set(geschrieben[name]))
    # der Regel-Worker laeuft weiter: eine gueltige Antwort auf den naechsten offenen Tick wird geschrieben
    seq = _offener_tick(welt, beantwortet)
    ziel = {name: _mitte(bereiche[name], SCHRITTE[satz][name]) for name in bereiche}
    welt.vom_server("down/setpoints", {"schema": 5, "kreis": 1, "seq": seq, "ts": welt.uhr.wand().isoformat(),
                                       "status": "ok", "reason": None, "levers": ziel, "learned": {}})
    welt.runde(2)
    assert {name: wert for name, wert in welt.anlagenstand()["hebel"].items() if name in ziel} == ziel
