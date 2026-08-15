"""Authentication and authorization dependencies.

Three dependencies, composed rather than repeated:

    require_auth       authenticated, whatever their state
    require_approved   authenticated AND status == APPROVED   -- guards job data
    require_admin      authenticated AND role == ADMIN AND approved -- guards /admin

Every one of them re-reads the user from the database. The token proves identity and
nothing else, so an account approved, suspended or demoted a moment ago takes effect on
its very next request. Trusting claims would leave a window exactly as long as the token
lifetime, which is the window an administrator suspending an account is trying to close.

`require_admin` also demands APPROVED. An administrator who has been suspended is
suspended; the role is what they may do, the status is whether they may do anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobplatform_shared.db import get_db

from .security import TokenError, decode_token

#: Messages are deliberately specific about *which* wall the caller hit, because "403" with
#: no explanation is indistinguishable from a broken deployment. They say nothing about
#: other accounts, so they leak nothing.
MESSAGES: Final[dict[str, str]] = {
    "unauthenticated": "Please log in to continue.",
    "PENDING": "Your account is waiting for administrator approval.",
    "REJECTED": "Your access request was rejected.",
    "SUSPENDED": "Your account has been suspended.",
    "not_admin": "You do not have administrator access.",
    "inactive": "This account is not active.",
}


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated caller, as the database currently describes them.

    Frozen so a route cannot mutate it and have that mutation appear to mean something --
    authorization decisions are made here, not downstream.
    """

    id: int
    email: str
    full_name: str | None
    role: str
    status: str
    is_active: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"

    @property
    def is_approved(self) -> bool:
        return self.status == "APPROVED"


class AuthError(Exception):
    """Raised instead of HTTPException so the shape of the body stays with the handlers."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def _bearer_token(request: Request) -> str | None:
    """Pull the token from the Authorization header, or the session cookie.

    The header is what the Next server sends. The cookie is accepted so the API remains
    directly usable -- with a real session -- during development and testing, without the
    frontend having to be in the loop.
    """
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        candidate = header[7:].strip()
        if candidate:
            return candidate
    return request.cookies.get("session") or None


async def _load_user(session: AsyncSession, user_id: int) -> CurrentUser | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT id, email, full_name, role::text AS role, status::text AS status,"
                    "       is_active"
                    "  FROM users WHERE id = :id"
                ),
                {"id": user_id},
            )
        )
        .mappings()
        .first()
    )
    return CurrentUser(**dict(row)) if row else None


async def require_auth(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    token = _bearer_token(request)
    if not token:
        raise AuthError(401, "unauthenticated", MESSAGES["unauthenticated"])
    try:
        user_id = decode_token(token)
    except TokenError as exc:
        raise AuthError(401, "unauthenticated", MESSAGES["unauthenticated"]) from exc

    user = await _load_user(session, user_id)
    # A token for a deleted account is not an authentication failure to explain in detail;
    # it is simply not authenticated.
    if user is None:
        raise AuthError(401, "unauthenticated", MESSAGES["unauthenticated"])
    if not user.is_active:
        raise AuthError(403, "account_inactive", MESSAGES["inactive"])
    return user


async def require_approved(
    user: Annotated[CurrentUser, Depends(require_auth)],
) -> CurrentUser:
    """Guards everything containing job data."""
    if user.status == "APPROVED":
        return user
    code = {
        "PENDING": "account_pending",
        "REJECTED": "account_rejected",
        "SUSPENDED": "account_suspended",
    }.get(user.status, "account_not_approved")
    raise AuthError(403, code, MESSAGES.get(user.status, "Your account is not approved."))


async def require_admin(
    user: Annotated[CurrentUser, Depends(require_auth)],
) -> CurrentUser:
    """Guards /admin. Deliberately requires approval as well as the role."""
    if not user.is_admin:
        # Same response whether the caller is a normal user or does not exist as an admin:
        # confirming that an /admin route exists and is merely forbidden tells a prober
        # nothing useful, but distinguishing "not admin" from "no such route" would.
        raise AuthError(403, "forbidden", MESSAGES["not_admin"])
    if not user.is_approved:
        raise AuthError(403, "account_not_approved", MESSAGES.get(user.status, "Not approved."))
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(require_auth)]
ApprovedUserDep = Annotated[CurrentUser, Depends(require_approved)]
AdminUserDep = Annotated[CurrentUser, Depends(require_admin)]
