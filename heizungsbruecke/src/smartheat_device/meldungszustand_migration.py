"""Einmalige Migration des Meldungszustands im Wartungsfenster, von Hand aufgerufen (kein Teil des Normalstarts):

    python -m smartheat_device.meldungszustand_migration --device-dir <Ordner> --release <Release>

Der Stand vor der Umstellung merkte offene Meldungen in der `backup.json` jedes Kreises (`runtime/kreis-<n>/`) als
Zustand je Schluessel (`notify_states`) und als fertigen Text (`notify_messages`). Seitdem steht dort die Meldung als
Code mit Parametern (`notify_meldungen`); die alten Texte liest der neue Code nie. Der Lauf entfernt beide Felder: der
Meldungszustand beginnt leer, ein noch bestehender Anlass wird beim naechsten Pruefen einmal neu gemeldet, der
Notbetrieb wird beim Start aus dem Failsafe-Zustand wieder gesaet.

Der Lauf sichert jede betroffene Datei als `backup.json.pre-<Release>` (Rueckweg: zurueckspielen) und schreibt die
Datei dann ohne die beiden Felder; alles andere bleibt unveraendert. Eine vorhandene Sicherung wird nie ueberschrieben,
eine Datei ueber einen Symlink oder mit unbrauchbarem Inhalt bricht den Lauf vor jeder Aenderung ab; scheitert das
Schreiben, wird der Stand vor dem Lauf wiederhergestellt. Ausgegeben wird je Lauf ein Code ohne Namen (Exit 0 bei einem
Erfolgscode, sonst 1)."""
import argparse
import contextlib
import json
import re
import sys
from pathlib import Path

from smartheat_core import dateien, zustandspfade

#: Die Felder des Stands vor der Umstellung, die der Lauf entfernt.
ALTE_FELDER = ("notify_states", "notify_messages")

MIGRIERT = "migriert"
BEREITS_AKTUELL = "bereits_aktuell"
ERFOLG = (MIGRIERT, BEREITS_AKTUELL)

RELEASE_UNGUELTIG = "release_ungueltig"
ORDNER_FEHLT = "ordner_fehlt"
DATEI_UNGUELTIG = "datei_ungueltig"
SICHERUNG_VORHANDEN = "sicherung_vorhanden"
SCHREIBEN_FEHLGESCHLAGEN = "schreiben_fehlgeschlagen"

_RELEASE = re.compile(r"[0-9A-Za-z._-]{1,32}")


def _vorhanden(pfad: Path) -> bool:
    return pfad.is_symlink() or pfad.exists()


def _zuruecknehmen(sicherungen: list[Path], ersetzt: dict[Path, bytes]) -> None:
    """Stellt die schon ersetzten Dateien wieder her und entfernt die Sicherungen dieses Laufs (best effort)."""
    for pfad, inhalt in ersetzt.items():
        with contextlib.suppress(OSError):
            dateien.schreiben(pfad, inhalt, modus=dateien.PRIVAT)
    for pfad in reversed(sicherungen):
        with contextlib.suppress(OSError):
            pfad.unlink()


def migrieren(ordner: Path, *, release: str) -> str:
    """Der Code des Laufs (siehe Konstanten). Aendert nichts, wenn der Code kein Erfolgscode ist; bei
    `schreiben_fehlgeschlagen` wird der Stand vor dem Lauf wiederhergestellt."""
    if not _RELEASE.fullmatch(release):
        return RELEASE_UNGUELTIG
    if not ordner.is_dir():
        return ORDNER_FEHLT
    runtime = ordner.parent / "runtime"
    betroffen: dict[Path, tuple[bytes, dict]] = {}
    for kreis in zustandspfade.kreisordner(runtime):
        datei = kreis / zustandspfade.BACKUP
        if not _vorhanden(datei):
            continue
        if datei.is_symlink() or not datei.is_file():
            return DATEI_UNGUELTIG
        try:
            roh = datei.read_bytes()
            werte = json.loads(roh)
        except (OSError, ValueError):
            return DATEI_UNGUELTIG
        if not isinstance(werte, dict):
            return DATEI_UNGUELTIG
        if any(feld in werte for feld in ALTE_FELDER):
            betroffen[datei] = (roh, werte)
    if not betroffen:
        return BEREITS_AKTUELL
    sicherungen = {datei: datei.with_name(f"{datei.name}.pre-{release}") for datei in betroffen}
    if any(_vorhanden(pfad) for pfad in sicherungen.values()):
        return SICHERUNG_VORHANDEN
    angelegt: list[Path] = []
    ersetzt: dict[Path, bytes] = {}
    try:
        for datei, (roh, _) in betroffen.items():
            dateien.schreiben(sicherungen[datei], roh, modus=dateien.PRIVAT)
            angelegt.append(sicherungen[datei])
        for datei, (roh, werte) in betroffen.items():
            neu = {feld: wert for feld, wert in werte.items() if feld not in ALTE_FELDER}
            ersetzt[datei] = roh
            dateien.schreiben(datei, json.dumps(neu).encode(), modus=dateien.PRIVAT)
    except OSError:
        _zuruecknehmen(angelegt, ersetzt)
        return SCHREIBEN_FEHLGESCHLAGEN
    return MIGRIERT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smartheat_device.meldungszustand_migration",
                                     description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--device-dir", required=True, type=Path)
    parser.add_argument("--release", required=True)
    args = parser.parse_args(argv)
    code = migrieren(args.device_dir, release=args.release)
    print(code)
    return 0 if code in ERFOLG else 1


if __name__ == "__main__":
    sys.exit(main())
