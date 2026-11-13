"""Kundentexte des Gateways; Gegenstueck zu HA_TEXTS."""
from smartheat_runtime.texts import HostTexts

SHG_TEXTS = HostTexts(
    traeger="des Gateways",
    delivery_prefix="SmartHeat-Gateway",
    source_unavailable_log="Signalquelle nicht erreichbar",
)
