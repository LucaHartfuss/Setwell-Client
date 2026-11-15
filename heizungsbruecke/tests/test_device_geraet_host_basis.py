"""Gemeinsamer Teil der Geraete-Hosts; das Verhalten beider Hosts prueft
test_host_konformitaet.py, hier nur, was ohne Host gilt."""
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import konfig_form
import pytest
from fakes import FakeClock

from smartheat_core import dateien, meldungen
from smartheat_device.commands import Done
from smartheat_device.geraet_host_basis import (
    EV_RELOAD,
    SIGN_OFF_WAIT_SECONDS,
    GeraetHostBasis,
    KritischesEnde,
    abmeldung_abwarten,
    wiederherstellungspunkt,
)
from smartheat_device.host import Anzeige
from smartheat_runtime import aufraeumen, dokument_config, entitlement
from smartheat_runtime.worker import RegulationWorker


class _Host(GeraetHostBasis):
    host, version, wunsch_herkunft = "test", "0.0.0", "test"

    def __init__(self, pfad: Path) -> None:
        super().__init__()
        self._pfad = pfad

    def aufraeumen_pfad(self) -> Path:
        return self._pfad

    def abgemeldet_pfad(self) -> Path:
        return self._pfad.with_name("abgemeldet.json")

    def entitlement_pfad(self, kreis: int) -> Path:
        return self._pfad.with_name(f"entitlement{kreis}.json")


def test_ein_neustart_aus_dem_geraetethread_waehrend_des_bindens_geht_nie_verloren(tmp_path):
    """Hauptthread bindet, Geraete-Thread fordert gleichzeitig an: genau ein EV_RELOAD erreicht den Worker."""
    for _ in range(200):
        host = _Host(tmp_path / "a.json")
        worker = RegulationWorker(FakeClock())
        kinds = []
        worker.register(EV_RELOAD, lambda event, ziel=kinds: ziel.append(event.kind))
        anfordern = threading.Thread(target=host.neustart_anfordern)
        anfordern.start()
        host.worker_binden(worker)
        anfordern.join()
        worker.run_pending()
        assert kinds == [EV_RELOAD]  # die Sperre serialisiert: entweder nachgestellt oder direkt, nie beides, nie keins


def test_der_neustart_ereignisname_ist_hostneutral():
    assert EV_RELOAD == "geraet_reload"


def test_ohne_aufraeumen_pfad_ist_die_basis_nicht_nutzbar(tmp_path):
    basis = GeraetHostBasis()
    with pytest.raises(NotImplementedError):
        basis.aufraeumen_pfad()


_FLACH = {"anlage": "t1", "hebelsatz": "vaillant_vrc720", "raumfuehler": ["sensor.a"], "abo": "aktiv"}
EINGERICHTET = konfig_form.nach_form(_FLACH)
LEER = konfig_form.nach_form({"anlage": None})  # wie der Server sie nach dem Erstkontakt schickt: kreise [] != fehlend


def _angefordert(host: _Host) -> bool:
    worker = RegulationWorker(FakeClock())
    kinds = []
    worker.register(EV_RELOAD, lambda event: kinds.append(event.kind))
    host.worker_binden(worker)
    worker.run_pending()
    return kinds == [EV_RELOAD]


def test_die_wahl_beim_entfernen_kommt_aus_der_abmelde_markierung_und_gilt_bis_zur_zustellung(tmp_path):
    host = _Host(tmp_path / "a.json")
    assert host.entfernen_wahl() is None  # kein Geraetekern verbunden
    host.verbinden(SimpleNamespace(konfiguration=lambda: EINGERICHTET))
    assert host.entfernen_wahl() is None  # keine Markierung
    dokument_config.abmelden(host.abgemeldet_pfad(), "t1")
    assert host.entfernen_wahl() is None  # einfaches Abmelden wuenscht nichts
    dokument_config.abmelden(host.abgemeldet_pfad(), "t1", freigeben=True)
    assert host.entfernen_wahl() == "freigeben"
    host.entfernen_erledigt()
    assert host.entfernen_wahl() is None and dokument_config.abgemeldet(host.abgemeldet_pfad(), EINGERICHTET)


def test_die_wahl_beim_entfernen_gilt_nur_fuer_die_aktuelle_anlage(tmp_path):
    host = _Host(tmp_path / "a.json")
    host.verbinden(SimpleNamespace(konfiguration=lambda: EINGERICHTET))
    dokument_config.abmelden(host.abgemeldet_pfad(), "andere", freigeben=True)
    assert host.entfernen_wahl() is None
    host.entfernen_erledigt()  # fremde Anlage: nichts zu loeschen
    assert dokument_config.entfernen_gewuenscht(host.abgemeldet_pfad(), {"anlage": "andere"})


def test_leere_konfiguration_nach_keiner_startet_nicht_neu(tmp_path):
    """Im Ruhezustand "nicht eingerichtet" liest die Laufzeit kein Laufzeitfeld; ein Neustart risse den
    Befehl mit, der gleich nach dem Erstkontakt laeuft. Auch leer nach leer mit anderem Laufzeitfeld nicht."""
    assert dokument_config.laufzeit_relevant(None, LEER)  # ohne (c) ginge der Neustart hinaus
    host = _Host(tmp_path / "a.json")
    host.konfiguration_uebernommen(None, LEER)
    host.konfiguration_uebernommen(LEER, {**LEER, "tagestick": "12:00"})
    assert not _angefordert(host)


@pytest.mark.parametrize("alt, neu", [(None, EINGERICHTET), (LEER, EINGERICHTET), (EINGERICHTET, LEER),
                                     (EINGERICHTET, konfig_form.nach_form({**_FLACH, "abo": "inaktiv"}))])
def test_eine_eingerichtete_konfiguration_startet_neu(tmp_path, alt, neu):
    """Gegenprobe zu (c): ist die alte oder die neue Konfiguration eingerichtet, bleibt der Neustart."""
    host = _Host(tmp_path / "a.json")
    host.konfiguration_uebernommen(alt, neu)
    assert _angefordert(host)


def test_leer_nach_leer_hebt_die_abmeldung_nicht_auf_und_startet_nicht_neu(tmp_path):
    """Die Abmelde-Markierung faellt nur durch eine eingerichtete Konfiguration derselben Anlage (dann Neustart, auch
    ohne Laufzeitaenderung); (c) aendert daran nichts."""
    host = _Host(tmp_path / "a.json")
    host.abgemeldet_pfad().write_text('{"anlage": "t1"}')
    host.konfiguration_uebernommen(None, LEER)
    assert not _angefordert(host) and host.abgemeldet_pfad().exists()
    host.konfiguration_uebernommen(EINGERICHTET, dict(EINGERICHTET))  # dieselbe Anlage, nichts Laufzeitrelevantes
    assert _angefordert(host) and not host.abgemeldet_pfad().exists()


# --- Start der Laufzeit: Konfiguration und Ursprungswerte ---

ALT = konfig_form.nach_form({**_FLACH, "ursprungswerte": {"heat_limit": 16.0}})
OFFEN = aufraeumen.Markierung("t1", ALT, False)


def _mit_konfiguration(tmp_path, gespeichert, start_markierung=None) -> _Host:
    """Host, dessen Geraetekern `gespeichert` zeigt; ein spaeteres host.device.gespeichert wirkt wie eine neue
    Konfiguration."""
    host = _Host(tmp_path / "a.json")
    host.verbinden(SimpleNamespace(gespeichert=gespeichert, konfiguration=lambda: host.device.gespeichert))
    host.start_markierung = start_markierung
    return host


def test_laufzeit_konfiguration_ohne_markierung_ist_die_gespeicherte(tmp_path):
    assert _mit_konfiguration(tmp_path, EINGERICHTET).laufzeit_konfiguration() == (EINGERICHTET, False)
    assert _mit_konfiguration(tmp_path, None).laufzeit_konfiguration() == ({}, False)
    assert _mit_konfiguration(tmp_path, LEER).laufzeit_konfiguration() == (LEER, False)


def test_laufzeit_konfiguration_mit_markierung_startet_abgemeldet_mit_der_alten(tmp_path):
    konfiguration, leer_abmelden = _mit_konfiguration(tmp_path, LEER, OFFEN).laufzeit_konfiguration()
    assert (konfiguration, leer_abmelden) == (ALT, True)
    assert konfiguration is not ALT  # eine Kopie: die Markierung bleibt unveraendert


def test_laufzeit_konfiguration_eine_schon_eingerichtete_gespeicherte_geht_vor(tmp_path):
    """Kam vor dem Start der Laufzeit schon eine neue Einrichtung an, laeuft die Laufzeit mit ihr (wie bisher)."""
    neu = konfig_form.nach_form({**_FLACH, "anlage": "t2"})
    assert _mit_konfiguration(tmp_path, neu, OFFEN).laufzeit_konfiguration() == (neu, False)


def test_ursprungswerte_server_markierung_hat_vorrang(tmp_path):
    """Steht das Zuruecksetzen aus, gelten die Werte der Markierung -- auch wenn danach eine neue Einrichtung (andere
    Anlage) eintrifft; ohne ausstehendes Zuruecksetzen gilt die aktuelle eingerichtete, sonst die der Laufzeit."""
    host = _mit_konfiguration(tmp_path, LEER, OFFEN)
    konfiguration, leer_abmelden = host.laufzeit_konfiguration()
    host.device.gespeichert = konfig_form.nach_form({**_FLACH, "anlage": "t2", "ursprungswerte": {"heat_limit": 12.0}})
    assert host.ursprungswerte_server(konfiguration, leer_abmelden) == {"heat_limit": 16.0}
    # ohne Markierung: die frisch eingetroffene Konfiguration
    assert host.ursprungswerte_server(konfiguration, False) == {"heat_limit": 12.0}
    # ohne Markierung und ohne eingerichtete aktuelle Konfiguration: die der Laufzeit
    host.device.gespeichert = LEER
    assert host.ursprungswerte_server(ALT, False) == {"heat_limit": 16.0}


def test_wiederherstellungspunkt_aus_der_sicherung(tmp_path):
    pfad = tmp_path / "backup.json"
    assert wiederherstellungspunkt(pfad) == {}  # keine Sicherung
    dateien.schreiben_json(pfad, {"restore_point": {"heat_limit": 16.0}}, modus=dateien.PRIVAT)
    punkt = wiederherstellungspunkt(pfad)
    assert punkt == {"heat_limit": 16.0}
    punkt["heat_limit"] = 1.0
    assert wiederherstellungspunkt(pfad) == {"heat_limit": 16.0}  # eine Kopie
    dateien.schreiben_json(pfad, {"restore_point": [1]}, modus=dateien.PRIVAT)
    assert wiederherstellungspunkt(pfad) == {}
    dateien.schreiben_json(pfad, [1], modus=dateien.PRIVAT)
    assert wiederherstellungspunkt(pfad) == {}


def test_abmeldung_abwarten_fertig_erst_bei_abgemeldet_dieser_anlage(tmp_path):
    """sign_off wartet auf den Status "abgemeldet" dieser Anlage; werte ist der Wiederherstellungspunkt, zurueckgesetzt
    gilt ohne `grund`. Die Frist ist die uebergebene; sie wird nicht verlaengert."""
    backup = tmp_path / "backup.json"
    dateien.schreiben_json(backup, {"restore_point": {"heat_limit": 16.0}}, modus=dateien.PRIVAT)
    status: dict | None = None
    wartend = abmeldung_abwarten("t1", lambda: status, backup, 123.0)
    assert wartend.deadline == 123.0
    for noch_nicht in (None, {}, {"status": "regelt", "tenant_id": "t1"},
                       {"status": "abgemeldet", "tenant_id": "andere"}, {"status": "abgemeldet"}):
        status = noch_nicht
        assert wartend.check() is None
    status = {"status": "abgemeldet", "tenant_id": "t1"}
    assert wartend.check() == Done({"zurueckgesetzt": True, "werte": {"heat_limit": 16.0}})
    status = {"status": "abgemeldet", "tenant_id": "t1", "grund": "anlage_nicht_erreichbar"}
    assert wartend.check() == Done({"zurueckgesetzt": False, "werte": {"heat_limit": 16.0}})


def test_die_frist_von_sign_off_bleibt_24_stunden():
    assert SIGN_OFF_WAIT_SECONDS == 24 * 3600


MEZ = timezone(timedelta(hours=1))


def _anzeige(zustand: str) -> Anzeige:
    return Anzeige(zustand=zustand, server_ok=True, device_state="uebernommen", eingerichtet=True, hinweise=(),
                   meldungen=(), konfiguration_abgelehnt=None)


class _Kritisch:
    """Host mit verbundenem Geraet unter kritischer Mindestversion: sammelt die Meldungen an den Geraetekern."""

    def __init__(self, tmp_path: Path, kritisch: bool = True) -> None:
        self.kritisch = kritisch
        self.gesendet: list[tuple] = []
        self.uhr = FakeClock()
        self.host = _Host(tmp_path / "a.json")
        self.host._kritisches_ende = KritischesEnde(self.host.update_noetig_kritisch, self.host.entitlement_pfad,
                                                    clock=self.uhr)
        self.host.verbinden(SimpleNamespace(
            konfiguration=lambda: EINGERICHTET, update_noetig_kritisch=lambda: self.kritisch,
            meldung=lambda key, meldung, *, offen: self.gesendet.append((key, meldung, offen))))

    def beginn(self, tag: int = 1) -> None:
        entitlement.mark_inactive(self.host.entitlement_pfad(1), datetime(2026, 1, tag, 8, tzinfo=MEZ), kritisch=True)


def test_unter_kritischer_mindestversion_geht_die_rote_meldung_mit_dem_datum_an_den_geraetekern(tmp_path):
    welt = _Kritisch(tmp_path)
    welt.beginn()
    welt.host.update_noetig_melden(_anzeige("update_noetig"))
    assert welt.gesendet == [("update_noetig", meldungen.neu("meldung.update_noetig", ende="2026-01-31"), True)]
    assert welt.gesendet[0][1].stufe == "rot"


def test_ohne_bekanntes_datum_geht_die_fassung_ohne_datum_hinaus_nie_beide(tmp_path):
    welt = _Kritisch(tmp_path)  # Uhr unklar: kein Beginn gespeichert
    welt.host.update_noetig_melden(_anzeige("update_noetig"))
    assert welt.gesendet == [("update_noetig", meldungen.neu("meldung.update_noetig_unbekannt"), True)]


def test_dieselbe_lage_wird_nicht_in_jedem_takt_neu_gemeldet(tmp_path):
    welt = _Kritisch(tmp_path)
    welt.beginn()
    for _ in range(5):
        welt.host.update_noetig_melden(_anzeige("update_noetig"))
    assert len(welt.gesendet) == 1


def test_das_spaeter_bekannte_datum_ersetzt_die_fassung_ohne_datum(tmp_path):
    welt = _Kritisch(tmp_path)
    welt.host.update_noetig_melden(_anzeige("update_noetig"))
    welt.beginn(10)
    welt.uhr.advance(KritischesEnde.CACHE_SECONDS)  # der Zwischenspeicher der Datei laeuft ab
    welt.host.update_noetig_melden(_anzeige("update_noetig"))
    assert [m[1].code for m in welt.gesendet] == ["meldung.update_noetig_unbekannt", "meldung.update_noetig"]
    assert welt.gesendet[-1][1].params == {"ende": "2026-02-09"} and welt.gesendet[-1][2] is True


def test_nach_dem_update_wird_die_meldung_genau_einmal_zurueckgenommen(tmp_path):
    welt = _Kritisch(tmp_path)
    welt.beginn()
    welt.host.update_noetig_melden(_anzeige("update_noetig"))
    welt.kritisch = False
    welt.host.update_noetig_melden(_anzeige("regelt"))
    welt.host.update_noetig_melden(_anzeige("regelt"))
    assert welt.gesendet[1:] == [("update_noetig", None, False)]
    welt.kritisch = True  # erneut kritisch: wieder offen, mit dem Datum
    welt.host.update_noetig_melden(_anzeige("update_noetig"))
    assert [m[2] for m in welt.gesendet] == [True, False, True]


def test_ohne_den_zustand_wird_nur_beim_ersten_mal_die_ruecknahme_gemeldet(tmp_path):
    welt = _Kritisch(tmp_path, kritisch=False)
    for zustand in ("regelt", "startet", "gesperrt"):
        welt.host.update_noetig_melden(_anzeige(zustand))
    assert welt.gesendet == [("update_noetig", None, False)]


def test_nach_einem_neustart_mit_dem_update_nimmt_die_erste_anzeige_die_offene_meldung_zurueck(tmp_path):
    vorher = _Kritisch(tmp_path)  # der Prozess vor dem Update: die rote Meldung steht beim Server offen
    vorher.beginn()
    vorher.host.update_noetig_melden(_anzeige("update_noetig"))
    assert [m[2] for m in vorher.gesendet] == [True]
    nachher = _Kritisch(tmp_path, kritisch=False)  # neuer Prozess, derselbe Datentraeger: das Update ist eingespielt
    nachher.host.update_noetig_melden(_anzeige("startet"))
    nachher.host.update_noetig_melden(_anzeige("regelt"))
    assert nachher.gesendet == [("update_noetig", None, False)]  # einmal, auch nach dem Neustart ohne eigenen Zustand


def test_nach_einem_neustart_unter_dem_zustand_meldet_die_erste_anzeige_ihn_wieder(tmp_path):
    welt = _Kritisch(tmp_path)
    welt.beginn()
    welt.host.update_noetig_melden(_anzeige("update_noetig"))
    neu = _Kritisch(tmp_path)
    neu.host.update_noetig_melden(_anzeige("update_noetig"))
    assert neu.gesendet == welt.gesendet and neu.gesendet[0][2] is True


def test_ohne_verbundenes_geraet_geht_nichts_hinaus_und_nichts_wirft(tmp_path):
    host = _Host(tmp_path / "a.json")
    host.update_noetig_melden(_anzeige("update_noetig"))
    host.update_noetig_melden(_anzeige("regelt"))
