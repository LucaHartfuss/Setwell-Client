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
    """Fake-Wanduhr (`jetzt`) und fake monotone Uhr (`mono`, Sekunden; `__call__` ist der Port `clock`)."""

    def __init__(self, jetzt):
        self.jetzt = jetzt
        self.mono = 1000.0

    def __call__(self) -> float:
        return self.mono


@pytest.fixture
def uhr(monkeypatch):
    """Fake-Wanduhr; der Tag der Zaehler beginnt je Test neu."""
    fake = _Uhr(TAG1)
    monkeypatch.setattr(wallclock, "_now", lambda: fake.jetzt)
    monkeypatch.setattr(wb, "_tag", None)
    monkeypatch.setattr(wb, "_anker", None)
    _AKTUELL[:] = [fake]
    return fake


_AKTUELL: list[_Uhr] = []


def _fortschreiben(vertrauen: bool) -> list[int]:
    fragen: list[int] = []

    def urteil() -> bool:
        fragen.append(1)
        return vertrauen

    wb.tag_fortschreiben(urteil, _AKTUELL[0])
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
    rt = SimpleNamespace(idle=False, uhr_vertrauen=lambda: False, clock=uhr)
    _handler_laeuft(rt, aufrufe)
    assert wb.today() == "2026-10-03"
    rt.uhr_vertrauen = lambda: True
    _handler_laeuft(rt, aufrufe)
    assert wb.today() == "2026-10-04"
    assert aufrufe == [1, 1]


def test_der_tag_wird_vor_dem_handler_fortgeschrieben(uhr):
    assert wb.today() == "2026-10-03"
    uhr.jetzt = TAG2
    rt = SimpleNamespace(idle=False, uhr_vertrauen=lambda: True, clock=uhr)
    gesehen: list[str] = []
    app._unless_idle(rt, lambda rt, event: gesehen.append(wb.today()), Event("x"))
    assert gesehen == ["2026-10-04"]


# --- ohne vertrauenswuerdige Uhr schreitet der Tag nach der monotonen Uhr fort ---

TAG = 86_400.0


def test_ohne_vertrauen_schreitet_der_tag_nach_der_monotonen_uhr_fort(uhr):
    assert wb.today() == "2026-10-03"
    _fortschreiben(False)  # setzt den Anker
    uhr.mono += TAG - 1
    _fortschreiben(False)
    assert wb.today() == "2026-10-03"
    uhr.mono += 1
    _fortschreiben(False)
    assert wb.today() == "2026-10-04"
    uhr.mono += TAG - 1
    _fortschreiben(False)
    assert wb.today() == "2026-10-04"
    uhr.mono += 1
    _fortschreiben(False)
    assert wb.today() == "2026-10-05"


def test_mehrere_volle_tage_zwischen_zwei_aufrufen_zaehlen_alle(uhr):
    assert wb.today() == "2026-10-03"
    _fortschreiben(False)
    uhr.mono += 2 * TAG + 5
    _fortschreiben(False)
    assert wb.today() == "2026-10-05"
    uhr.mono += TAG - 6  # der Rest der zwei Tage blieb im Anker, er geht nicht verloren
    _fortschreiben(False)
    assert wb.today() == "2026-10-05"
    uhr.mono += 1
    _fortschreiben(False)
    assert wb.today() == "2026-10-06"


def test_die_wanduhr_hat_ohne_vertrauen_keinen_einfluss_auf_den_fortschritt(uhr):
    assert wb.today() == "2026-10-03"
    _fortschreiben(False)
    uhr.jetzt = VORTAG  # die Wanduhr springt zurueck: nur die monotone Zeit zaehlt
    uhr.mono += TAG
    _fortschreiben(False)
    assert wb.today() == "2026-10-04"
    uhr.jetzt = datetime(2030, 1, 1)  # und weit vor: ohne monotone Zeit kein neuer Tag
    _fortschreiben(False)
    assert wb.today() == "2026-10-04"


def test_ohne_vertrauen_laeuft_der_tag_nie_rueckwaerts(uhr):
    assert wb.today() == "2026-10-03"
    _fortschreiben(False)
    uhr.mono -= 5 * TAG
    _fortschreiben(False)
    assert wb.today() == "2026-10-03"


def test_mit_vertrauen_gilt_die_wanduhr_und_der_anker_beginnt_dort_neu(uhr):
    assert wb.today() == "2026-10-03"
    _fortschreiben(False)
    uhr.mono += TAG - 10
    uhr.jetzt = TAG2
    _fortschreiben(True)
    assert wb.today() == "2026-10-04"
    uhr.mono += TAG - 1  # seit dem Setzen des Tages noch nicht ganz 24 Stunden
    _fortschreiben(False)
    assert wb.today() == "2026-10-04"
    uhr.mono += 1
    _fortschreiben(False)
    assert wb.today() == "2026-10-05"


def test_ein_tag_aus_den_monotonen_tagen_vor_der_wanduhr_wird_mit_vertrauen_zurueckgestellt(uhr):
    assert wb.today() == "2026-10-03"
    _fortschreiben(False)
    uhr.mono += 2 * TAG
    _fortschreiben(False)
    assert wb.today() == "2026-10-05"
    _fortschreiben(True)  # die Uhr wurde synchronisiert: Wanduhr steht auf dem 3.
    assert wb.today() == "2026-10-03"


def test_der_tag_wird_ueber_today_ohne_fortschreiben_gesetzt_und_beim_ersten_fortschreiben_verankert(uhr):
    assert wb.today() == "2026-10-03"  # kein Fortschreiben davor: kein Anker
    uhr.mono += 10 * TAG
    _fortschreiben(False)  # der erste Aufruf verankert nur
    assert wb.today() == "2026-10-03"
    uhr.mono += TAG
    _fortschreiben(False)
    assert wb.today() == "2026-10-04"


def test_ohne_je_vertrauenswuerdige_uhr_gibt_es_nach_zwei_monotonen_tagen_wieder_kaelteschutz_starts(uhr):
    """Die Gefaehrdung: der Start-Zaehler des Notfall-Boosts gilt auch fuer den Kaelteschutz. Ohne Vertrauen und ohne Neustart
    waren nach sechs Starts alle weiteren verwehrt; jetzt gibt es mit jedem monotonen Tag wieder sechs."""
    regel = wb.START_QUOTA
    starts = 0
    eintrag = None
    jetzt = 0.0
    _fortschreiben(False)
    for _ in range(96):  # zwei monotone Tage im Takt der Mindestwartezeit, die Wanduhr steht (nie synchron)
        if wb.allowed(eintrag, regel, jetzt, wb.today()):
            eintrag = wb.counted(eintrag, jetzt, wb.today())
            starts += 1
        jetzt += wb.INTERVAL_SECONDS
        uhr.mono += wb.INTERVAL_SECONDS
        _fortschreiben(False)
    assert starts == 2 * wb.MAX_PER_DAY


def test_ein_wanduhrsprung_allein_setzt_die_zaehler_ohne_vertrauen_nicht_zurueck(uhr):
    regel = wb.START_QUOTA
    assert wb.today() == "2026-10-03"
    _fortschreiben(False)
    eintrag = {"day": wb.today(), "count": 6, "last": 0.0}
    uhr.jetzt = datetime(2026, 10, 9, 3, 0)
    _fortschreiben(False)
    assert wb.allowed(eintrag, regel, 99999.0, wb.today()) is False


# --- Eintraege eines spaeteren Tages zaehlen als heute (die Uhr stand beim Start vor und wurde zurueckgestellt) ---

HEUTE = "2026-10-03"
SPAETER = "2026-10-09"


def test_ein_eintrag_eines_spaeteren_tages_zaehlt_als_heute():
    regel = wb.enforce_rule(6)
    eintrag = {"day": SPAETER, "count": 6, "last": 0.0}
    assert wb.allowed(eintrag, regel, 99999.0, HEUTE) is False
    assert wb.count_today(eintrag, HEUTE) == 6
    assert wb.limit_first_reached(eintrag, regel, HEUTE) is True
    assert wb.limit_first_reached({**eintrag, "limit_notified": HEUTE}, regel, HEUTE) is False


def test_ein_eintrag_eines_spaeteren_tages_wird_beim_zaehlen_fortgefuehrt():
    eintrag = {"day": SPAETER, "count": 3, "last": 0.0}
    assert wb.counted(eintrag, 5.0, HEUTE) == {"day": HEUTE, "count": 4, "last": 5.0}
    assert wb.added(eintrag, 2, 5.0, HEUTE) == {"day": HEUTE, "count": 5, "last": 5.0}


def test_ein_eintrag_eines_frueheren_tages_beginnt_weiter_neu():
    eintrag = {"day": "2026-10-01", "count": 6, "last": 0.0}
    assert wb.allowed(eintrag, wb.enforce_rule(6), 0.0, HEUTE) is True
    assert wb.count_today(eintrag, HEUTE) == 0
    assert wb.counted(eintrag, 5.0, HEUTE) == {"day": HEUTE, "count": 1, "last": 5.0}
    assert wb.added(eintrag, 2, 5.0, HEUTE) == {"day": HEUTE, "count": 2, "last": 5.0}


# --- ein Fehler beim Fortschreiben haelt den Handler nicht an ---

def test_ein_fehler_des_uhrurteils_gilt_als_nicht_vertrauenswuerdig_und_der_tag_schreitet_monoton_fort(uhr):
    assert wb.today() == "2026-10-03"
    _fortschreiben(False)
    uhr.mono += TAG

    def kaputt() -> bool:
        raise RuntimeError("kaputt")

    uhr.jetzt = TAG2
    wb.tag_fortschreiben(kaputt, uhr)
    assert wb.today() == "2026-10-04"


def test_ein_fehler_beim_fortschreiben_laesst_den_handler_laufen(uhr, monkeypatch):
    def kaputt(*_args):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(wb, "tag_fortschreiben", kaputt)
    aufrufe: list[int] = []
    rt = SimpleNamespace(idle=False, uhr_vertrauen=lambda: True, clock=uhr)
    _handler_laeuft(rt, aufrufe)
    assert aufrufe == [1]
