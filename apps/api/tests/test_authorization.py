"""The authorization matrix.

One question per test: can this kind of caller reach this kind of route. These are the
tests that would fail if someone later removed a guard, so they assert on status codes
rather than on response bodies -- a 200 where a 403 belongs is the bug, whatever the body
says.

The job routes are exercised through the real application with a real database, because
the guard is a FastAPI dependency and mocking it out would test the mock.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.integration

httpx = pytest.importorskip("httpx")


# Routes that return job rows. Every one must refuse an unapproved caller.
PROTECTED_JOB_ROUTES: list[str] = [
    "/api/v1/jobs",
    "/api/v1/jobs/latest",
    "/api/v1/jobs/today",
    "/api/v1/jobs/recent",
    "/api/v1/jobs/1",
    "/api/v1/search?q=engineer",
    "/api/v1/companies",
]

ADMIN_ROUTES: list[str] = [
    "/api/v1/admin/summary",
    "/api/v1/admin/users",
    "/api/v1/admin/audit",
    "/api/v1/admin/ingestion",
]

# Aggregate counts, deliberately public: the landing page advertises them to people who
# have not signed up yet, and they contain no job rows.
PUBLIC_ROUTES: list[str] = [
    "/api/v1/stats",
    "/api/v1/categories",
    "/health",
]


@pytest.fixture(scope="module")
def client(migrated_database: str) -> Iterator[Any]:
    os.environ.setdefault("ENVIRONMENT", "test")
    from app.main import create_app

    app = create_app()
    with httpx.Client(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _register(client: Any, email: str, password: str = "correct-horse-battery") -> None:
    r = client.post(
        "/api/v1/auth/register",
        json={"name": "Test User", "email": email, "password": password},
    )
    assert r.status_code in (201, 409), r.text


def _login(client: Any, email: str, password: str = "correct-horse-battery") -> str:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return str(r.json()["access_token"])


def _set_status(db_connection: Any, email: str, status: str) -> None:
    db_connection.execute("UPDATE users SET status = %s WHERE email = %s", (status, email))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- anonymous


@pytest.mark.parametrize("route", PUBLIC_ROUTES)
def test_anonymous_may_read_public_routes(client: Any, route: str) -> None:
    assert client.get(route).status_code == 200


@pytest.mark.parametrize("route", PROTECTED_JOB_ROUTES)
def test_anonymous_is_refused_job_data(client: Any, route: str) -> None:
    """The requirement in one test: no session, no job rows."""
    assert client.get(route).status_code == 401


@pytest.mark.parametrize("route", ADMIN_ROUTES)
def test_anonymous_is_refused_admin(client: Any, route: str) -> None:
    assert client.get(route).status_code == 401


def test_anonymous_may_register_and_log_in(client: Any) -> None:
    _register(client, "anon-flow@example.com")
    assert _login(client, "anon-flow@example.com")


# ----------------------------------------------------------------------------- pending


def test_registration_creates_a_pending_account(client: Any) -> None:
    r = client.post(
        "/api/v1/auth/register",
        json={
            "name": "Pending",
            "email": "pending-new@example.com",
            "password": "correct-horse-battery",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "PENDING"
    assert body["role"] == "USER"
    # The response must never carry credentials, whatever else changes about it.
    assert "password" not in body and "password_hash" not in body


@pytest.mark.parametrize("route", PROTECTED_JOB_ROUTES)
def test_pending_user_is_refused_job_data(client: Any, route: str) -> None:
    _register(client, "pending-jobs@example.com")
    token = _login(client, "pending-jobs@example.com")
    r = client.get(route, headers=_auth(token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "account_pending"


def test_pending_user_can_see_their_own_status(client: Any) -> None:
    """A pending user must reach /auth/me, or they cannot be told why they are stuck."""
    _register(client, "pending-me@example.com")
    token = _login(client, "pending-me@example.com")
    r = client.get("/api/v1/auth/me", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["status"] == "PENDING"


@pytest.mark.parametrize("route", ADMIN_ROUTES)
def test_pending_user_is_refused_admin(client: Any, route: str) -> None:
    _register(client, "pending-admin@example.com")
    token = _login(client, "pending-admin@example.com")
    assert client.get(route, headers=_auth(token)).status_code == 403


# ---------------------------------------------------------------------------- approved


@pytest.mark.parametrize("route", PROTECTED_JOB_ROUTES)
def test_approved_user_may_read_job_data(client: Any, db_connection: Any, route: str) -> None:
    _register(client, "approved@example.com")
    _set_status(db_connection, "approved@example.com", "APPROVED")
    token = _login(client, "approved@example.com")
    # 404 is a legitimate answer for /jobs/1 on an empty database; 403 never is.
    assert client.get(route, headers=_auth(token)).status_code in (200, 404)


@pytest.mark.parametrize("route", ADMIN_ROUTES)
def test_approved_user_is_still_refused_admin(client: Any, db_connection: Any, route: str) -> None:
    """Approval grants job data, never administration."""
    _register(client, "approved-not-admin@example.com")
    _set_status(db_connection, "approved-not-admin@example.com", "APPROVED")
    token = _login(client, "approved-not-admin@example.com")
    r = client.get(route, headers=_auth(token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


# -------------------------------------------------------------- rejected and suspended


@pytest.mark.parametrize(
    "status,code", [("REJECTED", "account_rejected"), ("SUSPENDED", "account_suspended")]
)
def test_rejected_and_suspended_are_refused_job_data(
    client: Any, db_connection: Any, status: str, code: str
) -> None:
    email = f"{status.lower()}@example.com"
    _register(client, email)
    _set_status(db_connection, email, status)
    token = _login(client, email)
    r = client.get("/api/v1/jobs", headers=_auth(token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == code


def test_status_change_takes_effect_without_a_new_token(client: Any, db_connection: Any) -> None:
    """The reason status is never a token claim.

    Suspending an account must lock it out immediately, not whenever its token happens to
    expire. The same token is used before and after.
    """
    _register(client, "revoked@example.com")
    _set_status(db_connection, "revoked@example.com", "APPROVED")
    token = _login(client, "revoked@example.com")
    assert client.get("/api/v1/jobs", headers=_auth(token)).status_code in (200, 404)

    _set_status(db_connection, "revoked@example.com", "SUSPENDED")
    assert client.get("/api/v1/jobs", headers=_auth(token)).status_code == 403


# ------------------------------------------------------------------------------- admin


@pytest.fixture()
def admin_token(client: Any, db_connection: Any) -> str:
    _register(client, "admin@example.com")
    db_connection.execute(
        "UPDATE users SET role = 'ADMIN', status = 'APPROVED' WHERE email = %s",
        ("admin@example.com",),
    )
    return _login(client, "admin@example.com")


@pytest.mark.parametrize("route", ADMIN_ROUTES)
def test_admin_may_read_admin_routes(client: Any, admin_token: str, route: str) -> None:
    assert client.get(route, headers=_auth(admin_token)).status_code == 200


def test_admin_may_read_job_data(client: Any, admin_token: str) -> None:
    assert client.get("/api/v1/jobs", headers=_auth(admin_token)).status_code in (200, 404)


def test_admin_can_approve_and_the_user_gains_access(
    client: Any, db_connection: Any, admin_token: str
) -> None:
    _register(client, "to-approve@example.com")
    user_token = _login(client, "to-approve@example.com")
    assert client.get("/api/v1/jobs", headers=_auth(user_token)).status_code == 403

    uid = db_connection.execute(
        "SELECT id FROM users WHERE email = %s", ("to-approve@example.com",)
    ).fetchone()[0]
    r = client.post(f"/api/v1/admin/users/{uid}/approve", headers=_auth(admin_token))
    assert r.status_code == 200
    assert r.json()["status"] == "APPROVED"

    assert client.get("/api/v1/jobs", headers=_auth(user_token)).status_code in (200, 404)


def test_approval_is_recorded_in_the_audit_log(
    client: Any, db_connection: Any, admin_token: str
) -> None:
    _register(client, "audited@example.com")
    uid = db_connection.execute(
        "SELECT id FROM users WHERE email = %s", ("audited@example.com",)
    ).fetchone()[0]
    client.post(f"/api/v1/admin/users/{uid}/approve", headers=_auth(admin_token))

    row = db_connection.execute(
        "SELECT action::text, previous_status, new_status FROM admin_audit_log"
        " WHERE target_user_id = %s ORDER BY id DESC LIMIT 1",
        (uid,),
    ).fetchone()
    assert row == ("ADMIN_APPROVED_USER", "PENDING", "APPROVED")


def test_admin_cannot_change_their_own_status(
    client: Any, db_connection: Any, admin_token: str
) -> None:
    """With one administrator, self-suspension is an unrecoverable lockout."""
    uid = db_connection.execute(
        "SELECT id FROM users WHERE email = %s", ("admin@example.com",)
    ).fetchone()[0]
    r = client.post(f"/api/v1/admin/users/{uid}/suspend", headers=_auth(admin_token))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "self_action"


def test_reactivate_only_applies_to_a_suspended_account(
    client: Any, db_connection: Any, admin_token: str
) -> None:
    """Otherwise it becomes a second, unreviewed route to approval."""
    _register(client, "never-reviewed@example.com")
    uid = db_connection.execute(
        "SELECT id FROM users WHERE email = %s", ("never-reviewed@example.com",)
    ).fetchone()[0]
    r = client.post(f"/api/v1/admin/users/{uid}/reactivate", headers=_auth(admin_token))
    assert r.status_code == 409


# ------------------------------------------------------------------- credential safety


def test_no_route_ever_returns_a_password_hash(
    client: Any, db_connection: Any, admin_token: str
) -> None:
    for route in ("/api/v1/auth/me", "/api/v1/admin/users"):
        body = client.get(route, headers=_auth(admin_token)).text
        assert "password_hash" not in body
        assert "$argon2" not in body


def test_a_forged_token_is_refused(client: Any) -> None:
    """Signed with the wrong key -- the signature check is the whole point."""
    import jwt

    forged = jwt.encode(
        {"sub": "1", "type": "access", "iat": 0, "exp": 9999999999}, "not-the-secret"
    )
    assert client.get("/api/v1/jobs", headers=_auth(forged)).status_code == 401


def test_a_refresh_token_is_not_accepted_as_an_access_token(
    client: Any, db_connection: Any
) -> None:
    _register(client, "token-type@example.com")
    _set_status(db_connection, "token-type@example.com", "APPROVED")
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "token-type@example.com", "password": "correct-horse-battery"},
    )
    refresh = r.json()["refresh_token"]
    assert client.get("/api/v1/jobs", headers=_auth(refresh)).status_code == 401
