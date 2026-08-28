"""Completing a certificate chain the shop's own server forgot to send.

Five stores on the live list fail with `unable to get local issuer certificate`.
That is not a bad certificate. www.wellgosh.com serves a valid DigiCert leaf and
simply omits the intermediate — which the leaf itself names, in its Authority
Information Access extension:

    CA Issuers - URI:http://cacerts.digicert.com/DigiCertTLSRSASHA2562020CA1-1.crt

Browsers fetch that and carry on. Python's ssl module does not, so a
misconfigured server looks to us like an untrustworthy one.

**Nothing here weakens verification.** The missing intermediate is fetched, added
to a copy of the system trust store, and then the connection is made again with
verification fully on. If the completed chain does not verify against a root we
already trusted, the repair fails and the store stays switched off — exactly as
it does today. A forged certificate gains nothing from this: an attacker can
serve any intermediate they like, and it still has to be signed by a real root.

The unverified read at the start exists only to learn which certificate to go
looking for. No request is made and no data is exchanged over it.
"""
from __future__ import annotations

import logging
import socket
import ssl
from hashlib import sha256
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

log = logging.getLogger(__name__)

# How far up the chain to walk. A server usually omits one intermediate; two is
# already unusual, and a certificate claiming to need five is not worth chasing.
MAX_DEPTH = 3
HANDSHAKE_TIMEOUT = 15.0


def cache_dir(db_path: Path) -> Path:
    """Where fetched intermediates live — beside the database, not in the repo."""
    return db_path.parent / "ca-cache"


def context_with(certs: Path | None) -> ssl.SSLContext:
    """The default verifying context, plus any intermediates we have collected."""
    context = ssl.create_default_context()
    if certs and certs.is_dir():
        for pem in sorted(certs.glob("*.pem")):
            try:
                context.load_verify_locations(cafile=str(pem))
            except ssl.SSLError as exc:
                log.warning("ignoring unusable cached certificate %s (%s)", pem.name, exc)
    return context


def verify_error(host: str, context: ssl.SSLContext, port: int = 443) -> str | None:
    """None if a real, fully verified handshake succeeds; otherwise why it did not.

    This is the whole test. Not "does the chain look plausible" — does OpenSSL,
    with its usual rules and the system roots, accept it. The reason matters too:
    completing a chain is what lets the *real* fault become visible. Four of the
    hosts here reported "unable to get local issuer certificate" until the
    missing intermediate was supplied, at which point they admitted to an expired
    certificate or a hostname the certificate does not cover.
    """
    try:
        with (
            socket.create_connection((host, port), timeout=HANDSHAKE_TIMEOUT) as raw,
            context.wrap_socket(raw, server_hostname=host),
        ):
            return None
    except ssl.SSLCertVerificationError as exc:
        return exc.verify_message or str(exc)
    except (OSError, ssl.SSLError) as exc:
        return f"{type(exc).__name__}: {exc}"


def verifies(host: str, context: ssl.SSLContext, port: int = 443) -> bool:
    return verify_error(host, context, port) is None


def _peer_certificate(host: str, port: int = 443) -> x509.Certificate | None:
    """Read the certificate the host presents, without judging it.

    Unverified on purpose and safe because of what is done with the result: the
    certificate is only read to find the URL of its issuer. It is never trusted,
    and nothing is sent over this connection.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with (
            socket.create_connection((host, port), timeout=HANDSHAKE_TIMEOUT) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        log.debug("%s: could not read the presented certificate (%s)", host, exc)
        return None
    return x509.load_der_x509_certificate(der) if der else None


def issuer_urls(cert: x509.Certificate) -> list[str]:
    """The caIssuers URLs a certificate publishes for its own issuer."""
    try:
        aia = cert.extensions.get_extension_for_class(
            x509.AuthorityInformationAccess
        ).value
    except x509.ExtensionNotFound:
        return []
    return [
        description.access_location.value
        for description in aia
        if description.access_method == x509.oid.AuthorityInformationAccessOID.CA_ISSUERS
        and isinstance(description.access_location, x509.UniformResourceIdentifier)
    ]


def _load(raw: bytes) -> x509.Certificate | None:
    """CA Issuers hands out DER far more often than PEM, but both turn up."""
    for load in (x509.load_der_x509_certificate, x509.load_pem_x509_certificate):
        try:
            return load(raw)
        except ValueError:
            continue
    return None


def fetch_issuer(cert: x509.Certificate, client: httpx.Client) -> x509.Certificate | None:
    """Download the certificate that signed `cert`, if it says where to find one."""
    for url in issuer_urls(cert):
        try:
            resp = client.get(url)
        except httpx.HTTPError as exc:
            log.debug("could not fetch %s (%s)", url, exc)
            continue
        if resp.status_code != 200 or len(resp.content) > 100_000:
            continue
        issuer = _load(resp.content)
        # A server can publish any URL it likes, so check the thing that came
        # back is actually this certificate's issuer before keeping it.
        if issuer is not None and issuer.subject == cert.issuer:
            return issuer
    return None


def repair(host: str, cache: Path, port: int = 443) -> tuple[bool, str | None]:
    """Try to make `host` verify by supplying the intermediates it omits.

    Returns (verified, reason). `verified` is True only if a fully verified
    handshake then succeeds; `reason` is the verification error that remains,
    which after a completed chain is the shop's actual problem rather than a
    symptom of the missing link. Certificates that helped are cached so the next
    run does not fetch them again.
    """
    context = context_with(cache)
    reason = verify_error(host, context, port)
    if reason is None:
        return True, None

    cert = _peer_certificate(host, port)
    if cert is None:
        return False, reason

    cache.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        for _ in range(MAX_DEPTH):
            issuer = fetch_issuer(cert, client)
            if issuer is None:
                return False, reason
            pem = issuer.public_bytes(Encoding.PEM)
            path = cache / f"{sha256(pem).hexdigest()[:16]}.pem"
            if not path.exists():
                path.write_bytes(pem)
            context.load_verify_locations(cadata=pem.decode("ascii"))
            reason = verify_error(host, context, port)
            if reason is None:
                log.info(
                    "%s: chain completed with %s — the shop omits it, we supply it",
                    host, issuer.subject.rfc4514_string()[:60],
                )
                return True, None
            # Still short: the intermediate we just added may itself be orphaned.
            cert = issuer
    return False, reason
