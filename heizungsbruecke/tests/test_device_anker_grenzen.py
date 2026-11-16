"""Grenzen des Ankers auf dem Geraet: Groessenlimit der Datei genau an der Kante, nur P-256-Schluessel zaehlen, jeder
Ablehnungszweig von `pruefen` liefert genau falsch (nie eine Ausnahme oder `None`)."""
import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from device_helpers import ANKER_AKTIV, anker_oeffentlich_pem, anker_signatur

from smartheat_device import anker

NOW = 1_800_000_000
ROUTE = "POST /devices/register"
GERAET = "shg-aaaaaaaaaaaaaaaa"
GRENZE = 16384
ANTWORT = {"device_state": "nicht_uebernommen", "certificate": "PEM", "mqtt_endpoint": "mqtt.e2e.test",
           "mqtt_port": 8883, "mqtt_ca": "CA"}


def _oeffentlich(key) -> str:
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def _datei(monkeypatch, tmp_path, inhalt: bytes):
    datei = tmp_path / "anker.pem"
    datei.write_bytes(inhalt)
    monkeypatch.setenv(anker.ENV_DATEI, str(datei))


def _signiert():
    return {**ANTWORT, **anker_signatur(ANTWORT, route=ROUTE, device_id=GERAET, ts=NOW)}


def _pruefen(antwort):
    return anker.pruefen(antwort, route=ROUTE, device_id=GERAET, ts=NOW)


def _mit_laenge(laenge: int) -> bytes:
    pem = _oeffentlich(ANKER_AKTIV).encode()
    inhalt = pem + b"\n" * (laenge - len(pem))
    assert len(inhalt) == laenge
    return inhalt


def test_eine_datei_genau_an_der_grenze_wird_gelesen(monkeypatch, tmp_path):
    _datei(monkeypatch, tmp_path, _mit_laenge(GRENZE))

    assert list(anker.schluessel()) == [anker._anker_id(ANKER_AKTIV.public_key())]


def test_eine_datei_ein_byte_ueber_der_grenze_zaehlt_nicht(monkeypatch, tmp_path):
    _datei(monkeypatch, tmp_path, _mit_laenge(GRENZE + 1))

    assert anker.schluessel() == {}
    assert _pruefen(_signiert()) is False


def test_nur_p256_schluessel_zaehlen(monkeypatch, tmp_path):
    fremd = ec.generate_private_key(ec.SECP384R1())
    rsa_key = rsa.generate_private_key(65537, 2048)
    _datei(monkeypatch, tmp_path, (_oeffentlich(fremd) + _oeffentlich(rsa_key)).encode())

    assert anker.schluessel() == {}


def test_fremde_schluessel_neben_dem_p256_schluessel_stoeren_nicht(monkeypatch, tmp_path):
    fremd = ec.generate_private_key(ec.SECP384R1())
    rsa_key = rsa.generate_private_key(65537, 2048)
    _datei(monkeypatch, tmp_path, (_oeffentlich(fremd) + _oeffentlich(rsa_key) + anker_oeffentlich_pem()).encode())

    assert len(anker.schluessel()) == 2


@pytest.mark.parametrize("kennung, signatur", [("ok", 2), (1, "ok"), (1, 2), (None, None), (["x"], "ok"),
                                               ({"a": 1}, "ok")])
def test_ein_falscher_typ_nur_eines_der_beiden_felder_ist_ungueltig(kennung, signatur):
    antwort = _signiert()
    kennung = antwort["anker_id"] if kennung == "ok" else kennung
    signatur = antwort["anker_signatur"] if signatur == "ok" else signatur

    assert _pruefen({**antwort, "anker_id": kennung, "anker_signatur": signatur}) is False


def test_jeder_ablehnungszweig_liefert_genau_falsch():
    antwort = _signiert()
    rohe = bytearray(base64.b64decode(antwort["anker_signatur"]))
    rohe[-1] ^= 1

    assert _pruefen(["kein", "objekt"]) is False
    assert _pruefen(ANTWORT) is False
    assert _pruefen({**antwort, "anker_id": "0" * 16}) is False
    assert _pruefen({**antwort, "anker_signatur": base64.b64encode(bytes(rohe)).decode()}) is False
    assert _pruefen({**antwort, "anker_signatur": "kein base64!"}) is False
    assert _pruefen(antwort) is True


def test_die_signatur_muss_streng_base64_sein():
    """Fremde Zeichen mitten in der Signatur duerfen nicht stillschweigend verworfen werden."""
    antwort = _signiert()
    signatur = antwort["anker_signatur"]
    for gestoert in (signatur[:4] + "!" + signatur[4:], signatur[:4] + "\n" + signatur[4:], signatur + "!"):
        assert _pruefen({**antwort, "anker_signatur": gestoert}) is False
