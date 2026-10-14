"""Fetch the service-principal certificate from Key Vault at startup.

Deliberately NOT a "download the pfx once and point config at it" helper. The
certificate never touches disk: it is pulled into memory with the *operator's*
own Entra identity (the same WAM sign-in the delegated mode uses) and handed
straight to CertificateCredential.

Two things fall out of that, both wanted:
  * no credential at rest in this folder, nothing to leak or forget to delete;
  * whoever runs the server must themselves have `get` on the vault secret, so
    revoking their Key Vault access revokes their use of the app identity.
"""
from __future__ import annotations

import base64

import requests

_API = "7.4"
_cache: dict[str, tuple[bytes, str | None]] = {}


def _vault_token(credential) -> str:
    return credential.get_token("https://vault.azure.net/.default").token


def _secret(credential, vault: str, name: str) -> str:
    url = (vault if vault.startswith("http") else f"https://{vault}.vault.azure.net")
    r = requests.get(f"{url.rstrip('/')}/secrets/{name}",
                     params={"api-version": _API},
                     headers={"Authorization": f"Bearer {_vault_token(credential)}"},
                     timeout=60)
    if not r.ok:
        raise RuntimeError(
            f"Key Vault {url} secret '{name}': {r.status_code} "
            f"{r.json().get('error', {}).get('message', r.text)[:300]}")
    return r.json()["value"]


def certificate(credential, spec: dict) -> tuple[bytes, str | None]:
    """Return (pkcs12_bytes, password) for a config `certificate_keyvault` block.

    spec: {"vault": "<vault-name>",
           "cert_secret": "<pfx-secret-name>",
           "password_secret": "<pfx-password-secret-name>"}
    """
    key = f"{spec.get('vault')}|{spec.get('cert_secret')}"
    if key in _cache:
        return _cache[key]

    raw = _secret(credential, spec["vault"], spec["cert_secret"])
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        # Some vaults hold the PEM text directly rather than a base64 PKCS12.
        data = raw.encode()

    password = None
    if spec.get("password_secret"):
        password = _secret(credential, spec["vault"], spec["password_secret"])

    _cache[key] = (data, password)
    return data, password


def describe(data: bytes, password: str | None) -> dict:
    """Metadata about the certificate - never the key material."""
    out = {"bytes": len(data), "password_protected": bool(password)}
    head = data[:40]
    if head.startswith(b"-----BEGIN"):
        out["format"] = "PEM"
        return out
    out["format"] = "PKCS12/PFX"
    try:
        from cryptography.hazmat.primitives.serialization import pkcs12
        _, cert, _ = pkcs12.load_key_and_certificates(
            data, password.encode() if password else None)
        if cert is not None:
            out["subject"] = cert.subject.rfc4514_string()
            out["not_after"] = cert.not_valid_after_utc.isoformat()
            out["thumbprint_sha1"] = cert.fingerprint(
                __import__("cryptography.hazmat.primitives.hashes",
                           fromlist=["hashes"]).SHA1()).hex()
    except Exception as exc:
        out["parse_error"] = str(exc)[:200]
    return out
