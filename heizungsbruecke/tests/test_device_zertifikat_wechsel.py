"""Zertifikatswechsel auf Anforderung (Befehl zertifikat_erneuern): neues Paar neben dem alten, MQTT-Anmeldung damit,
erst dann ersetzt es das alte und das Ergebnis traegt den Abdruck; sonst bleibt das alte Paar. Nie Schluessel oder PEM im Log."""
import hashlib
import logging

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from device_helpers import World

from smartheat_device import bootstrap, device, identity, nachrichten

NEU = bootstrap.NEU_SUFFIX
SCHLUESSEL = identity.TLS_KEY_FILE
ZERTIFIKAT = bootstrap.CERTIFICATE_FILE


def _befehl(command_id="w1"):
    return {"schema": 2, "command_id": command_id, "kind": "zertifikat_erneuern", "payload": {},
            "expires_at": "2027-01-15T09:00:00+00:00"}


def _abdruck(pem: str) -> str:
    der = x509.load_pem_x509_certificate(pem.encode()).public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def _welt(tmp_path) -> World:
    world = World(tmp_path)
    world.verbinden()
    world.link.sent.clear()
    return world


def _neue_antwort(world: World):
    def antwort(ident):
        return bootstrap.Antwort("uebernommen", world.neuer_zugang(ident))
    return antwort


def _anfordern(world: World, command_id="w1"):
    """Befehl zustellen; der Server (Attrappe) liefert ein Zertifikat fuer den neuen Schluessel."""
    world.client.script["certificate"] = [_neue_antwort(world)]
    world.empfangen("down/command", _befehl(command_id))


def _ergebnis(link):
    return link.of("up/result")[-1]


def test_der_wechsel_gelingt_und_meldet_den_abdruck(tmp_path):
    world = _welt(tmp_path)
    alt_schluessel = (world.dir / SCHLUESSEL).read_text()
    alt_zertifikat = (world.dir / ZERTIFIKAT).read_text()
    alter_link = world.link

    _anfordern(world)

    assert world.client.calls[-1] == ("certificate", {"erneuerung": True})
    assert alter_link.stopped and world.link is not alter_link and world.link.started
    # Solange die Anmeldung mit dem neuen Paar aussteht, gelten die alten Dateien; das neue liegt daneben.
    assert (world.dir / SCHLUESSEL).read_text() == alt_schluessel
    assert (world.dir / ZERTIFIKAT).read_text() == alt_zertifikat
    assert (world.dir / (SCHLUESSEL + NEU)).exists() and (world.dir / (ZERTIFIKAT + NEU)).exists()
    assert world.link.key_pem == (world.dir / (SCHLUESSEL + NEU)).read_text() != alt_schluessel
    assert world.link.of("up/result") == []

    world.link.verbinden()
    world.device.schritt()

    neu_zertifikat = (world.dir / ZERTIFIKAT).read_text()
    assert neu_zertifikat != alt_zertifikat and (world.dir / SCHLUESSEL).read_text() == world.link.key_pem
    assert not (world.dir / (SCHLUESSEL + NEU)).exists() and not (world.dir / (ZERTIFIKAT + NEU)).exists()
    ergebnis = _ergebnis(world.link)
    assert ergebnis["ok"] is True and ergebnis["result"] == {"fingerprint": _abdruck(neu_zertifikat)}
    assert world.device.identity.tls_key.public_key().public_numbers() == \
        identity.load_or_create(world.dir).tls_key.public_key().public_numbers()


def test_ein_verbindungsfehler_belaesst_das_alte_paar(tmp_path):
    world = _welt(tmp_path)
    alt_schluessel = (world.dir / SCHLUESSEL).read_text()
    alt_zertifikat = (world.dir / ZERTIFIKAT).read_text()
    _anfordern(world)
    neuer_link = world.link
    neuer_link.connect_failures = 1  # die Anmeldung mit dem neuen Zertifikat scheitert

    world.device.schritt()

    assert neuer_link.stopped
    assert (world.dir / SCHLUESSEL).read_text() == alt_schluessel
    assert (world.dir / ZERTIFIKAT).read_text() == alt_zertifikat
    assert not (world.dir / (SCHLUESSEL + NEU)).exists() and not (world.dir / (ZERTIFIKAT + NEU)).exists()
    # Der Takt baut den Link mit dem alten Paar neu auf; der Server stellt den offenen Befehl nach der Anmeldung
    # erneut zu, das Geraet meldet dann das gespeicherte Ergebnis "gescheitert".
    world.device.schritt()
    assert world.link is not neuer_link and world.link.key_pem == alt_schluessel
    world.link.verbinden()
    world.device.schritt()
    world.empfangen("down/command", _befehl())
    ergebnis = _ergebnis(world.link)
    assert ergebnis["ok"] is False and ergebnis["error"] == {"grund": "intern"} and ergebnis["result"] is None


def test_eine_abgelaufene_frist_belaesst_das_alte_paar(tmp_path):
    world = _welt(tmp_path)
    _anfordern(world)
    neuer_link = world.link
    world.clock.now += device.WECHSEL_SEKUNDEN + 1  # die neue Verbindung kommt nie zustande

    world.device.schritt()

    assert neuer_link.stopped
    assert not (world.dir / (SCHLUESSEL + NEU)).exists() and not (world.dir / (ZERTIFIKAT + NEU)).exists()


def test_der_server_lehnt_ab_dann_bleibt_alles_wie_es_war(tmp_path):
    world = _welt(tmp_path)
    alter_link = world.link
    alt_schluessel = (world.dir / SCHLUESSEL).read_text()
    world.client.script["certificate"] = [bootstrap.Rejected("certificate: HTTP 409")]

    world.empfangen("down/command", _befehl())

    assert not alter_link.stopped and world.link is alter_link
    assert (world.dir / SCHLUESSEL).read_text() == alt_schluessel
    assert not (world.dir / (SCHLUESSEL + NEU)).exists() and not (world.dir / (ZERTIFIKAT + NEU)).exists()
    ergebnis = _ergebnis(alter_link)
    assert ergebnis["ok"] is False and ergebnis["error"] == {"grund": "intern"}


def test_ein_zertifikat_fuer_einen_fremden_schluessel_wird_nicht_abgelegt(tmp_path):
    world = _welt(tmp_path)
    alter_link = world.link
    fremd = identity.load_or_create(tmp_path / "fremd")
    world.client.script["certificate"] = [
        lambda ident: bootstrap.Antwort("uebernommen", world.neuer_zugang(fremd))]

    world.empfangen("down/command", _befehl())

    assert not alter_link.stopped
    assert not (world.dir / (SCHLUESSEL + NEU)).exists() and not (world.dir / (ZERTIFIKAT + NEU)).exists()
    assert _ergebnis(alter_link)["ok"] is False


def test_ein_neustart_mitten_im_wechsel_startet_mit_dem_alten_paar(tmp_path):
    world = _welt(tmp_path)
    alt_schluessel = (world.dir / SCHLUESSEL).read_text()
    alt_zertifikat = (world.dir / ZERTIFIKAT).read_text()
    _anfordern(world)
    assert (world.dir / (SCHLUESSEL + NEU)).exists()  # der Wechsel laeuft, der Prozess endet hier

    links: list = []
    neu = device.Device(world.host, tmp_path, "https://accounts.example.test", clock=world.clock, wall=lambda: 1.0,
                        bootstrap_client=lambda ident: world.client,
                        link_factory=lambda *args: links.append(world.links[0].__class__(*args)) or links[-1])
    neu.schritt()

    assert not (world.dir / (SCHLUESSEL + NEU)).exists() and not (world.dir / (ZERTIFIKAT + NEU)).exists()
    assert (world.dir / SCHLUESSEL).read_text() == alt_schluessel
    assert (world.dir / ZERTIFIKAT).read_text() == alt_zertifikat
    assert len(links) == 1 and links[0].key_pem == alt_schluessel and links[0].zugang.certificate_pem == alt_zertifikat


def test_der_wire_koerper_traegt_die_erneuerung_nur_auf_anforderung(tmp_path):
    csr = identity.csr_pem(identity.load_or_create(tmp_path))
    assert nachrichten.zertifikat(csr) == {"csr": csr}
    assert nachrichten.zertifikat(csr, erneuerung=True) == {"csr": csr, "erneuerung": True}


def test_weder_schluessel_noch_pem_im_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    erfolg = _welt(tmp_path / "a")
    _anfordern(erfolg)
    erfolg.link.verbinden()
    erfolg.device.schritt()
    neu_schluessel = (erfolg.dir / SCHLUESSEL).read_text()
    neu_zertifikat = (erfolg.dir / ZERTIFIKAT).read_text()
    fehl = _welt(tmp_path / "b")
    _anfordern(fehl)
    versuchter_schluessel = fehl.link.key_pem
    versuchtes_zertifikat = fehl.link.zugang.certificate_pem
    fehl.link.connect_failures = 1
    fehl.device.schritt()
    scheitert = _welt(tmp_path / "c")
    scheitert.client.script["certificate"] = [bootstrap.Rejected("certificate: HTTP 409")]
    scheitert.empfangen("down/command", _befehl())

    text = caplog.text
    assert "Zertifikatswechsel" in text  # die Probe sieht die Zeilen ueberhaupt
    assert "PRIVATE KEY" not in text and "BEGIN" not in text
    for geheim in (neu_schluessel, neu_zertifikat, versuchter_schluessel, versuchtes_zertifikat):
        koerper = [zeile for zeile in geheim.splitlines() if zeile and not zeile.startswith("-----")]
        assert all(zeile not in text for zeile in koerper)


def test_ohne_verbindung_wird_nicht_gewechselt(tmp_path):
    world = World(tmp_path)  # nie verbunden: das Ergebnis haette keinen Weg, und das Paar darf nicht wechseln
    world.client.script["certificate"] = [_neue_antwort(world)]
    world.device.schritt()
    world.empfangen("down/command", _befehl())
    assert world.client.calls == []
    assert not (world.dir / (SCHLUESSEL + NEU)).exists()
