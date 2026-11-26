import logging
import os
import stat

import pytest

import werk_erzeugen
from smartheat_gateway import anmeldung
from smartheat_host import werk

PASSWORT = "Testpasswort-12345"  # erfunden, nur fuer diese Tests
ARGS = ["--seriennummer", "P-0001", "--hostname", "setwell-p-0001", "--bespielt", "2026-11-24"]


def _eingabe(*antworten):
    folge = iter(antworten)
    return lambda _frage: next(folge)


def _lauf(ziel, *extra, eingabe=None, args=ARGS):
    return werk_erzeugen.main([*args, *extra, str(ziel)], passwort_eingabe=eingabe or _eingabe(PASSWORT, PASSWORT))


def test_datei_besteht_den_leser_und_das_passwort_passt(tmp_path):
    ziel = tmp_path / "werk.json"
    assert _lauf(ziel) == 0
    daten = werk.lesen(ziel)
    assert daten is not None
    assert daten["seriennummer"] == "P-0001"
    assert daten["hostname"] == "setwell-p-0001"
    assert daten["bespielt"] == "2026-11-24"
    assert daten["hardware"] == "Raspberry Pi 5"
    assert anmeldung.pruefe(daten["passwort"], PASSWORT)
    assert not anmeldung.pruefe(daten["passwort"], PASSWORT + "x")


def test_datei_hat_modus_0644_und_kein_temp_rest(tmp_path):
    ziel = tmp_path / "werk.json"
    assert _lauf(ziel) == 0
    assert stat.S_IMODE(ziel.stat().st_mode) == 0o644
    assert [p.name for p in tmp_path.iterdir()] == ["werk.json"]


def test_salz_ist_je_aufruf_neu(tmp_path):
    erste, zweite = tmp_path / "a.json", tmp_path / "b.json"
    assert _lauf(erste) == 0
    assert _lauf(zweite) == 0
    assert werk.lesen(erste)["passwort"]["salz"] != werk.lesen(zweite)["passwort"]["salz"]


def test_bespielt_standard_ist_heute(tmp_path):
    ziel = tmp_path / "werk.json"
    args = ["--seriennummer", "P-0001", "--hostname", "setwell-p-0001"]
    assert _lauf(ziel, args=args) == 0
    assert werk.lesen(ziel)["bespielt"] == werk_erzeugen.date.today().isoformat()


@pytest.mark.parametrize("feld, wert", [
    ("--seriennummer", "p-0001"),
    ("--seriennummer", "AB"),
    ("--hostname", "Setwell_P"),
    ("--hostname", "x" * 64),
    ("--bespielt", "2026-13-40"),
    ("--hardware", "x" * 33),
])
def test_ungueltige_werte_ohne_datei(tmp_path, capsys, feld, wert):
    ziel = tmp_path / "werk.json"
    args = [*ARGS, feld, wert]  # argparse nimmt den letzten Wert
    assert _lauf(ziel, args=args) == 2
    assert list(tmp_path.iterdir()) == []
    assert PASSWORT not in capsys.readouterr().err


def test_ungleiche_passwoerter_ohne_datei(tmp_path):
    ziel = tmp_path / "werk.json"
    assert _lauf(ziel, eingabe=_eingabe(PASSWORT, PASSWORT + "x")) == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("passwort", ["", "kurz", "elf-zeichen"])
def test_zu_kurzes_passwort_ohne_datei(tmp_path, passwort):
    ziel = tmp_path / "werk.json"
    assert _lauf(ziel, eingabe=_eingabe(passwort, passwort)) == 2
    assert list(tmp_path.iterdir()) == []


def test_zu_langes_passwort_ohne_datei(tmp_path):
    ziel = tmp_path / "werk.json"
    lang = "a" * 129
    assert _lauf(ziel, eingabe=_eingabe(lang, lang)) == 2
    assert list(tmp_path.iterdir()) == []


def test_passwort_nicht_in_ausgabe_und_log(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    assert _lauf(tmp_path / "werk.json") == 0
    ausgabe = capsys.readouterr()
    assert PASSWORT not in ausgabe.out + ausgabe.err + caplog.text
    daten = (tmp_path / "werk.json").read_text()
    assert PASSWORT not in daten


def test_passwort_nicht_als_argument(tmp_path, capsys):
    with pytest.raises(SystemExit) as fehler:
        werk_erzeugen.main([*ARGS, "--passwort", PASSWORT, str(tmp_path / "werk.json")],
                           passwort_eingabe=_eingabe(PASSWORT, PASSWORT))
    assert fehler.value.code == 2
    assert PASSWORT not in capsys.readouterr().err


def test_symlink_am_ziel_wird_ersetzt_nicht_befolgt(tmp_path):
    fremd = tmp_path / "fremd.txt"
    fremd.write_text("unveraendert")
    ziel = tmp_path / "werk.json"
    os.symlink(fremd, ziel)
    assert _lauf(ziel) == 0
    assert fremd.read_text() == "unveraendert"
    assert not ziel.is_symlink()
    assert werk.lesen(ziel) is not None


def test_leser_lehnt_das_ergebnis_ab_keine_datei(tmp_path, monkeypatch):
    monkeypatch.setattr(werk_erzeugen.werk, "lesen", lambda pfad: None)
    ziel = tmp_path / "werk.json"
    assert _lauf(ziel) == 1
    assert list(tmp_path.iterdir()) == []


def test_ordner_fehlt_nicht_anlegen(tmp_path):
    ziel = tmp_path / "gibt-es-nicht" / "werk.json"
    assert _lauf(ziel) == 2
    assert not (tmp_path / "gibt-es-nicht").exists()
