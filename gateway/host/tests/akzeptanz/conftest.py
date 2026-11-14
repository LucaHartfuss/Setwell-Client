"""Sicherung der Akzeptanzsuite des Gateway-Hosts. Die Suite laeuft nur allein (pytest --confcutdir=tests/akzeptanz
tests/akzeptanz): ohne das lade pytest die Fixtures und Hilfen der uebrigen Tests mit, und die Suite waere nicht mehr
unabhaengig von ihnen. Maschinell geprueft werden: jede ID aus IDS hat genau einen Test, kein Test ohne Marker oder mit
unbekannter ID, kein skip und kein xfail (auch nicht zur Laufzeit), jeder Test laeuft und besteht (kein -k, --deselect,
-m, --co: ein unvollstaendiger Lauf endet mit Exit 1), keine Importe ausser Standardbibliothek, pytest, cryptography und
dem Produkt, und die Attrappen der Welt gleichen in ihrer Form den echten Rueckrufen (subprocess.run, werksreset.warten)."""
import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

ORDNER = Path(__file__).resolve().parent
#: alle Szenarien dieser Suite; jede ID hat genau einen Test mit @pytest.mark.akzeptanz("<ID>")
IDS = ("AK-G4-04", "AK-G9-03", "AK-G9-04", "AK-G10-03")
#: Tests der Suite (Node-ID) und die, die bestanden haben; die Deckung der Laeufe prueft pytest_sessionfinish
GESAMMELT: set[str] = set()
BESTANDEN: set[str] = set()
ERLAUBT = {"pytest", "cryptography"}
UEBERSPRUNGEN = ("skip", "skipif", "xfail")


def pytest_configure(config):
    config.addinivalue_line("markers", "akzeptanz(id): Szenario der Akzeptanzsuite (genau ein Test je ID)")
    cut = config.getoption("confcutdir")
    if cut is None or Path(str(cut)).resolve() != ORDNER:
        pytest.exit("Akzeptanzsuite: nur mit --confcutdir=tests/akzeptanz starten (die Hilfen der uebrigen Tests "
                    "duerfen nicht geladen werden)", returncode=1)
    fehler = _importfehler() + _signaturfehler()
    if fehler:
        pytest.exit("Akzeptanzsuite: " + "; ".join(fehler), returncode=1)


def _importfehler() -> list[str]:
    eigene = {datei.stem for datei in ORDNER.glob("*.py")}
    fehler = []
    for datei in sorted(ORDNER.glob("*.py")):
        for knoten in ast.walk(ast.parse(datei.read_text(encoding="utf-8"))):
            if isinstance(knoten, ast.Import):
                module = [alias.name for alias in knoten.names]
            elif isinstance(knoten, ast.ImportFrom):
                module = [("." * knoten.level) + (knoten.module or "")]
            else:
                continue
            for name in module:
                wurzel = name.split(".")[0]
                if not (wurzel in sys.stdlib_module_names or wurzel in ERLAUBT or wurzel in eigene
                        or wurzel.startswith("smartheat_")):
                    fehler.append(f"{datei.name}: Import {name!r} nicht erlaubt")
    return fehler


def _signaturfehler() -> list[str]:
    """Die Attrappen nehmen nur Parameter, die das Gegenstueck kennt (subprocess.run reicht den Rest an Popen)."""
    from welt import Docker, Welt
    from welt_release import AufzeichnendeStartauswahl

    from smartheat_host import startauswahl, werksreset

    erlaubt = set(inspect.signature(subprocess.run).parameters) | set(inspect.signature(subprocess.Popen).parameters)
    fehler = []
    for name in list(inspect.signature(Docker.__call__).parameters)[2:]:
        if name not in erlaubt:
            fehler.append(f"Docker.__call__: Parameter {name!r} kennt subprocess.run nicht")
    echt = list(inspect.signature(werksreset.warten).parameters)[:2]
    attrappe = list(inspect.signature(Welt.warte).parameters)[1:]
    if attrappe != echt:
        fehler.append(f"warte{tuple(attrappe)} weicht von werksreset.warten{tuple(echt)} ab")
    protokoll = {n: list(inspect.signature(f).parameters)[1:] for n, f in vars(startauswahl.Startauswahl).items()
                 if callable(f) and not n.startswith("_")}
    attrappe_methoden = {n: list(inspect.signature(f).parameters)[1:] for n, f in vars(AufzeichnendeStartauswahl).items()
                         if callable(f) and not n.startswith("_")}
    if attrappe_methoden != protokoll:
        fehler.append(f"Startauswahl-Attrappe {attrappe_methoden} weicht vom Protokoll {protokoll} ab")
    return fehler


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Deckung ueber alle gesammelten Tests, noch vor jeder Auswahl mit -k."""
    fehler, funktionen = [], {}
    for item in items:
        marker = item.get_closest_marker("akzeptanz")
        if marker is None or len(marker.args) != 1 or not isinstance(marker.args[0], str):
            fehler.append(f"{item.nodeid}: ohne Marker akzeptanz(\"AK-...\")")
            continue
        if any(item.get_closest_marker(name) for name in UEBERSPRUNGEN):
            fehler.append(f"{item.nodeid}: skip/xfail ist nicht erlaubt")
        GESAMMELT.add(item.nodeid)
        funktionen.setdefault(marker.args[0], set()).add(item.nodeid.partition("[")[0])
    for ident, namen in funktionen.items():
        if ident not in IDS:
            fehler.append(f"unbekannte ID {ident}")
        if len(namen) > 1:
            fehler.append(f"ID {ident} doppelt: {sorted(namen)}")
    fehler += [f"ID {ident} ohne Test" for ident in IDS if ident not in funktionen]
    if fehler:
        pytest.exit("Akzeptanzsuite: " + "; ".join(fehler), returncode=1)


def pytest_runtest_logreport(report):
    if report.when == "call" and report.passed:
        BESTANDEN.add(report.nodeid)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    """Gruen heisst: jeder Test der Suite lief und bestand. Abgewaehlte, nicht gelaufene oder nicht bestandene Tests
    (-k, --deselect, -m, --co, -x, PYTEST_ADDOPTS, addopts) machen den Lauf rot."""
    fehlend = sorted(GESAMMELT - BESTANDEN)
    if fehlend and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        bericht = session.config.pluginmanager.get_plugin("terminalreporter")
        text = f"Akzeptanzsuite unvollstaendig: {len(fehlend)} von {len(GESAMMELT)} Tests nicht bestanden, z. B. {fehlend[0]}"
        if bericht is not None:
            bericht.write_line(text, red=True)
        else:
            print(text)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Ein uebersprungener oder als erwartet fehlschlagend gewerteter Test (auch zur Laufzeit) ist rot."""
    ergebnis = yield
    bericht = ergebnis.get_result()
    if bericht.skipped or hasattr(bericht, "wasxfail"):
        bericht.outcome = "failed"
        bericht.longrepr = "Akzeptanzsuite: skip und xfail sind nicht erlaubt"
        if hasattr(bericht, "wasxfail"):
            del bericht.wasxfail


@pytest.fixture
def welt(tmp_path):
    """Das Gateway (Host) in tmp_path; alles Weitere regelt die Welt."""
    from welt import Welt

    return Welt(tmp_path)


@pytest.fixture
def release_welt(tmp_path, monkeypatch):
    """Geraet mit Updater-Zustand, lokaler Release-Seite und docker-Attrappe in tmp_path."""
    from welt_release import ReleaseWelt

    welt = ReleaseWelt(tmp_path, monkeypatch)
    yield welt
    welt.schliessen()
