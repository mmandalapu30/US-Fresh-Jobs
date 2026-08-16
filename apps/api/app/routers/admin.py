"""Administrative user management.

Every route here is behind `require_admin`, and every state change writes an audit row in
the same transaction as the change itself. That pairing is deliberate: an audit log written
separately can be skipped by a failure between the two writes, which is exactly when you
most want the record.

Two self-protection rules, both enforced server-side because the frontend cannot be
trusted to:

  * an admin may not change their own status  -- locking yourself out via the console
  * an admin may not remove their own admin role -- the same, one step slower

Neither is paternalism. With one administrator, either action is unrecoverable without
database access.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobplatform_shared.db import get_db

from ..core.deps import AdminUserDep, AuthError

router = APIRouter()

_STATUSES = ("PENDING", "APPROVED", "REJECTED", "SUSPENDED")

#: Which status each action moves a user to, and the audit verb it records.
_ACTIONS: dict[str, tuple[str, str]] = {
    "approve": ("APPROVED", "ADMIN_APPROVED_USER"),
    "reject": ("REJECTED", "ADMIN_REJECTED_USER"),
    "suspend": ("SUSPENDED", "ADMIN_SUSPENDED_USER"),
    "reactivate": ("APPROVED", "ADMIN_REACTIVATED_USER"),
}


class AdminUser(BaseModel):
    """A user as the console shows them. No password hash, by construction."""

    id: int
    name: str | None
    email: str
    phone: str | None = None
    role: str
    status: str
    created_at: dt.datetime
    approved_at: dt.datetime | None = None
    rejected_at: dt.datetime | None = None
    suspended_at: dt.datetime | None = None
    last_login_at: dt.datetime | None = None


class UserPage(BaseModel):
    items: list[AdminUser]
    total: int
    page: int
    page_size: int


class Summary(BaseModel):
    total_users: int
    pending: int
    approved: int
    rejected: int
    suspended: int
    admins: int


class AuditEntry(BaseModel):
    id: int
    action: str
    admin_email: str | None
    target_email: str | None
    previous_status: str | None
    new_status: str | None
    created_at: dt.datetime


_SELECT = (
    "SELECT id, full_name AS name, email, phone, role::text AS role, status::text AS status,"
    "       created_at, approved_at, rejected_at, suspended_at, last_login_at"
    "  FROM users"
)


@router.get("/admin/summary", response_model=Summary, summary="Counts for the dashboard cards")
async def summary(
    _: AdminUserDep,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> Summary:
    row = (
        (
            await session.execute(
                text(
                    "SELECT count(*) AS total_users,"
                    " count(*) FILTER (WHERE status = 'PENDING')   AS pending,"
                    " count(*) FILTER (WHERE status = 'APPROVED')  AS approved,"
                    " count(*) FILTER (WHERE status = 'REJECTED')  AS rejected,"
                    " count(*) FILTER (WHERE status = 'SUSPENDED') AS suspended,"
                    " count(*) FILTER (WHERE role = 'ADMIN')       AS admins"
                    " FROM users"
                )
            )
        )
        .mappings()
        .one()
    )
    return Summary(**dict(row))


@router.get("/admin/users", response_model=UserPage, summary="List, search and filter users")
async def list_users(
    _: AdminUserDep,
    session: Annotated[AsyncSession, Depends(get_db)],
    status: Annotated[
        str | None, Query(description="PENDING | APPROVED | REJECTED | SUSPENDED")
    ] = None,
    role: Annotated[str | None, Query(description="USER | ADMIN | SERVICE")] = None,
    q: Annotated[str | None, Query(max_length=200, description="name or email")] = None,
    sort: Annotated[Literal["created_desc", "created_asc", "email"], Query()] = "created_desc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> UserPage:
    where: list[str] = ["TRUE"]
    params: dict[str, object] = {}

    # Validated against the tuple rather than interpolated: the value reaches SQL as a
    # bound parameter either way, but rejecting an unknown status early gives a clearer
    # error than an enum cast failure.
    if status:
        if status.upper() not in _STATUSES:
            raise AuthError(400, "bad_request", f"Unknown status: {status}")
        where.append("status = :status")
        params["status"] = status.upper()
    if role:
        where.append("role = :role")
        params["role"] = role.upper()
    if q:
        where.append("(email ILIKE :q OR full_name ILIKE :q)")
        params["q"] = f"%{q}%"

    order = {
        "created_desc": "created_at DESC",
        "created_asc": "created_at ASC",
        "email": "email ASC",
    }[sort]
    # `clause` and `order` are assembled from the literal fragments above; every value is
    # bound. No request data reaches the SQL string itself.
    clause = " WHERE " + " AND ".join(where)

    total = (
        await session.execute(text(f"SELECT count(*) FROM users{clause}"), params)  # noqa: S608
    ).scalar_one()

    params |= {"limit": page_size, "offset": (page - 1) * page_size}
    rows = (
        (
            await session.execute(
                text(f"{_SELECT}{clause} ORDER BY {order} LIMIT :limit OFFSET :offset"),
                params,
            )
        )
        .mappings()
        .all()
    )

    return UserPage(
        items=[AdminUser(**dict(r)) for r in rows],
        total=int(total),
        page=page,
        page_size=page_size,
    )


@router.get("/admin/users/{user_id}", response_model=AdminUser, summary="One user")
async def get_user(
    user_id: int,
    _: AdminUserDep,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> AdminUser:
    row = (
        (await session.execute(text(f"{_SELECT} WHERE id = :id"), {"id": user_id}))
        .mappings()
        .first()
    )
    if not row:
        raise AuthError(404, "not_found", "No such user.")
    return AdminUser(**dict(row))


class ActionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post(
    "/admin/users/{user_id}/{action}", response_model=AdminUser, summary="Decide on a user"
)
async def act_on_user(
    user_id: int,
    action: Literal["approve", "reject", "suspend", "reactivate"],
    request: Request,
    admin: AdminUserDep,
    session: Annotated[AsyncSession, Depends(get_db)],
    _payload: ActionRequest | None = None,
) -> AdminUser:
    """Change a user's status and record who did it.

    The status change and its audit row commit together. Separating them would leave a
    window where a decision took effect with nothing recording who made it.
    """
    if user_id == admin.id:
        # An admin suspending or rejecting themselves is one click from an unrecoverable
        # lockout when they are the only admin.
        raise AuthError(400, "self_action", "You cannot change your own account status.")

    new_status, audit_action = _ACTIONS[action]

    current = (
        (
            await session.execute(
                text("SELECT status::text AS status FROM users WHERE id = :id"), {"id": user_id}
            )
        )
        .mappings()
        .first()
    )
    if not current:
        raise AuthError(404, "not_found", "No such user.")
    previous = current["status"]

    # Reactivate means "undo a suspension", not "approve anything". Applying it to a
    # pending user would grant access without anyone having reviewed them.
    if action == "reactivate" and previous != "SUSPENDED":
        raise AuthError(409, "not_suspended", "Only a suspended account can be reactivated.")

    stamps = {
        "APPROVED": "approved_at = now(), approved_by = :admin, rejected_at = NULL, rejected_by = NULL, suspended_at = NULL",
        "REJECTED": "rejected_at = now(), rejected_by = :admin",
        "SUSPENDED": "suspended_at = now()",
    }[new_status]

    row = (
        (
            await session.execute(
                text(
                    # `stamps` is selected from a fixed dict keyed by the validated action,
                    # not composed from input.
                    f"UPDATE users SET status = :status, {stamps}, updated_at = now()"  # noqa: S608
                    f" WHERE id = :id RETURNING id, full_name AS name, email, phone,"
                    f"       role::text AS role, status::text AS status, created_at,"
                    f"       approved_at, rejected_at, suspended_at, last_login_at"
                ),
                {"status": new_status, "admin": admin.id, "id": user_id},
            )
        )
        .mappings()
        .one()
    )

    await session.execute(
        text(
            "INSERT INTO admin_audit_log"
            " (admin_user_id, target_user_id, action, previous_status, new_status,"
            "  ip_address, user_agent)"
            " VALUES (:admin, :target, :action, :prev, :new, :ip, :ua)"
        ),
        {
            "admin": admin.id,
            "target": user_id,
            "action": audit_action,
            "prev": previous,
            "new": new_status,
            # The proxy's address is useless; the client's is what an investigation needs.
            # Caddy sets X-Forwarded-For, and only the first entry is meaningful.
            "ip": (
                request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else None)
            ),
            "ua": request.headers.get("user-agent", "")[:500] or None,
        },
    )
    await session.commit()
    return AdminUser(**dict(row))


@router.get("/admin/audit", response_model=list[AuditEntry], summary="Recent admin actions")
async def audit(
    _: AdminUserDep,
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> list[AuditEntry]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT l.id, l.action::text AS action, a.email AS admin_email,"
                    "       t.email AS target_email, l.previous_status, l.new_status, l.created_at"
                    "  FROM admin_audit_log l"
                    "  LEFT JOIN users a ON a.id = l.admin_user_id"
                    "  LEFT JOIN users t ON t.id = l.target_user_id"
                    " ORDER BY l.created_at DESC LIMIT :limit"
                ),
                {"limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [AuditEntry(**dict(r)) for r in rows]


class IngestRequestState(BaseModel):
    """What the fetch button should show right now."""

    id: int | None = None
    status: str  # QUEUED | RUNNING | SUCCEEDED | FAILED | SKIPPED | IDLE
    message: str | None = None
    requested_by: str | None = None
    created_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    #: True while anything is in flight, so the button can disable itself rather than
    #: letting an impatient click queue a second request behind the first.
    busy: bool = False


@router.post("/admin/ingest", response_model=IngestRequestState, status_code=202,
             summary="Fetch jobs from the source now")
async def request_ingest(
    admin: AdminUserDep,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> IngestRequestState:
    """Queue an on-demand fetch.

    Returns 202, not 200: the work has been accepted, not done. The API cannot run the
    ingest itself -- it has no Docker socket, and giving it one would let any
    request-handling bug start privileged containers on the host. A timer on the host
    claims the row instead, usually within a minute.
    """
    # One at a time. A second request while one is in flight is almost always an impatient
    # second click, and queueing it would run the whole thing twice for no benefit.
    existing = (
        await session.execute(
            text(
                "SELECT id, status::text AS status, created_at FROM ingest_requests"
                " WHERE status IN ('QUEUED', 'RUNNING') ORDER BY id DESC LIMIT 1"
            )
        )
    ).mappings().first()
    if existing:
        return IngestRequestState(
            id=existing["id"],
            status=existing["status"],
            message="A fetch is already in progress.",
            created_at=existing["created_at"],
            busy=True,
        )

    row = (
        await session.execute(
            text(
                "INSERT INTO ingest_requests (requested_by) VALUES (:by)"
                " RETURNING id, status::text AS status, created_at"
            ),
            {"by": admin.id},
        )
    ).mappings().one()
    await session.commit()

    return IngestRequestState(
        id=row["id"],
        status=row["status"],
        message="Fetch queued. It will start within a minute.",
        requested_by=admin.email,
        created_at=row["created_at"],
        busy=True,
    )


@router.get("/admin/ingest", response_model=IngestRequestState, summary="Fetch status")
async def ingest_status(
    _: AdminUserDep,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> IngestRequestState:
    """The most recent request, whatever became of it."""
    row = (
        await session.execute(
            text(
                "SELECT r.id, r.status::text AS status, r.message, r.created_at,"
                "       r.finished_at, u.email AS requested_by"
                "  FROM ingest_requests r"
                "  LEFT JOIN users u ON u.id = r.requested_by"
                " ORDER BY r.id DESC LIMIT 1"
            )
        )
    ).mappings().first()

    if not row:
        return IngestRequestState(status="IDLE")
    return IngestRequestState(
        id=row["id"],
        status=row["status"],
        message=row["message"],
        requested_by=row["requested_by"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        busy=row["status"] in ("QUEUED", "RUNNING"),
    )
