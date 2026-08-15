# 08 — Approval-based access control

The website URL is public. The job data is not. Registration creates an account that can
see nothing; an administrator decides whether it becomes useful.

> **Verification status.** Every claim below was exercised against a running stack with a
> real database: register through the public API, appear in the console, be refused job
> data, be approved, and reach it — with the same token, no re-login. The authorization
> matrix in `apps/api/tests/test_authorization.py` covers the same ground as tests.

---

## 1. The rule

**No unapproved caller can retrieve job data, whatever they call.** Enforcement is in the
API, not the frontend. Hiding a page would only change what someone *sees*; the guard is on
the endpoint, so a direct request with a valid session for a pending account is refused
just the same.

| Caller | Job data | `/admin` |
|---|---|---|
| Anonymous | 401 | 401 |
| Pending | 403 `account_pending` | 403 |
| Rejected | 403 `account_rejected` | 403 |
| Suspended | 403 `account_suspended` | 403 |
| Approved | **200** | 403 |
| Admin | **200** | **200** |

Login itself succeeds for every status. That is deliberate: a pending or suspended user
must be able to sign in far enough to be *told* what state they are in.

---

## 2. Why status is never in the token

The JWT carries an id and nothing else. Role and status are re-read from the database on
every request.

Putting them in the claims would be faster and would mean a suspended account kept its
access until the token expired — up to fifteen minutes of exactly the access an
administrator was trying to revoke. Verified: the same token that returned 200 returns 403
immediately after the status changes.

Consequently, **changing `JWT_SECRET` signs everyone out at once**, which is the lever to
pull if a token is ever believed to be compromised.

---

## 3. What is public, and why

Public: `/`, `/about`, `/login`, `/register`, and the aggregate counters `/api/v1/stats`
and `/api/v1/categories`.

Those two return counts, never rows — they are what lets the landing page say *"5,738 jobs
available, request access"* to somebody who has not signed up. Everything returning job
rows is guarded: `/jobs`, `/jobs/latest`, `/jobs/today`, `/jobs/recent`, `/jobs/{id}`,
`/search`, `/companies`.

If you would rather show nothing at all, guard those two as well — the landing page already
handles their absence.

---

## 4. Creating the first administrator

```bash
docker compose -f infra/docker/docker-compose.prod.yml \
  -f infra/docker/docker-compose.small.yml --env-file .env.production \
  run --rm --no-deps -e ADMIN_EMAIL=you@yourdomain.com -e ADMIN_PASSWORD='...' \
  ingest python scripts/create_admin.py
```

Or interactively, which keeps the password out of shell history:

```bash
... ingest python scripts/create_admin.py --email you@yourdomain.com
```

It refuses to run once an administrator exists, so leaving it in a bootstrap script cannot
quietly mint a second one. `--promote` raises an existing user instead.

**Use a real domain.** Reserved names — `.test`, `.local`, `.invalid` — are rejected by the
same validator the login endpoint uses. The script checks this itself now, because it
previously created an administrator the API then refused, discovered only at the login
screen.

---

## 5. Approving your first user

1. They visit `/register` and submit name, email, password, optionally phone.
2. They see *"Your account has been submitted for approval."*
3. You sign in at `/login` and go to **`/admin`**. Pending requests appear as a badge in
   the navigation and a banner on the dashboard.
4. **`/admin/users/pending`** lists them oldest first — whoever has waited longest is at
   the top.
5. **Approve**. A confirmation dialog asks first; the change is immediate.

They now have access on their next request. No re-login.

---

## 6. Statuses and roles are different questions

| | Answers |
|---|---|
| `status` | may this account do *anything* — PENDING, APPROVED, REJECTED, SUSPENDED |
| `role` | what it may do once approved — USER, ADMIN, SERVICE |
| `is_active` | operational kill switch, unrelated to approval |

Keeping them separate is what lets an administrator be suspended without ceasing to be an
administrator, and it keeps *"why can this person not log in"* answerable. `require_admin`
demands APPROVED as well as the role, so a suspended admin is suspended.

---

## 7. Self-protection

An administrator cannot change their own status, and `reactivate` applies only to a
suspended account. Both are enforced in the API.

Neither is paternalism. With one administrator, self-suspension is an unrecoverable lockout
without database access; and a `reactivate` that worked on a pending user would be a second
route to approval that skips review entirely.

---

## 8. The audit log

Every administrative decision writes to `admin_audit_log` **in the same transaction as the
change**. An audit row written separately is skipped by a failure between the two writes,
which is exactly when the record matters.

Stored: the acting admin, the target, the action, the transition, the caller's IP (from
`X-Forwarded-For`, since Caddy's own address is useless) and user agent. Recent entries
appear on the admin dashboard.

Both foreign keys are `RESTRICT`, so deleting an administrator who has acted fails loudly
rather than erasing the history of what they did.

---

## 9. Registration hardening

- Argon2id. Not bcrypt — it silently ignores everything past byte 72.
- Passwords at least 12 characters using two or more character classes. Length first:
  twelve characters with two classes resists cracking better than eight with four, and
  people can actually follow it.
- Login verifies against a dummy hash when no account exists, so a missing address costs
  the same time as a wrong password and the endpoint cannot enumerate users.
- Eight consecutive failures lock the account for fifteen minutes. It expires on its own,
  so an attacker cannot lock a known victim out indefinitely.
- Duplicate email returns 409. Pretending success to avoid confirming the address would
  leave a real user unable to tell a typo from an existing account.
- No response anywhere returns a password hash. The models enumerate their fields, so a
  column added later cannot leak through them.

---

## 10. Migration

```bash
docker compose ... run --rm --no-deps ingest \
  python -m alembic -c database/migrations/alembic.ini upgrade head
```

`0007` adds `status`, `phone`, the approval provenance columns, indexes on `status` and
`role`, and `admin_audit_log`.

**Existing users become APPROVED, not PENDING.** There are none in practice, but a
migration that silently locks out whoever is already there would be the wrong default for
anyone restoring an older dump.

Rebuild the **ingest** image before migrating — it carries `database/`, so a migration
added since its last build is not in it.

---

## 11. Not included

- **No email.** Notifications are described in the UI but nothing is sent. The approval
  path writes status and audit rows; wiring SMTP in later needs no schema change.
- **No CSRF token.** Mutations go through Next server actions, which carry their own
  action id, and the session cookie is `SameSite=Lax`. A form-post CSRF against `/admin`
  would need to defeat both. Worth revisiting if a classic HTML form is ever added.
- **No self-service password reset.** An administrator can promote and re-set a password
  with `create_admin.py --promote`; ordinary users have no route yet.
