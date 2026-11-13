"""Tag der Tageszaehler (Schreibbudget, Durchsetzen): folgt der Wanduhr nur bei vertrauenswuerdiger Uhr.

Ein Uhrsprung ueber die Tagesgrenze darf die Zaehler nicht zuruecksetzen: ohne Vertrauen bleibt der alte Tag."""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from smartheat_core import enforce, wallclock
from smartheat_core import write_budget as wb
from smartheat_runtime import app
from smartheat_runtime.worker import Event

TAG1 = datetime(2026, 10, 3, 23, 50)
TAG2 = datetime(2026, 10, 4, 0, 10)
VORTAG = datetime(2026, 10, 2, 12, 0)


class _Uhr:
    def __init__(self, jetzt):
        self.jetzt = jetzt


@pytest.fixture
def uhr(monkeypatch):
    """Fake-Wanduhr; der Tag der Zaehler beginnt je Test neu."""
    fake = _Uhr(TAG1)
    monkeypatch.setattr(wallclock, "_now", lambda: fake.jetzt)
    monkeypatch.setattr(wb, "_tag", None)
    return fake


def _fortschreiben(vertrauen: bool) -> list[int]:
    fragen: list[int] = []

    def urteil() -> bool:
        fragen.append(1)
        return vertrauen

    wb.tag_fortschreiben(urteil)
    return fragen


def test_der_erste_tag_ist_der_der_wanduhr(uhr):
    assert wb.today() == "2026-10-03"


def test_ohne_vertrauen_springt_der_tag_vorwaerts_nicht(uhr):
    assert wb.today() == "2026-10-03"
    uhr.jetzt = TAG2
    _fortschreiben(False)
    assert wb.today() == "2026-10-03"


def test_ohne_vertrauen_springt_der_tag_rueckwaerts_nicht(uhr):
    assert wb.today() == "2026-10-03"
    uhr.jetzt = VORTAG
    _fortschreiben(False)
    assert wb.today() == "2026-10-03"


def test_mit_vertrauen_folgt_der_tag_der_wanduhr_vorwaerts_und_rueckwaerts(uhr):
    assert wb.today() == "2026-10-03"
    uhr.jetzt = TAG2
    _fortschreiben(True)
    assert wb.today() == "2026-10-04"
    uhr.jetzt = VORTAG
    _fortschreiben(True)
    assert wb.today() == "2026-10-02"


def test_nach_dem_sprung_gilt_der_neue_tag_erst_mit_dem_vertrauen(uhr):
    assert wb.today() == "2026-10-03"
    uhr.jetzt = TAG2
    _fortschreiben(False)
    _fortschreiben(False)
    assert wb.today() == "2026-10-03"
    _fortschreiben(True)
    assert wb.today() == "2026-10-04"


def test_am_selben_tag_wird_das_vertrauen_nicht_gefragt(uhr):
    assert wb.today() == "2026-10-03"
    assert _fortschreiben(False) == []


def test_ein_sprung_ueber_mitternacht_setzt_das_tageslimit_nicht_zurueck(uhr):
    """Der Kern der Gefaehrdung: sechs Durchsetzungen sind verbraucht, die Uhr springt auf den naechsten Tag."""
    regel = wb.enforce_rule(6)
    eintrag = {"day": wb.today(), "count": 6, "last": 0.0}
    uhr.jetzt = TAG2
    _fortschreiben(False)
    assert wb.allowed(eintrag, regel, 99999.0, wb.today()) is False
    _fortschreiben(True)
    assert wb.allowed(eintrag, regel, 99999.0, wb.today()) is True


def test_das_gesamtlimit_des_tages_bleibt_nach_dem_sprung_erreicht(uhr):
    eintrag = wb.added(None, 5, 0.0, wb.today())
    uhr.jetzt = TAG2
    _fortschreiben(False)
    assert wb.count_today(eintrag, wb.today()) == 5


def test_enforce_liest_den_tag_der_zaehler(uhr):
    assert enforce._today() == date(2026, 10, 3)
    uhr.jetzt = TAG2
    _fortschreiben(False)
    assert enforce._today() == date(2026, 10, 3)
    _fortschreiben(True)
    assert enforce._today() == date(2026, 10, 4)


def _handler_laeuft(rt, aufrufe):
    app._unless_idle(rt, lambda rt, event: aufrufe.append(1), Event("x"))


def test_jedes_ereignis_des_workers_schreibt_den_tag_fort(uhr):
    assert wb.today() == "2026-10-03"
    uhr.jetzt = TAG2
    aufrufe: list[int] = []
    rt = SimpleNamespace(idle=False, uhr_vertrauen=lambda: False)
    _handler_laeuft(rt, aufrufe)
    assert wb.today() == "2026-10-03"
    rt.uhr_vertrauen = lambda: True
    _handler_laeuft(rt, aufrufe)
    assert wb.today() == "2026-10-04"
    assert aufrufe == [1, 1]


def test_der_tag_wird_vor_dem_handler_fortgeschrieben(uhr):
    assert wb.today() == "2026-10-03"
    uhr.jetzt = TAG2
    rt = SimpleNamespace(idle=False, uhr_vertrauen=lambda: True)
    gesehen: list[str] = []
    app._unless_idle(rt, lambda rt, event: gesehen.append(wb.today()), Event("x"))
    assert gesehen == ["2026-10-04"]
