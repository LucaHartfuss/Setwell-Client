"""Handgriffe an der Diagnoseseite der Welt (LAN-Seite mit dem Etikett-Passwort): die Werk-Datei mit einem Passwort, ein
fester freier Port statt des Zufallsports, Anmeldungen und ein uebergrosser Kopf ueber echte Sockets auf 127.0.0.1.
Gemischt in `welt.Welt` ein (braucht `host_dir`, `_monkeypatch`). Die scrypt-Parameter stehen hier woertlich."""
import base64
import gc
import hashlib
import json
import socket
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

from smartheat_gateway import runtime_main

SCRYPT = {"n": 131072, "r": 8, "p": 1}
SALZ = bytes(range(16))  # Salz der Werk-Datei (kein Geheimnis)
FRIST = 20.0  # Sekunden je Socket-Vorgang: ein haengender Server soll den Test nicht festhalten


def _scrypt(passwort: str) -> bytes:
    return hashlib.scrypt(passwort.encode(), salt=SALZ, **SCRYPT, maxmem=268435456, dklen=32)


def werk_schreiben(host_dir, passwort: str) -> dict:
    """werk.json (Kopie des Hosts) mit dem scrypt-Hash von `passwort`; gibt den Eintrag zurueck (fuer die Log-Pruefung)."""
    eintrag = {"verfahren": "scrypt", **SCRYPT, "salz": base64.b64encode(SALZ).decode(),
               "hash": base64.b64encode(_scrypt(passwort)).decode()}
    werk = {"format": "smartheat-werk-1", "seriennummer": "AKZ-0001", "hostname": "akzeptanz", "passwort": eintrag,
            "bespielt": "2026-01-01", "hardware": "test"}
    (host_dir / "werk.json").write_text(json.dumps(werk))
    return eintrag


def freier_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _antwort_lesen(sock: socket.socket) -> bytes:
    roh = b""
    try:
        while teil := sock.recv(65536):
            roh += teil
    except OSError:
        pass  # zurueckgesetzt: der Server hat die Verbindung ohne Antwort geschlossen
    return roh


def anmelden(port: int, passwort: str) -> tuple[int, dict]:
    """POST /anmelden wie ein Browser im LAN (ohne Origin); Status und Kopfzeilen der Antwort."""
    koerper = urllib.parse.urlencode({"passwort": passwort})
    anfrage = (f"POST /anmelden HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/x-www-form-urlencoded\r\n"
               f"Content-Length: {len(koerper)}\r\nConnection: close\r\n\r\n{koerper}")
    with socket.create_connection(("127.0.0.1", port), timeout=FRIST) as sock:
        sock.sendall(anfrage.encode())
        roh = _antwort_lesen(sock)
    kopf = roh.split(b"\r\n\r\n")[0].decode().split("\r\n")
    return int(kopf[0].split()[1]), {zeile.partition(":")[0].lower(): zeile.partition(":")[2].strip() for zeile in kopf[1:]}


def grosser_kopf(port: int, megabyte: int) -> bytes:
    """Eine Anfrage, deren Kopf `megabyte` MB gross ist; die Bytes, die der Server zurueckschickt (leer: ohne Antwort)."""
    with socket.create_connection(("127.0.0.1", port), timeout=FRIST) as sock:
        try:
            sock.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Fuell: ".encode())
            for _ in range(megabyte * 16):
                sock.sendall(b"a" * 65536)
            sock.sendall(b"\r\n\r\n")
        except OSError:
            pass  # der Server hat schon geschlossen
        return _antwort_lesen(sock)


class Flut:
    """`anzahl` parallele Anmeldungen (alle zugleich losgelassen) und ein uebergrosser Kopf, im Hintergrund; `warten`
    sammelt sie ein. `antworten` sind Status und Retry-After der Anmeldungen, `kopf_antwort` die Antwort auf den
    grossen Kopf."""

    def __init__(self, port: int, passwort: str, anzahl: int) -> None:
        self.antworten: list[tuple[int, str | None]] = []  # (Status, Retry-After) je Anmeldung
        self.kopf_antwort: bytes | None = None
        start = threading.Barrier(anzahl)

        def anmeldung() -> None:
            start.wait(FRIST)
            code, kopf = anmelden(port, passwort)
            self.antworten.append((code, kopf.get("retry-after")))

        def kopf() -> None:
            self.kopf_antwort = grosser_kopf(port, 6)

        self._faeden = [threading.Thread(target=anmeldung) for _ in range(anzahl)] + [threading.Thread(target=kopf)]
        for faden in self._faeden:
            faden.start()

    def laeuft(self) -> bool:
        return any(faden.is_alive() for faden in self._faeden)

    def warten(self) -> None:
        for faden in self._faeden:
            faden.join(FRIST * 2)
            assert not faden.is_alive(), "ein Faden der Flut haengt"


class Diagnose:
    def diagnose_vorbereiten(self, passwort: str) -> tuple[int, dict]:
        """Werk-Datei mit `passwort` ablegen und die Diagnoseseite auf einen festen freien Port legen (vor starten())."""
        eintrag = werk_schreiben(self.host_dir, passwort)
        self.diagnose_port = freier_port()
        self._monkeypatch.setattr(runtime_main, "DIAG_PORT", self.diagnose_port, raising=True)
        return self.diagnose_port, eintrag

    def diagnose_schliessen(self) -> None:
        """Teardown: den Server der Diagnoseseite beenden (der Aufrufer von start_server verwirft ihn)."""
        for objekt in gc.get_objects():
            if (isinstance(objekt, ThreadingHTTPServer) and objekt.server_address[1] == self.diagnose_port
                    and objekt.socket.fileno() != -1):  # ein Server, dessen Bind scheiterte, hat keinen Faden
                objekt.shutdown()
                objekt.server_close()
