"""Akzeptanzszenario G-7 der Gateway-Laufzeit: verstellt die Hersteller-App eine Einstellung immer wieder, setzt das
Gateway sie nur begrenzt oft und mit Abstand zurueck (Abrufkontingent der Hersteller-Cloud, je Anlage). Die Grenzen
stehen hier woertlich, nie aus dem Produktcode."""
import pytest
from anlagen import konfiguration

#: Werte des Dienstes je Hebelsatz (innerhalb der lokalen Grenzen, verschieden von den Startwerten der Anlage)
REGELWERTE = {"vaillant_vrc720": {"curve": 0.8, "room_setpoint": 21.5, "heat_limit": 17.0, "min_flow": 22.0},
              "viessmann_vicare": {"curve": 0.8, "level": 1.0, "room_setpoint": 21.0}}
#: hoechstens so viele Rueckschreibversuche am Tag, je Hebelsatz
VERSUCHE_AM_TAG = {"vaillant_vrc720": 6, "viessmann_vicare": 4}
VERSTELLUNGEN = 8
ABSTAND_MINDESTENS = 1800
#: Takt der App je Hebelsatz. Viessmann: schneller, als das Gateway zuruecksetzen darf (der Abstand der Versuche kommt
#: vom Gateway). Vaillant: so schnell, wie die Anlage ein Schreiben uebernimmt (Wartezeit 2100 s plus zwei Pruefungen);
#: schneller sieht das Gateway die Verstellung gar nicht als neu
ABSTAND_DER_APP = {"vaillant_vrc720": 2700, "viessmann_vicare": 1500}
EINSCHWINGEN = 2700  # die Anlage meldet eigene Schreibvorgaenge erst nach der Wartezeit (Vaillant 2100 s)
SCHRITT = 300
RAUM = 22.0  # warm genug: kein Boost
LIMIT_TEXT = "Abrufkontingent"  # nur die Limit-Meldung nennt das Kontingent der Hersteller-Cloud


def _geschrieben(welt) -> object:
    """Der Schreibvermerk der simulierten Anlage fuer die Kurve (aendert sich mit jedem Schreiben des Gateways)."""
    return welt.anlagenstand()["geschrieben_um"].get("curve")


@pytest.mark.akzeptanz("AK-G7-01")
@pytest.mark.parametrize("satz", ["vaillant_vrc720", "viessmann_vicare"])
def test_ak_g7_01_app_verstellt_die_kurve_achtmal_am_tag(welt, satz):
    dokument = konfiguration(satz)
    dokument["tagestick"] = "23:00"  # kein Tagestick im Fenster: der Dienst fordert die Werte nicht erneut
    regelwerte = REGELWERTE[satz]
    welt.starten()
    welt.verbinden()
    welt.einrichten(1, dokument)
    welt.antworten(regelwerte)
    welt.vergehen(EINSCHWINGEN, regelwerte, RAUM)
    assert welt.anlagenstand()["hebel"]["curve"] == 0.8  # die Werte sind geschrieben und eingeschwungen
    versuche = []  # Zeitpunkte (Welt-Uhr) der Rueckschreibversuche des Gateways
    vermerk = _geschrieben(welt)
    for nr in range(VERSTELLUNGEN):  # die App ist hartnaeckig: jedes Mal ein anderer Wert als die Regelwerte
        welt.app_verstellt("curve", 1.5 - 0.1 * (nr % 2))
        for _ in range(ABSTAND_DER_APP[satz] // SCHRITT):
            welt.vergehen(SCHRITT, regelwerte, RAUM, SCHRITT)
            if _geschrieben(welt) != vermerk:
                vermerk = _geschrieben(welt)
                versuche.append(welt.uhr())
    assert len(versuche) == VERSUCHE_AM_TAG[satz]  # nicht mehr Rueckschreibversuche am Tag
    assert all(spaeter - frueher >= ABSTAND_MINDESTENS for frueher, spaeter in zip(versuche, versuche[1:], strict=False))
    assert welt.anlagenstand()["hebel"]["curve"] != 0.8  # die Verstellung der App bleibt stehen
    texte = [meldung["text"] for meldung in welt.meldungen()]
    limit = [text for text in texte if LIMIT_TEXT in text]
    assert len(limit) == 1  # genau eine Meldung, dass heute nicht mehr zurueckgesetzt wird
    assert texte[-1] == limit[0]  # sie kam nach dem letzten Versuch, danach nichts mehr
