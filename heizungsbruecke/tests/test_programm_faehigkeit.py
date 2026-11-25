"""Die Faehigkeit `programm` des HA-Hosts: nur mit Zonendaten, Uhr-Port und schreibbarer Soll-Quelle."""
from zoneinfo import ZoneInfoNotFoundError

import pytest
from device_helpers import World
from fakes import FakeHa
from geraet_helpers import Device, Supervisor, _host, konfiguration_aus_optionen
from runtime_helpers import OPTIONS

from heizungsbruecke import geraet_host
from smartheat_device import programm


def _konfiguration(soll: str) -> dict:
    return konfiguration_aus_optionen({**OPTIONS, "entity_room_target": soll})


def test_hilfsfunktion_verlangt_alle_drei_bedingungen():
    assert programm.angeboten(uhr_port=True, soll_quellen=[True]) is True
    assert programm.angeboten(uhr_port=True, soll_quellen=[]) is True  # noch kein Kreis bekannt: der Host kann es
    assert programm.angeboten(uhr_port=False, soll_quellen=[True]) is False
    assert programm.angeboten(uhr_port=True, soll_quellen=[False]) is False
    assert programm.angeboten(uhr_port=True, soll_quellen=[True, False]) is False  # jeder Kreis braucht ein Ziel


def test_zonendaten_fehlen(monkeypatch):
    assert programm.zonendaten_vorhanden() is True

    def fehlt(name):
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(programm, "ZoneInfo", fehlt)
    assert programm.zonendaten_vorhanden() is False


def test_ha_host_meldet_programm_mit_schreibbarer_soll_entity(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch, device=Device(konf=_konfiguration("climate.wohnzimmer_thermostat")))
    assert host.faehigkeiten()["programm"] is True
    assert host.faehigkeiten()["drivers"] == [] and host.faehigkeiten()["zigbee"] is False


def test_ha_host_ohne_geraet_oder_kreis_kann_programm(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch)
    assert host.faehigkeiten()["programm"] is True  # Konfiguration unbekannt (Registrierung): der Host kann es


def test_ha_host_ohne_schreibbare_soll_entity_laesst_das_feld_weg(tmp_path, monkeypatch):
    for soll in ("sensor.room_target", "input_number.soll"):
        host, _, _ = _host(tmp_path, monkeypatch, device=Device(konf=_konfiguration(soll)))
        assert "programm" not in host.faehigkeiten(), soll


def test_ha_host_ohne_zonendaten_laesst_das_feld_weg(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch, device=Device(konf=_konfiguration("climate.wohnzimmer_thermostat")))
    monkeypatch.setattr(programm, "zonendaten_vorhanden", lambda: False)
    assert "programm" not in host.faehigkeiten()


def test_ha_host_ohne_uhr_port_laesst_das_feld_weg(tmp_path, monkeypatch):
    host, _, _ = _host(tmp_path, monkeypatch, device=Device(konf=_konfiguration("climate.wohnzimmer_thermostat")))
    monkeypatch.setattr(geraet_host.HaDeviceHost, "uhr_synchron", None)
    assert "programm" not in host.faehigkeiten()


def test_die_faehigkeit_wird_bei_jedem_aufruf_neu_gerechnet(tmp_path, monkeypatch):
    device = Device(konf=_konfiguration("sensor.room_target"))
    host, _, _ = _host(tmp_path, monkeypatch, device=device)
    assert "programm" not in host.faehigkeiten()
    device.konf = _konfiguration("climate.wohnzimmer_thermostat")
    assert host.faehigkeiten()["programm"] is True


def test_das_geraet_sieht_programm_vom_echten_ha_host(tmp_path):
    welt = World(tmp_path)
    echt = geraet_host.HaDeviceHost(FakeHa(), Supervisor())
    echt.verbinden(Device(konf=_konfiguration("climate.wohnzimmer_thermostat")))
    welt.host.faehigkeiten = echt.faehigkeiten
    welt.verbinden()
    assert welt.device.programm_faehig() is True
    echt.verbinden(Device(konf=_konfiguration("sensor.room_target")))
    assert welt.device.programm_faehig() is False


@pytest.mark.parametrize("fehler", [ValueError("x"), OSError("x")])
def test_unlesbare_zonendaten_zaehlen_als_fehlend(monkeypatch, fehler):
    def wirft(name):
        raise fehler

    monkeypatch.setattr(programm, "ZoneInfo", wirft)
    assert programm.zonendaten_vorhanden() is False
