"""Password hashing and access tokens.

Two decisions worth stating, because both are easy to get subtly wrong and neither is
visible from the call sites.

**Argon2id, not bcrypt.** It is the current password-hashing recommendation, it has no
72-byte silent truncation (bcrypt ignores everything past byte 72, so a long passphrase is
weaker than it looks), and passlib is not required for it.

**Tokens carry identity, never authority.** The JWT says who you are; it does not say what
you may do. Role and approval status are re-read from the database on every request, so an
account suspended a second ago loses access on its next call rather than when its token
happens to expire. Putting `role` or `status` in the claims would make revocation take up
to the token lifetime to bite -- which is precisely the window an admin suspending an
account is trying to close.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hmac
import secrets
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from jobplatform_shared import get_settings

#: Defaults follow the argon2-cffi maintainers' recommendation. Tuning these is a
#: deployment decision; raising them invalidates nothing, since verify() detects the
#: parameters from the stored hash and `needs_rehash` reports when an upgrade is due.
_hasher: Final = PasswordHasher()

#: A hash of a value nobody knows, used to spend the same CPU time verifying a password for
#: an email that does not exist as for one that does. Without it, "no such user" returns
#: measurably faster than "wrong password" and the login endpoint becomes an account
#: enumeration oracle.
_DUMMY_HASH: Final = _hasher.hash(secrets.token_urlsafe(32))

TOKEN_TYPE_ACCESS: Final = "access"  # noqa: S105  # a token *type*, not a secret
TOKEN_TYPE_REFRESH: Final = "refresh"  # noqa: S105  # a token *type*, not a secret


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Check a password, in constant-ish time whether or not the account exists.

    Pass ``None`` for an account with no password set (or no account at all) and this still
    performs a full verification against a dummy hash before returning False.
    """
    if not password_hash:
        with contextlib.suppress(VerifyMismatchError, VerificationError, InvalidHashError):
            _hasher.verify(_DUMMY_HASH, password)
        return False
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses weaker parameters than the current policy."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def create_access_token(user_id: int, *, expires_minutes: int | None = None) -> str:
    settings = get_settings()
    minutes = expires_minutes or settings.jwt_access_token_expire_minutes
    return _encode(user_id, TOKEN_TYPE_ACCESS, dt.timedelta(minutes=minutes))


def create_refresh_token(user_id: int, *, expires_days: int | None = None) -> str:
    settings = get_settings()
    days = expires_days or settings.jwt_refresh_token_expire_days
    return _encode(user_id, TOKEN_TYPE_REFRESH, dt.timedelta(days=days))


def _encode(user_id: int, token_type: str, lifetime: dt.timedelta) -> str:
    settings = get_settings()
    issued = _now()
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": token_type,
        "iat": issued,
        "exp": issued + lifetime,
        # A unique id per token, so a future revocation list has something to key on
        # without needing to store the token itself.
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


class TokenError(Exception):
    """A token was absent, malformed, expired, or not the type expected."""


def decode_token(token: str, *, expect: str = TOKEN_TYPE_ACCESS) -> int:
    """Return the user id a token asserts, or raise TokenError.

    ``algorithms`` is pinned to the configured algorithm rather than accepting whatever the
    token's own header claims -- accepting the header is the classic `alg: none`
    confusion, where an attacker chooses how their token gets verified.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("token is not valid") from exc

    # A refresh token presented as an access token would otherwise be honoured, quietly
    # extending an access lifetime to the refresh lifetime.
    if not hmac.compare_digest(str(payload.get("type", "")), expect):
        raise TokenError(f"expected a {expect} token")

    try:
        return int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("token subject is not a user id") from exc
