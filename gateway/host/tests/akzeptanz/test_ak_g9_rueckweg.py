"""AK-G9-04: Das neue Bundle wird in der Frist nicht gesund, das Gateway kehrt zum vorigen Bundle zurueck und dessen
Image bleibt erhalten. Die Frist von 600 s ist ueber health_seconds=0 abgekuerzt: sie gilt beim ersten Takt als abgelaufen."""
import pytest
from welt_release import ALT, NEU, ReleaseWelt


@pytest.mark.akzeptanz("AK-G9-04")
def test_ungesundes_bundle_kehrt_zum_vorigen_zurueck(release_welt: ReleaseWelt):
    welt = release_welt
    welt.angebot(NEU, gesund=False)

    assert welt.lauf() == "zurueckgerollt"

    # neu gestartet, dann das vorige Bundle wieder hochgefahren: dort laeuft es jetzt
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
