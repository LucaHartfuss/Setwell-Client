"""Einmalige Migration des Meldungszustands im Wartungsfenster: aus der `backup.json` jedes Kreises fallen die Felder
`notify_states` und `notify_messages` (gemerkte Meldungstexte); die Sicherung `backup.json.pre-<Release>` ist der
Rueckweg. Alles andere im Zustand bleibt Byte fuer Byte inhaltsgleich."""
import json
import os
import stat

import pytest

from smartheat_core import dateien, zustandspfade
from smartheat_device import meldungszustand_migration as migration
from smartheat_runtime.state import StateStore

RELEASE = "1.3.0"
ALT_1 = {
    "tenant_id": "kunde3", "plant_id": "p", "lifetime_writes": 5,
    "write_budget": {"writes:total": {"day": "2026-10-30", "count": 3}},
    "notify_states": {"notbetrieb": "aktiv", "batterie:sensor.bad": "niedrig"},
    "notify_messages": {"notbetrieb": "Heizungsbrücke: Server antwortet nicht, Notbetrieb aktiv."},
}
ALT_2 = {"tenant_id": "kunde3", "plant_id": "p", "notify_states": {"abo": "inaktiv"}}


@pytest.fixture
def anlage(tmp_path):
    """Ein Geraet mit zwei Kreisordnern im Stand vor der Umstellung."""
    (tmp_path / "device").mkdir()
    for nummer, inhalt in ((1, ALT_1), (2, ALT_2)):
        ordner = zustandspfade.kreis_dir(tmp_path / "runtime", nummer)
        ordner.mkdir(parents=True)
        (ordner / zustandspfade.BACKUP).write_text(json.dumps(inhalt))
        (ordner / zustandspfade.ROOM_TARGET).write_text(json.dumps({"wert": 21.5}))
    return tmp_path


def _lauf(daten, release=RELEASE):
    return migration.migrieren(daten / "device", release=release)


def _backup(daten, nummer):
    return daten / "runtime" / f"kreis-{nummer}" / zustandspfade.BACKUP


def _stand(daten):
    """Alle Dateien unter dem Datenordner mit Inhalt: der Vergleich vorher/nachher."""
    return {str(pfad.relative_to(daten)): pfad.read_bytes() for pfad in sorted(daten.rglob("*")) if pfad.is_file()}


# --- Erfolg -------------------------------------------------------------------------------------------------------

def test_die_meldungsfelder_fallen_weg_und_alles_andere_bleibt(anlage):
    assert _lauf(anlage) == migration.MIGRIERT

    ohne = {key: wert for key, wert in ALT_1.items() if not key.startswith("notify_")}
    assert json.loads(_backup(anlage, 1).read_text()) == ohne
    assert json.loads(_backup(anlage, 2).read_text()) == {"tenant_id": "kunde3", "plant_id": "p"}
    assert json.loads((_backup(anlage, 1).parent / zustandspfade.ROOM_TARGET).read_text()) == {"wert": 21.5}


def test_die_sicherung_ist_der_alte_stand_und_privat(anlage):
    vorher = {nummer: _backup(anlage, nummer).read_bytes() for nummer in (1, 2)}

    _lauf(anlage)

    for nummer in (1, 2):
        sicherung = _backup(anlage, nummer).with_name(f"backup.json.pre-{RELEASE}")
        assert sicherung.read_bytes() == vorher[nummer]
        assert stat.S_IMODE(sicherung.stat().st_mode) == 0o600
        assert stat.S_IMODE(_backup(anlage, nummer).stat().st_mode) == 0o600


def test_der_neue_stand_hat_keine_gemerkten_meldungen(anlage):
    _lauf(anlage)

    store = StateStore(_backup(anlage, 1), _backup(anlage, 1).with_name("failsafe_state.json"))

    assert store.state.notify_states == {} and store.state.notify_meldungen == {}
    assert store.state.lifetime_writes == 5


def test_nur_ein_kreis_mit_den_feldern_wird_angefasst(anlage):
    _backup(anlage, 2).write_text(json.dumps({"tenant_id": "kunde3"}))
    vorher = _backup(anlage, 2).read_bytes()

    assert _lauf(anlage) == migration.MIGRIERT

    assert _backup(anlage, 2).read_bytes() == vorher
    assert not _backup(anlage, 2).with_name(f"backup.json.pre-{RELEASE}").exists()


def test_ein_zweiter_lauf_aendert_nichts(anlage):
    _lauf(anlage)
    danach = _stand(anlage)

    assert _lauf(anlage, "1.3.1") == migration.BEREITS_AKTUELL
    assert _stand(anlage) == danach


def test_ohne_kreisordner_gibt_es_nichts_zu_tun(tmp_path):
    (tmp_path / "device").mkdir()

    assert _lauf(tmp_path) == migration.BEREITS_AKTUELL
    assert not (tmp_path / "runtime").exists()


def test_der_rueckweg_spielt_die_sicherung_zurueck(anlage):
    vorher = _stand(anlage)
    _lauf(anlage)

    for nummer in (1, 2):
        os.replace(_backup(anlage, nummer).with_name(f"backup.json.pre-{RELEASE}"), _backup(anlage, nummer))

    assert _stand(anlage) == vorher


# --- Verweigern: nichts wird veraendert -----------------------------------------------------------------------------

@pytest.mark.parametrize("release", ["", "1 3", "../x", "x" * 33])
def test_eine_ungueltige_release_aendert_nichts(anlage, release):
    vorher = _stand(anlage)

    assert _lauf(anlage, release) == migration.RELEASE_UNGUELTIG
    assert _stand(anlage) == vorher


def test_ohne_geraeteordner_bricht_der_lauf_ab(tmp_path):
    assert migration.migrieren(tmp_path / "fehlt", release=RELEASE) == migration.ORDNER_FEHLT


@pytest.mark.parametrize("inhalt", ["kein json", "[1, 2]", '"text"'])
def test_eine_unbrauchbare_datei_bricht_vor_jeder_aenderung_ab(anlage, inhalt):
    _backup(anlage, 2).write_text(inhalt)
    vorher = _stand(anlage)

    assert _lauf(anlage) == migration.DATEI_UNGUELTIG
    assert _stand(anlage) == vorher


def test_ein_symlink_statt_der_datei_bricht_vor_jeder_aenderung_ab(anlage, tmp_path):
    ziel = tmp_path / "anderswo.json"
    ziel.write_text(json.dumps(ALT_2))
    _backup(anlage, 2).unlink()
    os.symlink(ziel, _backup(anlage, 2))
    vorher = _stand(anlage)

    assert _lauf(anlage) == migration.DATEI_UNGUELTIG
    assert _stand(anlage) == vorher
    assert _backup(anlage, 2).is_symlink()


def test_eine_vorhandene_sicherung_wird_nie_ueberschrieben(anlage):
    _backup(anlage, 2).with_name(f"backup.json.pre-{RELEASE}").write_text("alt")
    vorher = _stand(anlage)

    assert _lauf(anlage) == migration.SICHERUNG_VORHANDEN
    assert _stand(anlage) == vorher


def test_scheitert_das_schreiben_wird_alles_zurueckgebaut(anlage, monkeypatch):
    vorher = _stand(anlage)
    echt = dateien.schreiben
    aufrufe = []

    def schreiben(pfad, daten, *, modus):
        aufrufe.append(pfad.name)
        if len(aufrufe) == 4:  # die zweite neue Datei: beide Sicherungen und die erste neue stehen schon
            raise OSError("Datentraeger voll")
        echt(pfad, daten, modus=modus)

    monkeypatch.setattr(dateien, "schreiben", schreiben)

    assert _lauf(anlage) == migration.SCHREIBEN_FEHLGESCHLAGEN
    assert _stand(anlage) == vorher


# --- Aufruf -------------------------------------------------------------------------------------------------------

def test_der_aufruf_gibt_den_code_aus_und_endet_nur_bei_erfolg_mit_0(anlage, capsys):
    argumente = ["--device-dir", str(anlage / "device"), "--release", RELEASE]

    assert migration.main(argumente) == 0
    assert capsys.readouterr().out.strip() == migration.MIGRIERT
    assert migration.main(argumente) == 0
    assert capsys.readouterr().out.strip() == migration.BEREITS_AKTUELL
    assert migration.main([*argumente[:2], "--release", "a b"]) == 1
    assert capsys.readouterr().out.strip() == migration.RELEASE_UNGUELTIG


def test_der_aufruf_verlangt_beide_angaben(anlage, capsys):
    with pytest.raises(SystemExit):
        migration.main(["--device-dir", str(anlage / "device")])
    capsys.readouterr()


def test_die_hilfe_nennt_den_zweck(capsys):
    with pytest.raises(SystemExit):
        migration.main(["--help"])
    assert "Einmalige Migration des Meldungszustands" in capsys.readouterr().out
