"""Akzeptanzszenario G-7 der Gateway-Laufzeit: verstellt die Hersteller-App eine Einstellung immer wieder, setzt das
Gateway sie nur begrenzt oft und mit Abstand zurueck (Abrufkontingent der Hersteller-Cloud, je Anlage). Und: fragt das
Gateway die Hersteller-Cloud ab, haelt es deren Sperre und sein eigenes Tageskontingent ein. Die Grenzen stehen hier
woertlich, nie aus dem Produktcode."""
import json

import pytest
from anlagen import konfiguration
from cloud_attrappe import ERNEUERUNG, KENNUNG, CloudAttrappe

from smartheat_gateway.drivers import registry

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

#: Kontingent der Viessmann-Cloud je Anlage: hoechstens so viele Aufrufe in jedem Zeitfenster dieser Laenge (Sekunden)
CLOUD_AUFRUFE = 1200
CLOUD_FENSTER = 86400
CLOUD_SPERRE = 600  # Retry-After der Cloud (Sekunden)
CLOUD_DROSSEL_BEI = 4  # der wievielte Aufruf mit 429 beantwortet wird
CLOUD_TAKT = 60  # Sekunden Weltzeit je Runde; der Treiber fragt in jeder Runde einmal ab
CLOUD_RUNDEN = 1450  # etwas mehr als das Fenster, damit das Kontingent nach dem Ablauf der ersten Aufrufe wieder frei wird
CLOUD_TEXT = "Abfragekontingent bei Viessmann"  # die Meldung zum erschoepften Kontingent nennt Cloud und Kontingent


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


@pytest.fixture
def cloud(welt, monkeypatch):
    """Die Hersteller-Cloud als Attrappe auf 127.0.0.1; der Treiber ruft sie mit der Wanduhr der Welt."""
    attrappe = CloudAttrappe(welt.uhr.epoche, drossel_bei=CLOUD_DROSSEL_BEI, retry_after=str(CLOUD_SPERRE))
    attrappe.starten()

    class _Cloudtreiber(registry.DRIVERS["vicare_cloud"]):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, wall=welt.uhr.epoche, **kwargs)

    monkeypatch.setitem(registry.DRIVERS, "vicare_cloud", _Cloudtreiber)
    monkeypatch.setenv("SHG_TEST_ENDPOINTS", "1")
    monkeypatch.setenv("SHG_VICARE_IAM_BASE", attrappe.adresse)
    monkeypatch.setenv("SHG_VICARE_API_BASE", attrappe.adresse)
    zugang = welt.paths.driver_secrets_dir
    zugang.mkdir(parents=True)
    (zugang / "vicare.json").write_text(json.dumps({"client_id": "kanarienvogel-kunde", "refresh_token": ERNEUERUNG}))
    yield attrappe
    attrappe.beenden()


@pytest.mark.akzeptanz("AK-G7-02")
def test_ak_g7_02_cloud_sperrt_und_das_kontingent_haelt(welt, cloud):
    treiber = {"id": "vicare_cloud", "parameter": {**KENNUNG, "poll_seconds": 120}}
    dokument = konfiguration("viessmann_vicare", treiber=treiber)
    dokument["tagestick"] = "23:00"  # kein Tagestick im Fenster
    hebel = {"curve": 1.4, "level": 0.0, "room_setpoint": 20.0}  # wie die Anlage: es gibt nichts zu schreiben
    welt.starten()
    welt.verbinden()
    welt.einrichten(1, dokument)
    welt.antworten(hebel)
    welt.vergehen(CLOUD_RUNDEN * CLOUD_TAKT, hebel, RAUM, CLOUD_TAKT)
    zeiten = [aufruf[2] for aufruf in cloud.aufrufe]  # Weltzeit (Epoche) jedes Aufrufs, der die Cloud erreichte
    codes = [aufruf[3] for aufruf in cloud.aufrufe]
    assert codes.count(429) == 1 and codes[CLOUD_DROSSEL_BEI - 1] == 429  # die Sperre der Cloud gab es genau einmal
    sperre = zeiten[CLOUD_DROSSEL_BEI - 1]
    assert zeiten[CLOUD_DROSSEL_BEI] - sperre >= CLOUD_SPERRE  # nach der 429 geht 600 s lang kein Aufruf hinaus
    erstes_fenster = [zeit for zeit in zeiten if zeit < zeiten[0] + CLOUD_FENSTER]
    assert len(erstes_fenster) == CLOUD_AUFRUFE  # das Kontingent wurde ausgeschoepft und nicht ueberschritten
    assert len(zeiten) > CLOUD_AUFRUFE  # nach dem Ablauf der ersten Aufrufe fragt das Gateway wieder ab
    for nr in range(len(zeiten) - CLOUD_AUFRUFE):  # in keinem 24-h-Fenster mehr als 1200: der 1201. Aufruf geht nicht hinaus
        assert zeiten[nr + CLOUD_AUFRUFE] - zeiten[nr] >= CLOUD_FENSTER
    texte = [meldung["text"] for meldung in welt.meldungen()]
    assert any(CLOUD_TEXT in text for text in texte), f"keine Meldung zum erschoepften Kontingent: {texte}"
