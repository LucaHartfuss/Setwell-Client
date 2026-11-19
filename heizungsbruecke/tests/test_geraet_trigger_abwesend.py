"""Away aus Home Assistant: die Ereignisse smartheat_abwesend und smartheat_abwesend_ende erreichen den Host erst nach
erneuter Pruefung (Kreis, Temperatur, 365 Tage aus safety.py); Ablehnung ist ein Hinweis mit festem Grund, nie mit Wert."""
import logging
from datetime import UTC, datetime, timedelta

import pytest
from test_geraet_trigger import Aufbau

from smartheat_core import safety

JETZT = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)
BIS = JETZT + timedelta(days=3)


class Away:
    """Aufbau mit den Rueckrufen des Hosts; `antwort` ist der Ablehnungsgrund, den der Host liefern soll (None = angenommen)."""

    def __init__(self, tmp_path, antwort=None, **mehr):
        self.setzen, self.beenden, self.abgelehnt = [], [], []
        self.antwort = antwort
        self.a = Aufbau(tmp_path, abwesend_setzen=self._setzen, abwesend_beenden=self._beenden,
                        abwesend_abgelehnt=self.abgelehnt.append, wanduhr=lambda: JETZT, **mehr)

    def _setzen(self, bis, temp, durch):
        self.setzen.append((bis, temp, durch))
        return self.antwort

    def _beenden(self, durch):
        self.beenden.append(durch)
        return self.antwort

    def senden(self, daten, name="smartheat_abwesend", context=None):
        self.a.fire({"platform": "event", "event": {"event_type": name, "data": daten, "context": context or {}}})
        self.a.run()


def _daten(**felder):
    return {"schema": 1, "kreis": 1, "bis": BIS.isoformat(), "temp": 17.5, **felder}


def test_ohne_die_rueckrufe_gibt_es_kein_abonnement(tmp_path):
    triggers = Aufbau(tmp_path).client._triggers
    assert not any(t.get("event_type", "").startswith("smartheat_abwesend") for t in triggers)


def test_mit_den_rueckrufen_sind_beide_ereignisse_abonniert(tmp_path):
    triggers = Away(tmp_path).a.client._triggers
    assert {"platform": "event", "event_type": "smartheat_abwesend"} in triggers
    assert {"platform": "event", "event_type": "smartheat_abwesend_ende"} in triggers


def test_ein_gueltiges_away_geht_mit_bis_temp_und_durch_an_den_host(tmp_path):
    t = Away(tmp_path)
    t.senden(_daten(), context={"user_id": "u"})
    assert t.setzen == [(BIS, 17.5, "nutzer")] and t.abgelehnt == [] and t.beenden == []


def test_ohne_temp_entscheidet_der_host(tmp_path):
    t = Away(tmp_path)
    t.senden({"schema": 1, "kreis": 1, "bis": BIS.isoformat()})
    assert t.setzen == [(BIS, None, None)] and t.abgelehnt == []


@pytest.mark.parametrize("temp", [safety.LEERSTAND_UNTERGRENZE_C, safety.BEDIENBEREICH_MAX_C])
def test_die_grenzen_der_temperatur_gelten(tmp_path, temp):
    t = Away(tmp_path)
    t.senden(_daten(temp=temp))
    assert t.setzen and t.abgelehnt == []


@pytest.mark.parametrize("temp", [safety.LEERSTAND_UNTERGRENZE_C - 0.5, safety.BEDIENBEREICH_MAX_C + 0.5, float("nan"),
                                  True, "heiss"])
def test_eine_temperatur_ausserhalb_wird_abgelehnt(tmp_path, temp):
    t = Away(tmp_path)
    t.senden(_daten(temp=temp))
    assert t.setzen == [] and t.abgelehnt == ["temp"]


def test_bis_genau_365_tage_gilt_eine_sekunde_mehr_nicht(tmp_path):
    t = Away(tmp_path)
    t.senden(_daten(bis=(JETZT + timedelta(days=365)).isoformat()))
    assert len(t.setzen) == 1 and t.abgelehnt == []
    t.senden(_daten(bis=(JETZT + timedelta(days=365, seconds=1)).isoformat()))
    assert len(t.setzen) == 1 and t.abgelehnt == ["bis"]


@pytest.mark.parametrize("bis", [JETZT.isoformat(), (JETZT - timedelta(hours=1)).isoformat(), "morgen", 5, None,
                                 "2026-11-21T12:00:00"])
def test_ein_bis_in_der_vergangenheit_ohne_zone_oder_unlesbar_wird_abgelehnt(tmp_path, bis):
    t = Away(tmp_path)
    t.senden(_daten(bis=bis))
    assert t.setzen == [] and t.abgelehnt == ["bis"]


def test_ein_unbekannter_kreis_wird_nur_vom_kleinsten_kreis_gemeldet(tmp_path):
    erster = Away(tmp_path / "1", anlagenkreise=frozenset({1, 2}), kreis=1)
    zweiter = Away(tmp_path / "2", anlagenkreise=frozenset({1, 2}), kreis=2)
    for t in (erster, zweiter):
        t.senden(_daten(kreis=3))
        t.senden({"schema": 1, "kreis": 3}, name="smartheat_abwesend_ende")
        assert t.setzen == [] and t.beenden == []
    assert erster.abgelehnt == ["kreis", "kreis"] and zweiter.abgelehnt == []


def test_das_ereignis_gilt_nur_dem_client_seines_kreises(tmp_path):
    t = Away(tmp_path, anlagenkreise=frozenset({1, 2}), kreis=2)
    t.senden(_daten(kreis=1))
    assert t.setzen == [] and t.abgelehnt == []
    t.senden(_daten(kreis=2))
    assert len(t.setzen) == 1


@pytest.mark.parametrize("kreis", [0, "1", True, None, 1.5])
def test_ein_ungueltiger_kreis_wird_verworfen(tmp_path, kreis):
    t = Away(tmp_path)
    t.senden(_daten(kreis=kreis))
    t.senden({"schema": 1, "kreis": kreis}, name="smartheat_abwesend_ende")
    assert t.setzen == [] and t.beenden == [] and t.abgelehnt == []


def test_das_ende_geht_mit_durch_an_den_host(tmp_path):
    t = Away(tmp_path)
    t.senden({"schema": 1, "kreis": 1}, name="smartheat_abwesend_ende", context={"parent_id": "p"})
    assert t.beenden == ["automation"] and t.setzen == [] and t.abgelehnt == []


def test_lehnt_der_host_ab_wird_der_grund_als_hinweis_gemeldet(tmp_path):
    t = Away(tmp_path, antwort="nicht_abwesend")
    t.senden({"schema": 1, "kreis": 1}, name="smartheat_abwesend_ende")
    t.senden(_daten())
    assert t.abgelehnt == ["nicht_abwesend", "nicht_abwesend"]


def test_ein_ereignis_ist_kein_wunsch_und_kein_lokaler_check(tmp_path):
    t = Away(tmp_path)
    t.senden(_daten())
    assert t.a.wuensche == [] and t.a.checks == []


def test_ein_unlesbarer_ereigniskern_wird_abgelehnt_ohne_absturz(tmp_path):
    t = Away(tmp_path)
    t.a.fire({"platform": "event", "event": {"event_type": "smartheat_abwesend", "data": "kaputt", "context": {}}})
    t.a.fire({"platform": "event", "event": {"event_type": "smartheat_abwesend", "context": {}}})
    t.a.run()
    assert t.setzen == []


def test_rueckkehrzeit_und_temperatur_stehen_nie_im_log(tmp_path, caplog):
    t = Away(tmp_path)
    gezogen_bis, gezogen_temp = JETZT + timedelta(days=77, minutes=13), 18.25
    with caplog.at_level(logging.DEBUG):
        t.senden(_daten(bis=gezogen_bis.isoformat(), temp=gezogen_temp))
        t.senden(_daten(bis=(JETZT + timedelta(days=400, minutes=13)).isoformat(), temp=gezogen_temp))
        t.senden(_daten(temp=33.77))
        t.senden({"schema": 1, "kreis": 1}, name="smartheat_abwesend_ende")
    assert t.abgelehnt == ["bis", "temp"]
    for geheim in (gezogen_bis.isoformat(), "T12:13", "-01-", "18.25", "33.77", "2027"):
        assert geheim not in caplog.text
    assert "77" not in caplog.text
