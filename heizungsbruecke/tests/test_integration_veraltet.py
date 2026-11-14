"""Meldung `integration_veraltet` des HA-Hosts: offen, solange das Lebenszeichen der Integration sagt, dass sie das
Status-Schema des Add-ons nicht liest und aelter ist; zu, sobald sie aktuell ist oder die Frist ohne Ereignis ablaeuft.
Der Weg ist die persistente Benachrichtigung in HA (die Integration kann sie nicht selbst zeigen), kein Ereignis und
kein Server-Weg; das Gateway hat keine Integration (Gegenprobe in gateway/tests/test_geraet_host.py)."""
import pytest
from fakes import FakeHa
from geraet_helpers import _host

from heizungsbruecke import geraet_status
from heizungsbruecke.integration_stand import FRIST_SECONDS
from smartheat_core import meldungen
from smartheat_device.host import Anzeige

SCHLUESSEL = "smartheat_integration_veraltet"
ALT = {"schema": 1, "version": "0.4.2", "v5_schemas": [geraet_status.EVENT_SCHEMA - 1]}
AKTUELL = {"schema": 1, "version": "0.9.0", "v5_schemas": [geraet_status.EVENT_SCHEMA]}
NEUER = {"schema": 1, "version": "1.0.0", "v5_schemas": [geraet_status.EVENT_SCHEMA + 1]}
TEXT = meldungen.text(meldungen.neu("meldung.integration_veraltet"))


def _anzeige():
    return Anzeige(zustand="regelt", server_ok=True, device_state="uebernommen", eingerichtet=True, hinweise=(),
                   meldungen=(), konfiguration_abgelehnt=None)


def _stand(host, clock, daten):
    host.integration_stand.ereignis(daten, clock())


def test_eine_veraltete_integration_oeffnet_die_meldung_einmal(tmp_path, monkeypatch):
    ha = FakeHa()
    host, _, clock = _host(tmp_path, monkeypatch, ha=ha)
    host.anzeigen(_anzeige())
    assert ha.persistent == [] and ha.dismissed == []  # ohne Lebenszeichen schweigt das Add-on
    _stand(host, clock, ALT)
    host.anzeigen(_anzeige())
    host.anzeigen(_anzeige())
    assert ha.persistent == [(SCHLUESSEL, TEXT)]
    assert not [e for e in ha.events if e[0] == "smartheat_meldung"]  # kein Ereignis, kein Weg zum Server


def test_eine_aktuelle_integration_nimmt_die_meldung_zurueck(tmp_path, monkeypatch):
    ha = FakeHa()
    host, _, clock = _host(tmp_path, monkeypatch, ha=ha)
    _stand(host, clock, ALT)
    host.anzeigen(_anzeige())
    _stand(host, clock, AKTUELL)
    host.anzeigen(_anzeige())
    host.anzeigen(_anzeige())
    assert ha.persistent == [(SCHLUESSEL, TEXT)] and ha.dismissed == [SCHLUESSEL]


def test_nach_fristablauf_ohne_ereignis_wird_die_meldung_zurueckgenommen(tmp_path, monkeypatch):
    ha = FakeHa()
    host, _, clock = _host(tmp_path, monkeypatch, ha=ha)
    _stand(host, clock, ALT)
    host.anzeigen(_anzeige())
    clock.advance(FRIST_SECONDS - 1)
    host.anzeigen(_anzeige())
    assert ha.dismissed == []
    clock.advance(1)
    host.anzeigen(_anzeige())
    assert ha.persistent == [(SCHLUESSEL, TEXT)] and ha.dismissed == [SCHLUESSEL]


def test_die_meldung_oeffnet_wieder_wenn_die_integration_erneut_veraltet_ist(tmp_path, monkeypatch):
    ha = FakeHa()
    host, _, clock = _host(tmp_path, monkeypatch, ha=ha)
    for daten in (ALT, AKTUELL, ALT):
        _stand(host, clock, daten)
        host.anzeigen(_anzeige())
    assert ha.persistent == [(SCHLUESSEL, TEXT)] * 2 and ha.dismissed == [SCHLUESSEL]


@pytest.mark.parametrize("daten", [AKTUELL, NEUER, None])
def test_gegenprobe_aktuelle_neuere_oder_keine_integration_schweigt(tmp_path, monkeypatch, daten):
    ha = FakeHa()
    host, _, clock = _host(tmp_path, monkeypatch, ha=ha)
    if daten is not None:
        _stand(host, clock, daten)
    for _ in range(3):
        host.anzeigen(_anzeige())
        clock.advance(FRIST_SECONDS)
    assert ha.persistent == [] and ha.dismissed == []  # auch keine ueberfluessige Rücknahme


def test_ein_fehler_von_ha_wirft_nicht_und_wird_im_naechsten_takt_wiederholt(tmp_path, monkeypatch):
    class WackeligeHa(FakeHa):
        ausfall = True

        def create_persistent_notification(self, title, message, notification_id):
            if self.ausfall:
                raise OSError("HA nicht erreichbar")
            super().create_persistent_notification(title, message, notification_id)

    ha = WackeligeHa()
    host, _, clock = _host(tmp_path, monkeypatch, ha=ha)
    _stand(host, clock, ALT)
    host.anzeigen(_anzeige())  # wirft nicht
    assert ha.persistent == []
    ha.ausfall = False
    host.anzeigen(_anzeige())
    assert ha.persistent == [(SCHLUESSEL, TEXT)]
