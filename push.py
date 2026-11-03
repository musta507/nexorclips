"""Avisos al móvil y al ordenador (Web Push) sin librerías extra: cifrado aes128gcm (RFC 8291) y firma VAPID (RFC 8292).

Solo hace falta poner en Railway VAPID_PRIVATE_KEY (la genera el panel en Ajustes). La clave pública sale de ella.
"""
import base64
import hashlib
import hmac
import json
import os
import struct
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s):
    s = (s or "").strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _raw_public(key):
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def new_vapid_keys():
    """(privada, pública) en base64url, para pegar la privada en Railway."""
    k = ec.generate_private_key(ec.SECP256R1())
    priv = k.private_numbers().private_value.to_bytes(32, "big")
    return b64u(priv), b64u(_raw_public(k))


def _private_from(b64):
    return ec.derive_private_key(int.from_bytes(unb64u(b64), "big"), ec.SECP256R1())


def vapid_public(private_b64):
    try:
        return b64u(_raw_public(_private_from(private_b64)))
    except Exception:  # noqa: BLE001
        return ""


def _hkdf(salt, ikm, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload, p256dh_b64, auth_b64, salt=None, server_key=None):
    """Cifra el mensaje para el navegador del clipper (formato aes128gcm)."""
    ua_public = unb64u(p256dh_b64)
    auth = unb64u(auth_b64)
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(server_key)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    secret = server_key.exchange(ec.ECDH(), peer)
    ikm = _hkdf(auth, secret, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack(">IB", 4096, len(as_public)) + as_public + body


def vapid_header(endpoint, private_b64, subject):
    key = _private_from(private_b64)
    u = urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": subject}, separators=(",", ":")).encode())
    der = key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{claims}.{sig}, k={b64u(_raw_public(key))}"


def send(sub, data, private_b64, subject="mailto:soporte@clipprofit.app", ttl=86400):
    """Envía un aviso. Devuelve el código HTTP (404/410 = la suscripción ya no existe)."""
    body = encrypt(json.dumps(data, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
    resp = requests.post(sub["endpoint"], data=body, timeout=10, headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(ttl),
        "Urgency": "normal", "Authorization": vapid_header(sub["endpoint"], private_b64, subject)})
    return resp.status_code
