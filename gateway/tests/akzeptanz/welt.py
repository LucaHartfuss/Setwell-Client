"""Die schmale Welt der Laufzeit-Akzeptanzsuite des Gateways: das Gateway startet wie im Container ueber
runtime_main (_vorbereiten, start) auf einem In-Speicher-Bus mit Fake-Zigbee2MQTT und einem Link ohne Netz; eine
Uhr (monoton und Wand) steht unter Kontrolle des Tests. Ein gewollter Neustart (prozess.Neustart) baut wie Compose einen
neuen Prozess auf denselben Dateien. Ersetzt werden nur Namen in runtime_main (Geraete-Host und Geraetekern mit
eingespeisten Uhren und Link, die beiden Diagnose-Ports) und die Wanduhr; beobachtet wird nur, was nach aussen geht:
Bus, Link, Dateien. Nichts aus den Hilfen der uebrigen Tests."""
import base64
import datetime
import itertools
import json
import re

from anlagen import GERAETE, SENSOR, THERMOSTAT
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from uhr import Uhr
from welt_diagnose import Diagnose
from welt_handgriffe import Handgriffe

from smartheat_core import dateien
from smartheat_device import bootstrap, identity, prozess
from smartheat_device.device import Device
from smartheat_gateway import runtime_main
from smartheat_gateway.bus import decode, encode
from smartheat_gateway.geraet_host import GatewayDeviceHost
from smartheat_gateway.paths import Paths
from smartheat_gateway.zigbee import ZigbeeMirror

BASE = "zigbee2mqtt"
_CLIENTEN = itertools.count(1)
ZEITDIENST_TAKT = 180  # Sekunden zwischen zwei Schreibvorgaengen des Zeitdienstes (echt: 60; der Leser gilt bis 300)


def _passt(filter_: str, topic: str) -> bool:
    muster = "^" + re.escape(filter_).replace(r"\+", "[^/]+").replace(r"/\#", "(/.*)?").replace(r"\#", ".*") + "$"
    return re.match(muster, topic) is not None


class Bus:
    """Lokaler Bus (Mosquitto) im Speicher: retained Werte, Abonnenten, Aufzeichnung; trennen/verbinden spielt den
    Ausfall nach (nach dem Verbinden kommen die retained Werte erneut, dann die Verbindungs-Haken)."""

    def __init__(self) -> None:
        self.abos: list[tuple[str, object]] = []
        self.retained: dict[str, object] = {}
        self.gesendet: list[tuple[str, object]] = []
        self.haken: list = []
        self._verbunden = True

    @property
    def connected(self) -> bool:
        return self._verbunden

    def subscribe(self, topic_filter, callback) -> None:
        self.abos.append((topic_filter, callback))
        if self._verbunden:
            for topic, wert in list(self.retained.items()):
                if _passt(topic_filter, topic):
                    callback(topic, encode(wert), True)

    def publish(self, topic, payload, *, retain=False) -> None:
        roh = encode(payload)
        self.gesendet.append((topic, decode(roh)))
        if retain:
            self.retained[topic] = decode(roh)
        if self._verbunden:
            for topic_filter, callback in list(self.abos):
                if _passt(topic_filter, topic):
                    callback(topic, roh, False)

    def on_connected(self, hook) -> None:
        self.haken.append(hook)

    def off_connected(self, hook) -> None:
        if hook in self.haken:
            self.haken.remove(hook)

    def wait_connected(self, timeout=None) -> bool:
        return self._verbunden

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def trennen(self) -> None:
        self._verbunden = False

    def verbinden(self) -> None:
        self._verbunden = True
        for topic_filter, callback in list(self.abos):
            for topic, wert in list(self.retained.items()):
                if _passt(topic_filter, topic):
                    callback(topic, encode(wert), True)
        for haken in list(self.haken):
            haken()


class Zigbee:
    """Zigbee2MQTT mit einem Raumfuehler und einem Thermostat: meldet retained Zustaende und antwortet auf set mit dem
    neuen Soll (das Thermostat uebernimmt es sofort). `gesetzt` ist die Folge der geschriebenen Soll-Werte,
    `set_nachrichten` zaehlt jede set-Nachricht an das Thermostat, gleich mit welchem Inhalt."""

    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self.gesetzt: list[float] = []
        self.set_nachrichten = 0
        bus.publish(f"{BASE}/bridge/state", {"state": "online"}, retain=True)
        bus.publish(f"{BASE}/bridge/devices", GERAETE, retain=True)
        bus.subscribe(f"{BASE}/+/set", self._set)

    def melden(self, ieee: str, **werte) -> None:
        self._bus.publish(f"{BASE}/{ieee}", werte, retain=True)

    def _set(self, topic, payload, retain) -> None:
        ieee, inhalt = topic.split("/")[1], decode(payload)
        if ieee == THERMOSTAT:
            self.set_nachrichten += 1
        if ieee == THERMOSTAT and isinstance(inhalt, dict) and "occupied_heating_setpoint" in inhalt:
            self.gesetzt.append(inhalt["occupied_heating_setpoint"])
            self.melden(ieee, occupied_heating_setpoint=inhalt["occupied_heating_setpoint"], local_temperature=20.0)


class Link:
    """Link zum Server ohne Netz: Aufzeichnung der gesendeten Nachrichten, verbinden/empfangen steuert der Test."""

    def __init__(self, device_id, zugang, key_pem, ereignis) -> None:
        self.device_id, self.ereignis = device_id, ereignis
        self.connected = self.gestoppt = False
        self.gesendet: list[tuple[str, dict]] = []
        self.client_nr = 0

    def start(self) -> None:
        self.client_nr = next(_CLIENTEN)

    def stop(self) -> None:
        self.gestoppt, self.connected = True, False

    def publish(self, name, payload):
        if not self.connected:
            return None
        self.gesendet.append((name, json.loads(json.dumps(payload, allow_nan=False))))
        if name == "up/inventory":  # PUBACK des Brokers: das Ergebnis der Inventur wartet darauf
            self.ereignis("gesendet", len(self.gesendet), self.client_nr, True)
        return len(self.gesendet)

    def port_wechseln_falls_noetig(self) -> None:
        pass

    def verbinden(self) -> None:
        self.connected = True
        self.ereignis("verbunden")

    def empfangen(self, name, inhalt) -> None:
        self.ereignis("nachricht", name, json.dumps(inhalt).encode())

    def von(self, name: str) -> list[dict]:
        return [inhalt for topic, inhalt in self.gesendet if topic == name]


class _KeinBootstrap:
    def register(self, **kwargs):
        raise AssertionError("Bootstrap darf in der Welt nicht laufen: der Zugang liegt schon vor")

    def certificate(self):
        raise AssertionError("Bootstrap darf in der Welt nicht laufen: der Zugang liegt schon vor")


def _zugang_ablegen(device_dir) -> None:
    """Identitaet anlegen und einen Zugang von einer Wegwerf-CA speichern, wie nach dem Bootstrap."""
    jetzt = datetime.datetime.now(datetime.UTC)
    schluessel = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "akzeptanz-ca")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(schluessel.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(jetzt - datetime.timedelta(minutes=1))
          .not_valid_after(jetzt + datetime.timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(schluessel, hashes.SHA256()))
    ident = identity.load_or_create(device_dir)
    csr = x509.load_pem_x509_csr(identity.csr_pem(ident).encode())
    zertifikat = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca.subject)
                  .public_key(csr.public_key()).serial_number(x509.random_serial_number())
                  .not_valid_before(jetzt - datetime.timedelta(minutes=1))
                  .not_valid_after(jetzt + datetime.timedelta(days=2))
                  .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
                  .sign(schluessel, hashes.SHA256()))
    pem = lambda objekt: objekt.public_bytes(serialization.Encoding.PEM).decode()  # noqa: E731
    zugang = bootstrap.Zugang(pem(zertifikat), bootstrap.Endpoint("mqtt.e2e.test", 8883, pem(ca)))
    bootstrap.speichern(device_dir, bootstrap.Antwort("uebernommen", zugang))


def _werk_ablegen(host_dir) -> None:
    """Die Kopie von werk.json im Host-Ordner, wie der Bespieler sie ablegt: das Gateway sendet die Seriennummer daraus im
    hello und baut ohne sie keins. Der Passworteintrag hat nur die Form (gerechnet wird in dieser Welt nichts)."""
    passwort = {"verfahren": "scrypt", "n": 131072, "r": 8, "p": 1,
                "salz": base64.b64encode(bytes(16)).decode(), "hash": base64.b64encode(bytes(32)).decode()}
    werk = {"format": "smartheat-werk-1", "seriennummer": "AKZ-0001", "hostname": "akzeptanz", "passwort": passwort,
            "bespielt": "2026-01-01", "hardware": "test"}
    (host_dir / "werk.json").write_text(json.dumps(werk))


class Welt(Handgriffe, Diagnose):
    """Eine Gateway-Anlage unter Test. `starten()` ist der Prozessstart; die Zeit laeuft nur ueber `runde`."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        self.paths = Paths(tmp_path / "data")
        self.host_dir = tmp_path / "host"
        self.host_dir.mkdir()
        _werk_ablegen(self.host_dir)
        self._monkeypatch, self.diagnose_port = monkeypatch, None
        self._status_um: datetime.datetime | None = None  # Wanduhrzeit der zuletzt geschriebenen host/status.json
        self.uhr, self.bus = Uhr(), Bus()
        self.z2m = Zigbee(self.bus)
        self.links: list[Link] = []
        self.starts = 0
        self.geraet = self.device = self.laufzeit = self.anlage = None
        self._geraete: list = []  # jeder Prozessstart baut einen Geraete-Host samt Hintergrund-Thread
        welt = self

        class _Host(GatewayDeviceHost):
            def __init__(self, paths, mirror, *, host_dir):
                super().__init__(paths, mirror, host_dir=host_dir, clock=welt.uhr, wall=welt.uhr.epoche,
                                 echt=welt.uhr.epoche)

        class _Geraet(Device):
            def __init__(self, host, data_dir, bootstrap_url, *, befehl_fehler):
                super().__init__(host, data_dir, bootstrap_url, clock=welt.uhr, wall=welt.uhr.epoche,
                                 echt=welt.uhr.epoche, bootstrap_client=lambda ident: _KeinBootstrap(),
                                 link_factory=welt._link, befehl_fehler=befehl_fehler)

        monkeypatch.setenv("SHG_HOST_DIR", str(self.host_dir))
        monkeypatch.setenv("SHG_DEVICE_API_URL", "https://accounts.example.test")
        monkeypatch.setenv("SHG_ALIVE_FILE", str(tmp_path / "alive"))
        monkeypatch.setattr(runtime_main, "GatewayDeviceHost", _Host, raising=True)
        monkeypatch.setattr(runtime_main, "Device", _Geraet, raising=True)
        monkeypatch.setattr(runtime_main, "DIAG_PORT", 0, raising=True)  # ein freier Port je Start
        monkeypatch.setattr(runtime_main, "HEALTH_PORT", 0, raising=True)
        monkeypatch.setattr("smartheat_core.wallclock._now", self.uhr.wand)
        _zugang_ablegen(self.paths.device_dir)

    def _link(self, *args) -> Link:
        self.links.append(Link(*args))
        return self.links[-1]

    @property
    def link(self) -> Link:
        return self.links[-1]

    def _zeitdienst(self) -> None:
        """Der Zeitdienst des Hosts meldet die Uhr als abgeglichen (host/status.json mit Wanduhrzeit). Der echte Dienst
        (smartheat_host/hoststatus.py, PERIOD_SECONDS) schreibt alle 60 s und der Leser nimmt die Datei bis 5 min als
        frisch; die Welt schreibt nur, wenn seit dem letzten Schreiben ZEITDIENST_TAKT Wanduhrzeit vergangen sind (ein
        traeger Dienst; die Datei ist beim Lesen hoechstens so alt, und das bleibt unter der Frist des Lesers)."""
        jetzt = self.uhr.wand()
        if self._status_um is not None and (jetzt - self._status_um).total_seconds() < ZEITDIENST_TAKT:
            return
        status = {"netz": True, "dns": True, "zeit_synchron": True, "ts": jetzt.isoformat()}
        dateien.schreiben_json(self.host_dir / "status.json", status, modus=dateien.LESBAR)
        self._status_um = jetzt

    def starten(self) -> None:
        """Ein Prozessstart wie im Container: Spiegel, Geraete-Host und Geraetekern (_vorbereiten), Laufzeit (start)."""
        self._zeitdienst()
        spiegel = ZigbeeMirror(self.bus, self.uhr)
        spiegel.start()
        self.geraet, self.device = runtime_main._vorbereiten(self.paths, spiegel, self._geraete.append)
        self.device.schritt()  # baut den Link (der Zugang liegt vor)
        self.laufzeit, self.anlage = runtime_main.start(self.paths, self.bus, spiegel, self.geraet, self.device,
                                                        self.uhr, driver_threads=False)
        self.starts += 1
        self.z2m.melden(SENSOR, temperature=20.0, battery=90)  # live gemeldet: retained Werte gelten nicht als frisch

    def beenden(self) -> None:
        """Teardown: Hintergrund-Threads der Befehle aller Prozessstarts stoppen (kein Thread ueberlebt den Test)."""
        for geraet in self._geraete:
            geraet.hintergrund.stop(timeout=2.0)
        if self.diagnose_port is not None:
            self.diagnose_schliessen()

    def container_neu_starten(self) -> None:
        """Der Container startet neu (Compose): der Prozess endet sauber, ein neuer baut sich auf denselben Dateien auf."""
        prozess.beenden(self.device, None, None)
        if self.diagnose_port is not None:
            self.diagnose_schliessen()
        self.starten()

    def arbeiten(self) -> None:
        """Geraetekern und Worker abarbeiten; ein gewollter Neustart baut einen neuen Prozess wie Compose."""
        for _ in range(5):
            try:
                self._zeitdienst()
                self.device.schritt()
                self.laufzeit.worker.run_pending()
                self.device.schritt()
                return
            except prozess.Neustart:
                prozess.beenden(self.device, None, None)  # wie prozess.laufen: erst sauber trennen
                if self.diagnose_port is not None:
                    self.diagnose_schliessen()  # der Prozess endet, mit ihm der Server auf dem festen Port
                self.starten()
        raise AssertionError("Laufzeit startet staendig neu")

    def runde(self, sekunden: float = 0) -> None:
        """Die Zeit um `sekunden` vorstellen (Treiber fragen den Anlagenstand), dann arbeiten."""
        self.uhr.vor(sekunden)
        for host in self.anlage.hosts.values():
            if host.driver is not None:
                host.driver.poll_once()
        self.arbeiten()

