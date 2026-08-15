"""Registration, login, logout, and "who am I".

Registration always produces a PENDING account. There is no path through this router that
grants access; approval is an administrative act, handled in `routers/admin.py`. That
separation is the whole point of the feature, so it is enforced structurally: this module
never writes `status`, `role`, or any of the approval columns to anything but their
defaults.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from jobplatform_shared import get_settings
from jobplatform_shared.db import get_db

from ..core.deps import AuthError, CurrentUserDep
from ..core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    needs_rehash,
    verify_password,
)

router = APIRouter()

#: Lock an account after this many consecutive failures, for this long. Slows credential
#: stuffing to uselessness without letting an attacker lock a known victim out
#: indefinitely -- the window expires on its own rather than needing an admin.
_MAX_FAILED_LOGINS: Final = 8
_LOCKOUT_MINUTES: Final = 15

_COOKIE_NAME: Final = "session"


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    password: str = Field(min_length=12, max_length=200)
    phone: str | None = Field(default=None, max_length=40)

    @field_validator("password")
    @classmethod
    def _strength(cls, v: str) -> str:
        """Length first, variety second.

        Twelve characters with two character classes resists offline cracking far better
        than eight with four classes and a substitution rule, and it is a rule people can
        actually follow without writing the result on a note.
        """
        classes = sum(
            bool(re.search(pattern, v)) for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
        )
        if classes < 2:
            raise ValueError("Use at least two of: lowercase, uppercase, digits, symbols.")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        cleaned = v.strip()
        if not re.fullmatch(r"[0-9+()\-.\s]{6,40}", cleaned):
            raise ValueError("Phone number contains unexpected characters.")
        return cleaned


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class PublicUser(BaseModel):
    """Everything the client is allowed to know about itself.

    Explicitly enumerated rather than serialised from the row: a model that lists its
    fields cannot leak a column added later, and `password_hash` is exactly the column that
    must never appear here.
    """

    id: int
    name: str | None
    email: str
    role: str
    status: str
    created_at: dt.datetime | None = None
    approved_at: dt.datetime | None = None


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        _COOKIE_NAME,
        token,
        # httponly: unreadable by JavaScript, so an XSS bug cannot exfiltrate the session.
        httponly=True,
        # secure only in production: a Secure cookie is dropped over plain HTTP, which
        # would make local development silently unable to log in.
        secure=settings.is_production,
        # lax, not strict: strict would drop the cookie on a link from an external site,
        # so an approved user arriving from email would appear logged out.
        samesite="lax",
        max_age=settings.jwt_access_token_expire_minutes * 60,
        path="/",
    )


@router.post("/auth/register", response_model=PublicUser, status_code=201, summary="Request access")
async def register(
    payload: RegisterRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PublicUser:
    """Create a PENDING account. Never grants access."""
    try:
        row = (
            (
                await session.execute(
                    text(
                        "INSERT INTO users (email, password_hash, full_name, phone, role, status)"
                        " VALUES (:email, :pw, :name, :phone, 'USER', 'PENDING')"
                        " RETURNING id, email, full_name, role::text AS role,"
                        "           status::text AS status, created_at"
                    ),
                    {
                        "email": payload.email.lower(),
                        "pw": hash_password(payload.password),
                        "name": payload.name.strip(),
                        "phone": payload.phone,
                    },
                )
            )
            .mappings()
            .one()
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        # Deliberately the same wording a caller would see for any duplicate, and a 409
        # rather than a 200 -- pretending success to avoid confirming the address would
        # leave a real user unable to tell a typo from an existing account.
        raise AuthError(409, "email_taken", "An account with that email already exists.") from None

    return PublicUser(
        id=row["id"],
        name=row["full_name"],
        email=row["email"],
        role=row["role"],
        status=row["status"],
        created_at=row["created_at"],
    )


@router.post("/auth/login", summary="Log in")
async def login(
    payload: LoginRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    """Authenticate and issue a session.

    Login succeeds for any status. That is deliberate: a pending or suspended user must be
    able to sign in far enough to be *told* what state they are in. Authorization -- what
    they can then reach -- is enforced separately, on every protected route.
    """
    row = (
        (
            await session.execute(
                text(
                    "SELECT id, email, full_name, password_hash, role::text AS role,"
                    "       status::text AS status, is_active, failed_login_count, locked_until"
                    "  FROM users WHERE email = :email"
                ),
                {"email": payload.email.lower()},
            )
        )
        .mappings()
        .first()
    )

    now = dt.datetime.now(dt.UTC)
    if row and row["locked_until"] and row["locked_until"] > now:
        raise AuthError(
            429,
            "too_many_attempts",
            "Too many failed attempts. Try again in a few minutes.",
        )

    # Runs even when the row is absent, so a missing account costs the same time as a wrong
    # password and the endpoint cannot be used to enumerate addresses.
    if not verify_password(payload.password, row["password_hash"] if row else None):
        if row:
            failures = int(row["failed_login_count"]) + 1
            lock = (
                now + dt.timedelta(minutes=_LOCKOUT_MINUTES)
                if failures >= _MAX_FAILED_LOGINS
                else None
            )
            await session.execute(
                text(
                    "UPDATE users SET failed_login_count = :n, locked_until = :lock,"
                    "       updated_at = now() WHERE id = :id"
                ),
                {"n": failures, "lock": lock, "id": row["id"]},
            )
            await session.commit()
        raise AuthError(401, "invalid_credentials", "Email or password is incorrect.")

    if not row["is_active"]:
        raise AuthError(403, "account_inactive", "This account is not active.")

    updates = {
        "id": row["id"],
        "pw": hash_password(payload.password)
        if needs_rehash(row["password_hash"])
        else row["password_hash"],
    }
    await session.execute(
        text(
            "UPDATE users SET failed_login_count = 0, locked_until = NULL,"
            "       last_login_at = now(), password_hash = :pw, updated_at = now()"
            " WHERE id = :id"
        ),
        updates,
    )
    await session.commit()

    token = create_access_token(int(row["id"]))
    _set_session_cookie(response, token)
    return {
        "access_token": token,
        "refresh_token": create_refresh_token(int(row["id"])),
        "token_type": "bearer",
        "user": PublicUser(
            id=row["id"],
            name=row["full_name"],
            email=row["email"],
            role=row["role"],
            status=row["status"],
        ).model_dump(),
    }


@router.post("/auth/logout", status_code=204, summary="Log out")
async def logout(response: Response) -> Response:
    """Clear the session cookie.

    Unauthenticated on purpose: logging out must work even when the token has already
    expired, otherwise the one moment a user most wants to clear their session is the one
    moment they cannot.
    """
    response.delete_cookie(_COOKIE_NAME, path="/")
    response.status_code = 204
    return response


@router.get("/auth/me", response_model=PublicUser, summary="The current user")
async def me(
    user: CurrentUserDep,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> PublicUser:
    """Identity and status for the caller. Requires authentication, not approval.

    A pending user needs this to render their own waiting page.
    """
    row = (
        (
            await session.execute(
                text("SELECT created_at, approved_at FROM users WHERE id = :id"),
                {"id": user.id},
            )
        )
        .mappings()
        .first()
    )
    return PublicUser(
        id=user.id,
        name=user.full_name,
        email=user.email,
        role=user.role,
        status=user.status,
        created_at=row["created_at"] if row else None,
        approved_at=row["approved_at"] if row else None,
    )
