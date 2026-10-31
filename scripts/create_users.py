"""
Create the three demo accounts, one per role, in the single `users` table.

    python scripts/create_users.py

The password comes from SEED_PASSWORD in the environment (.env), never from source code.
If it is not set, a random one is generated and printed once. Re-running is safe: existing
emails are left alone.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

import auth  # noqa: E402
from db import session_scope  # noqa: E402
from models import Party, User  # noqa: E402

ACCOUNTS = [
    ("admin@chaintwin.local", "Asha Admin", "ADMIN", "Aarav Dairy Works"),
    ("manager@chaintwin.local", "Mohan Manager", "MANAGER", "Aarav Dairy Works"),
    ("auditor@chaintwin.local", "Anita Auditor", "AUDITOR", None),
]


def main() -> int:
    password = os.getenv("SEED_PASSWORD")
    generated = not password
    if generated:
        password = "Ct" + secrets.token_urlsafe(9) + "7"
    with session_scope() as s:
        for email, name, role, party_name in ACCOUNTS:
            if s.scalar(select(User.user_id).where(User.email == email)):
                print(f"  exists   {email}")
                continue
            party_id = s.scalar(select(Party.party_id).where(Party.name == party_name)) if party_name else None
            auth.create_user(s, email=email, full_name=name, password=password, role_name=role,
                             party_id=party_id)
            print(f"  created  {email:28} {role}")
    if generated:
        print(f"\nGenerated password (shown once): {password}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
