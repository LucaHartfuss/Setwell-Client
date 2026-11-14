"""AK-G10-03: Ein Bundle mit fremder Signatur, mit widerrufenem Zertifikat oder mit aelterer Version wird nicht installiert.
Zustand, Bundles, Compose, Images und Startauswahl bleiben unveraendert."""
import pytest
from welt_release import AELTER, ALT, NEU, ReleaseWelt

FAELLE = {
    "fremde_signatur": (NEU, {"signatur_von": "fremd"}, "signatur_ungueltig"),
    "widerrufenes_zertifikat": (NEU, {}, "signatur_ungueltig"),
    "aeltere_version": (AELTER, {}, "version_zu_alt"),
}


@pytest.mark.akzeptanz("AK-G10-03")
@pytest.mark.parametrize("fall", FAELLE)
def test_unzulaessiges_bundle_wird_nicht_installiert(release_welt: ReleaseWelt, fall):
    welt = release_welt
    version, optionen, grund = FAELLE[fall]
    welt.angebot(version, **optionen)
    if fall == "widerrufenes_zertifikat":
        welt.widerrufen()
    vorher = welt.zustand()
    images = welt.images()

    assert welt.lauf() == "abgelehnt"

    # nichts installiert, nichts gestartet, nichts geladen
    assert welt.zustand() == vorher
    assert welt.bundles() == [ALT]
    assert welt.archive() == []
    assert not [befehl for befehl in welt.befehle() if befehl[1:2] in (["up"], ["pull"]) or befehl[:1] == ["load"]]
    assert welt.laeuft() == ALT
    assert welt.images() == images
    # die Startauswahl wird nicht angefasst
    assert welt.startauswahl_eingriffe() == []
    # abgelehnt wird mit Grund gemeldet
    assert [(m["result"], m["reason"]) for m in welt.meldungen()] == [("rollback", grund)]
    # eine ungueltige Signatur wird vor dem Abruf des Image-Archivs erkannt
    assert not [pfad for pfad in welt.abrufe if pfad.endswith(".oci.tar")]
