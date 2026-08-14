"""Unit tests for password hashing and signed access tokens."""

from __future__ import annotations

import time
import uuid

from app.core.config import Settings
from app.core.security import (
    TokenClaims,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def _settings(
    *,
    auth_secret: str = "x" * 32,
    auth_token_ttl_seconds: int = 86_400,
    auth_token_issuer: str = "coding-agent",
) -> Settings:
    return Settings(
        app_name="coding-agent",
        database_url="postgresql+asyncpg://u:p@localhost/db",
        redis_url="redis://localhost:6379/0",
        auth_secret=auth_secret,
        auth_token_ttl_seconds=auth_token_ttl_seconds,
        auth_token_issuer=auth_token_issuer,
        _env_file=None,
    )


def test_hash_password_roundtrip() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded) is True


def test_hash_password_rejects_wrong_password() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("wrong password", encoded) is False


def test_hash_password_is_salted() -> None:
    first = hash_password("same password")
    second = hash_password("same password")
    assert first != second
    assert verify_password("same password", first) is True
    assert verify_password("same password", second) is True


def test_hash_password_embed_includes_algorithm_and_iterations() -> None:
    encoded = hash_password("hunter2")
    parts = encoded.split("$")
    assert parts[0] == "pbkdf2_sha256"
    assert int(parts[1]) >= 1
    assert len(parts[2]) > 0
    assert len(parts[3]) > 0


def test_verify_password_rejects_malformed_hashes() -> None:
    for malformed in ("", "garbage", "pbkdf2_sha256$abc", "$1$$"):
        assert verify_password("any password", malformed) is False


def test_verify_password_rejects_unknown_scheme() -> None:
    encoded = hash_password("hunter2")
    wrong_scheme = "bcrypt" + encoded[len("pbkdf2_sha256") :]
    assert verify_password("hunter2", wrong_scheme) is False


def test_create_and_decode_access_token_roundtrip() -> None:
    settings = _settings(auth_secret="x" * 32)
    user_id = uuid.uuid4()
    claims = decode_access_token(create_access_token(user_id, settings), settings)
    assert claims is not None
    assert isinstance(claims, TokenClaims)
    assert claims.subject == user_id
    assert claims.issuer == settings.auth_token_issuer
    assert claims.issued_at.timestamp() <= time.time()
    assert claims.expires_at.timestamp() > time.time()


def test_decode_access_token_rejects_wrong_secret() -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id, _settings(auth_secret="a" * 32))
    assert decode_access_token(token, _settings(auth_secret="b" * 32)) is None


def test_decode_access_token_rejects_tampered_payload() -> None:
    settings = _settings(auth_secret="x" * 32)
    token = create_access_token(uuid.uuid4(), settings)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert decode_access_token(tampered, settings) is None


def test_decode_access_token_rejects_truncated_token() -> None:
    settings = _settings(auth_secret="x" * 32)
    token = create_access_token(uuid.uuid4(), settings)
    assert decode_access_token(token.split(".")[0], settings) is None


def test_decode_access_token_rejects_expired_token() -> None:
    import hashlib
    import hmac
    import json

    from app.core.security import _b64url_decode, _b64url_encode

    settings = _settings(auth_secret="x" * 32, auth_token_ttl_seconds=60)
    token = create_access_token(uuid.uuid4(), settings)
    header_b64, payload_b64, _ = token.split(".")

    payload = json.loads(_b64url_decode(payload_b64))
    payload["exp"] = int(time.time()) - 1
    expired_payload = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())

    signing_input = f"{header_b64}.{expired_payload}"
    signature = hmac.new(
        settings.auth_secret.encode(),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    expired = f"{header_b64}.{expired_payload}.{_b64url_encode(signature)}"
    assert decode_access_token(expired, settings) is None


def test_decode_access_token_rejects_garbage() -> None:
    settings = _settings(auth_secret="x" * 32)
    assert decode_access_token("not.a.token", settings) is None
    assert decode_access_token("", settings) is None
