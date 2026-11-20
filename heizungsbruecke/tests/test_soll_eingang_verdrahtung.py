"""Soll-Eingang mit der echten Verdrahtung der Laufzeit (Planrechnung des lokalen Checks, Lesewege des Workers): was die
Soll-Quelle nach dem Wiedereinschalten zeigt, wenn Leerstand oder Away lief, als sie aus war."""
from datetime import UTC, datetime
from pathlib import Path

from fakes import FakeClock
from test_regulation import RESTORE_POINT, WOCHENPLAN, _mit_plan, _runtime

from smartheat_runtime import app, regulation
from smartheat_runtime.soll_eingang import (
    EV_SOLL_MODUS,
    EV_SOLL_QUELLE,
    SollEingang,
    UebersteuerungsWeg,
)
from smartheat_runtime.soll_speicher import TargetStore
from smartheat_runtime.worker import Event, RegulationWorker

JETZT = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
LEERSTAND = {"von": "2026-11-01T00:00:00Z", "bis": "2026-11-20T10:00:00Z", "temp": 16.0, "aufheizen_ab": None}
ABWESEND = {"bis": "2026-11-20T10:00:00Z", "temp": 17.0, "aufheizen_ab": None}


class Quelle:
    def __init__(self, wert):
        self.wert, self.geschrieben = wert, []

    def schreiben(self, value):
        self.geschrieben.append(value)
        self.wert = value
        return True

    def angezeigt(self):
        return self.wert

    def aus(self):
        return False


def _aufbau(make_store, monkeypatch, tmp_path, bedienung):
    """Laufzeit mit Plan, Worker mit den Lesewegen der Laufzeit und der Soll-Eingang daran; das Thermostat zeigt 22."""
    rt = _runtime(make_store(backup={"last_room_target": 21.0, **RESTORE_POINT}), room_actual=20.0, room_target=21.0)
    _mit_plan(rt, monkeypatch, jetzt=JETZT, bedienung={"kreis": 1, "raum_soll": 21.0, "wochenplan": WOCHENPLAN, **bedienung})
    rt.worker = RegulationWorker(FakeClock())
    app._worker_lesewege(rt)
    rt.worker.register(EV_SOLL_QUELLE, lambda event: app._on_soll_quelle(rt, event))
    store = TargetStore(tmp_path / "soll.json", 21.0, rt.worker._clock if hasattr(rt.worker, "_clock") else FakeClock(),
                        lambda: JETZT.isoformat())
    quelle = Quelle(22.0)
    SollEingang(
        rt.worker, store, quelle, wunsch=lambda wert, herkunft, durch: wert, soll_quelle_aus=lambda aus, wirksam: None,
        entprellen_s=0.0,
        uebersteuerung=UebersteuerungsWeg(
            lage=rt.worker.geltendes_soll, moeglich=lambda: True, senden=lambda *args: None,
            betriebsart=rt.worker.betriebsart, betriebsart_trotz_aus=rt.worker.betriebsart_trotz_aus))
    return rt, quelle


def _modus(rt, modus):
    rt.worker.post(Event(EV_SOLL_MODUS, {"mode": modus}))
    rt.worker.run_pending()


def test_wieder_an_im_leerstand_zeigt_die_leerstandstemperatur_nicht_den_wunsch(make_store, monkeypatch, tmp_path):
    rt, quelle = _aufbau(make_store, monkeypatch, tmp_path, {"modus": "leerstand", "leerstand": LEERSTAND})
    _modus(rt, "off")
    regulation.run_local_check(rt)  # ohne Soll gerechnet: das geltende Soll des Kreises ist leer
    assert rt.soll_quelle_aus is True and rt.geltendes_soll.soll is None
    _modus(rt, "heat")
    assert quelle.geschrieben == [16.0]


def test_wieder_an_in_away_zeigt_die_abwesenheitstemperatur_nicht_den_wunsch(make_store, monkeypatch, tmp_path):
    rt, quelle = _aufbau(make_store, monkeypatch, tmp_path, {"modus": "abwesend", "abwesend": ABWESEND})
    _modus(rt, "off")
    regulation.run_local_check(rt)
    _modus(rt, "heat")
    assert quelle.geschrieben == [17.0]


def test_wieder_an_ohne_betriebsart_zeigt_den_wunsch(make_store, monkeypatch, tmp_path):
    rt, quelle = _aufbau(make_store, monkeypatch, tmp_path, {"modus": "manuell"})
    _modus(rt, "off")
    regulation.run_local_check(rt)
    _modus(rt, "heat")
    assert quelle.geschrieben == [21.0]


def test_beide_hosts_verdrahten_den_lesezugriff_trotz_aus():
    """Ohne ihn zeigt die Quelle nach dem Wiedereinschalten den Wunsch statt der Leerstandstemperatur."""
    wurzel = Path(__file__).resolve().parents[2]
    for datei in ("gateway/src/smartheat_gateway/host.py", "heizungsbruecke/src/heizungsbruecke/host.py"):
        assert "betriebsart_trotz_aus=worker.betriebsart_trotz_aus" in (wurzel / datei).read_text(), datei
