"""Datum der Ursprungswerte unter einer kritischen Mindestversion: lokaler Beginn (Entitlement-Datei je Kreis) plus
GRACE_PERIOD, als ISO-Datum in der Zeitzone des gespeicherten Beginns; ohne bekannten Beginn kein Datum."""
import logging
from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeClock

from heizungsbruecke import geraet_status
from smartheat_core import meldungen
from smartheat_device.geraet_host_basis import KritischesEnde
from smartheat_device.host import Anzeige
from smartheat_runtime import entitlement

MEZ = timezone(timedelta(hours=1))


def _beginn(pfad, jahr, monat, tag, stunde=0, minute=0, sekunde=0, kritisch=True):
    entitlement.mark_inactive(pfad, datetime(jahr, monat, tag, stunde, minute, sekunde, tzinfo=MEZ), kritisch=kritisch)


def _ende(tmp_path, kritisch=True, clock=None):
    return KritischesEnde(lambda: kritisch, lambda kreis: tmp_path / f"kreis{kreis}.json",
                                        clock=clock or FakeClock())


def test_das_datum_ist_der_beginn_plus_dreissig_tage(tmp_path):
    _beginn(tmp_path / "kreis1.json", 2026, 1, 1)
    assert _ende(tmp_path)([1]) == "2026-01-31"


def test_der_randtag_zaehlt_nach_dem_kalendertag_des_beginns(tmp_path):
    _beginn(tmp_path / "kreis1.json", 2026, 11, 1, 23, 59, 59)
    assert _ende(tmp_path)([1]) == "2026-12-01"  # kurz vor Mitternacht bleibt der Tag, nicht der naechste
    _beginn(tmp_path / "kreis2.json", 2026, 12, 5, 23, 59, 59)
    assert _ende(tmp_path)([2]) == "2027-01-04"  # ueber den Jahreswechsel


def test_ein_neustart_behaelt_das_datum(tmp_path):
    _beginn(tmp_path / "kreis1.json", 2026, 3, 10, 8)
    assert _ende(tmp_path)([1]) == "2026-04-09"
    # neuer Prozess: neue Instanz, spaeterer Zeitpunkt, dieselbe Datei; ein zweiter Beginn ueberschreibt nichts
    _beginn(tmp_path / "kreis1.json", 2026, 3, 20, 8)
    assert _ende(tmp_path, clock=FakeClock(start=10_000.0))([1]) == "2026-04-09"


def test_ohne_beginn_ist_das_datum_unbekannt(tmp_path):
    assert _ende(tmp_path)([1]) is None  # Uhr unklar: kein Beginn gespeichert
    assert _ende(tmp_path)([]) is None


def test_ohne_kritische_mindestversion_gibt_es_kein_datum(tmp_path):
    _beginn(tmp_path / "kreis1.json", 2026, 1, 1, kritisch=False)  # Beginn des Abos
    assert _ende(tmp_path, kritisch=False)([1]) is None


def test_mehrere_kreise_zeigen_das_fruehere_datum(tmp_path):
    _beginn(tmp_path / "kreis1.json", 2026, 2, 10)
    _beginn(tmp_path / "kreis2.json", 2026, 2, 1)
    assert _ende(tmp_path)([1, 2]) == "2026-03-03"
    assert _ende(tmp_path)([2, 1]) == "2026-03-03"


def test_eine_beschaedigte_datei_gibt_kein_datum_und_nur_eine_warnung_je_halbe_minute(tmp_path, caplog):
    (tmp_path / "kreis1.json").write_text("{kaputt")
    clock = FakeClock()
    ende = _ende(tmp_path, clock=clock)
    with caplog.at_level(logging.WARNING):
        assert [ende([1]) for _ in range(5)] == [None] * 5
    assert len([r for r in caplog.records if "nicht lesbar" in r.getMessage()]) == 1
    clock.advance(KritischesEnde.CACHE_SECONDS)
    with caplog.at_level(logging.WARNING):
        assert ende([1]) is None
    assert len([r for r in caplog.records if "nicht lesbar" in r.getMessage()]) == 2


def test_das_datum_erscheint_im_ereignis_als_parameter_und_ohne_datum_die_andere_fassung():
    anzeige = Anzeige(zustand="update_noetig", server_ok=True, device_state="uebernommen", eingerichtet=True,
                      hinweise=(), meldungen=(), konfiguration_abgelehnt=None)
    mit = geraet_status.meldungen_liste(anzeige, "2026-12-01", kritisch=True)
    assert mit == [{"key": "update_noetig", "code": "meldung.update_noetig", "params": {"ende": "2026-12-01"},
                    "stufe": "rot"}]
    ohne = geraet_status.meldungen_liste(anzeige, None, kritisch=True)
    assert ohne == [{"key": "update_noetig", "code": "meldung.update_noetig_unbekannt", "params": {}, "stufe": "rot"}]
    assert meldungen.aus_dict({feld: mit[0][feld] for feld in ("code", "params", "stufe")}) is not None  # gueltig


def test_die_lokale_sicht_traegt_das_datum_und_das_ereignis_gibt_es_weiter(tmp_path):
    from fakes import FakeHa
    ha = FakeHa()
    lokal = geraet_status.lokale_sicht(
        lambda: None, lambda kreis: None, lambda kreis: None, lambda: None,
        kritisch_ende=lambda kreise: "2026-12-01", update_noetig_kritisch=lambda: True)
    assert lokal()["update_noetig_ende"] == "2026-12-01" and lokal()["update_noetig_kritisch"] is True
    status = geraet_status.GeraetStatus(ha, "shg-aaaaaaaaaaaaaaaa", lokal, laufzeit_merken=lambda e: None)
    status.anzeige(Anzeige(zustand="update_noetig", server_ok=True, device_state="uebernommen", eingerichtet=True,
                           hinweise=(), meldungen=(), konfiguration_abgelehnt=None))
    gesendet = ha.events[-1][1]["meldungen"]
    assert [(m["code"], m["params"]) for m in gesendet] == [("meldung.update_noetig", {"ende": "2026-12-01"})]


def test_die_lokale_sicht_ohne_kritischen_eintrag_nennt_ihn_nicht():
    lokal = geraet_status.lokale_sicht(lambda: None, lambda kreis: None, lambda kreis: None, lambda: None,
                                       update_noetig_kritisch=lambda: False)
    assert lokal()["update_noetig_kritisch"] is False


def test_ob_ein_kritischer_eintrag_vorliegt_muss_jeder_aufrufer_ausdruecklich_angeben():
    anzeige = Anzeige(zustand="update_noetig", server_ok=True, device_state="uebernommen", eingerichtet=True,
                      hinweise=(), meldungen=(), konfiguration_abgelehnt=None)
    with pytest.raises(TypeError):
        geraet_status.meldungen_liste(anzeige, None)
    with pytest.raises(TypeError):
        geraet_status.lokale_sicht(lambda: None, lambda kreis: None, lambda kreis: None, lambda: None)
