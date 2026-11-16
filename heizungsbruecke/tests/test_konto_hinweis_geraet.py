"""Hinweis auf das uebernehmende Konto am Geraet: claim_state traegt ihn, bootstrap.json haelt ihn neben dem
Uebernahmestand, jede Freigabe loescht ihn, ein alter Stand ohne Feld bleibt lesbar, Form und Laenge sind begrenzt."""
import json

import pytest
from device_helpers import World

from smartheat_device import bootstrap, wire

MASKE = "a***@e***.com"


def _command(payload, command_id="c1"):
    return {"schema": 2, "command_id": command_id, "kind": "claim_state", "payload": payload,
            "expires_at": "2027-01-15T09:00:00+00:00"}


def _welt(tmp_path) -> World:
    world = World(tmp_path, access=False)
    world.client.script["register"] = [bootstrap.Antwort("nicht_uebernommen", world.zugang),
                                       bootstrap.Antwort("nicht_uebernommen", world.zugang)]
    world.verbinden()
    return world


def test_claim_state_speichert_den_hinweis_neben_dem_stand(tmp_path):
    world = _welt(tmp_path)
    world.empfangen("down/command", _command({"device_state": "uebernommen", "konto_hinweis": MASKE}))
    assert world.link.of("up/result")[-1]["ok"] is True
    assert bootstrap.device_state(world.dir) == "uebernommen"
    assert bootstrap.konto_hinweis(world.dir) == MASKE
    assert world.device.anzeige().konto_hinweis == MASKE
    assert json.loads((world.dir / bootstrap.STATE_FILE).read_text()) == {
        "device_state": "uebernommen", "konto_hinweis": MASKE}


def test_claim_state_ohne_hinweis_loescht_einen_alten(tmp_path):
    world = _welt(tmp_path)
    world.empfangen("down/command", _command({"device_state": "uebernommen", "konto_hinweis": MASKE}))
    world.empfangen("down/command", _command({"device_state": "uebernommen"}, "c2"))
    assert bootstrap.konto_hinweis(world.dir) is None
    assert world.device.anzeige().konto_hinweis is None


def test_ein_gleicher_hinweis_schreibt_nicht_neu(tmp_path):
    world = _welt(tmp_path)
    world.empfangen("down/command", _command({"device_state": "uebernommen", "konto_hinweis": MASKE}))
    stand = (world.dir / bootstrap.STATE_FILE).stat().st_mtime_ns
    world.empfangen("down/command", _command({"device_state": "uebernommen", "konto_hinweis": MASKE}, "c2"))
    assert (world.dir / bootstrap.STATE_FILE).stat().st_mtime_ns == stand


def test_die_freigabe_loescht_den_hinweis(tmp_path):
    world = _welt(tmp_path)
    world.empfangen("down/command", _command({"device_state": "uebernommen", "konto_hinweis": MASKE}))
    world.empfangen("down/command", {"schema": 2, "command_id": "c2", "kind": "new_claim_code", "payload": {},
                                     "expires_at": "2027-01-15T09:00:00+00:00"})
    assert bootstrap.device_state(world.dir) == "nicht_uebernommen"
    assert bootstrap.konto_hinweis(world.dir) is None
    assert "konto_hinweis" not in json.loads((world.dir / bootstrap.STATE_FILE).read_text())


def test_nicht_uebernommen_haelt_nie_einen_hinweis(tmp_path):
    bootstrap.device_state_speichern(tmp_path, "uebernommen")
    bootstrap.konto_hinweis_speichern(tmp_path, MASKE)
    bootstrap.device_state_speichern(tmp_path, "nicht_uebernommen")
    assert bootstrap.konto_hinweis(tmp_path) is None
    bootstrap.konto_hinweis_speichern(tmp_path, MASKE)  # ohne Uebernahme gibt es keinen Hinweis
    assert bootstrap.konto_hinweis(tmp_path) is None


def test_eine_register_antwort_uebernommen_behaelt_den_hinweis(tmp_path):
    bootstrap.device_state_speichern(tmp_path, "uebernommen")
    bootstrap.konto_hinweis_speichern(tmp_path, MASKE)
    bootstrap.device_state_speichern(tmp_path, "uebernommen")
    assert bootstrap.konto_hinweis(tmp_path) == MASKE


def test_ein_alter_stand_ohne_feld_bleibt_lesbar(tmp_path):
    (tmp_path / bootstrap.STATE_FILE).write_text(json.dumps({"device_state": "uebernommen"}))
    assert bootstrap.device_state(tmp_path) == "uebernommen"
    assert bootstrap.konto_hinweis(tmp_path) is None


@pytest.mark.parametrize("roh", [MASKE + "x" * wire.KONTO_HINWEIS_MAX_ZEICHEN, 7, ["a"], None, "", "anna@example.com",
                                 "a***@e***.c om", "***@e***.com", "a***@***"])
def test_ein_fremder_hinweis_auf_der_platte_wird_nicht_gezeigt(tmp_path, roh):
    (tmp_path / bootstrap.STATE_FILE).write_text(json.dumps({"device_state": "uebernommen", "konto_hinweis": roh}))
    assert bootstrap.konto_hinweis(tmp_path) is None


@pytest.mark.parametrize("roh, regel", [
    ("anna@example.com", "format"), ("a***@e***.com\n", "format"), ("a***@<b>***.com", "format"), (7, "ungueltig"),
    ("a***@e***." + "x" * wire.KONTO_HINWEIS_MAX_ZEICHEN, "bereich")])
def test_ein_zu_langer_oder_fremdfoermiger_hinweis_wird_abgelehnt(tmp_path, roh, regel):
    world = _welt(tmp_path)
    world.empfangen("down/command", _command({"device_state": "uebernommen", "konto_hinweis": roh}))
    ergebnis = world.link.of("up/result")[-1]
    assert ergebnis["ok"] is False and ergebnis["error"]["grund"] == "ungueltige_nutzlast"
    assert ergebnis["error"]["details"]["feld"] == "konto_hinweis" and ergebnis["error"]["details"]["regel"] == regel
    assert bootstrap.device_state(world.dir) == "nicht_uebernommen"  # nichts gespeichert
    assert bootstrap.konto_hinweis(world.dir) is None


def test_die_domain_ohne_endung_ist_eine_gueltige_maske(tmp_path):
    world = _welt(tmp_path)
    world.empfangen("down/command", _command({"device_state": "uebernommen", "konto_hinweis": "a***@l***"}))
    assert bootstrap.konto_hinweis(world.dir) == "a***@l***"
