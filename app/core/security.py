"""Password hashing and signed access tokens.

Everything here uses only the standard library: PBKDF2-HMAC-SHA256 for
password hashing and an HMAC-SHA256-signed, JWT-shaped token for access
control. Keeping cryptography in-repo avoids a native dependency (and its
supply chain) while still following the same construction rules a JWT
library would: the signature covers the header and payload, every decode
step is constant-time compared, and tokens expire server-side.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import Settings

_HASH_NAME = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16

_TOKEN_VERSION = "v1"
_TOKEN_ALGORITHM = "HS256"
_TOKEN_TYP = "JWT"


@dataclass(frozen=True)
class TokenClaims:
    """Verified claims extracted from an access token."""

    subject: uuid.UUID
    issuer: str
    token_id: str
    issued_at: datetime
    expires_at: datetime


def hash_password(password: str) -> str:
    """Hash ``password`` with a fresh random salt and return an encoded string.

    The result embeds the algorithm, iteration count, salt, and digest so
    verification never needs side information and parameters can evolve.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    return f"{_HASH_NAME}${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Return whether ``password`` matches an encoded password hash.

    Re-derives the digest and compares in constant time. Any malformed input
    (wrong scheme, unparsable fields, an absent password) verifies as False
    rather than raising.
    """
    try:
        name, iterations_text, salt_hex, hash_hex = encoded.split("$")
        if name != _HASH_NAME:
            return False
        iterations = int(iterations_text)
        if iterations < 1:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_access_token(user_id: uuid.UUID, settings: Settings) -> str:
    """Sign a fresh access token for ``user_id``.

    The token is self-contained (stateless): the resource server only needs
    the shared secret to verify it, so no per-token database rows are needed
    and logout simply discards the client-held token.
    """
    now = int(time.time())
    header = {"alg": _TOKEN_ALGORITHM, "typ": _TOKEN_TYP}
    claims = {
        "v": _TOKEN_VERSION,
        "iss": settings.auth_token_issuer,
        "sub": str(user_id),
        "iat": now,
        "exp": now + settings.auth_token_ttl_seconds,
        "jti": secrets.token_urlsafe(16),
    }
    signing_input = ".".join(
        (
            _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8")),
        )
    )
    signature = _b64url_encode(
        hmac.new(
            settings.auth_secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return f"{signing_input}.{signature}"


def decode_access_token(token: str, settings: Settings) -> TokenClaims | None:
    """Verify ``token`` and return its claims, or ``None`` when invalid.

    A token is rejected when the signature does not match, the version or
    issuer is unexpected, a claim is malformed, or the token has expired.
    """
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        return None

    signing_input = f"{header_b64}.{payload_b64}"
    expected = hmac.new(
        settings.auth_secret.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        if not hmac.compare_digest(_b64url_decode(signature_b64), expected):
            return None
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
    except (json.JSONDecodeError, ValueError):
        return None

    if header.get("alg") != _TOKEN_ALGORITHM or header.get("typ") != _TOKEN_TYP:
        return None
    if payload.get("v") != _TOKEN_VERSION or payload.get("iss") != settings.auth_token_issuer:
        return None

    subject_text = payload.get("sub")
    token_id = payload.get("jti")
    issuer = payload.get("iss")
    if not all(isinstance(value, str) for value in (subject_text, token_id, issuer)):
        return None
    try:
        subject = uuid.UUID(subject_text)
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except (ValueError, KeyError, TypeError):
        return None
    if expires_at <= time.time():
        return None

    return TokenClaims(
        subject=subject,
        issuer=issuer,
        token_id=token_id,
        issued_at=datetime.fromtimestamp(issued_at, tz=UTC),
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
    )


def _b64url_encode(data: bytes) -> str:
    """Base64url-encode ``data`` without padding characters."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    """Base64url-decode ``data``, tolerating missing padding."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)
