"""Meldung `meldung.uhr_unklar` je Kreis: das Geraet meldet einmal je Uebergang, sobald das geltende Soll des Kreises
`uhr_unklar` traegt, und nimmt die Meldung zurueck, wenn der Merker faellt. Der Merker selbst kommt aus der Plan-Rechnung
(test_core_plan.py); hier zaehlen nur die Uebergaenge an der Grenze zum Kunden."""
from types import SimpleNamespace

from fakes import FakeNotifySink

from smartheat_core import meldungen
from smartheat_core.plan import SollErgebnis
from smartheat_runtime import kreise, uhr_meldung
from smartheat_runtime.notifier import STATE_OK, Notifier
from smartheat_runtime.state import StateStore


def _soll(unklar: bool) -> SollErgebnis:
    return SollErgebnis(soll=21.0, soll_art="wunsch", naechster_wechsel=None, uhr_unklar=unklar)


def _rt(make_store, senke=None, soll=None):
    store = make_store()
    senke = senke or FakeNotifySink()
    return SimpleNamespace(store=store, notifier=Notifier(store, senke), kunde=senke, geltendes_soll=soll)


def test_unklar_wird_wahr_gibt_genau_eine_offene_meldung_und_zwei_weitere_takte_nichts(make_store):
    rt = _rt(make_store, soll=_soll(False))
    uhr_meldung.check(rt)
    assert rt.kunde.codes == [] and rt.kunde.offen == {}

    rt.geltendes_soll = _soll(True)
    for _ in range(3):
        uhr_meldung.check(rt)
    assert rt.kunde.codes == [("uhr_unklar", "meldung.uhr_unklar")]
    assert rt.kunde.offen == {"uhr_unklar": meldungen.neu("meldung.uhr_unklar")}
    assert rt.kunde.withdrawn == []
    assert rt.notifier.state("uhr_unklar") == uhr_meldung.STATE_UNKLAR


def test_die_meldung_traegt_keine_parameter_und_keine_zeit(make_store):
    rt = _rt(make_store, soll=_soll(True))
    uhr_meldung.check(rt)
    meldung = rt.kunde.offen["uhr_unklar"]
    assert meldung.params == {} and meldung.stufe == "gelb"
    assert meldungen.draht(meldung) == {"code": "meldung.uhr_unklar", "params": {}, "stufe": "gelb"}


def test_vertrauenswuerdige_uhr_nimmt_die_meldung_zurueck_ohne_push(make_store):
    rt = _rt(make_store, soll=_soll(True))
    uhr_meldung.check(rt)
    rt.geltendes_soll = _soll(False)
    for _ in range(3):
        uhr_meldung.check(rt)
    assert rt.kunde.withdrawn == ["uhr_unklar"] and rt.kunde.offen == {}
    assert rt.kunde.codes == [("uhr_unklar", "meldung.uhr_unklar")]  # der Push der Rücknahme bleibt aus
    assert rt.notifier.state("uhr_unklar") == STATE_OK


def test_wieder_unklar_nach_der_ruecknahme_ist_ein_neuer_uebergang(make_store):
    rt = _rt(make_store, soll=_soll(True))
    uhr_meldung.check(rt)
    rt.geltendes_soll = _soll(False)
    uhr_meldung.check(rt)
    rt.geltendes_soll = _soll(True)
    uhr_meldung.check(rt)
    assert [code for _, code in rt.kunde.codes] == ["meldung.uhr_unklar"] * 2
    assert rt.kunde.withdrawn == ["uhr_unklar"] and "uhr_unklar" in rt.kunde.offen


def test_ohne_gesetzte_betriebsart_meldet_eine_unklare_uhr_nichts(make_store):
    """Ohne Betriebsart ruht nichts: die Plan-Rechnung setzt den Merker dann nicht, es gibt keine Meldung."""
    rt = _rt(make_store, soll=_soll(False))
    for _ in range(3):
        uhr_meldung.check(rt)
    assert rt.kunde.codes == [] and rt.kunde.offen == {} and rt.kunde.withdrawn == []


def test_ohne_geltendes_soll_aendert_sich_nichts(make_store):
    """Vor dem ersten Takt und ausser Betrieb gibt es kein Soll: eine offene Meldung bleibt, eine geschlossene bleibt zu."""
    rt = _rt(make_store, soll=None)
    uhr_meldung.check(rt)
    assert rt.kunde.codes == []
    rt.geltendes_soll = _soll(True)
    uhr_meldung.check(rt)
    rt.geltendes_soll = None
    uhr_meldung.check(rt)
    assert len(rt.kunde.codes) == 1 and rt.kunde.withdrawn == [] and "uhr_unklar" in rt.kunde.offen


def test_ein_neustart_mit_offener_meldung_und_weiter_unklarer_uhr_meldet_nicht_erneut(make_store, tmp_path):
    rt = _rt(make_store, soll=_soll(True))
    uhr_meldung.check(rt)
    rt.store.flush()
    neu = SimpleNamespace(store=StateStore(tmp_path / "backup.json", tmp_path / "failsafe_state.json"),
                          kunde=FakeNotifySink(), geltendes_soll=_soll(True))
    neu.notifier = Notifier(neu.store, neu.kunde)
    uhr_meldung.check(neu)
    assert neu.kunde.codes == [] and neu.kunde.withdrawn == []


def test_zwei_kreise_nur_der_unklare_meldet_mit_seinem_kreispraefix(make_store, tmp_path):
    senke = FakeNotifySink()

    def kreis(nummer: int, unklar: bool):
        store = StateStore(tmp_path / f"k{nummer}" / "backup.json", tmp_path / f"k{nummer}" / "failsafe_state.json")
        return SimpleNamespace(store=store, notifier=Notifier(store, kreise._KreisMeldesenke(senke, nummer)),
                               geltendes_soll=_soll(unklar))

    eins, zwei = kreis(1, False), kreis(2, True)
    for _ in range(2):
        uhr_meldung.check(eins)
        uhr_meldung.check(zwei)
    assert senke.codes == [("kreis-2:uhr_unklar", "meldung.uhr_unklar")]
    assert list(senke.offen) == ["kreis-2:uhr_unklar"]
    assert eins.notifier.state("uhr_unklar") == STATE_OK


def test_der_gesundheitstakt_ruft_die_pruefung_je_kreis(make_store, monkeypatch):
    """Verdrahtung: der Takt der Ueberwachungen (app._on_health) ruft die Pruefung mit der Laufzeit des Kreises."""
    from smartheat_runtime import app, runtime
    from smartheat_runtime.worker import Event

    rt = _rt(make_store, soll=_soll(True))
    rt.worker = SimpleNamespace(schedule=lambda *_: None)
    rt.config = SimpleNamespace(local_check_interval=300, zustand_source=lambda: "betrieb")
    for modul, name in ((app.battery, "check_batteries"), (app.room_sensors, "check_room_sensors"),
                        (app.aussentemperatur, "check"), (app.erzeugerstoerung, "check"), (app.kontingent, "check"),
                        (app.enforce, "check_manual_override")):
        monkeypatch.setattr(modul, name, lambda _rt: None)
    app._on_health(rt, Event(runtime.EV_HEALTH))
    assert rt.kunde.codes == [("uhr_unklar", "meldung.uhr_unklar")]
