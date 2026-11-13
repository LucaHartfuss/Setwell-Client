"""Handgriffe an der Welt: was der Server und der Bewohner tun und was von aussen zu sehen ist (Link, Datei der
simulierten Anlage). Gemischt in `welt.Welt` ein (braucht `link`, `links`, `uhr`, `paths`, `arbeiten`)."""
import json

from anlagen import LEERE_KONFIGURATION, konfiguration


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

    def wuensche(self) -> list[dict]:
        """Alle Wuensche (up/wish), die ein Link dieser Welt gesendet hat."""
        return [inhalt for link in self.links for inhalt in link.von("up/wish")]

    def anlagenstand(self) -> dict:
        """Der Stand der simulierten Anlage (Datei des Treibers, schreibt nur die Laufzeit)."""
        return json.loads((self.paths.sim_dir / "plant.json").read_text())
