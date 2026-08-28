"""Completing a chain the shop's own server left short.

Five live stores failed with `unable to get local issuer certificate`, which
reads like a bad certificate and is not one: the server simply omits an
intermediate that its own certificate tells you where to find.
"""
from __future__ import annotations

import datetime as dt
import ssl

import httpx
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from pi import tls


def _certificate(name: str, aia_url: str | None = None) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Issuing CA")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
    )
    if aia_url:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess([
                x509.AccessDescription(
                    x509.oid.AuthorityInformationAccessOID.OCSP,
                    x509.UniformResourceIdentifier("http://ocsp.example/"),
                ),
                x509.AccessDescription(
                    x509.oid.AuthorityInformationAccessOID.CA_ISSUERS,
                    x509.UniformResourceIdentifier(aia_url),
                ),
            ]),
            critical=False,
        )
    return builder.sign(key, hashes.SHA256())


def test_the_issuer_url_is_read_out_of_the_certificate():
    """This is the whole trick: the certificate names its own missing issuer."""
    cert = _certificate("shop.example", "http://cacerts.example/intermediate.crt")
    assert tls.issuer_urls(cert) == ["http://cacerts.example/intermediate.crt"]


def test_a_certificate_without_the_extension_asks_for_nothing():
    assert tls.issuer_urls(_certificate("shop.example")) == []


@respx.mock
def test_the_downloaded_certificate_must_actually_be_the_issuer():
    """A server can publish any URL it likes, so what comes back is checked.

    This is belt and braces — OpenSSL would reject an impostor at the handshake
    anyway — but there is no reason to cache somebody else's certificate.
    """
    leaf = _certificate("shop.example", "http://cacerts.example/wrong.crt")
    impostor = _certificate("not-the-issuer.example")
    respx.get("http://cacerts.example/wrong.crt").mock(
        return_value=httpx.Response(200, content=impostor.public_bytes(Encoding.DER))
    )
    with httpx.Client() as client:
        assert tls.fetch_issuer(leaf, client) is None


@respx.mock
def test_an_issuer_served_as_pem_is_accepted_too():
    """CA Issuers hands out DER far more often than PEM, but both turn up."""
    leaf = _certificate("shop.example", "http://cacerts.example/ca.crt")
    # Same issuer name the leaf declares, which is what makes it the issuer.
    issuer = _certificate("Issuing CA", None)
    real = x509.load_der_x509_certificate(issuer.public_bytes(Encoding.DER))
    respx.get("http://cacerts.example/ca.crt").mock(
        return_value=httpx.Response(200, content=real.public_bytes(Encoding.PEM))
    )
    with httpx.Client() as client:
        found = tls.fetch_issuer(leaf, client)
    assert found is not None
    assert found.subject == leaf.issuer


def test_the_cache_lives_beside_the_database_not_in_the_repository(tmp_path):
    assert tls.cache_dir(tmp_path / "data" / "pi.db") == tmp_path / "data" / "ca-cache"


def test_an_unusable_cached_certificate_does_not_take_the_run_down(tmp_path):
    """Whatever ends up in that directory, collection still has to start."""
    cache = tmp_path / "ca-cache"
    cache.mkdir()
    (cache / "rubbish.pem").write_text("not a certificate")
    context = tls.context_with(cache)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED, "verification is never turned off"


def test_verification_is_never_relaxed(tmp_path):
    context = tls.context_with(tmp_path)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


class TestWhatDetectionDoesWithIt:
    @respx.mock
    async def test_a_repaired_certificate_is_probed_again(self, monkeypatch, tmp_path):
        """The point of repairing is to go on and read the shop."""
        from pi.sources import detect

        monkeypatch.setattr(detect, "repair", lambda host, cache: (True, None))
        respx.get("https://shop.example/products.json?limit=1").mock(
            side_effect=[
                httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer"),
                httpx.Response(200, json={"products": []}),
            ]
        )
        respx.get("https://shop.example/").mock(
            side_effect=[
                httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer"),
                httpx.Response(200, text='Shopify.currency = {"active":"GBP"};'),
            ]
        )
        respx.get("https://shop.example/meta.json").mock(return_value=httpx.Response(404))

        async with httpx.AsyncClient() as client:
            verdict = await detect.probe(client, "shop.example", ca_cache=tmp_path)

        assert verdict["platform"] == "shopify"
        assert verdict["currency"] == "GBP"

    @respx.mock
    async def test_the_real_fault_is_recorded_once_the_chain_is_complete(
        self, monkeypatch, tmp_path
    ):
        """Measured on the live list: four hosts blamed the missing intermediate
        until it was supplied, and then admitted to an expired certificate or a
        hostname their certificate does not cover. Those need a different remedy
        and should not be filed under the same one.
        """
        from pi.sources import detect

        monkeypatch.setattr(
            detect, "repair", lambda host, cache: (False, "certificate has expired")
        )
        respx.get("https://shop.example/products.json?limit=1").mock(
            side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer")
        )
        respx.get("https://shop.example/").mock(
            side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer")
        )

        async with httpx.AsyncClient() as client:
            verdict = await detect.probe(client, "shop.example", ca_cache=tmp_path)

        assert verdict["platform"] == "tls"
        assert verdict["error"] == "certificate: certificate has expired"
