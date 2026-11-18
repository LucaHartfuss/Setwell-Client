"""SignalSource ueber die HA-REST-API: Referenzen sind Entity-IDs bzw. `entity::attribut`
(ha_api.get_state). Uebersetzt die requests-Fehler in die Fehler des Ports; ungueltige Werte bleiben ValueError/
KeyError/TypeError."""
from collections.abc import Callable

import requests

from smartheat_runtime.ports import SignalNotFound, SourceUnavailable, Stoerung
from smartheat_runtime.soll_speicher import TargetStore

#: Referenz des Raum-Solls in der Manifest-Rolle room_target: der Wert kommt aus dem Soll-Speicher, nicht aus HA.
SOLL_REF = "smartheat:raum_soll"
#: Die Stoerungsquelle der Anlage: Integration mypyllant, Attribut und Art eines Wartungshinweises.
HERSTELLER = "mypyllant"
DTC_ATTRIBUT = "diagnostic_trouble_codes"
DTC_WARTUNG = "MAINTENANCE"


class HaSignalSource:
    def __init__(self, ha_api) -> None:
        self._ha_api = ha_api

    def get_state(self, ref: str) -> float:
        return _translated(self._ha_api.get_state, ref)

    def get_raw_state(self, ref: str) -> str:
        return _translated(self._ha_api.get_raw_state, ref)

    def get_unit(self, ref: str) -> str:
        return _translated(lambda r: self._ha_api.get_attribute(r.partition("::")[0], "unit_of_measurement"), ref)

    def device_key(self, ref: str) -> str:
        return ref.partition("::")[0]

    def get_stoerungen(self, ref: str) -> tuple[Stoerung, ...]:
        """Stoerungen aus dem Attribut `diagnostic_trouble_codes` der Entity (mypyllant, Option `fetch_dtc`):
        `[{serial_number, article_number, codes: [{code, type, title, description, occurrence_timestamp}]}]`. Nur
        Kennung und Art werden gelesen, nie Titel oder Beschreibung; `type` MAINTENANCE ist ein Hinweis. Fehlt das
        Attribut oder hat es eine fremde Form, steht nichts an."""
        roh = _translated(lambda r: self._ha_api.get_attribute_roh(r.partition("::")[0], DTC_ATTRIBUT), ref)
        if not isinstance(roh, list):
            return ()
        gefunden: list[Stoerung] = []
        for anlage in roh:
            codes = anlage.get("codes") if isinstance(anlage, dict) else None
            for eintrag in codes if isinstance(codes, list) else ():
                kennung = eintrag.get("code") if isinstance(eintrag, dict) else None
                if isinstance(kennung, str) and kennung:
                    gefunden.append(Stoerung(HERSTELLER, kennung, eintrag.get("type") == DTC_WARTUNG))
        return tuple(gefunden)


class GeraetSignale:
    """SignalSource des Geraete-Pfads: der Raum-Soll (SOLL_REF) kommt aus dem Soll-Speicher, alles
    andere aus HA. store_getter liefert None, solange der Host nicht geladen hat."""

    def __init__(self, ha_signals: HaSignalSource, store_getter: Callable[[], TargetStore | None]) -> None:
        self._ha, self._store = ha_signals, store_getter

    def get_state(self, ref: str) -> float:
        if ref == SOLL_REF:
            store = self._store()
            if store is None:
                raise SourceUnavailable("Soll-Speicher noch nicht geladen")
            return store.value
        return self._ha.get_state(ref)

    def get_raw_state(self, ref: str) -> str:
        return str(self.get_state(ref)) if ref == SOLL_REF else self._ha.get_raw_state(ref)

    def get_unit(self, ref: str) -> str:
        if ref == SOLL_REF:
            raise ValueError("keine Einheit")
        return self._ha.get_unit(ref)

    def device_key(self, ref: str) -> str:
        return ref if ref == SOLL_REF else self._ha.device_key(ref)

    def get_stoerungen(self, ref: str) -> tuple[Stoerung, ...]:
        return self._ha.get_stoerungen(ref)


def _translated(read, ref: str):
    try:
        return read(ref)
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            raise SignalNotFound(ref) from error
        raise SourceUnavailable(str(error)) from error
    except requests.RequestException as error:
        raise SourceUnavailable(str(error)) from error
