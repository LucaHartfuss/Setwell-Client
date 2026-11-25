"""Erzeuger fuer /werk/werk.json beim Bespielen eines Gateways (Werkzeug am Rechner der Bespiel-Station, nicht auf dem Geraet).

Aufruf:  python3 gateway/release/werk_erzeugen.py --seriennummer P-0001 --hostname setwell-p-0001 ZIEL

ZIEL ist der Pfad der Datei auf der eingehaengten Partition `werk` des bespielten Datentraegers. Das Geraetepasswort wird
nur per `getpass` zweimal abgefragt, nie als Argument gelesen und nie ausgegeben oder protokolliert; gespeichert wird
allein der scrypt-Hash mit frischem Salz. Das Format und seine Regeln bestimmt der Leser des Geraets
(`smartheat_host.werk`): der Erzeuger prueft die Werte vor dem Schreiben und die geschriebene Datei danach mit genau
diesem Leser; besteht sie nicht, bleibt keine Datei zurueck. Der Schreibweg ist der atomare des Geraetekerns (0644,
ersetzt einen Symlink am Zielnamen, statt ihm zu folgen)."""
import argparse
import base64
import getpass
import hashlib
import secrets
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))  # Aufruf als Skript ohne PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "heizungsbruecke" / "src"))
from smartheat_core import dateien  # noqa: E402
from smartheat_host import werk  # noqa: E402

PASSWORT_MIN = 12  # Zeichen; das Etikett-Passwort soll mindestens 60 Bit Zufall haben (12 Zeichen aus a-zA-Z0-9 = 71 Bit)
PASSWORT_MAX = 128
HARDWARE_STANDARD = "Raspberry Pi 5"
SCRYPT_MAXMEM = 268435456  # 256 MiB; n=131072, r=8 braucht rund 128 MiB
SALZ_BYTES = 16
HASH_BYTES = 32


def _b64(roh: bytes) -> str:
    return base64.b64encode(roh).decode()


def _daten(args: argparse.Namespace, passwort: str) -> dict:
    salz = secrets.token_bytes(SALZ_BYTES)
    verfahren = werk._SCRYPT
    hash_ = hashlib.scrypt(passwort.encode(), salt=salz, n=verfahren["n"], r=verfahren["r"], p=verfahren["p"],
                           maxmem=SCRYPT_MAXMEM, dklen=HASH_BYTES)
    return {
        "format": werk.FORMAT,
        "seriennummer": args.seriennummer,
        "hostname": args.hostname,
        "passwort": {"verfahren": "scrypt", **verfahren, "salz": _b64(salz), "hash": _b64(hash_)},
        "bespielt": args.bespielt,
        "hardware": args.hardware,
    }


def _passwort_problem(passwort: str) -> str | None:
    if not PASSWORT_MIN <= len(passwort) <= PASSWORT_MAX:
        return f"Passwort: {PASSWORT_MIN} bis {PASSWORT_MAX} Zeichen"
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="werk_erzeugen.py", description="schreibt werk.json fuer ein Gateway; das "
                                     "Passwort wird interaktiv abgefragt")
    parser.add_argument("--seriennummer", required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--hardware", default=HARDWARE_STANDARD)
    parser.add_argument("--bespielt", default=date.today().isoformat(), help="Datum JJJJ-MM-TT (Standard: heute)")
    parser.add_argument("ziel", type=Path, help="Pfad der werk.json auf der eingehaengten Partition werk")
    return parser


def main(argv: list[str] | None = None, *, passwort_eingabe: Callable[[str], str] = getpass.getpass) -> int:
    args = _parser().parse_args(argv)
    if not args.ziel.parent.is_dir():
        print(f"Ordner {args.ziel.parent} fehlt (Partition werk eingehaengt?)", file=sys.stderr)
        return 2
    vorab = {"format": werk.FORMAT, "seriennummer": args.seriennummer, "hostname": args.hostname,
             "bespielt": args.bespielt, "hardware": args.hardware}
    problem = werk._problem({**vorab, "passwort": {"verfahren": "scrypt", **werk._SCRYPT,
                                                   "salz": _b64(bytes(SALZ_BYTES)), "hash": _b64(bytes(HASH_BYTES))}})
    if problem is not None:
        print(f"Ungueltiger Wert: Feld {problem}", file=sys.stderr)
        return 2
    passwort = passwort_eingabe("Geraetepasswort: ")
    if passwort != passwort_eingabe("Geraetepasswort noch einmal: "):
        print("Die Passwoerter sind verschieden", file=sys.stderr)
        return 2
    problem = _passwort_problem(passwort)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 2
    daten = _daten(args, passwort)
    dateien.schreiben_json(args.ziel, daten, modus=dateien.LESBAR)
    if werk.lesen(args.ziel) != daten:
        args.ziel.unlink(missing_ok=True)
        print("Der Leser lehnt die geschriebene Datei ab: Datei entfernt", file=sys.stderr)
        return 1
    print(f"{args.ziel} geschrieben: {args.seriennummer}, {args.hostname}")
    return 0


if __name__ == "__main__":  # pragma: no cover - Einstieg am Rechner
    sys.exit(main())
