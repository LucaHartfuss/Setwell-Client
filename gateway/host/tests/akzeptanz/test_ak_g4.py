"""AK-G4-04: Waechter des Gateway-Hosts. Beobachtet wird nur, was der Host nach aussen tut: die `docker compose`-Aufrufe
(ueber die Attrappe fuer subprocess.run) bei einem Takt von 30 s."""
import pytest

PAUSEN = ("installiere", "installiert", "probestart", "auftrag", "werksreset")


def _neustart_nach_der_dritten_pruefung(welt) -> None:
    welt.takt("unhealthy", 2)
    welt.takt("healthy")  # keine drei in Folge: die Folge beginnt neu
    welt.takt("unhealthy", 2)
    assert welt.neustarts() == []
    welt.takt("unhealthy")  # die dritte in Folge
    assert len(welt.neustarts()) == 1
    assert welt.neustarts()[0][:4] == ["docker", "compose", "-p", "smartheat"]


def _hoechstens_drei_je_stunde(welt) -> None:
    welt.takt("unhealthy", 250)  # 7500 s ununterbrochen ungesund
    zeiten = welt.neustart_zeiten
    ersten_drei = [z for z in zeiten if z < zeiten[0] + 3600]
    assert len(ersten_drei) == 3, zeiten
    assert len(zeiten) >= 4, "nach Ablauf der Stunde kommt der Neustart wieder"
    for zeit in zeiten:
        assert len([z for z in zeiten if zeit - 3600 < z <= zeit]) <= 3, zeiten


def _keiner_in_der_pause(welt, art: str) -> None:
    welt.takt("unhealthy", 2)
    welt.pause_setzen(art)
    welt.takt("unhealthy", 20)
    assert welt.neustarts() == [], f"Neustart waehrend {art}"
    welt.pause_aufheben()
    welt.takt("unhealthy", 2)
    assert welt.neustarts() == [], "die Folge beginnt nach der Pause neu"
    welt.takt("unhealthy")
    assert len(welt.neustarts()) == 1


@pytest.mark.akzeptanz("AK-G4-04")
@pytest.mark.parametrize("lage", ("dreimal_ungesund", *PAUSEN))
def test_ak_g4_04_waechter_neustart_nach_der_dritten_pruefung(welt, lage):
    if lage == "dreimal_ungesund":
        _neustart_nach_der_dritten_pruefung(welt)
        _hoechstens_drei_je_stunde(welt)
    else:
        _keiner_in_der_pause(welt, lage)
