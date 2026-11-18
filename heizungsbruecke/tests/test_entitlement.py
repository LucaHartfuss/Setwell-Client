import json
import stat
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fakes import runtime_config

from smartheat_runtime import entitlement

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone(timedelta(hours=2)))


def test_mark_inactive_sets_once_and_is_idempotent(tmp_path):
    path = tmp_path / "entitlement_state.json"

    first, newly = entitlement.mark_inactive(path, NOW)
    second, newly_again = entitlement.mark_inactive(path, NOW + timedelta(days=3))

    assert (first, newly) == (NOW, True)
    assert (second, newly_again) == (NOW, False)
    assert json.loads(path.read_text()) == {"inactive_since": NOW.isoformat()}


@pytest.mark.parametrize("content", ["{kaputt", "[]", '{"inactive_since": "gestern"}',
                                     '{"inactive_since": "2026-09-01T12:00:00"}', '{"inactive_since": 5}'])
def test_corrupt_state_is_treated_as_newly_inactive(tmp_path, content):
    path = tmp_path / "entitlement_state.json"
    path.write_text(content)

    assert entitlement.load_inactive_since(path) is None
    since, newly = entitlement.mark_inactive(path, NOW)
    assert (since, newly) == (NOW, True)


def test_load_inactive_since_missing_file(tmp_path):
    assert entitlement.load_inactive_since(tmp_path / "fehlt.json") is None


def test_clear_removes_state_and_tolerates_missing_file(tmp_path):
    path = tmp_path / "entitlement_state.json"
    entitlement.mark_inactive(path, NOW)

    entitlement.clear(path)
    entitlement.clear(path)

    assert not path.exists()


def test_grace_arithmetic():
    assert entitlement.grace_end(NOW) == NOW + timedelta(days=30)
    assert entitlement.grace_expired(NOW, NOW + timedelta(days=30) - timedelta(seconds=1)) is False
    assert entitlement.grace_expired(NOW, NOW + timedelta(days=30)) is True
    # Unterschiedliche Zeitzonen-Offsets (Sommer-/Winterzeit) muessen korrekt vergleichen.
    later_utc = (NOW + timedelta(days=31)).astimezone(UTC)
    assert entitlement.grace_expired(NOW, later_utc) is True



def test_mark_inactive_writes_privately_and_completely(tmp_path):
    path = tmp_path / "entitlement_state.json"
    since, neu = entitlement.mark_inactive(path, datetime(2026, 10, 14, 12, tzinfo=UTC))
    assert neu and entitlement.load_inactive_since(path) == since
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and not path.with_name(path.name + ".tmp").exists()


def test_kritische_mindestversion_laeuft_30_tage_nach_wirksam_ab_ab():
    assert entitlement.kritische_mindestversion_abgelaufen(NOW, NOW + timedelta(days=30) - timedelta(seconds=1)) is False
    assert entitlement.kritische_mindestversion_abgelaufen(NOW, NOW + timedelta(days=30)) is True
    assert entitlement.kritische_mindestversion_abgelaufen(None, NOW + timedelta(days=400)) is False  # nichts kritisch
    # Zeitzonen mit anderem Versatz vergleichen den Augenblick, nicht die Wanduhrzahl.
    assert entitlement.kritische_mindestversion_abgelaufen(NOW, (NOW + timedelta(days=31)).astimezone(UTC)) is True


def test_das_fristende_zaehlt_ueber_den_haken_der_kritischen_mindestversion(monkeypatch):
    gerufen = []
    monkeypatch.setattr(entitlement, "kritische_mindestversion_abgelaufen",
                        lambda beginn, jetzt: gerufen.append((beginn, jetzt)) or True)

    assert entitlement.grace_expired(NOW, NOW) is True
    assert gerufen == [(NOW, NOW)]


@pytest.mark.parametrize("abo", [entitlement.ACTIVE, entitlement.INACTIVE, entitlement.UNKNOWN])
def test_eine_kritische_mindestversion_macht_den_status_inaktiv_egal_was_das_abo_sagt(abo):
    config = runtime_config(abo_source=lambda: abo, update_noetig_source=lambda: True)
    assert entitlement.query(config) == entitlement.INACTIVE


@pytest.mark.parametrize("abo", [entitlement.ACTIVE, entitlement.INACTIVE, entitlement.UNKNOWN])
def test_ohne_kritische_mindestversion_gilt_der_status_des_abos(abo):
    assert entitlement.query(runtime_config(abo_source=lambda: abo, update_noetig_source=lambda: False)) == abo
    assert entitlement.query(runtime_config(abo_source=lambda: abo)) == abo  # ohne Quelle: nicht kritisch


def test_kritisch_allein_heisst_kritisch_bei_einem_abo_das_nicht_inaktiv_ist():
    def allein(kritisch, abo):
        return entitlement.kritisch_allein(runtime_config(abo_source=lambda: abo, update_noetig_source=lambda: kritisch))

    assert allein(True, entitlement.ACTIVE) and allein(True, entitlement.UNKNOWN)
    assert not allein(True, entitlement.INACTIVE)
    assert not allein(False, entitlement.ACTIVE) and not allein(False, entitlement.INACTIVE)


def test_der_kritische_anlass_steht_neben_dem_fristbeginn_und_der_beginn_zaehlt_wie_beim_abo(tmp_path):
    path = tmp_path / "entitlement_state.json"
    since, neu = entitlement.mark_inactive(path, NOW, kritisch=True)

    assert (since, neu) == (NOW, True) and entitlement.ist_kritisch(path)
    assert json.loads(path.read_text()) == {"inactive_since": NOW.isoformat(), "kritisch": True}
    assert entitlement.mark_inactive(path, NOW + timedelta(days=3), kritisch=True) == (NOW, False)  # der Zaehler bleibt
    entitlement.mark_beendet(path)
    assert entitlement.ist_kritisch(path) and entitlement.ist_beendet(path)  # das Ende nimmt den Anlass nicht mit


def test_ein_abo_beginn_traegt_keinen_kritischen_anlass(tmp_path):
    path = tmp_path / "entitlement_state.json"
    entitlement.mark_inactive(path, NOW)
    assert not entitlement.ist_kritisch(path)
    entitlement.mark_inactive(path, NOW, kritisch=True)  # ein vorhandener Beginn bleibt, wie er ist
    assert not entitlement.ist_kritisch(path)


@pytest.mark.parametrize("content", [None, "{kaputt", "[]", "{}", '{"kritisch": false}', '{"kritisch": "ja"}',
                                     '{"kritisch": 1}', '{"inactive_since": "2026-09-25T12:00:00+02:00"}'])
def test_ohne_ausdruecklichen_kritischen_anlass_gilt_die_datei_nicht_als_kritisch(tmp_path, content):
    path = tmp_path / "entitlement_state.json"
    if content is not None:
        path.write_text(content)
    assert not entitlement.ist_kritisch(path)


def test_kritisch_aufheben_loescht_die_datei_nur_mit_kritischem_anlass(tmp_path):
    kritisch, abo = tmp_path / "kritisch.json", tmp_path / "abo.json"
    entitlement.mark_inactive(kritisch, NOW, kritisch=True)
    entitlement.mark_beendet(kritisch)
    entitlement.mark_inactive(abo, NOW)

    entitlement.kritisch_aufheben(kritisch)
    entitlement.kritisch_aufheben(abo)
    entitlement.kritisch_aufheben(tmp_path / "fehlt.json")  # nichts da: kein Fehler

    assert not kritisch.exists()  # Beginn und Ende weg: der Kreis kann wieder regeln
    assert entitlement.load_inactive_since(abo) == NOW  # die Frist des Abos bleibt


def test_das_ende_wird_neben_dem_fristbeginn_gespeichert_und_gelesen(tmp_path):
    path = tmp_path / "entitlement_state.json"
    entitlement.mark_inactive(path, NOW)
    assert not entitlement.ist_beendet(path)

    entitlement.mark_beendet(path)

    assert entitlement.ist_beendet(path)
    assert json.loads(path.read_text()) == {"inactive_since": NOW.isoformat(), "beendet": True}
    assert entitlement.load_inactive_since(path) == NOW
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_das_ende_gilt_auch_ohne_fristbeginn_in_der_datei(tmp_path):
    path = tmp_path / "entitlement_state.json"
    entitlement.mark_beendet(path)
    assert entitlement.ist_beendet(path)


@pytest.mark.parametrize("content", [None, "{kaputt", "[]", "{}", '{"beendet": false}', '{"beendet": "ja"}',
                                     '{"beendet": 1}', '{"inactive_since": "2026-09-25T12:00:00+02:00"}'])
def test_fehlt_das_ende_oder_ist_die_datei_unbrauchbar_gilt_nicht_beendet(tmp_path, content):
    path = tmp_path / "entitlement_state.json"
    if content is not None:
        path.write_text(content)
    assert not entitlement.ist_beendet(path)


def test_mark_beendet_ueber_kaputter_datei_schreibt_nur_das_ende(tmp_path):
    path = tmp_path / "entitlement_state.json"
    path.write_text("{kaputt")
    entitlement.mark_beendet(path)
    assert json.loads(path.read_text()) == {"beendet": True}


def test_clear_nimmt_auch_das_ende_zurueck(tmp_path):
    path = tmp_path / "entitlement_state.json"
    entitlement.mark_beendet(path)
    entitlement.clear(path)
    assert not entitlement.ist_beendet(path)
