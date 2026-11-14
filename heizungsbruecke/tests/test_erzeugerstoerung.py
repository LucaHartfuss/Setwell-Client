"""Stoerungen des Waermeerzeugers im Geraet: erst nach 15 Minuten ununterbrochenem Anstehen eine offene Meldung je
Kennung, geschlossen beim Wegfall, hoechstens acht offene je Kreis; Telemetrie nennt die offene Stoerung."""
import logging
from types import SimpleNamespace

import pytest
from fakes import FakeClock, FakeHa, FakeNotifySink

from heizungsbruecke.ha_sinks import EREIGNIS_MELDUNG, GeraetUndHaSink, HaNotifySink
from smartheat_core import meldungen
from smartheat_device.host import MeldungSink
from smartheat_runtime import erzeugerstoerung
from smartheat_runtime.notifier import STATE_OK, Notifier
from smartheat_runtime.ports import SignalNotFound, SourceUnavailable, Stoerung
from smartheat_runtime.roles import ChannelManifest

REF = "binary_sensor.anlage_trouble_codes"
QUELLE = "mypyllant"
BESTAETIGUNG = 15 * 60


class _Quelle:
    """Signalquelle mit von Hand gesetzten Stoerungen; ein Fehler als Wert wird geworfen."""

    def __init__(self) -> None:
        self.eintraege: tuple[Stoerung, ...] | Exception = ()
        self.gelesen: list[str] = []

    def get_stoerungen(self, ref):
        self.gelesen.append(ref)
        if isinstance(self.eintraege, Exception):
            raise self.eintraege
        return self.eintraege


def _stoerung(kennung, hinweis=False):
    return Stoerung(QUELLE, kennung, hinweis)


def _rt(make_store, *, refs=None, store=None):
    store = store or make_store()
    kunde = FakeNotifySink()
    return SimpleNamespace(
        manifest=ChannelManifest(refs={"fault_codes": REF} if refs is None else refs), signals=_Quelle(),
        store=store, notifier=Notifier(store, kunde), kunde=kunde, clock=FakeClock(), erzeuger=None,
    )


def _setzen(rt, *eintraege):
    rt.signals.eintraege = tuple(eintraege)


def _offen(rt):
    return {key: meldung.code for key, meldung in rt.kunde.offen.items()}


def test_eine_stoerung_wird_erst_nach_fuenfzehn_minuten_zur_offenen_meldung(make_store):
    rt = _rt(make_store)
    _setzen(rt, _stoerung("F.28"))

    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG - 1)
    erzeugerstoerung.check(rt)
    assert _offen(rt) == {} and rt.kunde.pushes == []  # eine kurze Stoerung bleibt still
    assert erzeugerstoerung.kennung(rt) is None

    rt.clock.advance(1)
    erzeugerstoerung.check(rt)
    assert _offen(rt) == {"erzeuger_stoerung:f_28": "erzeuger.stoerung"}
    meldung = rt.kunde.offen["erzeuger_stoerung:f_28"]
    assert meldung.stufe == "rot" and meldung.params == {"roh_text": "f_28", "quelle": QUELLE}
    assert erzeugerstoerung.kennung(rt) == "f_28"
    assert rt.signals.gelesen == [REF] * 3


def test_eine_stoerung_um_23_uhr_geht_als_rot_hinaus_ein_hinweis_zur_selben_zeit_als_gelb(make_store):
    """Die Integration schickt `rot` sofort, auch in der Ruhezeit (22 bis 7 Uhr), `gelb` nie; das Geraet gibt die Stufe
    ueber das Ereignis weiter. Die Uhr steht auf 23 Uhr, die Stufe haengt nicht von der Tageszeit ab."""
    ha = FakeHa()
    rt = _rt(make_store)
    rt.notifier = Notifier(rt.store, GeraetUndHaSink(MeldungSink(lambda *args, **kwargs: None), HaNotifySink(ha, []),
                                                     lambda: "geraet-1"))
    rt.clock = FakeClock(start=23 * 3600.0)
    _setzen(rt, _stoerung("f_28"), _stoerung("w_1", hinweis=True))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)

    ereignisse = {daten["code"]: daten for art, daten in ha.events if art == EREIGNIS_MELDUNG}
    assert (ereignisse["erzeuger.stoerung"]["stufe"], ereignisse["erzeuger.stoerung"]["offen"]) == ("rot", True)
    assert (ereignisse["erzeuger.hinweis"]["stufe"], ereignisse["erzeuger.hinweis"]["offen"]) == ("gelb", True)


def test_eine_bestehende_stoerung_meldet_nicht_noch_einmal(make_store):
    rt = _rt(make_store)
    _setzen(rt, _stoerung("f_28"))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)
    anzahl = len(rt.kunde.pushes) + len(rt.kunde.offen)

    for _ in range(3):
        rt.clock.advance(60)
        erzeugerstoerung.check(rt)
    assert len(rt.kunde.pushes) + len(rt.kunde.offen) == anzahl and rt.kunde.withdrawn == []


def test_ein_wartungshinweis_ist_ein_hinweis_und_keine_stoerung_fuer_die_telemetrie(make_store):
    rt = _rt(make_store)
    _setzen(rt, _stoerung("w_1", hinweis=True))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)

    assert _offen(rt) == {"erzeuger_stoerung:w_1": "erzeuger.hinweis"}
    assert rt.kunde.offen["erzeuger_stoerung:w_1"].stufe == "gelb"
    assert erzeugerstoerung.kennung(rt) is None  # ein Hinweis pausiert das Lernen nicht


def test_je_kennung_eine_meldung_und_die_aelteste_stoerung_geht_in_die_telemetrie(make_store):
    rt = _rt(make_store)
    _setzen(rt, _stoerung("f_75"))
    erzeugerstoerung.check(rt)
    rt.clock.advance(60)
    _setzen(rt, _stoerung("f_75"), _stoerung("f_28"))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)

    assert sorted(_offen(rt)) == ["erzeuger_stoerung:f_28", "erzeuger_stoerung:f_75"]
    assert erzeugerstoerung.kennung(rt) == "f_75"


def test_der_wegfall_schliesst_die_meldung_und_die_rueckkehr_beginnt_neu(make_store):
    rt = _rt(make_store)
    _setzen(rt, _stoerung("f_28"))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)
    pushes = len(rt.kunde.pushes)

    _setzen(rt)
    erzeugerstoerung.check(rt)
    assert rt.kunde.offen == {} and rt.kunde.withdrawn == ["erzeuger_stoerung:f_28"]
    assert rt.notifier.state("erzeuger_stoerung:f_28") == STATE_OK
    assert erzeugerstoerung.kennung(rt) is None
    assert len(rt.kunde.pushes) == pushes  # das Schliessen ist kein Push

    _setzen(rt, _stoerung("f_28"))  # kehrt zurueck: die 15 Minuten zaehlen von vorn
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG - 1)
    erzeugerstoerung.check(rt)
    assert rt.kunde.offen == {}
    rt.clock.advance(1)
    erzeugerstoerung.check(rt)
    assert _offen(rt) == {"erzeuger_stoerung:f_28": "erzeuger.stoerung"}


def test_eine_unterbrechung_vor_der_bestaetigung_setzt_die_zaehlung_zurueck(make_store):
    rt = _rt(make_store)
    _setzen(rt, _stoerung("f_28"))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG - 60)
    _setzen(rt)
    erzeugerstoerung.check(rt)
    _setzen(rt, _stoerung("f_28"))
    rt.clock.advance(30)
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG - 1)
    erzeugerstoerung.check(rt)
    assert rt.kunde.offen == {}


def test_hoechstens_acht_offene_meldungen_und_die_aelteren_gewinnen(make_store):
    rt = _rt(make_store)
    erste = [_stoerung(f"f_{n}") for n in range(8)]
    _setzen(rt, *erste)
    erzeugerstoerung.check(rt)
    rt.clock.advance(10)
    _setzen(rt, *erste, _stoerung("f_8"))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)

    assert sorted(_offen(rt)) == sorted(f"erzeuger_stoerung:f_{n}" for n in range(8))
    # eine geschlossene gibt den Platz frei, die zurueckgehaltene rueckt nach
    _setzen(rt, *erste[1:], _stoerung("f_8"))
    erzeugerstoerung.check(rt)
    assert "erzeuger_stoerung:f_8" in _offen(rt) and "erzeuger_stoerung:f_0" not in _offen(rt)


@pytest.mark.parametrize("roh", [None, 28, "", "F 28", "f/28", "f" * 33, "Stoerungä", "f_28\n"])
def test_eine_unbrauchbare_kennung_wird_uebersprungen_die_uebrigen_bleiben(make_store, roh):
    rt = _rt(make_store)
    _setzen(rt, _stoerung(roh), _stoerung("F.29"))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)
    assert _offen(rt) == {"erzeuger_stoerung:f_29": "erzeuger.stoerung"}


def test_ohne_rolle_fault_codes_wird_nichts_gelesen_und_nichts_gemeldet(make_store):
    rt = _rt(make_store, refs={})
    rt.signals.eintraege = SourceUnavailable("weg")
    erzeugerstoerung.check(rt)
    assert rt.signals.gelesen == [] and rt.kunde.offen == {} and erzeugerstoerung.kennung(rt) is None


def test_eine_nicht_erreichbare_quelle_laesst_die_offene_meldung_stehen(make_store):
    rt = _rt(make_store)
    _setzen(rt, _stoerung("f_28"))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)

    rt.signals.eintraege = SourceUnavailable("HA nicht erreichbar")
    rt.clock.advance(60)
    erzeugerstoerung.check(rt)
    assert _offen(rt) == {"erzeuger_stoerung:f_28": "erzeuger.stoerung"} and rt.kunde.withdrawn == []
    assert erzeugerstoerung.kennung(rt) == "f_28"


def test_eine_fehlende_entity_gilt_als_keine_stoerung(make_store):
    rt = _rt(make_store)
    _setzen(rt, _stoerung("f_28"))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)

    rt.signals.eintraege = SignalNotFound(REF)
    erzeugerstoerung.check(rt)
    assert rt.kunde.offen == {} and rt.kunde.withdrawn == ["erzeuger_stoerung:f_28"]


def test_nach_einem_neustart_bleibt_die_offene_meldung_ohne_neue_wartezeit_und_schliesst_beim_wegfall(make_store):
    store = make_store()
    vorher = _rt(make_store, store=store)
    _setzen(vorher, _stoerung("f_28"))
    erzeugerstoerung.check(vorher)
    vorher.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(vorher)

    danach = _rt(make_store, store=store)  # neuer Prozess: nur der Meldezustand der Ablage ist da
    _setzen(danach, _stoerung("f_28"))
    erzeugerstoerung.check(danach)
    assert danach.kunde.pushes == [] and danach.kunde.offen == {}  # nichts neu gemeldet
    assert erzeugerstoerung.kennung(danach) == "f_28"

    _setzen(danach)
    erzeugerstoerung.check(danach)
    assert danach.kunde.withdrawn == ["erzeuger_stoerung:f_28"]
    assert danach.notifier.state("erzeuger_stoerung:f_28") == STATE_OK


@pytest.mark.parametrize(("vorher_hinweis", "code_danach"), [(True, "erzeuger.stoerung"), (False, "erzeuger.hinweis")])
def test_ein_stufenwechsel_der_offenen_kennung_schliesst_die_meldung_und_zaehlt_neu(
        make_store, vorher_hinweis, code_danach):
    """Aus dem Wartungshinweis wird eine Stoerung (und umgekehrt): die offene Meldung traegt sonst die falsche Stufe.
    Die alte Meldung schliesst ohne Push, die neue Art braucht ihre eigenen 15 Minuten."""
    rt = _rt(make_store)
    _setzen(rt, _stoerung("f_28", hinweis=vorher_hinweis))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)
    pushes = len(rt.kunde.pushes)

    _setzen(rt, _stoerung("f_28", hinweis=not vorher_hinweis))
    erzeugerstoerung.check(rt)
    assert rt.kunde.offen == {} and rt.kunde.withdrawn == ["erzeuger_stoerung:f_28"]
    assert rt.notifier.state("erzeuger_stoerung:f_28") == STATE_OK
    assert len(rt.kunde.pushes) == pushes

    rt.clock.advance(BESTAETIGUNG - 1)
    erzeugerstoerung.check(rt)
    assert rt.kunde.offen == {}
    rt.clock.advance(1)
    erzeugerstoerung.check(rt)
    assert _offen(rt) == {"erzeuger_stoerung:f_28": code_danach}
    assert len(rt.kunde.pushes) == pushes + 1


def test_eine_offene_kennung_ohne_gueltige_ablage_wird_nach_dem_neustart_mit_der_echten_meldung_neu_gemeldet(make_store):
    store = make_store()
    vorher = _rt(make_store, store=store)
    _setzen(vorher, _stoerung("f_28", hinweis=True))
    erzeugerstoerung.check(vorher)
    vorher.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(vorher)
    store.update(notify_meldungen={})  # Bestand ohne Ablage

    danach = _rt(make_store, store=store)
    _setzen(danach, _stoerung("f_28", hinweis=True))
    erzeugerstoerung.check(danach)

    assert _offen(danach) == {"erzeuger_stoerung:f_28": "erzeuger.hinweis"} and len(danach.kunde.pushes) == 1
    assert store.state.notify_meldungen["erzeuger_stoerung:f_28"] == meldungen.zu_dict(
        meldungen.neu("erzeuger.hinweis", roh_text="f_28", quelle=QUELLE))
    erzeugerstoerung.check(danach)
    assert len(danach.kunde.pushes) == 1 and danach.kunde.withdrawn == []


def test_ein_wegfall_waehrend_des_neustarts_schliesst_die_gespeicherte_meldung(make_store):
    store = make_store()
    vorher = _rt(make_store, store=store)
    _setzen(vorher, _stoerung("f_28", hinweis=True))
    erzeugerstoerung.check(vorher)
    vorher.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(vorher)

    danach = _rt(make_store, store=store)
    erzeugerstoerung.check(danach)
    assert danach.kunde.withdrawn == ["erzeuger_stoerung:f_28"]
    assert danach.notifier.state("erzeuger_stoerung:f_28") == STATE_OK


def test_das_log_nennt_weder_kennung_noch_entity(make_store, caplog):
    rt = _rt(make_store)
    _setzen(rt, _stoerung("f_28"))
    with caplog.at_level(logging.DEBUG):
        erzeugerstoerung.check(rt)
        rt.clock.advance(BESTAETIGUNG)
        erzeugerstoerung.check(rt)
        rt.signals.eintraege = SourceUnavailable(REF)
        erzeugerstoerung.check(rt)
    assert "f_28" not in caplog.text and "anlage_trouble" not in caplog.text


def test_die_meldungen_gehen_mit_quelle_und_roh_auf_den_draht(make_store):
    rt = _rt(make_store)
    _setzen(rt, _stoerung("f_28"))
    erzeugerstoerung.check(rt)
    rt.clock.advance(BESTAETIGUNG)
    erzeugerstoerung.check(rt)
    meldung = rt.kunde.offen["erzeuger_stoerung:f_28"]
    assert meldungen.draht(meldung)["params"] == {"roh_text": "f_28"}
    assert meldungen.erzeuger_angaben(meldung) == {"quelle": QUELLE, "roh": "f_28"}
