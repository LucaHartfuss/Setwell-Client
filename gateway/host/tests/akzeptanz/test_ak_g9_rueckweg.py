"""AK-G9-04: Das neue Bundle muss binnen 600 s gesund sein. Wird es gesund, ist es installiert; sonst kehrt das Gateway
zum vorigen Bundle zurueck und dessen Image bleibt erhalten. Die Frist ist die des Geraets (Einstellungen ohne eigenen
Wert); Uhr und Schlaf der Welt rueckt sie vor, gewartet wird nicht. Die 600 steht hier als Literal."""
import pytest
from welt_release import ALT, NEU, ReleaseWelt

#: Fall -> (Sekunden bis der Container gesund meldet oder None fuer nie, Ergebnis des Laufs)
FAELLE = {
    "sofort_gesund": (0, "aktualisiert"),
    "gesund_kurz_vor_der_frist": (590, "aktualisiert"),
    "gesund_kurz_nach_der_frist": (610, "zurueckgerollt"),
    "nie_gesund": (None, "zurueckgerollt"),
}


@pytest.mark.akzeptanz("AK-G9-04")
@pytest.mark.parametrize("fall", FAELLE)
def test_bundle_gesund_in_der_frist_oder_rueckweg(release_welt: ReleaseWelt, fall):
    welt = release_welt
    sekunden, ergebnis = FAELLE[fall]
    welt.angebot(NEU, gesund=sekunden == 0)
    if sekunden:
        welt.gesund_ab(NEU, sekunden)

    assert welt.lauf() == ergebnis

    if ergebnis == "aktualisiert":
        # das neue Bundle laeuft und ist das aktuelle, das vorige wird nicht wieder hochgefahren
        assert welt.hochgefahren() == [NEU]
        assert welt.laeuft() == NEU
        zustand = welt.zustand()
        assert (zustand["current"], zustand["previous"]) == (NEU, ALT)
        assert "in_progress" not in zustand  # kein Auftrag offen
        assert NEU in welt.bundles()
        assert ReleaseWelt.runtime_image(NEU) in welt.images()
        assert [(m["version"], m["result"]) for m in welt.meldungen()] == [(NEU, "ok")]
        # gewartet wurde, bis der Container gesund war, und keine Sekunde ueber die Frist
        assert sekunden <= welt.zeit < 600
        assert welt.lauf() == "aktuell"
        assert welt.hochgefahren() == [NEU]
        return

    # nach der Frist: neu gestartet, dann das vorige Bundle wieder hochgefahren, dort laeuft es jetzt
    assert 600 <= welt.zeit <= 610
    assert welt.hochgefahren() == [NEU, ALT]
    assert welt.laeuft() == ALT
    # Image des vorigen Bundles bleibt, kein Image wurde entfernt
    assert ReleaseWelt.runtime_image(ALT) in welt.images()
    assert not [befehl for befehl in welt.befehle() if befehl[:2] == ["image", "rm"]]
    # Zustand: das vorige Bundle ist das aktuelle, kein Auftrag offen, sein Ordner besteht
    assert welt.zustand() == {"current": ALT}
    assert ALT in welt.bundles()
    # gemeldet wird der Rueckweg samt Grund
    assert [(m["result"], m["reason"]) for m in welt.meldungen()] == [("rollback", "ungesund")]
    # kein Systemwechsel: die Startauswahl wird nicht angefasst
    assert welt.startauswahl_eingriffe() == []
    # ein weiterer Lauf bietet dasselbe Bundle nicht erneut an
    assert welt.lauf() == "aktuell"
    assert welt.hochgefahren() == [NEU, ALT]
