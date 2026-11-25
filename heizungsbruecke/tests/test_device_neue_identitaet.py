"""Neue Identitaet nach der Freigabe (Befehl neue_identitaet): Schluessel, Zertifikat, Endpunkt und Status werden
verworfen, das Geraet registriert sich wie ein neues. Die Rastung des Sicherheitssatzes und die Sicherung bleiben
byte-gleich liegen (Regel 4); ein Absturz mitten im Verwerfen setzt beim Start fort; nie Schluessel im Log."""
import logging
from pathlib import Path

import pytest
from device_helpers import World, _konfiguration

from smartheat_core import dateien
from smartheat_device import bootstrap, device, documents, identity, wish

RASTUNG = b'{"sicherheitssatz": "nicht anfassen"}\n'
SICHERUNG = b'{"backup": 1}\n'
VERWORFEN = (identity.KEY_FILE, identity.TLS_KEY_FILE, bootstrap.CERTIFICATE_FILE, bootstrap.ENDPOINT_FILE,
             bootstrap.STATE_FILE)


def _befehl(command_id="n1"):
    return {"schema": 2, "command_id": command_id, "kind": "neue_identitaet", "payload": {},
            "expires_at": "2027-01-15T09:00:00+00:00"}


def _welt(tmp_path) -> World:
    world = World(tmp_path)
    world.verbinden()
    world.link.sent.clear()
    (world.dir / "sicherheitssatz.json").write_bytes(RASTUNG)
    (world.dir / "backup.json").write_bytes(SICHERUNG)
    return world


def _gerat_bereit(tmp_path) -> Path:
    """Ein Geraeteordner mit allen Dateien, die das Verwerfen betrifft, und den beiden, die bleiben muessen."""
    ordner = tmp_path / device.DEVICE_DIR
    identity.load_or_create(ordner)
    for name in VERWORFEN[2:]:
        (ordner / name).write_text("x")
    (ordner / "sicherheitssatz.json").write_bytes(RASTUNG)
    (ordner / "backup.json").write_bytes(SICHERUNG)
    return ordner


def test_verwerfen_loescht_nur_die_identitaet_und_laesst_rastung_und_sicherung_byte_gleich(tmp_path):
    ordner = _gerat_bereit(tmp_path)

    identity.verwerfen(ordner)

    for name in VERWORFEN:
        assert not (ordner / name).exists(), name
    assert (ordner / "sicherheitssatz.json").read_bytes() == RASTUNG
    assert (ordner / "backup.json").read_bytes() == SICHERUNG
    assert not (ordner / identity.MARKER_FILE).exists()


def test_verwerfen_raeumt_auch_die_dateien_eines_halben_zertifikatswechsels_weg(tmp_path):
    ordner = _gerat_bereit(tmp_path)
    for name in (identity.TLS_KEY_FILE, bootstrap.CERTIFICATE_FILE):
        (ordner / (name + bootstrap.NEU_SUFFIX)).write_text("x")

    identity.verwerfen(ordner)

    assert sorted(p.name for p in ordner.iterdir()) == ["backup.json", "sicherheitssatz.json"]


def test_der_marker_liegt_vor_dem_ersten_loeschen(tmp_path, monkeypatch):
    ordner = _gerat_bereit(tmp_path)
    gesehen = []
    echt = Path.unlink

    def unlink(self, *args, **kwargs):
        gesehen.append((self.name, (ordner / identity.MARKER_FILE).exists()))
        return echt(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    identity.verwerfen(ordner)

    loeschungen = [(name, marker) for name, marker in gesehen if name != identity.MARKER_FILE]
    assert loeschungen, "es wurde nichts geloescht"
    assert all(marker for _name, marker in loeschungen)
    assert gesehen[-1][0] == identity.MARKER_FILE  # der Marker geht als Letztes


def test_ein_absturz_nach_dem_marker_setzt_beim_naechsten_start_fort(tmp_path, monkeypatch):
    ordner = _gerat_bereit(tmp_path)
    echt = Path.unlink

    def abbruch(self, *args, **kwargs):
        if self.name == bootstrap.CERTIFICATE_FILE:
            raise OSError("Absturz")
        return echt(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", abbruch)
    with pytest.raises(OSError):
        identity.verwerfen(ordner)
    monkeypatch.undo()
    assert (ordner / identity.MARKER_FILE).exists() and (ordner / bootstrap.CERTIFICATE_FILE).exists()

    assert identity.fortsetzen(ordner) is True

    for name in VERWORFEN:
        assert not (ordner / name).exists(), name
    assert not (ordner / identity.MARKER_FILE).exists()
    assert (ordner / "sicherheitssatz.json").read_bytes() == RASTUNG
    assert (ordner / "backup.json").read_bytes() == SICHERUNG
    assert identity.fortsetzen(ordner) is False


def test_fortsetzen_nach_einem_absturz_raeumt_auch_dokumente_und_wunsch_puffer_weg(tmp_path):
    ordner = _gerat_bereit(tmp_path)
    for name in (*documents.DATEIEN.values(), documents.ZUSTAND_DATEI, wish.wunsch_datei(1), wish.wunsch_datei(2)):
        (ordner / name).write_text("{}")
    dateien.schreiben_json(ordner / identity.MARKER_FILE, {}, modus=dateien.PRIVAT)

    assert identity.fortsetzen(ordner) is True

    assert sorted(p.name for p in ordner.iterdir()) == ["backup.json", "sicherheitssatz.json"]


def test_der_start_mit_marker_setzt_fort_und_legt_eine_neue_identitaet_an(tmp_path):
    ordner = _gerat_bereit(tmp_path)
    alte_id = identity.load_or_create(ordner).device_id
    dateien.schreiben_json(ordner / identity.MARKER_FILE, {"verwerfen": True}, modus=dateien.PRIVAT)

    world = World(tmp_path, access=False)

    assert world.device.identity.device_id != alte_id
    assert not (world.dir / identity.MARKER_FILE).exists()
    assert (world.dir / "sicherheitssatz.json").read_bytes() == RASTUNG


def test_der_befehl_verwirft_meldet_ok_und_das_geraet_registriert_sich_neu(tmp_path):
    world = _welt(tmp_path)
    alte_id = world.device.identity.device_id
    alter_link = world.link
    world.client.script["register"] = [
        lambda ident: bootstrap.Antwort("nicht_uebernommen", world.neuer_zugang(ident))]

    world.empfangen("down/command", _befehl())

    ergebnis = alter_link.of("up/result")[-1]
    assert ergebnis["ok"] is True and ergebnis["result"] == {}
    assert (world.dir / "sicherheitssatz.json").read_bytes() == RASTUNG
    assert (world.dir / "backup.json").read_bytes() == SICHERUNG

    world.device.schritt()

    assert alter_link.stopped
    assert world.device.identity.device_id != alte_id
    assert [aufruf for aufruf, _ in world.client.calls] == ["register"]
    assert world.client.idents[-1].device_id == world.device.identity.device_id
    assert world.link is not alter_link and world.link.started
    assert bootstrap.device_state(world.dir) == "nicht_uebernommen"
    assert (world.dir / "sicherheitssatz.json").read_bytes() == RASTUNG


def test_die_neue_identitaet_verwirft_auch_die_dokumente_und_den_wunsch_puffer(tmp_path):
    world = World(tmp_path)
    world.verbinden()
    world.empfangen("down/config", _konfiguration(version=7))  # rastet den Sicherheitssatz selbst ein
    world.link.sent.clear()
    world.device.wunsch(22.0, herkunft="thermostat")
    world.device.schritt()
    rastung = (world.dir / "sicherheitssatz.json").read_bytes()
    (world.dir / "backup.json").write_bytes(SICHERUNG)
    assert world.device.documents.eingerichtet() and world.device.wunsch_puffer.kreise() == [1]
    assert (world.dir / documents.ZUSTAND_DATEI).exists() and (world.dir / wish.wunsch_datei(1)).exists()
    world.client.script["register"] = [
        lambda ident: bootstrap.Antwort("nicht_uebernommen", world.neuer_zugang(ident))]

    world.empfangen("down/command", _befehl())  # ohne vorheriges Abmelden
    world.device.schritt()

    assert [p.name for p in world.dir.glob(wish.WUNSCH_MUSTER)] == []
    for name in (*documents.DATEIEN.values(), documents.ZUSTAND_DATEI):
        assert not (world.dir / name).exists(), name
    assert not world.device.documents.eingerichtet()
    assert world.device.documents.version("konfiguration") == 0
    assert world.device.wunsch_puffer.kreise() == []
    assert (world.dir / "sicherheitssatz.json").read_bytes() == rastung
    assert (world.dir / "backup.json").read_bytes() == SICHERUNG


def test_scheitert_das_verwerfen_endet_der_befehl_mit_intern_und_die_identitaet_bleibt(tmp_path, monkeypatch):
    world = _welt(tmp_path)
    alte_id = world.device.identity.device_id

    def kaputt(_ordner):
        raise OSError("Platte voll")

    monkeypatch.setattr(identity, "verwerfen", kaputt)
    world.empfangen("down/command", _befehl())

    ergebnis = world.link.of("up/result")[-1]
    assert ergebnis["ok"] is False and ergebnis["error"] == {"grund": "intern"}
    world.device.schritt()
    assert world.device.identity.device_id == alte_id and not world.link.stopped


def test_weder_schluessel_noch_pem_stehen_im_log(tmp_path, caplog):
    world = _welt(tmp_path)
    geheim = [(world.dir / name).read_text() for name in (identity.KEY_FILE, identity.TLS_KEY_FILE)]
    world.client.script["register"] = [
        lambda ident: bootstrap.Antwort("nicht_uebernommen", world.neuer_zugang(ident))]

    with caplog.at_level(logging.DEBUG):
        world.empfangen("down/command", _befehl())
        world.device.schritt()

    text = caplog.text
    assert "PRIVATE KEY" not in text
    for pem in geheim:
        assert pem.splitlines()[1] not in text


def test_die_dateinamen_des_verwerfens_sind_die_des_bootstraps():
    assert (identity.CERTIFICATE_FILE, identity.ENDPOINT_FILE, identity.STATE_FILE, identity.NEU_SUFFIX) == (
        bootstrap.CERTIFICATE_FILE, bootstrap.ENDPOINT_FILE, bootstrap.STATE_FILE, bootstrap.NEU_SUFFIX)
