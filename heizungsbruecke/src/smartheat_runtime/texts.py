"""Texte, die den Host nennen: Absender und Traeger fuer das Rendern einer Meldung, dazu ein Log-Text. Standardwerte =
die Texte des HA-Add-ons; das Gateway liefert eigene ueber Host.texts. Importiert aus dem Paket nichts, nur den
Kern (Meldung -> Text)."""
from dataclasses import dataclass

from smartheat_core import meldungen
from smartheat_core.meldungen import Meldung


@dataclass(frozen=True)
class HostTexts:
    # Traeger des Datentraegers in der Meldung "Datentraeger nicht beschreibbar".
    traeger: str = meldungen.STANDARD_TRAEGER
    # Absender der Meldungen, die den Host nennen (Notbetrieb, Datenfehler, ...).
    delivery_prefix: str = meldungen.STANDARD_PRAEFIX
    # Log, wenn die Signalquelle nicht antwortet (battery, room_sensors).
    source_unavailable_log: str = "Home Assistant nicht erreichbar"

    def text(self, meldung: Meldung) -> str:
        """Kundentext einer Meldung fuer diesen Host."""
        return meldungen.text(meldung, praefix=self.delivery_prefix, traeger=self.traeger)


HA_TEXTS = HostTexts()
