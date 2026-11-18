"""Handgriffe an der Welt: was der Server und der Bewohner tun und was von aussen zu sehen ist (Link, Datei der
simulierten Anlage). Gemischt in `welt.Welt` ein (braucht `link`, `links`, `uhr`, `paths`, `arbeiten`)."""
import datetime
import json

from anlagen import LEERE_KONFIGURATION, SENSOR, THERMOSTAT, konfiguration


class Handgriffe:
    def verbinden(self) -> None:
        self.link.verbinden()
        self.arbeiten()

    def vom_server(self, name: str, inhalt: dict) -> None:
        self.link.empfangen(name, inhalt)
        self.arbeiten()

    def einrichten(self, version: int = 1, dokument: dict | None = None) -> None:
        """Konfiguration des Servers (Standard: ein Kreis, Simulationstreiber): Neustart in den Betrieb, verbinden."""
        self.konfiguration_senden(version, dokument if dokument is not None else konfiguration())
        self.verbinden()

    def konfiguration_senden(self, version: int, dokument: dict) -> None:
        """Der Server sendet eine Konfiguration (down/config); ein Neustart, den sie ausloest, laeuft mit."""
        self.vom_server("down/config", {"schema": 2, "version": version, **dokument})

    def abmelden(self, version: int) -> None:
        """Der Server meldet die Anlage ab: leere Konfiguration (die Laufzeit stellt zurueck und startet neu)."""
        self.konfiguration_senden(version, LEERE_KONFIGURATION)

    def ergebnis(self, version: int) -> dict:
        """Das Ergebnis (up/result) zur Konfiguration `version`: ok und, wenn abgelehnt, Grund und Feld."""
        treffer = [r for link in self.links for r in link.von("up/result")
                   if r["dokument"] == "konfiguration" and r["version"] == version]
        assert treffer, f"kein Ergebnis zur Konfiguration {version}"
        fehler = treffer[-1].get("error") or {}
        return {"ok": treffer[-1]["ok"], "grund": fehler.get("grund"), "feld": (fehler.get("details") or {}).get("feld")}

    def antworten(self, hebel: dict) -> None:
        """Die Antwort des Dienstes auf den letzten Tick (down/setpoints, seq aus dem letzten Snapshot)."""
        seq = self.link.von("up/snapshot")[-1]["seq"]
        self.vom_server("down/setpoints", {"schema": 5, "kreis": 1, "seq": seq, "ts": self.uhr.wand().isoformat(),
                                           "status": "ok", "reason": None, "levers": hebel, "learned": {}})

    def vergehen(self, sekunden: float, hebel: dict, raum: float, schritt: float = 300) -> None:
        """Die Zeit in Schritten vorstellen; nach jedem Schritt meldet der Raumfuehler `raum` und der Dienst antwortet
        mit denselben Hebelwerten (beide schweigen nie, sonst pausiert die Regelung bzw. folgt der Notbetrieb)."""
        for _ in range(round(sekunden / schritt)):
            self.raum(raum)
            self.runde(schritt)
            self.antworten(hebel)

    def bedienung(self, version: int, raum_soll: float) -> None:
        """Der Bewohner stellt den Wunsch ueber die Oberflaeche des Dienstes (down/operation)."""
        self.vom_server("down/operation", {"schema": 2, "version": version, "quelle": "portal",
                                           "ts": self.uhr.wand().isoformat(),
                                           "kreise": [{"kreis": 1, "modus": "manuell", "raum_soll": raum_soll}]})

    def befehl(self, command_id: str, kind: str) -> None:
        """Der Server sendet einen Befehl ohne Nutzlast (down/command)."""
        frist = (self.uhr.wand() + datetime.timedelta(hours=12)).isoformat()
        self.vom_server("down/command", {"schema": 2, "command_id": command_id, "kind": kind, "payload": {},
                                         "expires_at": frist})

    def raum(self, temperatur: float) -> None:
        """Der Raumfuehler meldet die Raumtemperatur."""
        self.z2m.melden(SENSOR, temperature=temperatur, battery=90)

    def bis_nach_tagestick(self, temperatur: float) -> None:
        """Die Uhr steht danach nach dem Tagestick (12:00) des Tages; der Raumfuehler meldet bis dahin laufend."""
        while self.uhr.wand().hour < 12:
            self.raum(temperatur)
            self.runde(1800)

    def neuer_tag_mit_tick(self, temperatur: float) -> str:
        """Genau ein Tag vergeht (Tagesbudget und Schreibabstaende gelten wieder), der Raumfuehler meldet, der Tagestick
        sendet einen Snapshot: dessen Folgenummer. Setzt voraus, dass die Uhr nach 12:00 steht (bis_nach_tagestick)."""
        self.uhr.vor(86400 - 300 - 2)  # der Raumfuehler ist danach veraltet; ohne Arbeit, nichts Fachliches faellt an
        self.raum(temperatur)
        self.runde(300)
        return self.link.von("up/snapshot")[-1]["seq"]

    def thermostat_dreht(self, wert: float) -> None:
        """Der Bewohner dreht am Thermostat: es meldet den neuen Soll (ein Wunsch, kein Schreiben der Laufzeit)."""
        self.z2m.melden(THERMOSTAT, occupied_heating_setpoint=wert, local_temperature=23.5)

    def app_verstellt(self, hebel: str, wert: float) -> None:
        """Die Hersteller-App verstellt einen Hebel der Anlage (Datei der simulierten Anlage)."""
        datei = self.paths.sim_dir / "plant.json"
        stand = json.loads(datei.read_text())
        stand["hebel"][hebel] = wert
        datei.write_text(json.dumps(stand))

    def status(self) -> list[str]:
        """Der Laufzeitstatus jeder Statusmeldung (up/status), die ein Link dieser Welt gesendet hat, in der Reihenfolge."""
        return [inhalt["runtime_status"]["status"] for link in self.links for inhalt in link.von("up/status")
                if inhalt["runtime_status"]]

    def letzter_status(self) -> dict:
        """Die juengste Statusmeldung (up/status) ueber alle Links dieser Welt."""
        return [inhalt for link in self.links for inhalt in link.von("up/status")][-1]

    def befehlsergebnis(self, command_id: str) -> dict:
        """Das Ergebnis (up/result) des Befehls `command_id`."""
        treffer = [r for link in self.links for r in link.von("up/result") if r.get("command_id") == command_id]
        assert treffer, f"kein Ergebnis zum Befehl {command_id}"
        return treffer[-1]

    def meldungen(self) -> list[dict]:
        """Alle Meldungen (Eintraege von up/notifications), die ein Link dieser Welt gesendet hat."""
        return [eintrag for link in self.links for inhalt in link.von("up/notifications") for eintrag in inhalt["items"]]

    def wuensche(self) -> list[dict]:
        """Alle Wuensche (up/wish), die ein Link dieser Welt gesendet hat."""
        return [inhalt for link in self.links for inhalt in link.von("up/wish")]

    def anlagenstand(self) -> dict:
        """Der Stand der simulierten Anlage (Datei des Treibers, schreibt nur die Laufzeit)."""
        return json.loads((self.paths.sim_dir / "plant.json").read_text())
