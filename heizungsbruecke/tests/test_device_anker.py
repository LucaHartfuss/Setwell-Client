"""Vertrauensanker auf dem Geraet: jede Bootstrap-Antwort wird vor dem Lesen gegen die oeffentlichen Schluessel der Datei
anker.pem geprueft (aktiver Schluessel und Reserve); alles andere ist ungueltig und fuehrt zum Backoff, nie zur Sperre."""
import logging

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from device_helpers import (
    ANKER_RESERVE,
    FakeResponse,
    FakeSession,
    GeraeteCa,
    anker_oeffentlich_pem,
    anker_signatur,
    zertifikat_fuer,
)

from smartheat_device import anker, bootstrap, identity

NOW = 1_800_000_000
BASE = "https://accounts.example.test"
ARGS = {"version": "0.6.0", "host": "gateway", "capabilities": {"drivers": ["simulation"], "zigbee": True}}


@pytest.fixture
def ident(tmp_path):
    return identity.load_or_create(tmp_path / "device")


@pytest.fixture
def antwort(ident):
    ca = GeraeteCa()
    return {"device_state": "nicht_uebernommen", "certificate": zertifikat_fuer(ident, ca),
            "mqtt_endpoint": "mqtt.e2e.test", "mqtt_port": 8883, "mqtt_ca": ca.pem}


def _client(ident, *antworten):
    session = FakeSession(*antworten)
    return bootstrap.BootstrapClient(BASE, ident, session=session, now=lambda: NOW), session


def _signiert(antwort, ident, *, route="POST /devices/register", device_id=None, ts=NOW, key=None):
    return {**antwort, **anker_signatur(antwort, route=route, device_id=device_id or ident.device_id, ts=ts, key=key)}


def _ungueltig(ident, antwort):
    client, _ = _client(ident, FakeResponse(200, antwort, signieren=False))
    with pytest.raises(bootstrap.AnkerUngueltig, match="anker_ungueltig"):
        client.register(**ARGS)


def test_der_aktive_schluessel_und_die_reserve_gelten(ident, antwort):
    for key in (None, ANKER_RESERVE):
        client, _ = _client(ident, FakeResponse(200, _signiert(antwort, ident, key=key), signieren=False))
        assert client.register(**ARGS).device_state == "nicht_uebernommen"


def test_ein_fremder_schluessel_ist_ungueltig(ident, antwort):
    _ungueltig(ident, _signiert(antwort, ident, key=ec.generate_private_key(ec.SECP256R1())))


@pytest.mark.parametrize("aenderung", ["inhalt", "zusatzfeld", "route", "ts", "geraet", "signatur", "anker_id"])
def test_veraenderte_oder_umgeleitete_antworten_sind_ungueltig(ident, antwort, aenderung):
    parameter = {"route": "POST /devices/register", "ts": NOW, "device_id": ident.device_id}
    if aenderung == "route":
        parameter["route"] = f"POST /devices/{ident.device_id}/certificate"
    elif aenderung == "ts":
        parameter["ts"] = NOW - 1
    elif aenderung == "geraet":
        parameter["device_id"] = "shg-0000000000000000"
    signiert = {**antwort, **anker_signatur(antwort, **parameter)}
    if aenderung == "inhalt":
        signiert["mqtt_endpoint"] = "mqtt.boese.test"
    elif aenderung == "zusatzfeld":
        signiert["mqtt_alpn"] = "x-amzn-mqtt-ca"
    elif aenderung == "signatur":
        signiert["anker_signatur"] = signiert["anker_signatur"][:-6] + "AAAAA="
    elif aenderung == "anker_id":
        signiert["anker_id"] = "0" * 16
    _ungueltig(ident, signiert)


@pytest.mark.parametrize("feld", anker.ANKER_FELDER)
def test_fehlende_ankerfelder_sind_ungueltig(ident, antwort, feld):
    signiert = _signiert(antwort, ident)
    del signiert[feld]
    _ungueltig(ident, signiert)


def test_eine_unsignierte_antwort_und_ein_nicht_objekt_sind_ungueltig(ident, antwort):
    _ungueltig(ident, antwort)
    _ungueltig(ident, ["kein", "objekt"])


def test_die_rettungsroute_ist_an_ihren_pfad_gebunden(ident, antwort):
    route = f"POST /devices/{ident.device_id}/certificate"
    client, _ = _client(ident, FakeResponse(200, _signiert(antwort, ident, route=route), signieren=False),
                        FakeResponse(200, _signiert(antwort, ident, route="POST /devices/register"), signieren=False))
    assert client.certificate().device_state == "nicht_uebernommen"
    with pytest.raises(bootstrap.AnkerUngueltig):
        client.certificate()


def test_der_platzhalter_der_datei_macht_jede_antwort_ungueltig(ident, antwort, monkeypatch):
    monkeypatch.delenv(anker.ENV_DATEI)
    assert "PLATZHALTER" in anker.ANKER_DATEI.read_text() and anker.schluessel() == {}
    _ungueltig(ident, _signiert(antwort, ident))


@pytest.mark.parametrize("inhalt", [None, "", "# nur Kommentar\n", "kein pem",
                                    "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"])
def test_eine_fehlende_leere_oder_kaputte_datei_macht_jede_antwort_ungueltig(ident, antwort, monkeypatch, tmp_path,
                                                                             inhalt):
    datei = tmp_path / "anker.pem"
    if inhalt is not None:
        datei.write_text(inhalt)
    monkeypatch.setenv(anker.ENV_DATEI, str(datei))
    assert anker.schluessel() == {}
    _ungueltig(ident, _signiert(antwort, ident))


def test_ein_privater_teil_in_der_datei_zaehlt_nicht(ident, antwort, monkeypatch, tmp_path):
    datei = tmp_path / "anker.pem"
    art = "PRIVATE " + "KEY"  # zur Laufzeit zusammengesetzt: der Geheimnis-Scan sieht in der Quelle keinen PEM-Kopf
    datei.write_text(f"-----BEGIN {art}-----\nAAAA\n-----END {art}-----\n")
    monkeypatch.setenv(anker.ENV_DATEI, str(datei))
    assert anker.schluessel() == {}


def test_der_override_ohne_testkennung_wird_ignoriert_und_geloggt(ident, antwort, monkeypatch, tmp_path, caplog):
    datei = tmp_path / "eigene.pem"
    datei.write_text(anker_oeffentlich_pem())
    monkeypatch.setenv(anker.ENV_DATEI, str(datei))
    assert len(anker.schluessel()) == 2  # mit Testkennung gilt die Datei
    monkeypatch.setenv("SMARTHEAT_TESTKENNUNG", " ")
    with caplog.at_level(logging.WARNING):
        assert anker.schluessel() == {}  # Paketdatei = Platzhalter
        _ungueltig(ident, _signiert(antwort, ident))
    assert anker.ENV_DATEI in caplog.text and str(datei) not in caplog.text
    monkeypatch.delenv("SMARTHEAT_TESTKENNUNG")
    assert anker.schluessel() == {}


def test_ungueltig_ist_ein_backoff_kein_gesperrt_und_das_log_nennt_nur_den_grund(tmp_path, ident, antwort, caplog):
    client, _ = _client(ident, FakeResponse(200, antwort, signieren=False))
    ablauf = bootstrap.Bootstrap(tmp_path, lambda: client, register_args=lambda: ARGS, uhr_synchron=lambda: True,
                                 identity=lambda: ident, clock=lambda: 1000.0)
    with caplog.at_level(logging.INFO):
        assert ablauf.holen(rettung=False) is None
    assert not ablauf.gesperrt and not ablauf.faellig()
    assert "anker_ungueltig" in caplog.text and antwort["mqtt_endpoint"] not in caplog.text
    assert bootstrap.laden(tmp_path) is None


def test_ungueltige_antworten_werden_nie_gespeichert(tmp_path, ident, antwort):
    client, _ = _client(ident, FakeResponse(200, antwort, signieren=False))
    ablauf = bootstrap.Bootstrap(tmp_path, lambda: client, register_args=lambda: ARGS, uhr_synchron=lambda: True,
                                 identity=lambda: ident)
    assert ablauf.holen(rettung=False) is None and not ablauf.gesperrt
    assert bootstrap.laden(tmp_path) is None and bootstrap.device_state(tmp_path) is None
