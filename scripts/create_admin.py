#!/usr/bin/env python
"""Create the first administrator.

    ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='...' python scripts/create_admin.py
    python scripts/create_admin.py --email you@example.com          # prompts, no echo
    python scripts/create_admin.py --email you@example.com --promote

No password appears in this file, and none is generated for you. The two ways in are an
environment variable, which a deployment already has a safe channel for, and an
interactive prompt, which never reaches shell history.

Idempotent in the way that matters: if any administrator already exists it stops, so
running it twice -- or leaving it in a bootstrap script -- cannot quietly mint a second
admin account. `--promote` is the deliberate exception, for turning an existing approved
user into an admin.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg in ("packages/shared", "packages/schemas"):
    sys.path.insert(0, str(ROOT / pkg))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="administrator's email (or set ADMIN_EMAIL)")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="promote an existing user to admin instead of refusing when one exists",
    )
    args = parser.parse_args()

    from sqlalchemy import create_engine, text

    from jobplatform_shared import get_settings
    from jobplatform_shared.passwords import hash_password

    settings = get_settings()

    email = (args.email or os.environ.get("ADMIN_EMAIL") or "").strip().lower()
    if not email:
        print("An email is required: --email or ADMIN_EMAIL", file=sys.stderr)
        return 2

    # Validate exactly as the login endpoint does. Without this the script will happily
    # create an administrator whose address the API then refuses -- an admin who cannot
    # sign in, discovered only at the worst moment. Reserved domains like .test and
    # .local are the ones that bite.
    try:
        from email_validator import EmailNotValidError, validate_email

        email = validate_email(email, check_deliverability=False).normalized.lower()
    except ImportError:
        if "@" not in email:
            print("That does not look like an email address.", file=sys.stderr)
            return 2
    except EmailNotValidError as exc:
        print(f"{email} is not an address the API will accept: {exc}", file=sys.stderr)
        return 2

    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        # getpass, not input: the password must not be echoed, and must not land in a
        # scrollback buffer someone else can read.
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm: "):
            print("Passwords do not match.", file=sys.stderr)
            return 2
    if len(password) < 12:
        print("Password must be at least 12 characters.", file=sys.stderr)
        return 2

    # A plain sync engine: this runs once, by hand, and does not need the async stack.
    #
    # The driver has to be named explicitly. Stripping it entirely leaves a bare
    # postgresql:// URL, and SQLAlchemy then defaults to psycopg2 -- which is not what
    # this project installs, so the script died on ModuleNotFoundError rather than
    # anything to do with the database. psycopg (v3) arrives with jobplatform-shared.
    url = str(settings.database_url).replace("+asyncpg", "+psycopg")
    if "+" not in url.split("://", 1)[0]:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, future=True)

    with engine.begin() as conn:
        existing_admins = conn.execute(
            text("SELECT count(*) FROM users WHERE role = 'ADMIN'")
        ).scalar_one()

        if existing_admins and not args.promote:
            print(
                f"{existing_admins} administrator(s) already exist -- refusing to create another.\n"
                "Use --promote to raise an existing user, or approve the account from /admin.",
                file=sys.stderr,
            )
            return 1

        row = (
            conn.execute(
                text(
                    "SELECT id, role::text AS role, status::text AS status FROM users WHERE email = :e"
                ),
                {"e": email},
            )
            .mappings()
            .first()
        )

        if row:
            # Promotion also approves: an admin who cannot get past the approval gate is
            # an admin in name only, and that is the exact state that locks everyone out.
            conn.execute(
                text(
                    "UPDATE users SET role = 'ADMIN', status = 'APPROVED', is_active = TRUE,"
                    "       password_hash = :pw, approved_at = now(), updated_at = now()"
                    " WHERE id = :id"
                ),
                {"pw": hash_password(password), "id": row["id"]},
            )
            print(f"Promoted existing user {email} to ADMIN / APPROVED.")
        else:
            conn.execute(
                text(
                    "INSERT INTO users (email, password_hash, full_name, role, status,"
                    "                   is_active, approved_at)"
                    " VALUES (:e, :pw, :n, 'ADMIN', 'APPROVED', TRUE, now())"
                ),
                {"e": email, "pw": hash_password(password), "n": "Administrator"},
            )
            print(f"Created administrator {email}.")

    print("Log in at /login. Pending users appear at /admin/users/pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
