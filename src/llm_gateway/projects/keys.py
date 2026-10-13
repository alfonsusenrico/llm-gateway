"""Project API keys: `lgw.<dev|prod>.<key_id>.<secret>`.

The secret is 256 random bits, so a keyed SHA-256 is enough to store it; a slow
password hash would only give an attacker a CPU cost to trigger on a public
endpoint.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from llm_gateway.projects.contract import Environment

KEY_PATTERN = re.compile(r"^lgw\.(dev|prod)\.([a-z2-7]{12})\.([A-Za-z0-9_-]{43})$")
_ENVIRONMENT_BY_SHORT: dict[str, Environment] = {"dev": "development", "prod": "production"}
_SHORT_BY_ENVIRONMENT = {value: key for key, value in _ENVIRONMENT_BY_SHORT.items()}


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """A freshly generated key. `raw` is shown once and never stored."""

    key_id: str
    environment: Environment
    raw: str
    secret_hmac: str


@dataclass(frozen=True, slots=True)
class PresentedKey:
    key_id: str
    environment: Environment
    secret: str


def decode_pepper(encoded: str) -> bytes:
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        pepper = base64.urlsafe_b64decode(padded)
    except ValueError as exc:
        raise ValueError("pepper must be base64url") from exc
    if len(pepper) < 32:
        raise ValueError("pepper must decode to at least 32 bytes")
    return pepper


def secret_hmac(pepper: bytes, secret: str) -> str:
    return hmac.new(pepper, secret.encode("ascii"), hashlib.sha256).hexdigest()


def issue_key(environment: Environment, pepper: bytes) -> IssuedKey:
    key_id = base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower()[:12]
    secret = secrets.token_urlsafe(32)
    raw = f"lgw.{_SHORT_BY_ENVIRONMENT[environment]}.{key_id}.{secret}"
    return IssuedKey(key_id, environment, raw, secret_hmac(pepper, secret))


def parse_key(raw: str) -> PresentedKey | None:
    """Return the key's parts, or None when it does not have the key format."""
    match = KEY_PATTERN.match(raw)
    if match is None:
        return None
    short, key_id, secret = match.groups()
    return PresentedKey(key_id, _ENVIRONMENT_BY_SHORT[short], secret)


def secret_matches(pepper: bytes, secret: str, stored_hmac: str) -> bool:
    return hmac.compare_digest(secret_hmac(pepper, secret), stored_hmac)
