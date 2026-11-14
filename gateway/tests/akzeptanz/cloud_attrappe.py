"""Attrappe der Hersteller-Cloud (Viessmann) fuer die Akzeptanzsuite: ein kleiner HTTP-Server auf 127.0.0.1 mit freiem
Port (nur Standardbibliothek), der die Anmeldung (Token) und die Abfrage- und Kommando-Schnittstelle der Anlage bedient.
Er zaehlt jeden eingehenden Aufruf der Schnittstelle mit der Zeit der Welt, antwortet auf Wunsch einmal mit 429 und
einem frei gewaehlten Retry-After und nimmt Kommandos an wie eine Anlage (die Merkmale aendern sich). Zugangsdaten sind
Kanarienvogel-Werte; nichts davon ist echt."""
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KENNUNG = {"installation_id": 2012345, "gateway_serial": "7637415000000001", "device_id": "0", "heizkreis": 0}
ZUGRIFF = "kanarienvogel-zugriff"
ERNEUERUNG = "kanarienvogel-erneuerung"


def _merkmal(name: str, eigenschaften: dict, kommandos: dict | None = None) -> dict:
    return {"feature": name, "isEnabled": True, "properties": eigenschaften, "commands": kommandos or {}}


def _zahl(wert: float) -> dict:
    return {"type": "number", "value": wert}


def _bereich(unten: float, oben: float, schritt: float) -> dict:
    return {"type": "number", "required": True, "constraints": {"min": unten, "max": oben, "stepping": schritt}}


#: Merkmale der Anlage zu Beginn: Heizkurve, Normalprogramm mit Raum-Soll, aktives Programm, Aussentemperatur
MERKMALE = [
    _merkmal("heating.circuits.0.heating.curve", {"shift": _zahl(0), "slope": _zahl(1.4)},
             {"setCurve": {"isExecutable": True, "params": {"shift": _bereich(-13, 40, 1),
                                                            "slope": _bereich(0.2, 3.5, 0.1)}}}),
    _merkmal("heating.circuits.0.operating.programs.normal", {"temperature": _zahl(20)},
             {"setTemperature": {"isExecutable": True, "params": {"targetTemperature": _bereich(3, 37, 1)}}}),
    _merkmal("heating.circuits.0.operating.programs.active", {"value": {"type": "string", "value": "normal"}}),
    _merkmal("heating.sensors.temperature.outside", {"value": _zahl(5)}),
]


class CloudAttrappe:
    """`aufrufe`: je eingehendem Aufruf der Schnittstelle (nicht des Token-Wegs) Methode, Pfad, Zeit der Welt und
    Antwortcode. `drossel_bei` ist die Nummer des Aufrufs (ab 1), den die Cloud mit 429 und `retry_after` beantwortet;
    nur dieser eine."""

    def __init__(self, zeit, *, drossel_bei: int | None = None, retry_after: str = "600") -> None:
        self.zeit, self.drossel_bei, self.retry_after = zeit, drossel_bei, retry_after
        self.aufrufe: list[tuple[str, str, float, int]] = []
        self.token_aufrufe = 0
        self.merkmale = copy.deepcopy(MERKMALE)
        self._sperre = threading.Lock()
        attrappe = self

        class _Anfrage(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _antworten(self, code: int, inhalt: dict, kopf: dict | None = None) -> None:
                roh = json.dumps(inhalt).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(roh)))
                for name, wert in (kopf or {}).items():
                    self.send_header(name, wert)
                self.end_headers()
                self.wfile.write(roh)

            def _koerper(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_GET(self) -> None:
                self._schnittstelle("GET")

            def do_POST(self) -> None:
                if self.path.startswith("/idp/"):
                    self._koerper()
                    with attrappe._sperre:
                        attrappe.token_aufrufe += 1
                    self._antworten(200, {"access_token": ZUGRIFF, "refresh_token": ERNEUERUNG, "expires_in": 3600})
                else:
                    self._schnittstelle("POST")

            def _schnittstelle(self, methode: str) -> None:
                koerper = self._koerper()
                with attrappe._sperre:
                    nummer = len(attrappe.aufrufe) + 1
                    if nummer == attrappe.drossel_bei:
                        code = 429
                    elif self.headers.get("Authorization") != f"Bearer {ZUGRIFF}":
                        code = 401
                    else:
                        code = 200
                    attrappe.aufrufe.append((methode, self.path, attrappe.zeit(), code))
                    if code == 200 and methode == "POST":
                        attrappe._kommando(self.path, json.loads(koerper or b"{}"))
                    daten = copy.deepcopy(attrappe.merkmale)
                if code == 429:
                    self._antworten(429, {"error": "RATE_LIMIT_EXCEEDED"}, {"Retry-After": attrappe.retry_after})
                elif code == 401:
                    self._antworten(401, {"error": "UNAUTHORIZED"})
                else:
                    self._antworten(200, {"data": daten} if methode == "GET" else {"data": {"success": True}})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Anfrage)
        self._server.daemon_threads = True
        self._faden = threading.Thread(target=self._server.serve_forever, name="cloud-attrappe", daemon=True)

    @property
    def adresse(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def starten(self) -> None:
        self._faden.start()

    def beenden(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._faden.join(timeout=5)

    def _kommando(self, pfad: str, werte: dict) -> None:
        """Eine Anlage uebernimmt ein Kommando: Pfad `.../features/<Merkmal>/commands/<Kommando>`."""
        merkmal = pfad.rpartition("/features/")[2].partition("/commands/")[0]
        kommando = pfad.rpartition("/")[2]
        for eintrag in self.merkmale:
            if eintrag["feature"] != merkmal:
                continue
            ziel = {"setCurve": {"shift": "shift", "slope": "slope"},
                    "setTemperature": {"targetTemperature": "temperature"}}.get(kommando, {})
            for name, eigenschaft in ziel.items():
                if name in werte:
                    eintrag["properties"][eigenschaft] = _zahl(werte[name])
