"""Meldung `push_nicht_zugestellt` des HA-Hosts: die Integration gibt einen Push-Eintrag nach 24 h auf und feuert das
Ereignis `smartheat_push_aufgegeben`; das Add-on haelt je (Stufe, Kreis) eine Geraete-Meldung offen, bis 24 h lang kein
neues Ereignis gleicher Stufe und gleichen Kreises kam (monotone Uhr, Uhr-Port). Der Stand ueberlebt keinen Neustart.
Das Gateway hat keine Integration und kein Ereignis (Gegenprobe in gateway/tests/test_geraet_host.py)."""
import pytest
from geraet_helpers import _host

from heizungsbruecke.push_stand import FRIST_SECONDS, PushStand
from smartheat_core import meldungen
from smartheat_device import nachrichten, wire
from smartheat_device.host import Anzeige

CODE = "meldung.push_nicht_zugestellt"


def _anzeige():
    return Anzeige(zustand="regelt", server_ok=True, device_state="uebernommen", eingerichtet=True, hinweise=(),
                   meldungen=(), konfiguration_abgelehnt=None)


def _gemeldet(device):
    return [e for e in device.meldungen if e[0].endswith(("_rot", "_orange"))]


def test_die_frist_ist_24_stunden():
    assert FRIST_SECONDS == 24 * 3600


def test_ein_ereignis_oeffnet_die_meldung_mit_stufe_und_kreis_einmal(tmp_path, monkeypatch):
    host, device, clock = _host(tmp_path, monkeypatch)
    host.anzeigen(_anzeige())
    assert _gemeldet(device) == []
    host.push_stand.aufgegeben("rot", 2, clock())
    host.anzeigen(_anzeige())
    host.anzeigen(_anzeige())
    assert _gemeldet(device) == [("kreis-2:push_nicht_zugestellt_rot", meldungen.neu(CODE, stufe="rot"), True)]


def test_ein_anlagenweites_ereignis_traegt_keinen_kreis(tmp_path, monkeypatch):
    host, device, clock = _host(tmp_path, monkeypatch)
    host.push_stand.aufgegeben("orange", None, clock())
    host.anzeigen(_anzeige())
    assert _gemeldet(device) == [("push_nicht_zugestellt_orange", meldungen.neu(CODE, stufe="orange"), True)]
    assert nachrichten.draht_key("push_nicht_zugestellt_orange") == ("push_nicht_zugestellt_orange", None)


def test_der_draht_key_traegt_den_kreis_als_feld(tmp_path):
    key, kreis = nachrichten.draht_key("kreis-2:push_nicht_zugestellt_rot")
    assert kreis == 2 and key.startswith("push_nicht_zugestellt_rot:")


def test_die_meldung_ist_orange_mit_dem_parameter_stufe():
    meldung = meldungen.neu(CODE, stufe="rot")
    assert meldung.stufe == "orange" and meldungen.draht(meldung)["params"] == {"stufe": "rot"}
    assert meldungen.text(meldung)


def test_23_stunden_59_minuten_offen_24_stunden_zu(tmp_path, monkeypatch):
    host, device, clock = _host(tmp_path, monkeypatch)
    host.push_stand.aufgegeben("rot", 1, clock())
    host.anzeigen(_anzeige())
    clock.advance(FRIST_SECONDS - 60)
    host.anzeigen(_anzeige())
    assert [e[2] for e in _gemeldet(device)] == [True]
    clock.advance(60)
    host.anzeigen(_anzeige())
    host.anzeigen(_anzeige())
    assert _gemeldet(device) == [("kreis-1:push_nicht_zugestellt_rot", meldungen.neu(CODE, stufe="rot"), True),
                                 ("kreis-1:push_nicht_zugestellt_rot", None, False)]


def test_ein_zweites_ereignis_verlaengert_die_frist(tmp_path, monkeypatch):
    host, device, clock = _host(tmp_path, monkeypatch)
    host.push_stand.aufgegeben("rot", 1, clock())
    host.anzeigen(_anzeige())
    clock.advance(FRIST_SECONDS - 60)
    host.push_stand.aufgegeben("rot", 1, clock())
    clock.advance(FRIST_SECONDS - 60)
    host.anzeigen(_anzeige())
    assert [e[2] for e in _gemeldet(device)] == [True]  # 23 h 59 nach dem zweiten Ereignis: noch offen
    clock.advance(60)
    host.anzeigen(_anzeige())
    assert [e[2] for e in _gemeldet(device)] == [True, False]


def test_stufe_und_kreis_laufen_unabhaengig(tmp_path, monkeypatch):
    host, device, clock = _host(tmp_path, monkeypatch)
    host.push_stand.aufgegeben("rot", 1, clock())
    clock.advance(3600)
    host.push_stand.aufgegeben("orange", 1, clock())
    host.push_stand.aufgegeben("rot", 2, clock())
    host.anzeigen(_anzeige())
    assert {e[0] for e in _gemeldet(device)} == {
        "kreis-1:push_nicht_zugestellt_rot", "kreis-1:push_nicht_zugestellt_orange", "kreis-2:push_nicht_zugestellt_rot"}
    clock.advance(FRIST_SECONDS - 3600)  # nur das erste Ereignis ist 24 h alt
    host.anzeigen(_anzeige())
    zu = [e[0] for e in _gemeldet(device) if not e[2]]
    assert zu == ["kreis-1:push_nicht_zugestellt_rot"]


def test_nach_dem_schliessen_oeffnet_ein_neues_ereignis_wieder(tmp_path, monkeypatch):
    host, device, clock = _host(tmp_path, monkeypatch)
    host.push_stand.aufgegeben("rot", 1, clock())
    host.anzeigen(_anzeige())
    clock.advance(FRIST_SECONDS)
    host.anzeigen(_anzeige())
    host.push_stand.aufgegeben("rot", 1, clock())
    host.anzeigen(_anzeige())
    assert [e[2] for e in _gemeldet(device)] == [True, False, True]


def test_ein_neuer_host_hat_keine_offene_meldung(tmp_path, monkeypatch):
    host, device, clock = _host(tmp_path, monkeypatch)
    host.push_stand.aufgegeben("rot", 1, clock())
    host.anzeigen(_anzeige())
    host2, device2, _ = _host(tmp_path, monkeypatch)
    host2.anzeigen(_anzeige())
    assert _gemeldet(device2) == []


def test_ein_fehler_beim_melden_bricht_die_anzeige_nicht_ab_und_wird_wiederholt(tmp_path, monkeypatch):
    host, device, clock = _host(tmp_path, monkeypatch)
    host.push_stand.aufgegeben("rot", 1, clock())
    echt = device.meldung
    versuche = []

    def kaputt(key, meldung, *, offen):
        versuche.append(key)
        raise RuntimeError("boom")

    device.meldung = kaputt
    host.anzeigen(_anzeige())  # darf nicht werfen
    device.meldung = echt
    host.anzeigen(_anzeige())
    assert versuche and [e[2] for e in _gemeldet(device)] == [True]


def test_nur_der_stand_ohne_ereignis_meldet_nichts(tmp_path, monkeypatch):
    host, device, _ = _host(tmp_path, monkeypatch)
    assert isinstance(host.push_stand, PushStand)
    host.anzeigen(_anzeige())
    assert _gemeldet(device) == []


def test_der_code_steht_in_der_tabelle_aber_nicht_unter_den_hinweisen_des_servers():
    assert CODE in meldungen.TABELLE
    assert "push_nicht_zugestellt" not in wire.MELDUNGEN  # geraeteeigen: der Server sendet ihn nie als Hinweis


@pytest.mark.parametrize("stufe", ["rot", "orange"])
def test_jede_stufe_hat_einen_schlichten_key_unter_40_zeichen(stufe):
    assert len(f"push_nicht_zugestellt_{stufe}") <= 40
