"""Zaehler der Aenderungen am Thermostat im Leerstand: gleitendes Fenster, persistent, nur die Uhr des Speichers."""
from datetime import UTC, datetime, timedelta

import pytest

from smartheat_runtime.leerstand_zaehler import (
    BEENDEN_AB,
    FENSTER,
    UEBERSTEUERUNG_DAUER,
    LeerstandZaehler,
)

START = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)
KNAPP = timedelta(seconds=1)


class Uhr:
    def __init__(self):
        self.jetzt = START

    def __call__(self) -> str:
        return self.jetzt.isoformat()

    def weiter(self, dauer: timedelta) -> None:
        self.jetzt += dauer


@pytest.fixture
def uhr():
    return Uhr()


def _zaehler(tmp_path, uhr) -> LeerstandZaehler:
    return LeerstandZaehler(tmp_path / "leerstand.json", uhr)


def test_die_konstanten_sind_vier_und_vierundzwanzig_stunden_und_die_dritte_aenderung():
    assert (UEBERSTEUERUNG_DAUER.total_seconds(), FENSTER.total_seconds(), BEENDEN_AB) == (4 * 3600, 24 * 3600, 3)


def test_die_aenderungen_im_fenster_werden_gezaehlt(tmp_path, uhr):
    zaehler = _zaehler(tmp_path, uhr)
    assert zaehler.anzahl() == 0
    assert [zaehler.eintragen() for _ in range(3)] == [1, 2, 3]
    assert zaehler.anzahl() == 3


def test_knapp_vor_dem_ende_des_fensters_zaehlt_eine_aenderung_noch(tmp_path, uhr):
    zaehler = _zaehler(tmp_path, uhr)
    zaehler.eintragen()
    uhr.weiter(FENSTER - KNAPP)
    assert zaehler.anzahl() == 1
    assert zaehler.eintragen() == 2


def test_nach_dem_ende_des_fensters_faellt_sie_heraus(tmp_path, uhr):
    zaehler = _zaehler(tmp_path, uhr)
    zaehler.eintragen()
    uhr.weiter(FENSTER)
    assert zaehler.anzahl() == 0
    uhr.weiter(KNAPP)
    assert zaehler.eintragen() == 1


def test_das_fenster_gleitet_die_aelteste_faellt_zuerst_heraus(tmp_path, uhr):
    zaehler = _zaehler(tmp_path, uhr)
    zaehler.eintragen()
    uhr.weiter(timedelta(hours=10))
    zaehler.eintragen()
    uhr.weiter(timedelta(hours=10))
    assert zaehler.eintragen() == 3
    uhr.weiter(timedelta(hours=4) + KNAPP)  # die erste ist jetzt ueber 24 h alt
    assert zaehler.anzahl() == 2


def test_nach_einem_neustart_bleibt_das_fenster_erhalten(tmp_path, uhr):
    _zaehler(tmp_path, uhr).eintragen()
    uhr.weiter(timedelta(hours=1))
    zweiter = _zaehler(tmp_path, uhr)
    assert zweiter.anzahl() == 1
    assert zweiter.eintragen() == 2


@pytest.mark.parametrize("inhalt", ["kein json", "[1, 2", '{"a": 1}', '["morgen", 5, null]', '"text"'],
                         ids=["text", "abgebrochen", "objekt", "unbrauchbare_eintraege", "string"])
def test_eine_kaputte_datei_ist_ein_leeres_fenster(tmp_path, uhr, inhalt):
    (tmp_path / "leerstand.json").write_text(inhalt)
    zaehler = _zaehler(tmp_path, uhr)
    assert zaehler.anzahl() == 0
    assert zaehler.eintragen() == 1


def test_eine_fehlende_datei_ist_ein_leeres_fenster(tmp_path, uhr):
    assert _zaehler(tmp_path, uhr).anzahl() == 0


def test_eintraege_aus_der_zukunft_oder_ohne_zeitzone_zaehlen_nicht(tmp_path, uhr):
    zukunft = (START + timedelta(hours=1)).isoformat()
    (tmp_path / "leerstand.json").write_text(f'["{zukunft}", "2026-01-15T19:00:00"]')
    assert _zaehler(tmp_path, uhr).anzahl() == 0


def test_leeren_beginnt_ein_neues_fenster_auch_nach_dem_neustart(tmp_path, uhr):
    zaehler = _zaehler(tmp_path, uhr)
    zaehler.eintragen()
    zaehler.leeren()
    assert zaehler.anzahl() == 0
    assert _zaehler(tmp_path, uhr).anzahl() == 0


def test_die_datei_ist_lesbar_und_ohne_tempdatei(tmp_path, uhr):
    import os
    import stat

    _zaehler(tmp_path, uhr).eintragen()
    assert stat.S_IMODE(os.stat(tmp_path / "leerstand.json").st_mode) == 0o644
    assert not (tmp_path / "leerstand.json.tmp").exists()
