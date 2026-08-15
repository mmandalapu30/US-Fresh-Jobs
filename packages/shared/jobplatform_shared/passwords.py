"""Password hashing, shared by everything that needs it.

Lives here rather than in the API because more than one process legitimately hashes a
password: the API when someone registers or signs in, and `scripts/create_admin.py` when
bootstrapping the first administrator. Keeping it in `apps/api` meant the admin script
could only run inside the API image, which is not the image the operational scripts ship
in -- the failure was a bare ModuleNotFoundError at exactly the moment someone is trying
to get into a fresh deployment.

Argon2id rather than bcrypt: it is the current recommendation, and bcrypt silently ignores
everything past byte 72, so a long passphrase is weaker than it looks.

Token handling deliberately stays in the API. Only the API issues or verifies them, and a
JWT secret has no business being reachable from the ingestion worker.
"""

from __future__ import annotations

import contextlib
import secrets
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: Library defaults, which follow the argon2-cffi maintainers' recommendation. Raising
#: these later invalidates nothing: verify() reads the parameters from the stored hash, and
#: `needs_rehash` reports when an upgrade is due.
_hasher: Final = PasswordHasher()

#: A hash of a value nobody knows. Verifying against it costs the same as verifying a real
#: one, so "no such account" and "wrong password" take the same time and the login endpoint
#: cannot be used to enumerate addresses.
_DUMMY_HASH: Final = _hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Check a password, taking the same time whether or not the account exists."""
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
    """True when a stored hash uses weaker parameters than the current policy."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True
