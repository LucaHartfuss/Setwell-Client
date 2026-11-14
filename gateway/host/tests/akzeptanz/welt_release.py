"""Schmale Welt des Updaters fuer die Akzeptanzsuite: ein Geraet in tmp_path (SHG_ROOT mit `updater/state.json`, einem
installierten Bundle, Broker-Endpunkt und Hoststatus), eine lokale Release-Seite (127.0.0.1) mit Bundles und `/healthz`,
eine `docker`-Attrappe im PATH (Compose und Images als Dateien, jeder Aufruf steht im Protokoll) und eine Startauswahl,
die jeden Aufruf aufzeichnet. Die Schluessel entstehen zur Laufzeit in tmp_path (nie im Repo). Einstieg in den Updater ist
allein `updater.main(["--once"], einstellungen=...)`; beobachtet werden Ausgabe, Dateien und Aufrufprotokolle."""
import base64
import contextlib
import functools
import hashlib
import io
import json
import socket
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from smartheat_host import updater

ALT = "0.9.1"  # laeuft auf dem Geraet
NEU = "0.9.2"  # wird angeboten
AELTER = "0.9.0"  # aelter als die laufende Version
MOSQUITTO = "eclipse-mosquitto:2@sha256:" + "a" * 64
#: Zeitpunkte relativ zu jetzt: die Root gilt seit 2 Tagen, das Release-Zertifikat seit einem Tag fuer 30 Tage
ROOT_VOR = timedelta(days=2)
BLATT_VOR = timedelta(days=1)
BLATT_BIS = timedelta(days=30)
#: Buchfuehrung der Ablehnung in state.json: aendert sich bei jeder Ablehnung, gehoert nicht zu Phase und System
BUCHFUEHRUNG = ("rejected", "rejected_context", "pending_reports")

DOCKER = r'''#!@PYTHON@
import json
import sys
from pathlib import Path

ORDNER = Path(__file__).resolve().parent.parent / "docker"
IMAGES = ORDNER / "images"
argv = sys.argv[1:]
with open(ORDNER / "aufrufe.log", "a") as log:
    log.write(json.dumps(argv) + "\n")


def name(ref):
    return ref.replace("/", "_")


if argv[:1] == ["compose"]:
    datei = Path(argv[argv.index("-f") + 1])
    befehl = argv[argv.index("-f") + 2:]
    if befehl[0] == "up":
        zeilen = datei.read_text().splitlines()
        fehlt = [z.split("image:", 1)[1].strip() for z in zeilen if z.strip().startswith("image:")]
        fehlt = [ref for ref in fehlt if not (IMAGES / name(ref)).exists()]
        if fehlt:
            print("Image nicht vorhanden: " + ", ".join(fehlt), file=sys.stderr)
            sys.exit(1)
        (ORDNER / "laufend").write_text(datei.parent.name)
    elif befehl[0] == "ps":
        gesundheit = ORDNER / "gesundheit" / datei.parent.name
        zustand = gesundheit.read_text() if gesundheit.exists() else "healthy"
        print(json.dumps([{"Service": "runtime", "Health": zustand}]))
elif argv[:2] == ["image", "inspect"]:
    sys.exit(0 if (IMAGES / name(argv[2])).exists() else 1)
elif argv[:2] == ["image", "ls"]:
    for datei in sorted(IMAGES.iterdir()):
        ref = datei.read_text().strip()
        print(ref.split("@")[0].split(":")[0] + "@" + ref.split("@")[1])
elif argv[:2] == ["image", "rm"]:
    (IMAGES / name(argv[2])).unlink(missing_ok=True)
elif argv[:1] == ["load"]:
    ref = Path(argv[2]).read_text().split()[1]
    (IMAGES / name(ref)).write_text(ref)
else:
    sys.exit(2)
'''

COMPOSE = """services:
  runtime:
    image: {runtime}
    pull_policy: never
  mosquitto:
    image: {mosquitto}
"""


class AufzeichnendeStartauswahl:
    """Startauswahl (rauc/tryboot) als Attrappe: Slot A laeuft, jeder Aufruf steht in `aufrufe`."""

    def __init__(self) -> None:
        self.aufrufe: list[tuple] = []

    def gestartet(self) -> str:
        self.aufrufe.append(("gestartet",))
        return "A"

    def primaer(self) -> str:
        self.aufrufe.append(("primaer",))
        return "A"

    def im_probestart(self) -> bool:
        self.aufrufe.append(("im_probestart",))
        return False

    def compatible(self) -> str:
        self.aufrufe.append(("compatible",))
        return "smartheat-gateway-pi5"

    def installieren(self, bundle: Path) -> None:
        self.aufrufe.append(("installieren", bundle))

    def probestart(self) -> None:
        self.aufrufe.append(("probestart",))

    def bestaetigen(self) -> None:
        self.aufrufe.append(("bestaetigen",))

    def zurueckschalten(self) -> None:
        self.aufrufe.append(("zurueckschalten",))

    def normal_neustarten(self) -> None:
        self.aufrufe.append(("normal_neustarten",))


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def neue_root(cn: str, von: datetime):
    schluessel = ec.generate_private_key(ec.SECP256R1())
    nutzung = x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                            encipher_only=False, decipher_only=False)
    zertifikat = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(_name(cn))
                  .public_key(schluessel.public_key()).serial_number(x509.random_serial_number())
                  .not_valid_before(von).not_valid_after(von + timedelta(days=5000))
                  .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                  .add_extension(nutzung, critical=True).sign(schluessel, hashes.SHA256()))
    return schluessel, zertifikat


def neues_blatt(root_schluessel, root_zertifikat, von: datetime, bis: datetime):
    schluessel = ec.generate_private_key(ec.SECP256R1())
    nutzung = x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                            encipher_only=False, decipher_only=False)
    zertifikat = (x509.CertificateBuilder().subject_name(_name("Release")).issuer_name(root_zertifikat.subject)
                  .public_key(schluessel.public_key()).serial_number(x509.random_serial_number())
                  .not_valid_before(von).not_valid_after(bis)
                  .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                  .add_extension(nutzung, critical=True)
                  .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False)
                  .sign(root_schluessel, hashes.SHA256()))
    return schluessel, zertifikat


def signiere(schluessel, zertifikat, daten: bytes) -> str:
    """Signaturtext der Kette: Zertifikat (PEM) und ECDSA-SHA256-Signatur (DER, Base64) ueber `daten`."""
    signatur = schluessel.sign(daten, ec.ECDSA(hashes.SHA256()))
    return json.dumps({"format": "smartheat-kette-1", "zertifikat": zertifikat.public_bytes(serialization.Encoding.PEM).decode(),
                       "signatur": base64.b64encode(signatur).decode()})


class _Seite(SimpleHTTPRequestHandler):
    """Release-Seite: die Dateien der Bundles und `/healthz` mit Serverkontakt; jeder Abruf steht in `abrufe`."""

    abrufe: list[str]

    def do_GET(self):
        self.abrufe.append(self.path)
        if self.path != "/healthz":
            return super().do_GET()
        koerper = json.dumps({"server_ok": True, "runtime_status": "laeuft"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(koerper)))
        self.end_headers()
        self.wfile.write(koerper)

    def log_message(self, *args):
        pass


class ReleaseWelt:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp_path = tmp_path
        self.root = tmp_path / "geraet"
        self.docker = tmp_path / "docker"
        self.seite = tmp_path / "seite"
        self.startauswahl = AufzeichnendeStartauswahl()
        #: Uhr der Welt in Sekunden: der Schlaf des Updaters rueckt sie vor, echt gewartet wird nie
        self.zeit = 0.0
        self._gesund_ab: tuple[str, float] | None = None
        monkeypatch.setitem(updater.Updater.__init__.__kwdefaults__, "clock", lambda: self.zeit)
        monkeypatch.setitem(updater.Updater.__init__.__kwdefaults__, "sleep", self._schlafen)
        self.jetzt = datetime.now(UTC)
        root_schluessel, self.root_zertifikat = neue_root("Test Root", self.jetzt - ROOT_VOR)
        self.blatt = neues_blatt(root_schluessel, self.root_zertifikat, self.jetzt - BLATT_VOR, self.jetzt + BLATT_BIS)
        fremd_schluessel, fremd_zertifikat = neue_root("Fremde Root", self.jetzt - ROOT_VOR)
        self.fremdes_blatt = neues_blatt(fremd_schluessel, fremd_zertifikat, self.jetzt - BLATT_VOR, self.jetzt + BLATT_BIS)
        (tmp_path / "release-roots.pem").write_text(self.root_zertifikat.public_bytes(serialization.Encoding.PEM).decode())
        monkeypatch.setenv("SHG_ROOT", str(self.root))
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
        self._docker_anlegen()
        self._broker = socket.create_server(("127.0.0.1", 0))
        self.seite.mkdir()
        self.abrufe: list[str] = []
        handler = type("Seite", (_Seite,), {"abrufe": self.abrufe})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(handler, directory=str(self.seite)))
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self._geraet_anlegen()

    def schliessen(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._broker.close()

    # --- Aufbau ---

    def _schreiben(self, pfad: Path, inhalt: str) -> None:
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text(inhalt)

    def _docker_anlegen(self) -> None:
        skript = self.tmp_path / "bin" / "docker"
        self._schreiben(skript, DOCKER.replace("@PYTHON@", sys.executable))
        skript.chmod(0o755)
        (self.docker / "images").mkdir(parents=True)
        self._schreiben(self.docker / "aufrufe.log", "")

    def _image_bekannt(self, ref: str) -> None:
        self._schreiben(self.docker / "images" / ref.replace("/", "_"), ref)

    @staticmethod
    def runtime_image(version: str) -> str:
        return "smartheat-gateway@sha256:" + hashlib.sha256(version.encode()).hexdigest()

    def _geraet_anlegen(self) -> None:
        """Das Geraet laeuft auf ALT: Bundle installiert, beide Images da, Broker und Uhr in Ordnung."""
        bundle = self.root / "bundles" / ALT
        self._schreiben(bundle / "docker-compose.yml", COMPOSE.format(runtime=self.runtime_image(ALT), mosquitto=MOSQUITTO))
        self._schreiben(bundle / "mosquitto.conf", "listener 1883\n")
        self._image_bekannt(self.runtime_image(ALT))
        self._image_bekannt(MOSQUITTO)
        self._schreiben(self.docker / "laufend", ALT)
        self._schreiben(self.root / "updater" / "state.json", json.dumps({"current": ALT}))
        self._schreiben(self.root / "data" / "device" / "endpoint.json",
                        json.dumps({"host": "127.0.0.1", "port": self._broker.getsockname()[1]}))
        self._schreiben(self.root / "host" / "status.json",
                        json.dumps({"zeit_synchron": True, "ts": self.jetzt.isoformat()}))

    # --- Angebot ---

    def angebot(self, version: str, *, signatur_von: str = "eigen", gesund: bool = True) -> None:
        """Legt das Bundle `version` auf die Release-Seite und nennt es als Soll-Software. `signatur_von`: "eigen" (Kette
        der vertrauten Root) oder "fremd" (Zertifikat einer Root, die das Geraet nicht kennt). `gesund`: wie der Container
        nach dem Start antwortet."""
        ordner = self.seite / version
        runtime = self.runtime_image(version)
        archiv = f"image {runtime}\n".encode()
        dateien = {"docker-compose.yml": COMPOSE.format(runtime=runtime, mosquitto=MOSQUITTO).encode(),
                   "mosquitto.conf": b"listener 1883\n"}
        manifest = json.dumps({
            "version": version, "min_updater_version": "0.1.0", "art": "funktion",
            "files": {name: hashlib.sha256(inhalt).hexdigest() for name, inhalt in dateien.items()},
            "images": {"runtime": runtime, "mosquitto": MOSQUITTO},
            "archiv": {"datei": "gateway-image.oci.tar", "sha256": hashlib.sha256(archiv).hexdigest(), "bytes": len(archiv),
                       "name": "smartheat-gateway", "digest": runtime.split("@")[1]},
            "ablauf": (self.jetzt + BLATT_BIS).strftime("%Y-%m-%dT%H:%M:%SZ")}, sort_keys=True).encode()
        for name, inhalt in {**dateien, "gateway-image.oci.tar": archiv, "manifest.json": manifest}.items():
            (ordner).mkdir(parents=True, exist_ok=True)
            (ordner / name).write_bytes(inhalt)
        schluessel, zertifikat = self.fremdes_blatt if signatur_von == "fremd" else self.blatt
        self._schreiben(self.root / "data" / "software_soll.json", json.dumps({
            "version": version, "manifest_url": f"http://127.0.0.1:{self._server.server_port}/{version}/manifest.json",
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(), "signature": signiere(schluessel, zertifikat, manifest)}))
        self._schreiben(self.docker / "gesundheit" / version, "healthy" if gesund else "unhealthy")

    def gesund_ab(self, version: str, sekunden: float) -> None:
        """Der Container von `version` meldet sich erst `sekunden` nach dem Start gesund (Uhr der Welt, Start bei 0)."""
        self._gesund_ab = (version, sekunden)

    def _schlafen(self, sekunden: float) -> None:
        self.zeit += sekunden
        if self._gesund_ab is not None and self.zeit >= self._gesund_ab[1]:
            self._schreiben(self.docker / "gesundheit" / self._gesund_ab[0], "healthy")

    def widerrufen(self) -> None:
        """Das Release-Zertifikat gilt nicht mehr: ein neueres wurde schon angenommen, die Untergrenze liegt hinter dem
        Beginn dieses Zertifikats (und vor der Uhr des Geraets)."""
        self._schreiben(self.root / "updater" / "state.json", json.dumps(
            {"current": ALT, "zertifikat_untergrenze": (self.jetzt - timedelta(hours=12)).isoformat()}))

    # --- Lauf ---

    def lauf(self) -> str:
        """Ein Durchlauf des Updaters (`--once`); die Ausgabe nennt das Ergebnis. Die Gesundheitsfrist ist die des Geraets
        (Einstellungen ohne `health_seconds`), Uhr und Schlaf sind die der Welt."""
        ausgabe = io.StringIO()
        einstellungen = updater.Einstellungen(
            release_roots=self.tmp_path / "release-roots.pem", allow_http=True,
            health_url=f"http://127.0.0.1:{self._server.server_port}/healthz",
            compose_project="akzeptanz", env_datei=self.tmp_path / "gateway.env", startauswahl=lambda: self.startauswahl)
        with contextlib.redirect_stdout(ausgabe):
            updater.main(["--once"], einstellungen=einstellungen)
        return ausgabe.getvalue().strip()

    # --- Beobachtung ---

    def zustand(self) -> dict:
        """state.json ohne die Buchfuehrung der Ablehnung und ohne leere Werte (fehlend und leer gelten gleich)."""
        daten = json.loads((self.root / "updater" / "state.json").read_text())
        return {k: v for k, v in daten.items() if k not in BUCHFUEHRUNG and v not in (None, [], 0)}

    def meldungen(self) -> list[dict]:
        pfad = self.root / "data" / "software_meldungen.json"
        return json.loads(pfad.read_text())["meldungen"] if pfad.is_file() else []

    def bundles(self) -> list[str]:
        return sorted(p.name for p in (self.root / "bundles").iterdir())

    def archive(self) -> list[str]:
        ordner = self.root / "archive"
        return sorted(p.name for p in ordner.iterdir()) if ordner.is_dir() else []

    def befehle(self) -> list[list[str]]:
        """Aufrufe von docker: bei `compose` die Version des Bundles gefolgt vom Befehl, sonst die Argumente."""
        ergebnis = []
        for zeile in (self.docker / "aufrufe.log").read_text().splitlines():
            argv = json.loads(zeile)
            if argv[:1] == ["compose"]:
                ort = argv.index("-f")
                ergebnis.append([Path(argv[ort + 1]).parent.name, *argv[ort + 2:]])
            else:
                ergebnis.append(argv)
        return ergebnis

    def hochgefahren(self) -> list[str]:
        """Versionen, die `docker compose up` bekommen haben, in der Reihenfolge der Aufrufe."""
        return [befehl[0] for befehl in self.befehle() if befehl[1:2] == ["up"]]

    def startauswahl_eingriffe(self) -> list[tuple]:
        """Aufrufe der Startauswahl, die etwas verstellen (Lesen der Lage zaehlt nicht)."""
        lesen = {"gestartet", "primaer", "im_probestart", "compatible"}
        return [aufruf for aufruf in self.startauswahl.aufrufe if aufruf[0] not in lesen]

    def laeuft(self) -> str:
        return (self.docker / "laufend").read_text()

    def images(self) -> set[str]:
        return {p.read_text() for p in (self.docker / "images").iterdir()}
