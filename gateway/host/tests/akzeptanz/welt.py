"""Schmale Welt des Gateway-Hosts fuer die Akzeptanzsuite: ein Wurzelordner (SHG_ROOT) in tmp_path mit `updater/state.json`,
der echte `bundles.ComposeRunner` mit einer `subprocess.run`-Attrappe (sie antwortet auf `docker compose ... ps` mit dem
eingestellten Health und merkt sich jeden Aufruf), die Uhr als Zahl und der Container als Attrappe, die nur ueber Dateien
antwortet (`data/sim/plant.json`, `data/werksreset_bereit.json`). Beobachtet werden Aufrufprotokolle und Dateien, nie
Zustaende oder Stores der Host-Module. Kein Import aus tests/ oder testhilfen/."""
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from smartheat_host import bundles, waechter, werksreset

VERSION = "0.3.0"
NEUE_VERSION = "0.4.0"
MONOTON_START = 1000.0
WAND_START = 1_800_000_000.0
#: Hebelwerte der Anlage vor dem Abmelden (Server) und die Ursprungswerte, die der Container beim Werksreset zurueckstellt
SERVERWERTE = {"heizkurve": 1.8, "vorlauf_min": 28.0}
URSPRUNGSWERTE = {"heizkurve": 1.2, "vorlauf_min": 20.0}
#: was ein Werksreset loescht bzw. stehen laesst (relativ zum Wurzelordner)
KUNDENDATEN = ("data/device/identitaet.json", "data/device/sicherheitssatz.json", "data/secrets/drivers/vicare.json",
               "bus/credentials/runtime/bus.json", "bus/mosquitto/passwd", "zigbee2mqtt/configuration.yaml",
               "host/knopf.json")
BLEIBT = ("host/werk.json", "host/status.json", "bundles/0.3.0/docker-compose.yml", "archive/image.tar")


class Stromausfall(BaseException):
    """Der Strom ist weg: der laufende Host-Prozess tut nichts mehr."""


class Docker:
    """`subprocess.run` des Hosts. `ps` liefert den Health von `runtime`; alle Aufrufe stehen in `aufrufe`."""

    def __init__(self) -> None:
        self.health = "healthy"
        self.aufrufe: list[list[str]] = []
        self.beim_aufruf = lambda befehl: None

    def __call__(self, argv, *, capture_output, text, check, timeout):
        self.aufrufe.append(list(argv))
        befehl = list(argv[8:])  # docker compose -p <projekt> --env-file <datei> -f <datei> <befehl...>
        self.beim_aufruf(befehl)
        ausgabe = json.dumps({"Service": "runtime", "Health": self.health}) if befehl[:1] == ["ps"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=ausgabe, stderr="")


class Welt:
    def __init__(self, tmp_path: Path, ordner: str = "geraet") -> None:
        self.root = tmp_path / ordner
        self.tmp_path = tmp_path
        self.docker = Docker()
        self.compose = bundles.ComposeRunner(self.root, run=self.docker, env_datei=tmp_path / "gateway.env")
        self.monoton = MONOTON_START
        self.wand = WAND_START
        self.zustand: dict = {}
        self.neustart_zeiten: list[float] = []
        self.fristen: list[float] = []
        self.ablauf: list[str] = []
        self.beim_down: dict = {}
        self.nachher: Welt | None = None
        self.antwortzeit: float | None = None
        self.strom_weg = False
        self.docker.beim_aufruf = self._beim_compose
        if not self.root.exists():
            self._anlegen()

    # --- Dateien ---

    def _schreiben(self, rel: str, inhalt: str) -> None:
        pfad = self.root / rel
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text(inhalt)

    def _anlegen(self) -> None:
        self.state()
        self._schreiben("data/sim/plant.json", json.dumps(SERVERWERTE))
        for rel in KUNDENDATEN:
            self._schreiben(rel, "kunde")
        for rel in BLEIBT:
            self._schreiben(rel, "bleibt")

    def state(self, **mehr) -> None:
        """`updater/state.json` neu schreiben (laufende Version, dazu Auftrag oder Systemwechsel)."""
        self._schreiben("updater/state.json", json.dumps({"current": VERSION, **mehr}))

    def pause_setzen(self, art: str) -> None:
        """Ein Grund, aus dem der Waechter nichts neu starten darf: Phase des Systemwechsels, Updater-Auftrag, Werksreset."""
        if art == "auftrag":
            self.state(in_progress={"version": NEUE_VERSION})
        elif art == "werksreset":
            self._schreiben("host/werksreset.json", "{}")
        else:
            self.state(system={"version": NEUE_VERSION, "phase": art, "slot_alt": "A"})

    def pause_aufheben(self) -> None:
        self.state()
        (self.root / "host" / "werksreset.json").unlink(missing_ok=True)

    def baum(self) -> dict[str, str]:
        return {str(p.relative_to(self.root)): p.read_text() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def datei(self, rel: str) -> str | None:
        pfad = self.root / rel
        return pfad.read_text() if pfad.is_file() else None

    def anfrage_liegt(self) -> bool:
        return (self.root / "host" / "werksreset.json").exists()

    def protokoll(self) -> list[dict]:
        text = self.datei("host/werksreset_protokoll.json")
        return json.loads(text) if text else []

    # --- Waechter ---

    def takt(self, health: str, anzahl: int = 1) -> None:
        """Ein Takt des Waechters je 30 s: Health einstellen, `schritt` aufrufen."""
        self.docker.health = health
        for _ in range(anzahl):
            vorher = len(self.neustarts())
            self.zustand = waechter.schritt(self.root, self.compose, self.zustand, self.monoton)
            self.neustart_zeiten += [self.monoton] * (len(self.neustarts()) - vorher)
            self.monoton += 30.0

    def neustarts(self) -> list[list[str]]:
        return [aufruf for aufruf in self.docker.aufrufe if aufruf[8:] == ["restart", "runtime"]]

    # --- Werksreset ---

    def _beim_compose(self, befehl: list[str]) -> None:
        if befehl[:1] == ["down"]:
            self.ablauf.append("down")
            self.beim_down = {"plant": self.datei("data/sim/plant.json"), "dateien": set(self.baum())}

    def warte(self, bedingung, frist):
        """Fristablauf ueber die Uhr der Welt, kein echtes Warten; der Container antwortet nach `antwortzeit` s."""
        self.fristen.append(frist)
        anfrage = json.loads((self.root / "host" / "werksreset.json").read_text())
        if self.strom_weg:
            shutil.copytree(self.root, self.tmp_path / "nach_stromausfall")
            raise Stromausfall
        if self.antwortzeit is not None and self.antwortzeit <= frist:
            self.wand += self.antwortzeit
            self._schreiben("data/sim/plant.json", json.dumps(URSPRUNGSWERTE))
            ts = (datetime.fromisoformat(anfrage["ts"]) + timedelta(seconds=self.antwortzeit)).isoformat()
            self._schreiben("data/werksreset_bereit.json",
                            json.dumps({"seq": anfrage["seq"], "ts": ts, "ursprungswerte": True}))
        else:
            self.wand += frist
        return bedingung()

    def _journal_leeren(self) -> None:
        self.ablauf.append("journal")

    def _neustart(self) -> None:
        self.ablauf.append("neustart")

    def werksreset(self, antwortzeit: float | None = None, strom_weg: bool = False) -> dict | None:
        """Der Host fuehrt den Werksreset aus; der Container antwortet nach `antwortzeit` Sekunden (None: nie). Bei
        `strom_weg` bricht alles in Phase `angefordert` ab; der Stand der Platte dann steht unter `nach_stromausfall`."""
        self.antwortzeit, self.strom_weg = antwortzeit, strom_weg
        try:
            return werksreset.ausfuehren(self.root, self.compose, warte=self.warte,
                                         jetzt=lambda: self.wand, neustart=self._neustart,
                                         journal_leeren=self._journal_leeren)
        except Stromausfall:
            return None

    def start_nach_stromausfall(self) -> bool:
        """Neue Welt auf den Dateien vom Stromausfall; der Erststart fuehrt vor `compose up` einen Reset zu Ende bzw. verwirft ihn."""
        neu = Welt(self.tmp_path, "nach_stromausfall")
        ergebnis = werksreset.beim_start(neu.root, neu.compose, jetzt=lambda: self.wand, journal_leeren=neu._journal_leeren)
        self.nachher = neu
        return ergebnis
