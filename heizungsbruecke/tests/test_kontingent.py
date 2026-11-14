"""Kontingent der Quelle: ein erschoepftes Abfragekontingent wird dem Kunden einmal gemeldet (nur Push), die Rueckkehr
entwarnt; Quellen ohne Kontingent (HA) und die Aussentemperatur-Ueberwachung bleiben davon unberuehrt."""
from types import SimpleNamespace

from fakes import FakeNotifySink

from smartheat_core import meldungen
from smartheat_runtime import aussentemperatur, kontingent
from smartheat_runtime.notifier import STATE_OK, Notifier
from smartheat_runtime.ports import QuelleErschoepft
from smartheat_runtime.roles import ChannelManifest

TEXT = "Das Abfragekontingent bei Viessmann ist erschöpft."


class _Quelle:
    def __init__(self) -> None:
        self.erschoepft = False

    def kontingent_erschoepft(self) -> bool:
        return self.erschoepft

    def get_state(self, ref):
        raise QuelleErschoepft(TEXT)


def _rt(make_store, signals):
    store = make_store()
    kunde = FakeNotifySink()
    return SimpleNamespace(manifest=ChannelManifest(refs={"outdoor_temp": "treiber:outdoor_temp"}), signals=signals,
                           store=store, notifier=Notifier(store, kunde), kunde=kunde)


def test_erschoepftes_kontingent_meldet_einmal_und_entwarnt(make_store):
    quelle = _Quelle()
    rt = _rt(make_store, quelle)

    for _ in range(3):
        kontingent.check(rt)
    assert rt.kunde.pushes == []  # Kontingent in Ordnung

    quelle.erschoepft = True
    for _ in range(3):
        kontingent.check(rt)
    assert [key for key, _text in rt.kunde.pushes] == ["kontingent"]  # einmal je Wechsel
    assert rt.kunde.codes == [("kontingent", "meldung.kontingent_erschoepft")]
    assert rt.kunde.shown == {}  # nicht kritisch: nur ein Push
    assert rt.notifier.state("kontingent") == kontingent.STATE_ERSCHOEPFT

    quelle.erschoepft = False
    kontingent.check(rt)
    assert [key for key, _text in rt.kunde.pushes] == ["kontingent"] * 2
    assert rt.kunde.codes[-1] == ("kontingent", "meldung.kontingent_frei")
    assert rt.notifier.state("kontingent") == STATE_OK


def test_die_kontingent_meldung_traegt_nur_code_und_parameter_keinen_freitext(make_store):
    """Auf den Draht gehen nur Code und Parameter; weder der Kundentext der Quelle noch der Herstellername steht im
    Meldungsobjekt."""
    quelle = _Quelle()
    quelle.erschoepft = True
    rt = _rt(make_store, quelle)
    kontingent.check(rt)
    meldung = rt.kunde.push_meldungen[0][1]
    assert meldung == meldungen.neu("meldung.kontingent_erschoepft")
    assert "Viessmann" not in repr(meldungen.draht(meldung))
    assert "Viessmann" not in repr(meldungen.zu_dict(meldung))


def test_quelle_ohne_kontingent_hat_nichts_zu_melden(make_store):
    rt = _rt(make_store, SimpleNamespace())  # wie die Quelle des HA-Hosts: kein kontingent_erschoepft
    kontingent.check(rt)
    assert rt.kunde.pushes == [] and rt.notifier.state("kontingent") == STATE_OK


def test_aussentemperatur_zaehlt_ein_erschoepftes_kontingent_nicht_als_ausfall(make_store):
    """Die Meldung zum Kontingent erklaert den fehlenden Wert; keine zweite Meldung zur Aussentemperatur, kein Fehler."""
    rt = _rt(make_store, _Quelle())
    rt.texts = SimpleNamespace(source_unavailable_log="Quelle nicht erreichbar")
    for _ in range(10):
        aussentemperatur.check(rt)
    assert rt.kunde.pushes == [] and rt.store.state.outdoor_misses == 0
