"""Fake-Bootstrap fuer die Docker-Pruefungen (signiert CSRs, nennt den Fake-Broker)."""
import json
import subprocess
import urllib.error
import urllib.request

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fake_bootstrap import FakeBootstrap

from smartheat_device import anker, bootstrap, identity, wire

NOW = 1_800_000_000


@pytest.fixture
def ca_dir(tmp_path):
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(tmp_path / "ca.key"), "-out", str(tmp_path / "ca.pem"), "-days", "2",
                    "-subj", "/CN=fake-ca"], check=True, capture_output=True)
    return tmp_path


@pytest.fixture
def anker_key(tmp_path, monkeypatch):
    """Eigener Testschluessel des Vertrauensankers; das Geraet liest seinen oeffentlichen Teil aus einer Datei in tmp_path
    (Override gilt nur mit Testkennung)."""
    key = ec.generate_private_key(ec.SECP256R1())
    datei = tmp_path / "anker-test.pem"
    datei.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                    serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setenv(anker.ENV_DATEI, str(datei))
    monkeypatch.setenv("SMARTHEAT_TESTKENNUNG", "gateway-tests")
    return key


@pytest.fixture
def fake(ca_dir, anker_key):
    server = FakeBootstrap(ca_dir, "fake-broker", anker_key=anker_key)
    server.start()
    yield server
    server.stop()


def _post(url: str, body: dict, *, ts: int | None = NOW) -> dict:
    headers = {"Content-Type": "application/json"}
    if ts is not None:
        headers[wire.HEADER_TIMESTAMP] = str(ts)
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def test_register_signs_the_csr_and_names_the_broker(fake, tmp_path, ca_dir):
    ident = identity.load_or_create(tmp_path / "device")
    body = identity.register_body(ident, version="0.6.0", host="gateway", capabilities={"drivers": [], "zigbee": True})
    answer = _post(f"{fake.url}/devices/register", body)
    assert set(answer) == set(wire.BOOTSTRAP_RESPONSE)  # genau der Vertrag, kein poll_after
    assert answer["device_state"] == "uebernommen" and answer["mqtt_endpoint"] == "fake-broker"
    assert answer["mqtt_port"] == 8883 and answer["mqtt_ca"] == (ca_dir / "ca.pem").read_text()
    cert = x509.load_pem_x509_certificate(answer["certificate"].encode())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == ident.device_id
    assert identity.passt_zum_schluessel(ident, answer["certificate"])
    assert fake.device_ids == [ident.device_id] and fake.registrations == 1
    assert anker.pruefen(answer, route="POST /devices/register", device_id=ident.device_id, ts=NOW)


def test_without_key_or_timestamp_the_answer_carries_no_anchor_fields(ca_dir, fake, tmp_path):
    ident = identity.load_or_create(tmp_path / "device")
    body = {"csr": identity.csr_pem(ident)}
    ohne_zeit = _post(f"{fake.url}/devices/register", body, ts=None)
    assert not set(anker.ANKER_FELDER) & set(ohne_zeit)
    ohne_schluessel = FakeBootstrap(ca_dir, "fake-broker")
    ohne_schluessel.start()
    try:
        antwort = _post(f"{ohne_schluessel.url}/devices/register", body)
    finally:
        ohne_schluessel.stop()
    assert not set(anker.ANKER_FELDER) & set(antwort)


def test_the_d1_bootstrap_client_accepts_the_answers(fake, tmp_path):
    ident = identity.load_or_create(tmp_path / "device")
    client = bootstrap.BootstrapClient(fake.url, ident)
    antwort = client.register(version="0.6.0", host="gateway", capabilities={"drivers": [], "zigbee": True})
    assert antwort.device_state == "uebernommen" and antwort.zugang.endpoint.host == "fake-broker"
    assert antwort.zugang.endpoint.port == 8883 and antwort.zugang.endpoint.alpn is None
    assert client.certificate().device_state == "uebernommen"
    assert (fake.registrations, fake.certificates) == (1, 1)


def test_certificate_route_and_controls(fake, tmp_path):
    ident = identity.load_or_create(tmp_path / "device")
    _post(f"{fake.url}/_e2e/device_state", {"device_state": "nicht_uebernommen"})
    answer = _post(f"{fake.url}/devices/{ident.device_id}/certificate", {"csr": identity.csr_pem(ident)})
    assert answer["device_state"] == "nicht_uebernommen"
    _post(f"{fake.url}/_e2e/files", {"0.9.1/manifest.json": "eyJ4IjogMX0="})
    with urllib.request.urlopen(f"{fake.url}/_e2e/files/0.9.1/manifest.json", timeout=5) as response:
        assert json.loads(response.read()) == {"x": 1}
    with urllib.request.urlopen(f"{fake.url}/_e2e/state", timeout=5) as response:
        assert json.loads(response.read())["certificates"] == 1


def test_a_request_without_csr_is_400(fake):
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(f"{fake.url}/devices/register", {"device_id": "x"})
    assert error.value.code == 400


def test_a_broken_csr_and_an_unknown_device_state_are_400(fake):
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(f"{fake.url}/devices/register", {"csr": "kein CSR"})
    assert error.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(f"{fake.url}/_e2e/device_state", {"device_state": "irgendwas"})
    assert error.value.code == 400
    assert fake.device_state == "uebernommen" and fake.registrations == 0


def test_the_certificate_route_refuses_a_csr_for_another_device(fake, tmp_path):
    ident = identity.load_or_create(tmp_path / "device")
    other = identity.load_or_create(tmp_path / "other")
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(f"{fake.url}/devices/{other.device_id}/certificate", {"csr": identity.csr_pem(ident)})
    assert error.value.code == 403
    assert fake.certificates == 0 and fake.device_ids == []
